"""AI visual-emphasis post-production for an already-finished Short - "Visual Master".

Upload a rendered vertical video and this module:
  1. detects visual cuts + transcribes the speech with timing (reuses sfx_agent),
  2. samples one frame per punchy moment and asks an Opus 4.8 VISION agent to DIRECT the edit:
     for each moment it decides whether a thick red ARROW should point at the concrete subject
     the line is about, plus (optionally) either a kawaii pixel NEKO or categorized MEME
     reaction matching the mood. ARROWS AND REACTIONS ONLY - no text stamps, no circles.
  3. renders every effect as an ANIMATED transparent overlay: arrows FLY IN straight from their
     side with an overshoot and then nudge-point at the target; nekos bounce in with a springy
     overshoot,
  4. adds a fitting click/ding/whoosh/impact from the local SFX library exactly when each
     overlay appears.

This edit style is deliberately DENSE (an effect every few seconds), matching the loud viral
'dark facts' look. A moment with no clear target simply gets nothing. Mirrors sfx_agent (the
SFX Master) but for visuals.
"""

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

import agent_core
import pipeline
import sfx_agent

ROOT = Path(__file__).resolve().parent
VISUAL_OUTPUT_DIR = ROOT / "projects" / "_visual_enhanced"
EMOTION_DIR = ROOT / "static" / "emotions"
MEME_DIR = ROOT / "assets" / "meme_stickers"
MEME_CATALOG_PATH = MEME_DIR / "catalog.json"

ARROW_RED = (225, 32, 25)

# direction the arrow POINTS (tail -> tip), keyed by the side it comes IN from
_DIRS = {
    "left": (1.0, 0.0), "right": (-1.0, 0.0), "top": (0.0, 1.0), "bottom": (0.0, -1.0),
    "top-left": (0.707, 0.707), "top-right": (-0.707, 0.707),
    "bottom-left": (0.707, -0.707), "bottom-right": (-0.707, -0.707),
}


def available_emotions():
    """The kawaii neko emotion characters on disk (auto-generate them once if missing)."""
    if not EMOTION_DIR.exists() or not any(EMOTION_DIR.glob("*.png")):
        try:
            subprocess.run(["python", str(ROOT / "scripts" / "make_emotion_chars.py")], timeout=90)
        except Exception:
            pass
    return sorted(p.stem for p in EMOTION_DIR.glob("*.png")) if EMOTION_DIR.exists() else []


