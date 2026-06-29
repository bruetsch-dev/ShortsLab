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
CAT_DB = {
    "bright_whoosh": -14, "swipe_whoosh": -13, "whoosh_hit_combo": -12, "impact_hit": -10,
    "low_impact": -13, "caption_pop": -18, "ui_click": -21, "notification_ding": -18,
    "idea_reveal": -18, "camera_flash": -15, "flash_blink": -17, "school_bell": -19,
    "payment_ding": -19, "message_sent": -19,
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
                return cached["data"]
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
    return data
