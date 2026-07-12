"""Scan, analyze, classify, trim and policy-filter the user's LOCAL sound-effect files.

No SFX is generated or downloaded here - this only uses the real files the user dropped into
soundeffects/ (and the other supported folders). Every file is decoded once, measured
(duration / peak / rms / onset / low+high energy / spectral centroid / transient), classified into
the viral-documentary category map (filename hints win, audio features verify), trimmed when it is
too long or starts late (so playback is immediate and <= ~2s), and tagged with a usage policy:
core | topic_specific | meme_only | disabled_by_default.

Trimmed working copies live in generated_assets/sfx_trimmed/ - originals are never overwritten.
The classification is cached to that folder so the scan only re-runs when files change.
"""

import json
import math
import subprocess
import time
from pathlib import Path

import numpy as np

import pipeline

ROOT = Path(__file__).resolve().parent
TRIM_DIR = ROOT / "generated_assets" / "sfx_trimmed"
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
SCAN_DIRS = ["soundeffects", "assets/sfx", "public/sfx", "project_assets/sfx",
             "uploaded_sfx", "reference_sfx", "sfx"]

# The 16-category viral-documentary map (order = report order).
SFX_CATEGORIES = ["bright_whoosh", "swipe_whoosh", "whoosh_hit_combo", "impact_hit", "low_impact",
                  "caption_pop", "ui_click", "notification_ding", "idea_reveal", "camera_flash",
                  "flash_blink", "school_bell", "payment_ding", "message_sent",
                  "meme_only", "disabled_by_default"]
# categories the DEFAULT (serious documentary) style may use automatically
DEFAULT_ALLOWED = {"bright_whoosh", "swipe_whoosh", "whoosh_hit_combo", "impact_hit", "low_impact",
                   "caption_pop", "ui_click", "notification_ding", "idea_reveal", "camera_flash",
                   "flash_blink", "school_bell", "payment_ding", "message_sent"}
TOPIC_SPECIFIC = {"school_bell", "payment_ding", "message_sent"}

# ---------------------------------------------------------------------------
# HUMAN GROUND TRUTH (tools/sfx_trainer.py). When soundeffects/sfx_labels.json is ACTIVE it
# OVERRIDES the filename/feature classifier below: each sound is routed into the placement
# buckets its human ROLE dictates, plus a per-reaction pool keyed by reaction slug. Cuts then
# only ever see transition sounds, accents only accent sounds, etc.
# ---------------------------------------------------------------------------
LABELS_PATH = ROOT / "soundeffects" / "sfx_labels.json"
# role -> the placement categories place_editor_sfx already knows how to fire
ROLE_TO_CATEGORIES = {
    "transition": ["bright_whoosh", "swipe_whoosh", "whoosh_hit_combo"],  # CUTS (clicks live here too)
    "impact":     ["impact_hit"],                                         # hook + big reveals (hard hit)
    "accent":     ["caption_pop"],                                        # small word/number pop
    "ui":         ["ui_click"],                                           # click / tap texture
    # "riser"/"hook_riser" -> own pools (data['risers'] / ['hook_risers']); swell INTO a beat.
    # skip is never auto-placed (available only in the timeline library for manual drag).
}
# a few reactions ALSO backfill a legacy slot the placement code uses directly
REACTION_TO_CATEGORIES = {
    "camera_photo": ["camera_flash"],      # freeze-frame flash
    "idea_reveal":  ["idea_reveal"],       # lightbulb/reveal
    "notification": ["notification_ding"], # ding
}
# The hook-opening + big-moment slots (impact_hit / low_impact). Impact-ROLE sounds fill them first;
# these heavy reactions only BACKFILL an otherwise-empty bucket (so it still works with 0 impacts).
# death/sad_downer are NOT backfill material: the death gong fired on every generic big moment
# and at scene starts ("2s before the word death") - meaning-bound sounds only fire via the
# word-triggered reaction pass.
IMPACT_FROM_REACTIONS = {"impact_hit": ["shock_reveal"]}

# Reactions whose sound is so meaning-specific (death gong, sad aww, cute sparkle) that it must
# NEVER play as a generic impact even when the file also carries the "impact" role label.
MEANING_BOUND_REACTIONS = {"death", "sad_downer", "cute_aww"}