def available_memes():
    """Categorized transparent meme stickers whose assigned sound also exists locally."""
    try:
        raw = json.loads(MEME_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for meme_id, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        image = MEME_DIR / f"{meme_id}.png"
        sound = ROOT / "soundeffects" / str(rec.get("sound") or "")
        if image.exists() and sound.exists():
            out[meme_id] = {**rec, "image_path": str(image), "sound_path": str(sound)}
    return out


def log(status_cb, message):
    if status_cb:
        status_cb(message)


def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _video_props(path, ffprobe):
    """Return (width, height, fps, duration) for the uploaded video."""
    w = h = 0
    fps = 30.0
    dur = 0.0
    if not ffprobe:
        return 1080, 1920, 30.0, 0.0
    try:
        r = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "stream=width,height,r_frame_rate", "-show_entries", "format=duration",
                  "-of", "json", str(path)], timeout=30)
        data = json.loads(r.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        w = int(st.get("width") or 0)
        h = int(st.get("height") or 0)
        rate = str(st.get("r_frame_rate") or "30/1")
        if "/" in rate:
            num, den = rate.split("/")
            fps = float(num) / float(den) if float(den) else 30.0
        else:
            fps = float(rate)
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except Exception:
        pass
    return (w or 1080), (h or 1920), (fps if 1 < fps < 121 else 30.0), dur


# User-selectable arrow/effect density. "low" matches the app's historical behaviour; medium/high
# check MORE candidate moments AND tell the director to accept a larger share of them.
VFX_AMOUNT_PROFILES = {
    "low": {
        "min_gap": 1.8, "cap": 18,
        "direct": "give MOST moments (roughly 2 out of 3) a red arrow when a concrete target "
                  "exists, and only skip a moment when there is genuinely nothing concrete on screen.",
        "neko": "Use it on the strongest ~1 in 3 moments, else 'none'.",
        "meme": "Use a meme sparingly, only on the strongest ~1 in 4 emotionally obvious moments.",
    },
    "medium": {
        "min_gap": 1.2, "cap": 28,
        "direct": "give nearly every moment (roughly 3 out of 4) a red arrow when any concrete "
                  "target exists - skipping should be the exception, not the rule.",
        "neko": "Use it on the strongest ~1 in 2 moments, else 'none'.",
        "meme": "Use a meme on roughly 1 in 3 emotionally obvious moments.",
    },
    "high": {
        "min_gap": 0.8, "cap": 42,
        "direct": "this is a HYPER-dense edit: give practically EVERY moment (9 out of 10) a red "
                  "arrow whenever anything concrete is visible - a face, object, sign, detail, "
                  "anything. Only output 'none' when the frame is pure blur or empty background.",
        "neko": "Be generous: use it on ~2 in 3 emotionally coloured moments, else 'none'.",
        "meme": "Use a meme generously on roughly 1 in 2 emotionally obvious moments.",
    },
}


def vfx_amount_profile(amount):
    return VFX_AMOUNT_PROFILES.get(str(amount or "low").strip().lower(),
                                   VFX_AMOUNT_PROFILES["low"])


def _candidate_times(cuts, phrases, duration, min_gap=1.8, cap=18):
    """Punchy moments worth directing: each visual cut + each phrase start. DENSE - the reference
    style wants an effect every few seconds, so we check many moments and let the AI pick."""
    times = sorted(set(
        [round(float(t), 2) for t in (cuts or []) if 0.3 < float(t) < duration - 0.4]
        + [round(float(p.get("start", 0.0)) + 0.15, 2) for p in (phrases or [])
           if 0.3 < float(p.get("start", 0.0)) < duration - 0.4]))
    spaced = []
    for t in times:
        if not spaced or t - spaced[-1] >= min_gap:
            spaced.append(t)
    if not spaced and duration > 3:
        spaced = [round(duration * f, 2) for f in (0.2, 0.4, 0.6, 0.8)]
    return spaced[:cap]


def _phrase_at(phrases, t):
    for p in (phrases or []):
        if float(p.get("start", 0)) - 0.3 <= t <= float(p.get("end", 0)) + 0.3:
            return str(p.get("text", ""))
    return ""


def analyze_effects(video_path, times, phrases, ffmpeg, reasoning_model=None, status_cb=None,
                    emotions=None, memes=None, vfx_amount="low"):
    """One frame per candidate moment -> contact sheet -> the vision agent DIRECTS the edit.
    Returns (effect_events, char_events, meme_events):
      effects  = {time, type: arrow, cx, cy, from, target, confidence}
      characters = {time, emotion, cx, cy}."""
    emotions = list(emotions or [])
    memes = dict(memes or {})
    profile = vfx_amount_profile(vfx_amount)
    if not os.environ.get("WAVESPEED_API_KEY") or not times:
        return ([], [], [])
    work = Path(video_path).parent / "_visual_frames"
    work.mkdir(parents=True, exist_ok=True)
    frames, kept_times = [], []
    for k, t in enumerate(times):
        fp = work / f"cand_{k:02d}.jpg"
        if pipeline.extract_poster_frame(video_path, fp, ffmpeg=ffmpeg, at=t):
            frames.append(fp); kept_times.append(t)
    if not frames:
        return ([], [], [])

    effects, char_events, meme_events = [], [], []
    diagnostics = {"candidates": len(frames), "tiles_returned": 0, "arrow_requested": 0,
                   "low_confidence": 0, "caption_conflict": 0, "accepted": 0}
    emo_line = ""
    if emotions:
        emo_line = (
            "INDEPENDENTLY of the effect, a moment whose MOOD clearly calls for a reaction can ALSO get "
            "ONE kawaii pixel cat reaction ('emotion'): pick from EXACTLY this list: "
            f"{', '.join(emotions)}. Match the feeling of the line (shocking->shocked, funny->laughing, "
            "sad->sad/crying, love/romance->love, confident->cool, creepy->scared, confusing->confused, "
            "opinionated->angry, curious->thinking). " + profile["neko"] + " Give char_cx, char_cy "
            "(0-1) in an EMPTY corner away from the subject and the caption band.\n")
    meme_line = ""
    if memes:
        meme_options = "\n".join(
            f"- {meme_id}: {rec.get('reaction')} | use for: {', '.join(rec.get('cues') or [])}"
            for meme_id, rec in memes.items()
        )
        meme_line = (
            "A moment may instead receive ONE transparent meme reaction. Pick 'meme' from the exact IDs "
            "below only when its reaction meaning clearly matches the spoken phrase; otherwise use 'none'. "
            f"{profile['meme']} A moment may have a neko OR a meme, never both. Give meme_cx and meme_cy "
            "in an empty corner outside the caption band.\n"
            f"MEME OPTIONS:\n{meme_options}\n")
    BATCH = 12
    for b0 in range(0, len(frames), BATCH):
        sub_f = frames[b0:b0 + BATCH]
        sub_t = kept_times[b0:b0 + BATCH]
        sheet = agent_core.create_media_contact_sheet(
            sub_f, work / f"_sheet_{b0}.jpg",
            title="One frame per moment (tile index = moment). Coordinates are 0-1 WITHIN each tile.")
        if not sheet:
            continue
        lines = "\n".join(
            f"tile {j}: t={sub_t[j]:.1f}s | said: \"{_phrase_at(phrases, sub_t[j])[:110]}\""
            for j in range(len(sub_t)))
        prompt = (
            "You are the motion-graphics DIRECTOR for a loud, viral 'dark facts' documentary Short. Each "
            "tile is one frame at a punchy moment, with the words spoken then. This editing style is DENSE "
            f"and energetic ({str(vfx_amount).upper()} density): {profile['direct']}\n"
            "'effect' = 'arrow': a thick red arrow flies in and POINTS at the concrete subject that proves "
            "the line (face, person, object, sign, money, food, vehicle, crowd, shelf, screen, uniform, odd "
            "detail). Give the target centre cx, cy (0-1 within the tile) and 'from' = the side with empty "
            "space it should fly in from: left|right|top|bottom|top-left|top-right|bottom-left|bottom-right. "
            "'effect' = 'none' when nothing concrete is visible.\n"
            "NEVER aim an arrow at empty space, blur, generic background, or the caption band "
            "(cy roughly 0.50-0.72).\n"
            "ABSOLUTELY NO TEXT: never generate captions, subtitles, labels, words, stamps, numbers, "
            "speech bubbles, or title cards. The only allowed visuals are arrows and the supplied "
            "transparent neko/meme reaction images.\n"
            + emo_line + meme_line + "\n"
            f"Moments:\n{lines}\n\nReturn one entry for EVERY listed tile, including explicit 'none' entries.\n"
            'Return STRICT JSON: {"tiles": {"<tile_index>": {"effect": "arrow|none", '
            '"cx": 0-1, "cy": 0-1, "from": "left|right|top|bottom|top-left|top-right|bottom-left|bottom-right", '
            '"target": "short desc", "confidence": 0-10'
            + (', "emotion": "none|<from the list>", "char_cx": 0-1, "char_cy": 0-1' if emotions else '')
            + (', "meme": "none|<exact meme ID>", "meme_cx": 0-1, "meme_cy": 0-1' if memes else '')
            + '}, ...}}')
        messages = [
            {"role": "system", "content": "You direct dense, punchy visual emphasis; every effect must aim at something concrete and visible. Return JSON only."},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": agent_core.image_data_url(sheet)}},
            ]},
        ]
        payload = {"model": reasoning_model or "anthropic/claude-opus-4.8", "messages": messages,
                   "temperature": 0.35, "max_tokens": 1800, "response_format": {"type": "json_object"}}
        try:
            log(status_cb, f"Vision agent directing {len(sub_f)} moment(s) (arrows + nekos)...")
            data = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, payload, timeout=180)
            plan = agent_core.extract_json_object(data["choices"][0]["message"]["content"]) or {}
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Vision direction failed ({exc.__class__.__name__}); skipping this batch.")
            continue
        tiles = plan.get("tiles") if isinstance(plan.get("tiles"), dict) else {}
        diagnostics["tiles_returned"] += len(tiles)
        for key, d in tiles.items():
            try:
                j = int(key)
            except (TypeError, ValueError):
                continue
            if not (0 <= j < len(sub_t)) or not isinstance(d, dict):
                continue
            t_here = round(float(sub_t[j]), 2)
            # --- optional meme/neko reaction (at most one per moment; meme wins if the model
            # accidentally returned both because it has the more specific semantic mapping) ---
            meme_id = str(d.get("meme", "none") or "none").strip()
            if memes and meme_id in memes:
                try:
                    mcx = min(0.88, max(0.12, float(d.get("meme_cx", 0.80))))
                    mcy = min(0.88, max(0.12, float(d.get("meme_cy", 0.20))))
                except (TypeError, ValueError):
                    mcx, mcy = 0.80, 0.20
                if 0.50 <= mcy <= 0.72:
                    mcy = 0.18
                rec = memes[meme_id]
                meme_events.append({"time": t_here, "meme": meme_id, "cx": mcx, "cy": mcy,
                                    "reaction": rec.get("reaction", "reaction"),
                                    "sound_path": rec.get("sound_path", "")})
            emo = str(d.get("emotion", "none") or "none").lower().strip()
            if meme_id not in memes and emotions and emo in emotions:
                try:
                    ccx = min(0.9, max(0.1, float(d.get("char_cx", 0.82))))
                    ccy = min(0.9, max(0.1, float(d.get("char_cy", 0.20))))
                except (TypeError, ValueError):
                    ccx, ccy = 0.82, 0.20
                if 0.46 <= ccy <= 0.78:              # keep clear of the caption band
                    ccy = 0.18
                char_events.append({"time": t_here, "emotion": emo, "cx": ccx, "cy": ccy})
            # --- effect (ARROWS ONLY - no stamps, no circles) ---
            etype = str(d.get("effect", "none") or "none").lower().strip()
            if etype != "arrow":
                continue
            diagnostics["arrow_requested"] += 1
            try:
                conf = float(d.get("confidence", 0))
                cx = min(0.94, max(0.06, float(d.get("cx", 0.5))))
                cy = min(0.94, max(0.06, float(d.get("cy", 0.5))))
            except (TypeError, ValueError):
                continue
            # Medium/high are explicitly dense modes. Confidence 5.5 still represents a visible
            # concrete target; the old hard 6.0 threshold discarded many otherwise valid tiles.
            min_conf = 6.0 if str(vfx_amount).lower() == "low" else 5.5
            if conf < min_conf:
                diagnostics["low_confidence"] += 1
                continue
            if 0.57 <= cy <= 0.67:                                    # only the central caption core
                diagnostics["caption_conflict"] += 1
                continue
            side = str(d.get("from", "")).lower().strip().replace("_", "-")
            if side not in _DIRS:
                side = "left" if cx >= 0.5 else "right"
            effects.append({"time": t_here, "type": "arrow", "cx": cx, "cy": cy, "from": side,
                            "target": str(d.get("target", ""))[:80],
                            "confidence": round(conf, 1)})
            diagnostics["accepted"] += 1

    def _space(evs, gap):
        evs.sort(key=lambda e: e["time"])
        out = []
        for e in evs:
            if not out or e["time"] - out[-1]["time"] >= gap:
                out.append(e)
        return out
    arrow_gap = max(0.72, float(profile["min_gap"]) * 0.92)
    log(status_cb, "Visual density audit: "
        f"{diagnostics['candidates']} candidates, {diagnostics['tiles_returned']} tiles returned, "
        f"{diagnostics['arrow_requested']} arrows requested, {diagnostics['accepted']} accepted "
        f"({diagnostics['low_confidence']} low-confidence, "
        f"{diagnostics['caption_conflict']} caption conflicts).")
    return (_space(effects, arrow_gap), _space(char_events, 2.0), _space(meme_events, 2.0))


