"""Editor agent: assemble the final 9:16 Short from the generated scene clips.

Hard cuts, no dead air: each clip is trimmed to its planned scene duration and
normalized to 1080x1920/30fps, concatenated, then ONE overlay pass burns the short
viral captions (1-5 words, bold white, black outline, lower-center) and the audio
pass mixes background music + satisfying SFX (whooshes on cuts, clicks on ASSESS,
riser before the reveal, payoff hit on REVEAL) with loudnorm.
"""

import math
import subprocess
from pathlib import Path

import pipeline

ROOT = Path(__file__).resolve().parent.parent


def log(cb, msg):
    if cb:
        cb(msg)


def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _caption_overlay(caption, w, h, out_path):
    """Bold white caption with a heavy black outline + soft shadow, lower-center."""
    from PIL import Image, ImageDraw
    font = pipeline.get_font(int(h * 0.052), True)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    cx, cy = w // 2, int(h * 0.74)
    stroke = max(6, int(h * 0.006))
    dr.text((cx + 4, cy + 5), caption, font=font, anchor="mm",
            fill=(0, 0, 0, 160), stroke_width=stroke, stroke_fill=(0, 0, 0, 160))
    dr.text((cx, cy), caption, font=font, anchor="mm",
            fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    img.save(out_path)
    return out_path


def _pick_music():
    music_dir = ROOT / "background music"
    if music_dir.exists():
        for f in sorted(music_dir.glob("*")):
            if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
                return f
    return None


def _sfx_segments(plan):
    """Satisfying SFX timeline from the local library: whoosh on each cut, click/pop on
    ASSESS starts, soft riser before REVEAL, payoff hit on the REVEAL cut."""
    try:
        import sfx_library
        data = sfx_library.build_library(status_cb=None)
        lib = data["library"]
        rec_by = {r["use_path"]: r for r in data["records"]}
    except Exception:
        return []

    def pick(cat, idx=0):
        files = lib.get(cat) or []
        return files[idx % len(files)] if files else None

    segs = []
    t = 0.0
    reveal_started = False
    for i, sc in enumerate(plan["scenes"]):
        cat = None
        if sc["phase"] == "ASSESS":
            cat = "ui_click" if lib.get("ui_click") else "notification_ding"
        elif sc["phase"] == "REVEAL" and not reveal_started:
            reveal_started = True
            riser = pick("idea_reveal") or pick("notification_ding")
            if riser and t >= 1.0:
                import sfx_library as _sl
                segs.append({"path": Path(riser), "start": round(max(0.0, t - 1.0), 3),
                             "duration": 1.0, "volume": round(min(0.9, _sl.db_to_gain(_sl.CAT_DB.get("idea_reveal", -14))), 3),
                             "category": "idea_reveal", "reason": "pre_reveal_riser"})
            cat = "impact_hit" if lib.get("impact_hit") else "whoosh_hit_combo"
        elif i > 0:
            cat = ("swipe_whoosh" if lib.get("swipe_whoosh") else "bright_whoosh") \
                if i % 2 else ("bright_whoosh" if lib.get("bright_whoosh") else "swipe_whoosh")
        if cat and lib.get(cat):
            import sfx_library as _sl
            path = pick(cat, i)
            rec = rec_by.get(path, {})
            segs.append({"path": Path(path), "start": round(max(0.0, t - 0.04), 3),
                         "duration": round(min(0.55, float(rec.get("trim_len") or 0.4)) + 0.02, 3),
                         "volume": round(min(0.9, _sl.db_to_gain(_sl.CAT_DB.get(cat, -12))), 3),
                         "category": cat, "reason": f"{sc['phase'].lower()}_cut"})
        t += float(sc["duration_seconds"])
    return segs


def build_short(plan, media, project_dir, out_path, status_cb=None):
    """Trim + normalize accepted clips, concat, burn captions, mix music + SFX -> MP4."""
    ffmpeg = pipeline.find_ffmpeg()
    ffprobe = pipeline.find_ffprobe(ffmpeg)
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found.")
    tmp = Path(project_dir) / "review" / "_vt_build"
    tmp.mkdir(parents=True, exist_ok=True)

    # 1) trim/normalize each accepted scene into a uniform part
    parts, timeline = [], []
    t = 0.0
    for sc in plan["scenes"]:
        rec = media.get(sc["scene_id"]) or {}
        clip = rec.get("clip")
        if not (clip and Path(clip).exists()):
            continue
        dur = float(sc["duration_seconds"])
        part = tmp / f"part_{sc['scene_id']:02d}.mp4"
        vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,fps=30,setsar=1,format=yuv420p")
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip),
              "-t", f"{dur:.2f}", "-vf", vf, "-an",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", str(part)], timeout=300)
        if part.exists() and part.stat().st_size > 4096:
            parts.append(part)
            timeline.append({"scene": sc, "start": round(t, 3), "end": round(t + dur, 3)})
            t += dur
    if len(parts) < 4:
        raise RuntimeError(f"Only {len(parts)} usable scene clip(s) - not enough for a Short.")

    # 2) concat
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    joined = tmp / "joined.mp4"
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
          "-i", str(lst), "-c", "copy", str(joined)], timeout=300)
    if not joined.exists():
        raise RuntimeError("concat failed")

    # 3) burn captions in one overlay pass
    log(status_cb, "Editing final short... (captions)")
    cap_inputs, filters = [], []
    for k, row in enumerate(timeline):
        cap = str(row["scene"].get("caption") or "").strip()
        if not cap:
            continue
        png = tmp / f"cap_{k:02d}.png"
        _caption_overlay(cap, 1080, 1920, png)
        cap_inputs.append((png, row["start"], min(row["end"], row["start"] + max(1.2, (row["end"] - row["start"]) * 0.9))))
    cur_video = joined
    if cap_inputs:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined)]
        for png, _s, _e in cap_inputs:
            cmd += ["-i", str(png)]
        chain, cur = [], "0:v"
        for k, (_png, s, e) in enumerate(cap_inputs):
            nxt = f"c{k}"
            chain.append(f"[{cur}][{k + 1}:v]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})'[{nxt}]")
            cur = nxt
        capped = tmp / "capped.mp4"
        cmd += ["-filter_complex", ";".join(chain), "-map", f"[{cur}]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", str(capped)]
        _run(cmd, timeout=600)
        if capped.exists():
            cur_video = capped

    # 4) audio: music bed + SFX, loudnorm
    log(status_cb, "Editing final short... (music + SFX)")
    total = t
    music = _pick_music()
    segs = _sfx_segments(plan)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(cur_video)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    for s in segs:
        cmd += ["-i", str(s["path"])]
    afs, mix = [], []
    base = 2 if music else 1
    if music:
        afs.append(f"[1:a]volume=0.22,atrim=0:{total:.3f},afade=t=out:st={max(0.0, total - 1.2):.3f}:d=1.2[mus]")
        mix.append("mus")
    for k, s in enumerate(segs):
        delay = int(round(s["start"] * 1000))
        afs.append(f"[{base + k}:a]atrim=0:{s['duration']:.3f},asetpts=PTS-STARTPTS,"
                   f"volume={s['volume']:.3f},adelay={delay}:all=1[s{k}]")
        mix.append(f"s{k}")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if mix:
        afs.append("".join(f"[{m}]" for m in mix)
                   + f"amix=inputs={len(mix)}:duration=longest:dropout_transition=0:normalize=0,"
                   f"loudnorm=I=-15:TP=-1.0:LRA=11,atrim=0:{total:.3f}[aout]")
        cmd += ["-filter_complex", ";".join(afs), "-map", "0:v:0", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart", str(out_path)]
    else:                                              # no audio assets at all: silent track
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
                "-shortest", "-map", "0:v:0", "-map", f"{base}:a",
                "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(out_path)]
    r = _run(cmd, timeout=600)
    if not out_path.exists():
        raise RuntimeError(f"final mux failed: {(r.stderr or '')[-400:]}")
    return {"video": str(out_path), "duration": round(total, 1), "scenes_used": len(parts)}