def choose_riser_for_target(pool, target_duration, duration_getter=None):
    """Choose the riser needing the least speed change to fill ``0..target_duration``.

    Returns ``(item, source_duration, playback_rate)``. ``playback_rate`` follows ffmpeg/
    HTML audio semantics: source duration divided by rate equals the requested output duration.
    """
    try:
        target = float(target_duration)
    except (TypeError, ValueError):
        return None, 0.0, 1.0
    if target <= 0:
        return None, 0.0, 1.0
    candidates = []
    for index, item in enumerate(pool or []):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        try:
            source_duration = float(item.get("dur") or 0.0)
        except (TypeError, ValueError):
            source_duration = 0.0
        if source_duration <= 0 and duration_getter:
            try:
                source_duration = float(duration_getter(item.get("path")) or 0.0)
            except Exception:
                source_duration = 0.0
        if source_duration <= 0:
            continue
        rate = source_duration / target
        # Ratio distance treats 0.5x and 2x as equally disruptive. Stable index is the tie-break.
        candidates.append((abs(math.log(max(rate, 1e-6))), index, item,
                           source_duration, rate))
    if not candidates:
        return None, 0.0, 1.0
    _score, _index, item, source_duration, rate = min(candidates, key=lambda row: (row[0], row[1]))
    return item, source_duration, rate


def load_active_labels():
    """Return (labels_dict, reactions_meta) if sfx_labels.json exists AND is active, else (None, None)."""
    try:
        data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not data.get("active"):
        return None, None
    return data.get("labels", {}), data.get("reactions", [])


def _label_roles(rec):
    roles = rec.get("roles")
    if roles is None:
        one = rec.get("role")
        roles = [one] if one else []
    return [r for r in roles if r]


def _label_reactions(rec):
    rx = rec.get("reactions")
    if rx is None:
        one = rec.get("reaction")
        rx = [one] if one else []
    return [r for r in rx if r]


def route_by_labels(data, meme_enabled=False):
    """If the human labels are active, rebuild data['library'] from them (role->buckets) and add
    data['reactions'] = {slug: [paths]}. Cheap (no ffmpeg) - runs on cached records every call, so
    editing labels takes effect immediately without a re-scan. No-op when labels are inactive."""
    labels, _ = load_active_labels()
    if labels is None:
        return data
    rec_by_file = {r["file"]: r for r in data.get("records", [])}
    library = {c: [] for c in SFX_CATEGORIES}
    reactions = {}
    risers = []
    hook_risers = []
    used = 0
    for fn, rec in labels.items():
        r = rec_by_file.get(fn)
        if not r:
            continue
        policy = rec.get("policy", "core")
        usable = policy in ("core", "topic_specific") or (policy == "meme_only" and meme_enabled)
        if not usable:
            continue
        roles = _label_roles(rec)
        rxs = _label_reactions(rec)
        if not roles or "skip" in roles:
            continue
        used += 1
        path = r["use_path"]
        # risers use the ORIGINAL (untrimmed) file so the full swell/peak is available
        _riser_item = {"path": r.get("path") or path, "dur": float(r.get("duration") or 0.0)}
        if "riser" in roles:
            risers.append(_riser_item)
        if "hook_riser" in roles:
            hook_risers.append(_riser_item)
        meaning_bound = any(s in MEANING_BOUND_REACTIONS for s in rxs)
        for role in roles:
            for cat in ROLE_TO_CATEGORIES.get(role, []):
                # a death gong / aww labeled "impact" must not fire on generic big moments -
                # it only plays word-triggered through its reaction slug
                if meaning_bound and cat in ("impact_hit", "low_impact"):
                    continue
                library[cat].append(path)
        for slug in rxs:
            reactions.setdefault(slug, []).append(path)
            for cat in REACTION_TO_CATEGORIES.get(slug, []):
                library[cat].append(path)
    # impact-role sounds already filled impact_hit; backfill from heavy reactions ONLY if empty
    for cat, slugs in IMPACT_FROM_REACTIONS.items():
        if not library[cat]:
            for slug in slugs:
                library[cat].extend(reactions.get(slug, []))
    dedup = lambda xs: list(dict.fromkeys(xs))
    data = dict(data)
    data["library"] = {c: dedup(v) for c, v in library.items()}
    data["reactions"] = {s: dedup(v) for s, v in reactions.items()}
    data["risers"] = risers
    data["hook_risers"] = hook_risers
    data["label_driven"] = True
    data["labeled_sounds_used"] = used
    return data