# --------------------------------------------------------------------- overlay renderers

def _ease_out_back(u, s=1.70158):
    u = min(1.0, max(0.0, u))
    return 1.0 + (s + 1) * (u - 1) ** 3 + s * (u - 1) ** 2


def _mov_from_frames(frame_dir, fps, out_path, ffmpeg):
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-framerate", f"{fps:.4f}",
          "-i", str(frame_dir / "f_%03d.png"), "-c:v", "qtrle", "-pix_fmt", "argb", str(out_path)],
         timeout=240)
    return out_path if out_path.exists() else None


def render_arrow_clip(event, w, h, fps, out_dir, idx, ffmpeg, hold=1.35):
    """The STYLED red sticker arrow (tapered curved shaft, cream outline, bevel, hard shadow -
    pipeline.build_arrow_sprite) that FLIES IN from its side with an overshoot + settle, then
    NUDGE-POINTS at the target 3x with a tiny wobble, then fades out. Any of 8 directions.
    Transparent qtrle .mov. Returns (clip_path, start_s, end_s) or None."""
    from PIL import Image
    dx, dy = _DIRS.get(event.get("from", "left"), (1.0, 0.0))
    angle = math.atan2(dy, dx)
    tx, ty = event["cx"] * w, event["cy"] * h
    gap = h * 0.016                               # tip stops just short of the target
    length = h * 0.15
    shaft = max(16, int(h * 0.019))               # THICK
    built = pipeline.build_arrow_sprite(length, shaft, angle, curve=0.0, alpha=1.0)   # STRAIGHT arrow
    if not built:
        return None
    sprite, (tip_sx, tip_sy) = built
    tip_x, tip_y = tx - dx * gap, ty - dy * gap
    # keep the whole sprite inside the frame: shift back along its own axis if needed
    sx0, sy0 = tip_x - tip_sx, tip_y - tip_sy     # sprite paste origin
    shift = 0.0
    if sx0 < 0:
        shift = max(shift, -sx0 / (abs(dx) or 1e6))
    if sy0 < 0:
        shift = max(shift, -sy0 / (abs(dy) or 1e6))
    if sx0 + sprite.width > w:
        shift = max(shift, (sx0 + sprite.width - w) / (abs(dx) or 1e6))
    if sy0 + sprite.height > h:
        shift = max(shift, (sy0 + sprite.height - h) / (abs(dy) or 1e6))
    if shift:                                     # move the arrow back along -direction
        tip_x = tx - dx * (gap + shift)
        tip_y = ty - dy * (gap + shift)

    frame_dir = out_dir / f"arrow_{idx:02d}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    n = max(10, int(round(hold * fps)))
    fly_dist = h * 0.10                           # how far back it starts
    for i in range(n):
        local = i / float(n - 1) if n > 1 else 1.0
        rot = 0.0
        if local < 0.24:                          # fly in with overshoot (past target, settle back)
            u = local / 0.24
            p = _ease_out_back(u, s=2.0)
            off = -(1.0 - p) * fly_dist
            alpha = min(1.0, u * 2.6)
            rot = (1.0 - u) * -7.0                # slight tilt that settles on arrival
        elif local > 0.86:                        # quick fade + slight retreat
            u = (local - 0.86) / 0.14
            off = -u * h * 0.02
            alpha = 1.0 - u
        else:                                     # pointing nudge: 3 pulses toward the target
            u = (local - 0.24) / 0.62
            off = math.sin(u * math.pi * 3.0) * h * 0.012 * (1.0 - 0.25 * u)
            alpha = 1.0
            rot = math.sin(u * math.pi * 3.0) * 2.2   # tiny wobble synced with the pulses
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if alpha > 0.03:
            spr = sprite
            if abs(rot) > 0.2:
                spr = sprite.rotate(rot, resample=Image.Resampling.BICUBIC,
                                    center=(tip_sx, tip_sy))
            if alpha < 0.999:
                spr = spr.copy()
                spr.putalpha(spr.split()[3].point(lambda p_: int(p_ * alpha)))
            canvas.alpha_composite(spr, (int(tip_x + dx * off - tip_sx),
                                         int(tip_y + dy * off - tip_sy)))
        canvas.save(frame_dir / f"f_{i:03d}.png")
    clip = _mov_from_frames(frame_dir, fps, out_dir / f"arrow_{idx:02d}.mov", ffmpeg)
    if not clip:
        return None
    start = max(0.0, float(event["time"]) - 0.05)
    return clip, round(start, 3), round(start + n / fps, 3)


def render_char_clip(event, w, h, fps, out_dir, idx, ffmpeg, hold=1.5):
    """A kawaii neko reaction with a springy BOUNCE-IN (overshoot + fade), a gentle bob, then a
    quick pop-out. Transparent qtrle .mov."""
    from PIL import Image
    png = EMOTION_DIR / f"{event['emotion']}.png"
    if not png.exists():
        return None
    base = Image.open(png).convert("RGBA")
    target = int(h * 0.17)
    base = base.resize((target, target), Image.NEAREST)
    frame_dir = out_dir / f"char_{idx:02d}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    cx = int(min(w - target * 0.55, max(target * 0.55, event["cx"] * w)))
    cy = int(min(h - target * 0.55, max(target * 0.55, event["cy"] * h)))
    n = max(8, int(round(hold * fps)))
    for i in range(n):
        local = i / float(n - 1) if n > 1 else 1.0
        if local < 0.30:
            u = local / 0.30
            scale = max(0.0, _ease_out_back(u))
            alpha = min(1.0, u * 1.6)
            bob = 0.0
        elif local > 0.88:
            u = (local - 0.88) / 0.12
            scale = max(0.0, 1.0 - u)
            alpha = max(0.0, 1.0 - u)
            bob = 0.0
        else:
            scale = 1.0
            alpha = 1.0
            bob = math.sin((local - 0.30) * math.pi * 3) * h * 0.006
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if scale > 0.02 and alpha > 0.03:
            sz = max(2, int(target * scale))
            spr = base.resize((sz, sz), Image.NEAREST)
            if alpha < 0.999:
                spr.putalpha(spr.split()[3].point(lambda p: int(p * alpha)))
            canvas.alpha_composite(spr, (int(cx - sz / 2), int(cy - sz / 2 + bob)))
        canvas.save(frame_dir / f"f_{i:03d}.png")
    clip = _mov_from_frames(frame_dir, fps, out_dir / f"char_{idx:02d}.mov", ffmpeg)
    if not clip:
        return None
    start = max(0.0, float(event["time"]) - 0.05)
    return clip, round(start, 3), round(start + n / fps, 3)