# Manual filename hints from the user (stem without extension) -> (category, policy).
SFX_HINTS = {
    "swoosh-sound-effects": ("swipe_whoosh", "core"),
    "woosh-sound-effect": ("bright_whoosh", "core"),
    "bubble-hitsound": ("caption_pop", "core"),
    "pop-sfx": ("caption_pop", "core"),
    "pop-sound-effect": ("caption_pop", "core"),
    "pop-button": ("caption_pop", "core"),
    "meme-click": ("ui_click", "core"),
    "mouse-click-sound": ("ui_click", "topic_specific"),
    "ding-click": ("notification_ding", "core"),
    "idea-ding-sound-effect": ("idea_reveal", "core"),
    "bright-idea": ("idea_reveal", "core"),
    "pop-ding": ("notification_ding", "core"),
    "camera-flash": ("camera_flash", "core"),
    "camera-flash-sound-effect": ("camera_flash", "core"),
    "blink": ("flash_blink", "core"),
    "death-bong": ("low_impact", "core"),
    "udar-ot-vzgliada-skaly": ("impact_hit", "core"),
    "service-bell": ("school_bell", "topic_specific"),
    "apple-pay-sound": ("payment_ding", "topic_specific"),
    "iphone-sent-message": ("message_sent", "topic_specific"),
    "imessage-ding": ("message_sent", "topic_specific"),
    "huh-sound": ("meme_only", "meme_only"),
    "aww": ("meme_only", "meme_only"),
    "sus-violin": ("meme_only", "meme_only"),
    "shiny-pokemon": ("disabled_by_default", "disabled_by_default"),
    "taco-bell-bong": ("disabled_by_default", "disabled_by_default"),
    "wrong-answer-gameshow": ("disabled_by_default", "disabled_by_default"),
    "youtube-subscribe-sound-effects": ("disabled_by_default", "disabled_by_default"),
}

# per-category trim length cap (seconds); hard ceiling 2.0s for everything
CAT_MAXLEN = {
    "caption_pop": 0.45, "ui_click": 0.40, "camera_flash": 0.70, "flash_blink": 0.55,
    "bright_whoosh": 1.0, "swipe_whoosh": 1.0, "whoosh_hit_combo": 1.2, "impact_hit": 1.2,
    "low_impact": 1.5, "notification_ding": 1.0, "idea_reveal": 1.2, "school_bell": 1.5,
    "payment_ding": 1.2, "message_sent": 1.0,
}
# default mixing volume per category, in dBFS (midpoint of the user's ranges)
# Per-category mix level in dB below the speech. Bumped ~+6 dB (2026-06): the editor SFX were
# coming through far too quietly under the loud voice (whooshes/impacts barely audible). These now
# sit clearly present like the reference TikTok edits while still under the narration.
CAT_DB = {
    "bright_whoosh": -8, "swipe_whoosh": -7, "whoosh_hit_combo": -6, "impact_hit": -5,
    "low_impact": -8, "caption_pop": -12, "ui_click": -15, "notification_ding": -12,
    "idea_reveal": -12, "camera_flash": -9, "flash_blink": -11, "school_bell": -13,
    "payment_ding": -13, "message_sent": -13,
}


def db_to_gain(db):
    return float(10.0 ** (float(db) / 20.0))


def _log(status_cb, msg):
    if status_cb:
        status_cb(msg)


def _decode(path, ffmpeg, sr=22050):
    """Decode any audio file to a mono float32 numpy array (best-effort, [] on failure)."""
    try:
        raw = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr),
                              "-f", "f32le", "-"], capture_output=True, timeout=60).stdout
        return np.frombuffer(raw, dtype="<f4")
    except Exception:
        return np.zeros(0, dtype="f4")