def render_meme_clip(event, w, h, fps, out_dir, idx, ffmpeg, hold=1.55):
    """A photographic/illustrated meme sticker: quick impact-pop, small rotational overshoot,
    subtle float, then a fast shrink. Aspect ratio and smooth source detail are preserved."""
    from PIL import Image
    png = MEME_DIR / f"{event['meme']}.png"
    if not png.exists():
        return None
    base = Image.open(png).convert("RGBA")
    max_w, max_h = int(w * 0.38), int(h * 0.24)
    ratio = min(max_w / max(1, base.width), max_h / max(1, base.height))
    bw, bh = max(2, int(base.width * ratio)), max(2, int(base.height * ratio))
    base = base.resize((bw, bh), Image.Resampling.LANCZOS)
    frame_dir = out_dir / f"meme_{idx:02d}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    cx = int(min(w - bw * 0.52, max(bw * 0.52, event["cx"] * w)))
    cy = int(min(h - bh * 0.52, max(bh * 0.52, event["cy"] * h)))
    n = max(10, int(round(hold * fps)))
    for i in range(n):
        local = i / float(n - 1) if n > 1 else 1.0
        if local < 0.25:
            u = local / 0.25
            scale = max(0.02, _ease_out_back(u, s=2.25))
            alpha = min(1.0, u * 2.1)
            angle = (1.0 - u) * -8.0
        elif local > 0.88:
            u = (local - 0.88) / 0.12
            scale, alpha, angle = max(0.02, 1.0 - u), max(0.0, 1.0 - u), u * 4.0
        else:
            u = (local - 0.25) / 0.63
            scale = 1.0 + math.sin(u * math.pi * 2) * 0.018
            alpha = 1.0
            angle = math.sin(u * math.pi * 2) * 1.2
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if alpha > 0.03:
            sw, sh = max(2, int(bw * scale)), max(2, int(bh * scale))
            spr = base.resize((sw, sh), Image.Resampling.LANCZOS)
            if abs(angle) > 0.15:
                spr = spr.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
            if alpha < 0.999:
                spr.putalpha(spr.getchannel("A").point(lambda p: int(p * alpha)))
            canvas.alpha_composite(spr, (int(cx - spr.width / 2), int(cy - spr.height / 2)))
        canvas.save(frame_dir / f"f_{i:03d}.png")
    clip = _mov_from_frames(frame_dir, fps, out_dir / f"meme_{idx:02d}.mov", ffmpeg)
    if not clip:
        return None
    start = max(0.0, float(event["time"]) - 0.05)
    return clip, round(start, 3), round(start + n / fps, 3)


def composite_overlays(video_path, overlay_clips, out_path, ffmpeg, status_cb=None):
    """Overlay each transparent clip onto the base video during its time window (audio copied)."""
    if not overlay_clips:
        return None
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-i", str(video_path)]
    for clip, s, _e in overlay_clips:
        # -itsoffset shifts this overlay's frame 0 to the event time on the base timeline, so the
        # animation plays AT the event (not already finished by the time `enable` turns on).
        cmd += ["-itsoffset", f"{s:.3f}", "-i", str(clip)]
    parts, cur = [], "0:v"
    for k, (_clip, s, e) in enumerate(overlay_clips):
        nxt = f"o{k}"
        parts.append(f"[{cur}][{k + 1}:v]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})':eof_action=pass[{nxt}]")
        cur = nxt
    fc = ";".join(parts)
    cmd += ["-filter_complex", fc, "-map", f"[{cur}]"]
    cmd += ["-map", "0:a?", "-c:a", "copy", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "18", "-preset", "veryfast", "-movflags", "+faststart", str(out_path)]
    log(status_cb, f"Compositing {len(overlay_clips)} animated overlay(s) onto the video...")
    r = _run(cmd, timeout=900)
    if not out_path.exists():
        raise RuntimeError(f"ffmpeg overlay composite failed: {(r.stderr or '')[-500:]}")
    return out_path


# alias kept for compatibility with older callers/tests
composite_arrows = composite_overlays


def _overlay_sfx_segments(events, ffprobe, status_cb=None):
    """A fitting local SFX exactly when each overlay appears. Dings + mouse clicks are favored;
    nekos get a pop."""
    try:
        import sfx_library
        data = sfx_library.build_library(status_cb=None)
        lib = data["library"]
    except Exception:
        return []
    by_type = {
        "char": ["caption_pop", "notification_ding", "ui_click"],
        "arrow": ["ui_click", "notification_ding", "swipe_whoosh", "bright_whoosh"],
    }
    rotation = ["ui_click", "notification_ding", "ui_click", "notification_ding",
                "swipe_whoosh", "caption_pop", "bright_whoosh", "impact_hit"]
    per_cat = {}
    segs = []
    for k, e in enumerate(events):
        etype = e.get("type") or ("char" if e.get("emotion") else "arrow")
        if e.get("meme") and e.get("sound_path"):
            path = Path(e["sound_path"])
            if path.exists():
                dur = min(0.72, sfx_agent.media_duration(path, ffprobe) or 0.45)
                segs.append({"path": path, "start": round(max(0.0, float(e["time"]) - 0.045), 3),
                             "duration": round(max(0.12, dur), 3), "volume": 0.28,
                             "category": "meme_reaction", "reason": f"meme_{e['meme']}_appear"})
                continue
        prefs = by_type.get(etype, []) + [rotation[k % len(rotation)]]
        cat = next((c for c in prefs if lib.get(c)), None)
        if not cat:
            continue
        files = lib.get(cat) or []
        j = per_cat.get(cat, 0); per_cat[cat] = j + 1
        path = files[j % len(files)]
        db = sfx_library.CAT_DB.get(cat, -12)
        vol = round(min(0.9, sfx_library.db_to_gain(db)), 3)
        dur = min(0.55, float(next((r["trim_len"] for r in data["records"]
                                    if r["use_path"] == path), 0.4) or 0.4))
        segs.append({"path": Path(path), "start": round(max(0.0, float(e["time"]) - 0.06), 3),
                     "duration": round(dur + 0.02, 3), "volume": vol,
                     "category": cat, "reason": f"{etype}_appear"})
    return segs