def _features(a, sr=22050):
    """Measure the perceptual features used to verify the filename hint + drive trimming."""
    f = {"duration": 0.0, "peak": 0.0, "rms": 0.0, "onset": 0.0, "low_ratio": 0.0,
         "high_ratio": 0.0, "centroid": 0.0, "transient": 0.0, "is_click": False,
         "is_whoosh": False, "is_bass": False, "is_tonal": False}
    n = a.size
    if n < 8:
        return f
    f["duration"] = n / sr
    peak = float(np.max(np.abs(a))) or 1e-6
    f["peak"] = peak
    f["rms"] = float(np.sqrt(np.mean(a ** 2)))
    # onset = first sample clearly above the noise floor
    thr = 0.07 * peak
    above = np.where(np.abs(a) > thr)[0]
    f["onset"] = float(above[0] / sr) if above.size else 0.0
    # short-window envelope -> transient strength (how punchy the attack is)
    win = max(1, sr // 100)
    env = np.sqrt(np.convolve(a ** 2, np.ones(win) / win, mode="same"))
    if env.max() > 0:
        f["transient"] = float(np.max(np.diff(env)) / (env.max() + 1e-9))
    # spectrum: low / high energy split + centroid
    mag = np.abs(np.fft.rfft(a * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    tot = float(mag.sum()) + 1e-9
    f["low_ratio"] = float(mag[freqs < 300].sum() / tot)
    f["high_ratio"] = float(mag[freqs > 3000].sum() / tot)
    f["centroid"] = float((freqs * mag).sum() / tot)
    # flatness (tonal vs noisy): geometric/arithmetic mean of magnitude
    m = mag + 1e-9
    flat = float(np.exp(np.mean(np.log(m))) / (np.mean(m)))
    f["is_tonal"] = flat < 0.15
    f["is_click"] = f["duration"] < 0.35 and f["transient"] > 0.05
    f["is_whoosh"] = (not f["is_tonal"]) and f["centroid"] > 1500 and f["duration"] < 1.4
    f["is_bass"] = f["low_ratio"] > 0.45 and peak > 0.4
    return f


def classify(stem, feats):
    """Return (category, policy). Filename hint is authoritative (the user mapped them); when a
    file is unknown, fall back to the audio features."""
    key = stem.lower().strip()
    if key in SFX_HINTS:
        return SFX_HINTS[key]
    # feature fallback for unknown files
    if feats["is_bass"] and feats["transient"] > 0.04:
        return ("impact_hit", "core")
    if feats["is_whoosh"]:
        return ("bright_whoosh", "core")
    if feats["is_click"]:
        return ("caption_pop" if feats["high_ratio"] > 0.3 else "ui_click", "core")
    if feats["is_tonal"] and feats["duration"] < 1.6:
        return ("notification_ding", "core")
    return ("disabled_by_default", "disabled_by_default")


def _prepare(src, dst, category, feats, ffmpeg):
    """Make a clean working copy: cut leading silence (audio starts immediately), cap length to
    <= ~2s, normalize the peak (so per-category dB volumes are consistent), tiny in/out fades.
    Returns (path, trim_start, trim_len) or None on failure."""
    start = max(0.0, feats["onset"] - 0.02)            # keep a 20ms pre-roll for the attack
    cap = min(2.0, CAT_MAXLEN.get(category, 1.2))
    length = min(cap, max(0.12, feats["duration"] - start))
    fade_out = min(0.06, length * 0.25)
    norm = min(8.0, 0.92 / max(0.02, feats["peak"]))    # normalize toward -0.7 dBFS peak
    dst.parent.mkdir(parents=True, exist_ok=True)
    af = (f"volume={norm:.3f},afade=t=in:st=0:d=0.004,"
          f"afade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out:.3f}")
    try:
        subprocess.run([ffmpeg, "-y", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(src),
                        "-af", af, "-ar", "44100", "-ac", "1", str(dst)],
                       capture_output=True, timeout=60)
    except Exception:
        return None
    return (dst, round(start, 3), round(length, 3)) if dst.exists() and dst.stat().st_size > 1024 else None


def _scan_files():
    found = []
    for d in SCAN_DIRS:
        folder = ROOT / d
        if not folder.exists():
            continue
        for p in sorted(folder.rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS and "sfx_trimmed" not in p.parts:
                found.append(p)
    # de-dupe by name (first folder wins)
    seen, uniq = set(), []
    for p in found:
        if p.name.lower() not in seen:
            seen.add(p.name.lower()); uniq.append(p)
    return uniq


def build_library(status_cb=None, meme_enabled=False, force=False):
    """Scan + classify + trim the local SFX. Returns dict:
    {"library": {category: [usable file paths]}, "records": [...], "report": {...}}.
    Cached in generated_assets/sfx_trimmed/sfx_index.json (re-scans when files change)."""
    ffmpeg = pipeline.find_ffmpeg()
    files = _scan_files()
    TRIM_DIR.mkdir(parents=True, exist_ok=True)
    cache = TRIM_DIR / "sfx_index.json"
    sig = sorted((p.name, int(p.stat().st_mtime), p.stat().st_size) for p in files)
    if cache.exists() and not force:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("sig") == sig:
                _log(status_cb, f"SFX Scanner: using cached scan of {len(files)} audio file(s).")
                return route_by_labels(cached["data"], meme_enabled=meme_enabled)
        except Exception:
            pass

    _log(status_cb, f"SFX Scanner: found {len(files)} audio files.")
    library = {c: [] for c in SFX_CATEGORIES}
    records = []
    trimmed_count = 0
    for p in files:
        a = _decode(p, ffmpeg)
        feats = _features(a)
        category, policy = classify(p.stem, feats)
        requires_trim = feats["duration"] > 1.2 or feats["onset"] > 0.06
        usable = policy in ("core", "topic_specific") or (policy == "meme_only" and meme_enabled)
        use_path = str(p)
        trim_start, trim_len = 0.0, round(feats["duration"], 3)
        # every usable file gets a normalized working copy (so dB volumes are consistent), and is
        # trimmed when it is too long / starts late.
        if usable and ffmpeg:
            out = TRIM_DIR / f"{p.stem}_trim.wav"
            res = _prepare(p, out, category, feats, ffmpeg)
            if res:
                use_path, trim_start, trim_len = str(res[0]), res[1], res[2]
                if requires_trim:
                    trimmed_count += 1
                    _log(status_cb, f"SFX Trim: created trimmed version for {p.name} "
                                    f"({feats['duration']:.2f}s -> {trim_len:.2f}s, start {trim_start:.2f}s).")
        rec = {
            "file": p.name, "path": str(p), "use_path": use_path,
            "duration": round(feats["duration"], 3), "peak": round(feats["peak"], 3),
            "rms": round(feats["rms"], 4), "onset": round(feats["onset"], 3),
            "low_ratio": round(feats["low_ratio"], 3), "high_ratio": round(feats["high_ratio"], 3),
            "centroid": round(feats["centroid"], 1), "transient": round(feats["transient"], 4),
            "estimated_category": category, "usage_policy": policy,
            "usable": bool(usable), "requires_trim": bool(requires_trim),
            "trim_start": trim_start, "trim_len": trim_len, "volume_db": CAT_DB.get(category, -18),
            "reason": f"hint+features -> {category} ({policy})",
        }
        records.append(rec)
        if usable:
            library[category].append(use_path)

    classification = {c: [r["file"] for r in records if r["estimated_category"] == c]
                      for c in SFX_CATEGORIES}
    report = {
        "sfx_policy": "provided_local_assets_only",
        "sfx_generated_random_assets": False,
        "meme_sfx_enabled": bool(meme_enabled),
        "sfx_scanner": {
            "audio_files_found": len(files),
            "audio_files_used": sum(1 for r in records if r["usable"]),
            "audio_files_unused": sum(1 for r in records if not r["usable"]),
            "trimmed_versions_created": trimmed_count,
        },
        "sfx_classification": classification,
        "records": records,
    }
    data = {"library": library, "records": records, "report": report}
    core = sum(1 for r in records if r["usage_policy"] == "core")
    topic = sum(1 for r in records if r["usage_policy"] == "topic_specific")
    meme = sum(1 for r in records if r["usage_policy"] == "meme_only")
    disabled = sum(1 for r in records if r["usage_policy"] == "disabled_by_default")
    _log(status_cb, f"SFX Classifier: core={core}, topic_specific={topic}, meme_only={meme}, disabled={disabled}.")
    _log(status_cb, "SFX Library: using provided local SFX only.")
    try:
        cache.write_text(json.dumps({"sig": sig, "data": data}, indent=0), encoding="utf-8")
    except Exception:
        pass
    # cache stores the auto classification; the human labels re-route on top (cheap, no re-scan)
    return route_by_labels(data, meme_enabled=meme_enabled)