def enhance_video_with_arrows(video_path, reasoning_model=None, status_cb=None, out_dir=None,
                              add_characters=True, add_memes=True, vfx_amount="low",
                              add_arrows=True, write_project=True):
    # add_arrows=False -> detect but do NOT render the arrow layer (used when the clip run already
    # drew its own in-render arrows and only wants the neko/meme reaction layers composited on top).
    # write_project=False -> skip creating a standalone visualmaster_* timeline project.
    video_path = Path(video_path)
    if not video_path.exists():
        raise RuntimeError("Uploaded video not found.")
    if video_path.suffix.lower() not in sfx_agent.VIDEO_EXTS:
        raise RuntimeError(f"Unsupported video type: {video_path.suffix}")
    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot process video.")

    out_dir = Path(out_dir) if out_dir else VISUAL_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{video_path.stem}_visual_{stamp}.mp4"

    w, h, fps, duration = _video_props(video_path, ffprobe)
    if duration <= 0:
        duration = sfx_agent.media_duration(video_path, ffprobe)
    log(status_cb, f"Loaded video: {w}x{h} @ {fps:.1f}fps, {duration:.1f}s.")

    log(status_cb, "Detecting scene changes...")
    cuts = sfx_agent.detect_scene_cuts(video_path, ffmpeg)
    phrases = sfx_agent.transcribe_with_timing(video_path, ffmpeg, ffprobe, duration, status_cb=status_cb)
    _prof = vfx_amount_profile(vfx_amount)
    times = _candidate_times(cuts, phrases, duration,
                             min_gap=_prof["min_gap"], cap=_prof["cap"])
    log(status_cb, f"Directing {len(times)} punchy moment(s) (amount: {str(vfx_amount)})...")

    emotions = available_emotions() if add_characters else []
    memes = available_memes() if add_memes else {}
    log(status_cb, f"Reaction library: {len(emotions)} neko emotion(s), {len(memes)} categorized meme(s).")
    effects, char_events, meme_events = analyze_effects(
        video_path, times, phrases, ffmpeg, reasoning_model,
        status_cb=status_cb, emotions=emotions, memes=memes, vfx_amount=vfx_amount)
    if not add_arrows:
        effects = []                          # detected but not rendered (arrows already baked in)
    n_arrow = len(effects)
    log(status_cb, f"Direction: {n_arrow} arrow(s)"
                   + f", {len(char_events)} neko(s), {len(meme_events)} meme reaction(s).")
    if not effects and not char_events and not meme_events:
        raise RuntimeError("The director found nothing concrete to emphasise in this video. "
                           "Try a video with visible subjects/objects.")

    tmp = out_dir / f"_visualtmp_{stamp}"
    tmp.mkdir(parents=True, exist_ok=True)
    overlay_clips = []
    for i, me in enumerate(meme_events):
        clip = render_meme_clip(me, w, h, fps, tmp, i, ffmpeg)
        if clip:
            overlay_clips.append(clip)
            log(status_cb, f"Meme '{me['meme']}' ({me.get('reaction')}) at {me['time']:.1f}s")
    for i, ce in enumerate(char_events):          # nekos first (effects layer on top)
        clip = render_char_clip(ce, w, h, fps, tmp, i, ffmpeg)
        if clip:
            overlay_clips.append(clip)
            log(status_cb, f"Neko '{ce['emotion']}' at {ce['time']:.1f}s")
    for i, e in enumerate(effects):
        clip = render_arrow_clip(e, w, h, fps, tmp, i, ffmpeg)
        if clip:
            overlay_clips.append(clip)
            log(status_cb, f"Arrow at {e['time']:.1f}s -> {(e.get('target') or '')[:40]}")
    if not overlay_clips:
        raise RuntimeError("Overlay rendering failed (no clips produced).")

    overlaid = out_dir / f"_overlaid_{stamp}.mp4"
    composite_overlays(video_path, overlay_clips, overlaid, ffmpeg, status_cb=status_cb)

    segments = _overlay_sfx_segments(effects + char_events + meme_events, ffprobe, status_cb=status_cb)
    if segments:
        log(status_cb, f"Adding {len(segments)} click/ding/impact(s)...")
        sfx_agent.mix_into_video(overlaid, segments, out_path, ffmpeg, ffprobe, duration, status_cb=status_cb)
        try:
            overlaid.unlink(missing_ok=True)
        except Exception:
            pass
    else:
        overlaid.replace(out_path)
    log(status_cb, "Visual enhancement complete.")

    plan_path = out_path.with_name(out_path.stem + "_plan.json")
    plan_path.write_text(json.dumps({
        "source_video": str(video_path), "duration": round(duration, 2),
        "effect_counts": {"arrow": n_arrow, "neko": len(char_events), "meme": len(meme_events)},
        "arrows": [{"time": e["time"], "cx": e["cx"], "cy": e["cy"],
                    "from": e.get("from"), "target": e.get("target", ""),
                    "confidence": e.get("confidence")}
                   for e in effects],
        "characters": [{"time": c["time"], "emotion": c["emotion"], "cx": c["cx"], "cy": c["cy"]}
                       for c in char_events],
        "memes": [{"time": m["time"], "meme": m["meme"], "reaction": m.get("reaction"),
                    "cx": m["cx"], "cy": m["cy"], "sound": Path(m.get("sound_path", "")).name}
                   for m in meme_events],
    }, indent=2), encoding="utf-8")

    # Editable project: split the CLEAN original into scene clips and store every arrow/neko as an
    # editable overlay so the timeline editor can move/restyle/delete each one and re-render.
    project_dir = None
    if write_project:
        try:
            project_dir = _write_visual_timeline_project(
                video_path, effects, char_events, meme_events, cuts, phrases, duration, ffmpeg, out_path,
                status_cb=status_cb)
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Timeline project not written ({exc}); the enhanced video is still saved.")

    return {"video": str(out_path), "original_video": str(video_path), "visual_plan": str(plan_path),
            "arrow_count": n_arrow, "character_count": len(char_events),
            "meme_count": len(meme_events), "plan_source": "opus_vision",
            "project_dir": (str(agent_core.PROJECTS_DIR / project_dir) if project_dir else None)}


def _write_visual_timeline_project(video_path, effects, char_events, meme_events, cuts, phrases, duration,
                                   ffmpeg, enhanced_out, status_cb=None):
    """Create a REAL project for the VFX-master output: the CLEAN original video is split at the
    detected cuts into per-scene clips (original audio -> voice track), and every arrow/neko lands
    in that scene's `overlays` list - fully editable (move / scale / restyle / delete / re-render)
    exactly like an agent-produced project's visuals. Mirrors sfx_agent._write_timeline_project."""
    import shutil
    video_path = Path(video_path)
    base = re.sub(r"[^a-z0-9_]+", "_", video_path.stem.lower()).strip("_")[:36] or "upload"
    slug = f"visualmaster_{base}_{time.strftime('%H%M%S')}"
    pdir = agent_core.PROJECTS_DIR / slug
    clip_dir = pdir / "seedance 2.0"
    for d in (clip_dir, pdir / "input", pdir / "config", pdir / "renders"):
        d.mkdir(parents=True, exist_ok=True)

    def _run(cmd, timeout=600):
        subprocess.run(cmd, capture_output=True, timeout=timeout)

    # original audio -> the voice track laid back over the (muted) clean segments
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path), "-vn",
          "-ar", "48000", "-ac", "2", str(pdir / "input" / "voiceover.wav")], timeout=300)

    scenes = sfx_agent._scenes_from_cuts_and_phrases(cuts, phrases, duration)
    log(status_cb, f"Timeline project: splitting the video into {len(scenes)} clean segment(s)...")
    cfg_scenes = []
    for k, sc in enumerate(scenes):
        start, end = float(sc["start"]), float(sc["end"])
        seg_dur = max(0.15, end - start)
        name = f"seg_{k:02d}.mp4"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}",
              "-i", str(video_path), "-t", f"{seg_dur:.3f}", "-an",
              "-c:v", "libx264", "-crf", "19", "-preset", "veryfast", str(clip_dir / name)])
        cfg_scenes.append({
            "id": f"{k + 1:02d}", "name": f"Segment {k + 1:02d}",
            "script": sc.get("script", ""), "exact_voice_text": sc.get("exact_voice_text", ""),
            "start": round(start, 3), "end": round(end, 3), "clip": name, "asset": name,
            "seedance": True, "seedance_start_trim": 0.0, "render_caption": False, "overlays": [],
        })

    ffprobe = pipeline.find_ffprobe(ffmpeg)
    try:
        _snd_segs = _overlay_sfx_segments(effects + char_events + meme_events, ffprobe, status_cb=None)
    except Exception:
        _snd_segs = []
    sfx_by_time = {round(float(s.get("start", 0.0)), 2): s for s in _snd_segs}

    def _scene_for(at):
        return next((s for s in cfg_scenes if s["start"] <= at < s["end"]), cfg_scenes[-1])

    def _rel(at, scene):
        span = max(0.05, scene["end"] - scene["start"])
        return max(0.0, min(0.96, (at - scene["start"]) / span))

    # Arrows -> fully editable "arrows" overlays (the timeline inspector's arrow-style / scale /
    # rotation / animation controls drive exactly this type, and it re-renders cleanly). Nekos stay
    # baked into the initial render (the render pipeline has no character-overlay draw path), so we
    # do not add them as overlays to avoid a re-render silently dropping them.
    for i, e in enumerate(effects):
        at = float(e["time"]); scene = _scene_for(at); st = _rel(at, scene)
        snd = sfx_by_time.get(round(at, 2)) or {}
        scene["overlays"].append({
            "id": f"vm-arrow-{i:03d}", "type": "arrows", "shape": "arrow",
            "from": e.get("from") or "left",
            "start": round(st, 4), "end": round(min(1.0, st + 0.42), 4),
            "cx": round(float(e.get("cx", 0.5)), 4), "cy": round(float(e.get("cy", 0.4)), 4),
            "editor_x": round(float(e.get("cx", 0.5)), 4), "editor_y": round(float(e.get("cy", 0.4)), 4),
            "editor_scale": 1.0, "editor_rotation": 0,
            "arrow_style": e.get("arrow_style") or "default_thick_red_arrow",
            "animation": "slide", "animation_duration": 0.3,
            "appear_sfx_path": str(snd["path"]) if snd.get("path") else "",
            "appear_sfx_volume": float(snd.get("volume", 0.22)) if snd.get("path") else 0.0,
        })

    config = {
        "project_slug": slug, "title": f"Visual Master - {video_path.stem}"[:70],
        "duration": round(float(duration), 3), "scenes": cfg_scenes,
        "sfx_enabled": False, "render_captions": False, "animated_captions": False,
        "caption_mode": "off", "captions_enabled": False,
        "use_seedance_clips": True, "seedance_clip_start_trim": 0.0,
        "background_music_choice": "none", "audio_master_gain": 1.0,
        "visual_master_source": str(video_path),
    }
    (pdir / "config" / "project.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        shutil.copy2(enhanced_out, pdir / "renders" / Path(enhanced_out).name)
    except Exception:
        pass
    log(status_cb, f"Timeline project ready: {slug} (arrows remain editable; rendered neko/meme "
                   "reactions remain baked into the enhanced preview).")
    return slug
