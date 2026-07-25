"""Discovery mode for Clip Short: NO script given -> the agent finds one long, fascinating
process/craft TikTok (e.g. a Chinese craftsman turning bamboo into chopsticks the old way),
watches it (vision on a timestamped frame sheet), writes a mini-short script that explains
the process, records the voiceover, and recuts the LONG source video so every sentence
plays over the matching stage of the process.

Fully driven by the user's logged-in TikTok session + the WaveSpeed LLM adapter; renders
through the normal pipeline so the result stays timeline-editable."""
import json
import math
import re
import subprocess
import time
from pathlib import Path

import agent_core
import clip_scraper
import pipeline

log = agent_core.log

MIN_SRC_SECONDS = 45          # "long" TikTok: a real process, not a 10s clip
MAX_SRC_SECONDS = 600
SHEET_FRAMES = 24
MAX_CANDIDATES_TRIED = 12
MAX_CANDIDATES_TRIED_STORY = 26   # story mode screens a much wider pool (user: may take longer)

# Curated fallback queries when the user gives no topic hint AND the query LLM fails
# in a non-fatal way. ASIAN craft/process topics only (user rule: Chinese/Japanese style).
CANDIDATE_LIBRARY_DIR = Path(__file__).parent / "candidate library"


def _record_candidates(accepted, style):
    """CANDIDATE LIBRARY (user 2026-07-23): persist EVERY candidate that was ever shown
    as a pick - across all runs - so the mini/discovery UI can browse them later and
    start a run directly from a saved candidate. Keyed by TikTok video id; sheets are
    copied out of the tmp dir before it is cleaned."""
    import shutil
    sheets = CANDIDATE_LIBRARY_DIR / "sheets"
    sheets.mkdir(parents=True, exist_ok=True)
    fp = CANDIDATE_LIBRARY_DIR / "candidates.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    now = time.strftime("%Y-%m-%d %H:%M")
    for a in accepted:
        c, info = a["cand"], a["info"]
        cid = str(c.get("id") or "")
        if not cid:
            continue
        dst = sheets / f"{cid}.jpg"
        try:
            if a.get("sheet") and Path(a["sheet"]).exists() and not dst.exists():
                shutil.copy2(a["sheet"], dst)
        except OSError:
            pass
        prev_dir = CANDIDATE_LIBRARY_DIR / "previews"
        prev_dir.mkdir(parents=True, exist_ok=True)
        pdst = prev_dir / f"{cid}.mp4"
        try:
            pv = Path(str(a.get("preview") or ""))
            if a.get("preview") and pv.exists() and not pdst.exists():
                shutil.copy2(pv, pdst)
        except OSError:
            pass
        prev = data.get(cid) or {}
        data[cid] = {
            "id": cid, "url": str(c.get("url") or ""), "author": str(c.get("author") or ""),
            "likes": int(c.get("likes") or 0), "dur": int(c.get("dur") or 0),
            "desc": str(c.get("desc") or ""),
            "title": str(info.get("topic_title") or prev.get("title") or ""),
            "premise": str(info.get("premise") or prev.get("premise") or ""),
            "appeal": info.get("appeal") or prev.get("appeal"),
            "style": style,
            "stages": [f"{st['start']:.0f}-{st['end']:.0f}s: {st['action']}"
                       for st in (info.get("stages") or [])][:8],
            "first_seen": prev.get("first_seen") or now, "last_seen": now,
            "picked": bool(prev.get("picked")), "project": prev.get("project") or "",
            "sheet": f"sheets/{cid}.jpg" if dst.exists() else str(prev.get("sheet") or ""),
            "preview": f"previews/{cid}.mp4" if pdst.exists() else str(prev.get("preview") or ""),
        }
    fp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def _browser_preview(src, dst, ff):
    """TikTok downloads are HEVC - browsers without an H.265 license show a BLACK video
    with working audio (user bug 2026-07-23). Transcode a small H.264 preview for the
    picker/library players; pass-through when the source is already H.264."""
    ffp = pipeline.find_ffprobe(ff)
    r = subprocess.run([ffp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name", "-of", "csv=p=0", str(src)],
                       capture_output=True, text=True)
    if (r.stdout or "").strip().lower() == "h264":
        return Path(src)
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src),
                    "-vf", "scale=480:-2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "27", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", str(dst)], check=True)
    return Path(dst)


def _mark_candidate_picked(cand_id, project_slug):
    fp = CANDIDATE_LIBRARY_DIR / "candidates.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        entry = data.get(str(cand_id))
        if entry is not None:
            entry["picked"] = True
            entry["project"] = str(project_slug)
            fp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _update_candidate_file(cand_id, abs_path):
    """Remember where a candidate's video file finally lives (project folder), so the
    library can PLAY it and a pinned re-run can reuse it without re-downloading."""
    fp = CANDIDATE_LIBRARY_DIR / "candidates.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        entry = data.get(str(cand_id))
        if entry is not None:
            entry["file"] = str(abs_path)
            fp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def load_candidate_library():
    """All candidates ever shown, newest first (for the UI library)."""
    fp = CANDIDATE_LIBRARY_DIR / "candidates.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows = list(data.values())
    rows.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    return rows


_FALLBACK_QUERIES = [
    "手工 竹筷子 制作", "伝統工芸 職人", "japanese knife making process", "古法制作",
    "中国 传统 手艺", "japanese sword forging", "和菓子 職人", "chinese street food process",
]


def _plan_queries(hint, reasoning_model, status_cb=None, style="process", region=""):
    """LLM: turn 'find something fascinating' (or the user's rough hint) into TikTok
    search queries that surface LONG process/craft videos (or story skits in mini mode)."""
    if style == "story":
        jp = region == "japan"
        # WIDE + SPECIFIC search (user 2026-07-25 "es müssen special videos sein, nicht 0815"):
        # one query per ANGLE FAMILY so the pool is not five variations of the same prank,
        # and every query must name a concrete unusual SITUATION, never a genre word.
        families = ("1 hidden-camera prank that ESCALATES, 2 a strict rule being enforced on "
                    "someone, 3 a confrontation/interrogation (phone, messages, lies), "
                    "4 an unusual JOB or PLACE with a woman at its centre (host club, maid "
                    "cafe, idol training, night shift, family restaurant), 5 a physical "
                    "challenge/competition/test, 6 a role reversal or identity swap, "
                    "7 a family clash (mother-in-law, siblings, parents meeting the partner), "
                    "8 a transformation the camera SHOWS (before/after, makeover, training "
                    "over days), 9 a public-place reaction with a twist, 10 an eccentric "
                    "person doing something with visible consequences")
        sys_p = ("You find LONG TikTok videos (40s-10min) that tell ONE complete little STORY "
                 "or skit with several scenes, ALWAYS featuring "
                 + ("Japanese" if jp else "Japanese/Korean/Chinese")
                 + " WOMEN or COUPLES (or similar: sisters, families, models, hosts).\n"
                 "The user is TIRED of generic clips. A good target video is SPECIFIC and "
                 "unusual: it has a place, a rule, a job or an escalation you could describe "
                 "in one sentence and someone would want to watch. A bad target is a person "
                 "talking to the camera in one room.\n"
                 f"Cover these ANGLE FAMILIES, one or two queries each: {families}.\n"
                 "Every query must name a CONCRETE situation (people + place or rule or "
                 "action). NEVER submit bare genre words like 'couple prank', 'funny skit', "
                 "'japanese comedy' - those return the same mainstream clips everyone sees. "
                 "AVOID: calm daily-life vlogs, cooking, GRWM, hauls, dance-only, "
                 "single-person talking heads.\n"
                 "Return JSON: {\"queries\": [16 search strings]}. About half in "
                 + ("Japanese" if jp else "Japanese, Korean or Chinese")
                 + " using native words ("
                 + ("ドッキリ, コント, 兄妹, 姑, ホスト, メイド喫茶, 検証, 大食い" if jp else
                    "ドッキリ, コント, 検証, 情侣 恶搞, 姐妹 剧情, 몰카, 커플 장난, 실험")
                 + "), the rest in English. No hashtags, no Western creators.")
        user_p = (f"The user wants a video about: {hint}" if hint else
                  "No topic given - make the 16 queries as DIFFERENT from each other as "
                  "possible; each one should surface a different kind of video.")
        data = agent_core._post_llm_json(reasoning_model, [
            {"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
            max_tokens=1000, temperature=0.85)
        queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
        if not queries:
            raise RuntimeError("Discovery: the query planner returned no queries.")
        log(status_cb, f"Mini discovery: {len(queries)} angle-diverse queries planned.")
        log(status_cb, "  " + " | ".join(queries[:16]))
        return queries[:16]
    sys_p = ("You find LONG TikTok videos (45s-10min) that show a complete fascinating process "
             "from EAST ASIA (China/Japan, also Korea/Taiwan/SE Asia): traditional crafts, "
             "old-school manufacturing, cooking from raw ingredients, restoration, temple/village "
             "trades - e.g. a Chinese craftsman turning bamboo into chopsticks the old way, a "
             "Japanese artisan forging a knife. The viewer must be able to FOLLOW THE PROCESS "
             "visually. Return JSON: {\"queries\": [8 search strings]}. Mix English with Chinese "
             "and Japanese queries - native-language queries find the authentic long uploads. "
             "No hashtags, no Western topics.")
    user_p = (f"The user wants a video about: {hint}" if hint else
              "No topic given - pick fascinating ASIAN process topics yourself (vary them: bamboo, "
              "wood, metal, food, tea, fiber, ceramics, lacquer, old machines...).")
    data = agent_core._post_llm_json(reasoning_model, [
        {"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
        max_tokens=600, temperature=0.6)
    queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()]
    if not queries:
        raise RuntimeError("Discovery: the query planner returned no queries.")
    log(status_cb, "Discovery queries: " + " | ".join(queries[:8]))
    return queries[:8]


def _search_long(queries, status_cb=None, min_seconds=None, want=12, per_author=99):
    """Search TikTok (logged-in session) and keep only LONG candidates, best first.

    `want` controls how deep each query is scrolled and `per_author` caps how many clips
    one creator may contribute - without that cap a single prolific account floods the
    pool and every candidate looks the same (user 2026-07-25)."""
    import tiktok_login
    seen, out = set(), []
    by_author = {}
    for q in queries:
        try:
            items = tiktok_login.search_sync(q, want=want, status_cb=status_cb,
                                             sort="MOST_LIKED", timeout_s=240)
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Discovery search '{q}' failed: {exc.__class__.__name__}")
            continue
        for it in items or []:
            vid = str(it.get("id") or "")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            author = it.get("author")
            author = str((author.get("uniqueId") if isinstance(author, dict) else author) or "")
            dur = 0
            try:
                dur = int((it.get("video") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                pass
            if not author or dur < (min_seconds or MIN_SRC_SECONDS) or dur > MAX_SRC_SECONDS:
                continue
            likes = 0
            try:
                likes = int((it.get("stats") or {}).get("diggCount") or 0)
            except (TypeError, ValueError):
                pass
            if by_author.get(author.lower(), 0) >= per_author:
                continue
            by_author[author.lower()] = by_author.get(author.lower(), 0) + 1
            desc = str(it.get("desc") or "")[:180]
            out.append({"id": vid, "author": author, "likes": likes, "dur": dur,
                        "desc": desc, "query": q,
                        "url": f"https://www.tiktok.com/@{author}/video/{vid}"})
    out.sort(key=lambda c: -(c["likes"] * math.log(max(c["dur"], 46))))
    log(status_cb, f"Discovery: {len(out)} long candidates "
                   f"({MIN_SRC_SECONDS}-{MAX_SRC_SECONDS}s) across {len(queries)} queries.")
    return out


_WESTERN_HINT = re.compile(
    r"\b(the|and|with|my|his|her|our|your|prank|boyfriend|girlfriend|husband|wife|mom|dad|"
    r"guys|omg|lol|funny|reaction|challenge|couple|family|vlog|pov)\b", re.I)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def _looks_western(desc):
    """Caption heuristic: no CJK characters plus several English function words."""
    d = str(desc or "")
    if _CJK.search(d):
        return False
    return len(_WESTERN_HINT.findall(d)) >= 2 or bool(
        re.search(r"\b(USA|America|San Diego|New York|London|LA|Texas|California)\b", d, re.I))


def _fallback_pool(cands, keep):
    """Used when the LLM prescreen fails: never hand back the raw like-ranked list (that put a
    San Diego ramen contest first) - drop obvious Western captions and spread the creators."""
    out, per_author = [], {}
    for c in cands:
        if _looks_western(c.get("desc") or ""):
            continue
        a = str(c.get("author") or "").lower()
        if per_author.get(a, 0) >= 1:
            continue
        per_author[a] = per_author.get(a, 0) + 1
        out.append(c)
        if len(out) >= keep:
            break
    return out or cands[:keep]


def _prescreen_story(cands, reasoning_model, status_cb=None, keep=22):
    """ONE cheap text pass over the WHOLE candidate pool before any download.

    Ranking by likes alone returns the same mainstream clips every time (user 2026-07-25:
    "es müssen special videos sein, nicht 0815"). This scores every candidate's caption for
    how SPECIFIC/unusual it is, drops obviously Western uploads, and returns a diverse,
    ranked shortlist so the expensive download+vision work is spent on the best material."""
    if not cands:
        return []
    lines = []
    for i, c in enumerate(cands):
        lines.append(f"[{i}] @{c['author']} ({c['dur']}s, {c['likes']:,} likes): "
                     + (c.get("desc") or "")[:140].replace("\n", " "))
    # Compact CSV, sent in BATCHES: one JSON answer for 140 entries overran max_tokens and
    # came back truncated, so the whole prescreen silently kept nothing (2026-07-25 bug).
    BATCH = 45
    sys_p = (
        "You pre-screen TikTok search results for a viral story-short factory. The videos "
        "must show JAPANESE / KOREAN / CHINESE women or couples in a little STORY with "
        "several scenes.\n"
        "For every entry rate how SPECIAL it looks: a concrete unusual situation (a rule, a "
        "job, a place, an escalation, a test, a confrontation) scores high; a generic clip "
        "that could be any account scores low.\n"
        "Answer COMPACTLY as JSON: {\"items\": [{\"i\": index, \"s\": special 0-10, "
        "\"c\": \"prank|rule|confront|job|challenge|swap|family|transform|public|other\", "
        "\"w\": 1 if Western else 0}, ...]} - one entry per index, no other keys, no prose.\n"
        "Rules: w=1 for Western/English-language creators (captions read as native English, "
        "or the place is in the US/Europe) - disqualified. s<=3 for pure vlog, cooking, "
        "GRWM, haul, dance, single talking head, or a caption too vague to picture. "
        "Be harsh: at most a quarter may score 8 or higher.")
    rows = []
    for start in range(0, min(len(lines), 180), BATCH):
        chunk = lines[start:start + BATCH]
        try:
            data = agent_core._post_llm_json(reasoning_model, [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": "\n".join(chunk)}],
                max_tokens=3000, temperature=0.2)
            got = data.get("items") or []
            rows.extend(got)
            log(status_cb, f"Discovery: prescreen batch {start//BATCH + 1} "
                           f"-> {len(got)}/{len(chunk)} rated.")
        except Exception as exc:  # noqa: BLE001 - a bad batch must not kill the pass
            log(status_cb, f"Discovery: prescreen batch {start//BATCH + 1} failed ({exc}).")
    if not rows:
        log(status_cb, "Discovery: text prescreen returned nothing - using the raw pool.")
        return _fallback_pool(cands, keep)
    scored = []
    for r in rows:
        try:
            idx = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(cands)):
            continue
        c = dict(cands[idx])
        try:
            c["special"] = float(r.get("s", r.get("special")) or 0)
        except (TypeError, ValueError):
            c["special"] = 0.0
        c["category"] = str(r.get("c") or r.get("category") or "other")
        desc = c.get("desc") or ""
        western = bool(r.get("w") or r.get("western")) or _looks_western(desc)
        if western or c["special"] < 5:
            continue
        scored.append(c)
    if not scored:
        log(status_cb, "Discovery: prescreen kept nothing - using the filtered raw pool.")
        return _fallback_pool(cands, keep)
    scored.sort(key=lambda c: (-c["special"], -c["likes"]))
    # category spread: never let one story type own the shortlist
    out, per_cat = [], {}
    for c in scored:
        cat = c["category"]
        if per_cat.get(cat, 0) >= 4:
            continue
        per_cat[cat] = per_cat.get(cat, 0) + 1
        out.append(c)
        if len(out) >= keep:
            break
    log(status_cb, f"Discovery: text prescreen kept {len(out)}/{len(cands)} "
                   f"(categories: {', '.join(f'{k}x{v}' for k, v in per_cat.items())}).")
    return out


def _probe_technical(path, status_cb=None):
    """Objective, LLM-free quality probe: letterbox share + real scene changes per minute.

    Catches two failures the vision pass kept waving through: videos that are mostly black
    bars (unusable as a 9:16 short) and single-shot talking heads (no visual story)."""
    ff = pipeline.find_ffmpeg()
    out = {"letterbox": 0.0, "cuts_per_min": 0.0}
    try:
        r = subprocess.run([ff, "-hide_banner", "-t", "40", "-i", str(path),
                            "-vf", "cropdetect=24:2:0", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=180)
        crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
        if crops:
            w, h, _x, _y = (int(v) for v in crops[-1])
            probe = subprocess.run([pipeline.find_ffprobe(ff), "-v", "error", "-select_streams",
                                    "v:0", "-show_entries", "stream=width,height",
                                    "-of", "csv=p=0", str(path)], capture_output=True, text=True)
            fw, fh = (int(v) for v in probe.stdout.strip().split(",")[:2])
            if fw and fh:
                out["letterbox"] = round(1.0 - (w * h) / float(fw * fh), 3)
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run([ff, "-hide_banner", "-t", "60", "-i", str(path),
                            "-vf", "select='gt(scene,0.32)',metadata=print",
                            "-an", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=180)
        cuts = len(re.findall(r"lavfi\.scene_score", r.stderr or ""))
        secs = min(60.0, _video_duration(path) or 60.0)
        out["cuts_per_min"] = round(cuts * 60.0 / max(1.0, secs), 1)
    except Exception:  # noqa: BLE001
        pass
    return out


def _is_portrait(path):
    ffp = pipeline.find_ffprobe(pipeline.find_ffmpeg())
    r = subprocess.run([ffp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
        return h > w
    except (ValueError, AttributeError):
        return False


def _video_duration(path):
    ffp = pipeline.find_ffprobe(pipeline.find_ffmpeg())
    r = subprocess.run([ffp, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def _frame_sheet(src, work_dir, status_cb=None, tag=""):
    """4x4 contact sheet of the long source with BIG timestamp labels, so the vision model
    can name process stages with usable start/end times. One sheet PER candidate (tag)."""
    from PIL import Image, ImageDraw
    ff = pipeline.find_ffmpeg()
    total = _video_duration(src)
    # Adaptive density: one frame every ~6s (was a fixed 24 -> ~12.5s gaps on a 300s
    # source, which let the vision model miss fast intro cuts and misplace stage
    # boundaries - the root cause of "saw footage under the boiling sentence").
    n = max(SHEET_FRAMES, min(48, int(total // 6) or SHEET_FRAMES))
    times = [round(total * (0.02 + 0.96 * k / (n - 1)), 1) for k in range(n)]
    W, H, LBL = 240, 426, 30
    cols = 4 if n <= 32 else 6
    rows = math.ceil(n / cols)
    canvas = Image.new("RGB", (W * cols, (H + LBL) * rows), (8, 8, 8))
    dr = ImageDraw.Draw(canvas)
    for i, t in enumerate(times):
        fp = Path(work_dir) / f"_ds_{tag}_{i:02d}.jpg"
        subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(t), "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={W}:{H}", str(fp)], check=True)
        x, y = (i % cols) * W, (i // cols) * (H + LBL)
        canvas.paste(Image.open(fp), (x, y))
        dr.text((x + 8, y + H + 7), f"[{i:02d}]  t={t:.0f}s", fill=(120, 255, 150))
    sheet = Path(work_dir) / (f"discover_sheet_{tag}.jpg" if tag else "discover_sheet.jpg")
    canvas.save(sheet, quality=88)
    log(status_cb, f"Discovery: built {n}-frame sheet of the {total:.0f}s source.")
    return sheet, total


def _verify_recut(scenes, sentences, stages, src_final, clip_dir, work_dir,
                  reasoning_model, status_cb=None):
    """Self-check AFTER cutting: show the vision model each cut clip next to its sentence;
    mismatched clips are re-cut at the timestamp the model points to. One pass, one call -
    this catches wrong stage boundaries (the 'saw under the boiling sentence' failure)."""
    from PIL import Image, ImageDraw
    import scrape_v2
    ff = pipeline.find_ffmpeg()
    total = _video_duration(src_final)
    W, H, LBL = 200, 356, 26
    canvas = Image.new("RGB", (W * 3, (H + LBL) * len(scenes)), (8, 8, 8))
    dr = ImageDraw.Draw(canvas)
    for i, sc in enumerate(scenes):
        d = max(0.4, float(sc["end"]) - float(sc["start"]))
        clip = Path(clip_dir) / sc["clip"]
        for j, frac in enumerate((0.15, 0.5, 0.85)):
            fp = Path(work_dir) / f"_vr_{i:02d}_{j}.jpg"
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(round(d * frac, 2)),
                            "-i", str(clip), "-frames:v", "1", "-vf", f"scale={W}:{H}",
                            str(fp)], check=True)
            if fp.exists():
                canvas.paste(Image.open(fp), (j * W, i * (H + LBL)))
        dr.text((8, i * (H + LBL) + H + 5), f"[{i}]", fill=(120, 255, 150))
    sheet = Path(work_dir) / "verify_sheet.jpg"
    canvas.save(sheet, quality=88)

    stage_txt = "; ".join(f"{k}: {float(s['start']):.0f}-{float(s['end']):.0f}s "
                          f"{s['action']}" for k, s in enumerate(stages))
    sent_txt = "\n".join(f"[{i}] \"{s['text']}\"" for i, s in enumerate(sentences))
    prompt = (
        "You see a contact sheet: each ROW = one cut video clip (3 frames), labeled [i]. "
        "Each clip plays UNDER this narration sentence:\n" + sent_txt + "\n"
        f"The clips were cut from one {total:.0f}s source video whose stages are: "
        + stage_txt + "\n"
        "For each row judge STRICTLY: does the visible footage show the action the "
        "sentence describes? Return JSON: {\"clips\": {\"0\": {\"match\": true}, "
        "\"1\": {\"match\": false, \"use_time\": 82}, ...}}. use_time = the second in the "
        "SOURCE video where the sentence's action IS visible (required when match=false).")
    try:
        data = scrape_v2._vision_json(prompt, str(sheet), max_tokens=1200, temperature=0.1,
                                      reasoning_model=reasoning_model)
    except Exception as exc:  # noqa: BLE001 - verification is best-effort
        log(status_cb, f"Discovery: cut verification skipped ({exc}).")
        return
    fixed = 0
    for i, sc in enumerate(scenes):
        v = (data.get("clips") or {}).get(str(i)) or {}
        if v.get("match") is not False:
            continue
        try:
            t0 = float(v.get("use_time"))
        except (TypeError, ValueError):
            continue
        d = round(float(sc["end"]) - float(sc["start"]), 2)
        t0 = min(max(0.0, t0), max(0.0, total - d - 0.2))
        # Keep the source's own audio. This pass re-cuts clips the vision review found
        # mismatched, and it used to force -an: every corrected clip lost its audio bed,
        # so a story that had dual audio ended up part silent with no warning.
        keep_audio = "audio" in (subprocess.run(
            [pipeline.find_ffprobe(ff), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(src_final)],
            capture_output=True, text=True).stdout or "")
        cmd = [ff, "-y", "-loglevel", "error", "-ss", str(round(t0, 2)),
               "-t", str(d + 0.05), "-i", str(src_final),
               "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                      "crop=1080:1920,fps=30"]
        cmd += ["-c:a", "aac", "-b:a", "128k"] if keep_audio else ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                str(Path(clip_dir) / sc["clip"])]
        subprocess.run(cmd, check=True)
        fixed += 1
        log(status_cb, f"Discovery: clip {i} did not match its sentence - re-cut at {t0:.0f}s.")
    log(status_cb, f"Discovery: cut verification done ({fixed} clip(s) re-cut)." if fixed
        else "Discovery: cut verification passed - all clips match their sentences.")


def _vision_stages(sheet, total, cand, reasoning_model, status_cb=None, style="process"):
    """Vision: rate the candidate + segment the process (or story beats) into stages."""
    import scrape_v2
    if style == "story":
        # The output schema must be the LAST thing in this prompt. When the numbered
        # judging steps came after it, the model answered with only the key named in the
        # final step ({"stages": []}) and every candidate lost its appeal/title, which
        # failed a whole run with "no candidate survived the vision review" (2026-07-25).
        prompt = (
            "You see a frame sheet (rows in time order) of ONE long TikTok video; each frame is "
            f"labeled with its timestamp (video length {total:.0f}s). Caption: \"{cand['desc']}\".\n"
            "Judge whether this tells a COMPLETE little STORY/skit with clear beats, "
            "prominently featuring Japanese/Korean/Chinese women or couples (or sisters, "
            "families, models).\n"
            "REJECT (is_process=false, appeal at most 4) when ANY of these applies: the people "
            "are WESTERN/non-East-Asian, or the burned-in text is fluent creator-written "
            "English; it is one person talking to the camera, or everything happens in ONE "
            "spot with no real change of scene; the plot only works through spoken dialogue or "
            "on-screen text (the recut carries a NEW narration, so the visuals alone must tell "
            "it); dance-only, slideshow, text cards, men-only, product ad; heavy black bars, "
            "tiny inset video or a screen-recording layout. Be strict - a rejected candidate "
            "costs nothing, a bad one ruins the short.\n"
            "STAGES: 4-8 story beats in time order. `action` describes ONLY the STATE that is "
            "verifiably visible in the frames (who is where, doing what, which expression) - "
            "NEVER infer motion between frames ('rushes in', 'arrives') unless a frame "
            "literally shows it; anything invented desyncs voice and video. Set is_reveal=true "
            "on the beat carrying the punchline.\n"
            "APPEAL, as a harsh critic - the scores must SPREAD, not cluster: 9-10 = you would "
            "stop scrolling instantly, unusual situation, visible escalation and a payoff "
            "(rare, ~1 in 10); 7-8 = a clear premise with real tension or comedy; 5-6 = "
            "watchable but ordinary, the kind of clip everyone has seen; 4 or less = generic "
            "or rejected above. Never give 8 just because a video is fine - 'fine' is a 6. If "
            "you cannot state a one-line premise with tension or humor, appeal is at most 5.\n"
            "Reply with ONE JSON object and no other text, containing ALL of these keys:\n"
            "{\"appeal\": 1-10, \"is_process\": true/false, "
            "\"topic_title\": \"short English title of the story\", "
            "\"premise\": \"the story in one punchy line\", "
            "\"stages\": [{\"start\": sec, \"end\": sec, \"action\": \"what is visible\", "
            "\"is_reveal\": true/false}, ...]}")
        data = scrape_v2._vision_json(prompt, str(sheet), max_tokens=1500, temperature=0.1,
                                      reasoning_model=reasoning_model)
        if data.get("appeal") is None:
            # One malformed answer must never cost the candidate: ask again, schema only.
            log(status_cb, "Discovery: vision answer incomplete - retrying with a plain schema.")
            data = scrape_v2._vision_json(
                prompt.split("Reply with ONE JSON object")[0]
                + "Reply with ONE JSON object and nothing else: {\"appeal\": 1-10, "
                  "\"is_process\": true/false, \"topic_title\": \"short English title\", "
                  "\"premise\": \"one line\", \"stages\": [{\"start\": sec, \"end\": sec, "
                  "\"action\": \"what is visible\", \"is_reveal\": true/false}]}",
                str(sheet), max_tokens=1500, temperature=0.1,
                reasoning_model=reasoning_model) or data
        stages = []
        for st in (data.get("stages") or []):
            try:
                a, b = float(st.get("start")), float(st.get("end"))
            except (TypeError, ValueError):
                continue
            if b > a >= 0:
                stages.append({"start": a, "end": min(b, total),
                               "action": str(st.get("action") or "").strip(),
                               "is_reveal": bool(st.get("is_reveal"))})
        data["stages"] = stages
        log(status_cb, f"Mini discovery vision: appeal {data.get('appeal')}/10, "
                       f"{len(stages)} story beats - {data.get('topic_title')}")
        return data
    prompt = (
        "You see a frame sheet (4 columns, rows in time order) of ONE long TikTok video; each frame is labeled with its "
        f"timestamp (video length {total:.0f}s). Caption of the post: \"{cand['desc']}\".\n"
        "Task: judge whether this shows a COMPLETE, fascinating real-world process (craft, "
        "cooking, manufacturing, restoration...) that a narrator could explain step by step.\n"
        "Return JSON: {\"appeal\": 1-10, \"is_process\": true/false, "
        "\"topic_title\": \"short English title of what happens\", "
        "\"stages\": [{\"start\": sec, \"end\": sec, \"action\": \"what visibly happens\", "
        "\"is_reveal\": true/false}, ...]}.\n"
        "Rules: 4-8 stages, strictly in time order, covering the interesting parts of the video "
        "(skip intros/outros/talking heads). The `action` must describe ONLY what is literally "
        "on screen in that window (objects, materials, colors, hands) - the narration will be "
        "written from these descriptions, so anything you invent will desync voice and video. "
        "Mark is_reveal=true on the stage that shows the FINISHED product (the payoff shot); "
        "if the finished product never appears, mark none. Calibrate appeal honestly: 9-10 = "
        "exceptional and rare, 7-8 = good, 6 = usable, below = reject (static, text-card, "
        "talking head, unclear).")
    data = scrape_v2._vision_json(prompt, str(sheet), max_tokens=1500, temperature=0.1,
                                  reasoning_model=reasoning_model)
    stages = []
    for st in (data.get("stages") or []):
        try:
            a, b = float(st.get("start")), float(st.get("end"))
        except (TypeError, ValueError):
            continue
        if b - a >= 1.5 and 0 <= a < total:
            stages.append({"start": max(0.0, a), "end": min(total, b),
                           "action": str(st.get("action") or "")})
    stages.sort(key=lambda s: s["start"])
    data["stages"] = stages
    log(status_cb, f"Discovery vision: appeal {data.get('appeal')}/10, "
                   f"{len(stages)} stages - {data.get('topic_title')}")
    return data


def _write_script(analysis, hint, reasoning_model, status_cb=None, style="process"):
    """LLM: write the mini-short narration; every sentence is tied to one visual stage."""
    stages = analysis["stages"]
    stage_lines = "\n".join(f"[{i}] {s['start']:.0f}-{s['end']:.0f}s: {s['action']}"
                            for i, s in enumerate(stages))
    reveal_idx = next((i for i, s in enumerate(stages) if s.get("is_reveal")), None)
    sys_p = (
        "You write narrations for viral 30-40s documentary mini shorts (TikTok/YouTube Shorts). "
        "You get the visual stages of ONE long source video; the short will show these stages in "
        "order while your narration explains the process.\n"
        "Return JSON: {\"title\": str, \"slug\": \"kebab-case-slug\", "
        "\"sentences\": [{\"text\": \"one sentence\", \"stage\": stage_index}, ...], "
        "\"hook_keywords\": [4-8 UPPERCASE words from the text], "
        "\"impact_word\": \"the single most shocking word of sentence 1\"}.\n"
        "HARD RULES:\n"
        "1. VOICE = VIDEO: every sentence may ONLY talk about what its assigned stage literally "
        "shows (per the stage description). Never mention an object, color or result that is "
        "not visible in that stage - if the finished product is never shown, do not describe "
        "how it looks.\n"
        "2. HOOK: sentence 1 must stop the scroll in one breath: a concrete stake or "
        "contradiction anchored to what stage 0 SHOWS. Patterns: "
        "'This X sells for thousands, and it starts as Y.' / "
        "'Nobody believes this X is made from Y.' / 'One mistake here ruins three weeks of "
        "work.' NEVER open with 'This ancient technique...' or 'For centuries...' - that is an "
        "instant swipe-away.\n"
        "3. PAYOFF: the LAST sentence must be assigned to the reveal stage (the finished "
        "product) when one exists, and its wording must match that shot.\n"
        "4. 6-10 sentences, 85-120 words total, plain punchy English, stages strictly "
        "non-decreasing, every stage index must exist, no dashes (use commas), no emojis, "
        "no hashtags, every sentence ends with . ! or ?"
        + (f"\nThe reveal stage is index {reveal_idx}." if reveal_idx is not None
           else "\nNo reveal stage exists: never describe the finished product's look."))
    user_p = (f"Video: {analysis.get('topic_title')}\nStages:\n{stage_lines}"
              + (f"\nUser's direction: {hint}" if hint else ""))
    if style == "story":
        # Mini-mode auto discovery: DUBBING-style voiceover over a skit/story with Asian
        # women/couples (user prompt 2026-07-24: professional voiceover writer + video
        # localization expert; direct speech, exact pacing, planned pauses).
        transcript = str(analysis.get("transcript") or "").strip()
        sys_p = (
            "You are a professional voiceover writer and video localization expert. You "
            "write the English voiceover for a viral 25-40s skit mini short (TikTok). You "
            "get the story beats of ONE source video (a skit featuring Asian women or "
            "couples) plus a timestamped translation of what the characters ACTUALLY say. "
            "The voiceover must feel 100% natural and perfectly synchronized with the "
            "visuals and the original audio (which stays audible quietly underneath).\n"
            "Return JSON: {\"title\": str, \"slug\": \"kebab-case-slug\", "
            "\"sentences\": [{\"text\": \"one sentence\", \"stage\": beat_index, "
            "\"pause_after\": seconds}, ...], "
            "\"hook_keywords\": [4-8 UPPERCASE words from the text], "
            "\"impact_word\": \"the single most gripping word of sentence 1\"}.\n"
            "HARD RULES:\n"
            "1. HONOR THE ORIGINAL DIALOGUE: never contradict or invent what characters "
            "say - use the transcript's names, pleas and punchlines. If the transcript "
            "says a name, use THAT name.\n"
            "2. DIRECT SPEECH over summary: when a character speaks, dub it as direct "
            "speech instead of narrating about it. WRONG: 'The father begs for an "
            "answer.' RIGHT: 'Please, pick me!' Mix short dubbed lines with minimal "
            "narration glue.\n"
            "3. EXACT PACING: every sentence may ONLY cover what its assigned beat "
            "literally shows RIGHT THEN - never describe actions that have not happened "
            "yet or are already over. Describe the visible STATE ('she sits in the "
            "classroom'), never assumed movement ('she rushes in' is WRONG if the beat "
            "shows her seated).\n"
            "4. PAUSES: do NOT talk wall-to-wall. Set \"pause_after\" (0 to 1.5 seconds) "
            "after sentences where a visual punchline, an original reaction or a funny "
            "original sound should breathe; use 0 elsewhere. Plan 2-4 real pauses per "
            "short.\n"
            "5. TONE: this is comedy - dynamic, playful, alive; never a dry documentary. "
            "Sentence 1 must still ORIENT the viewer (who, where, the rule/stake) in one "
            "breath, anchored to beat 0; NEVER open with 'Watch what happens...'.\n"
            "6. FORM: the LAST sentence lands on the punchline beat when one exists. "
            "6-10 sentences, 70-100 words, present tense, beats strictly non-decreasing, "
            "every beat index must exist, no dashes (use commas), no emojis, no hashtags, "
            "every sentence ends with . ! or ?"
            + (f"\nThe punchline beat is index {reveal_idx}." if reveal_idx is not None
               else "\nNo punchline beat exists: end on the last calm beat instead."))
        user_p = (f"Video: {analysis.get('topic_title')}\nStory beats:\n{stage_lines}"
                  + (f"\nOriginal dialogue (timestamped English translation):\n{transcript}"
                     if transcript else "\n(No usable dialogue transcript - rely on the "
                                        "beats and keep dubbing to visible reactions.)")
                  + (f"\nUser's direction: {hint}" if hint else ""))
    data = agent_core._post_llm_json(reasoning_model, [
        {"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
        max_tokens=1200, temperature=0.6 if style == "story" else 0.5)
    sentences = []
    for s in (data.get("sentences") or []):
        txt = re.sub(r"\s+", " ", str(s.get("text") or "")).strip()
        try:
            idx = int(s.get("stage"))
        except (TypeError, ValueError):
            idx = 0
        if txt:
            if not re.search(r"[.!?]$", txt):
                txt += "."
            try:
                _pa = max(0.0, min(1.5, float(s.get("pause_after") or 0.0)))
            except (TypeError, ValueError):
                _pa = 0.0
            sentences.append({"text": txt, "stage": max(0, min(idx, len(stages) - 1)),
                              "pause_after": _pa})
    if len(sentences) < 4:
        raise RuntimeError("Discovery: the script writer returned too few sentences.")
    # stages must be non-decreasing so the recut plays forward through the source
    for i in range(1, len(sentences)):
        if sentences[i]["stage"] < sentences[i - 1]["stage"]:
            sentences[i]["stage"] = sentences[i - 1]["stage"]
    # PAYOFF GUARANTEE: the last sentence always plays over the reveal stage when one exists
    # (user: "am schluss von green emerald geredet und es kam nichts davon als visual").
    if reveal_idx is not None and sentences:
        sentences[-1]["stage"] = max(reveal_idx, sentences[-1]["stage"]) \
            if reveal_idx >= sentences[-1]["stage"] else reveal_idx
    data["sentences"] = sentences
    data["script"] = " ".join(s["text"] for s in sentences)
    log(status_cb, f"Discovery script ({len(sentences)} sentences): {data.get('title')}")
    return data


def _transcribe_source(src, status_cb=None):
    """Timestamped transcript of the ORIGINAL source audio (user prompt 2026-07-24:
    the writer must know what the characters ACTUALLY say - names, pleas, punchlines -
    so the dub never invents dialogue). Auto language (Chinese/Japanese/Korean), local
    faster-whisper; returns lines like "[0:18] ..." or "" when nothing usable."""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segs, info = model.transcribe(str(src), task="translate", vad_filter=True)
        # Whisper hallucinates these on music/ambient-only passages - never real dialogue.
        _hallu = ("thank you for watching", "thanks for watching", "please subscribe",
                  "see you next time", "subscribe to", "like and subscribe")
        lines = []
        for seg in segs:
            txt = str(seg.text or "").strip()
            if not txt or any(h in txt.lower() for h in _hallu):
                continue
            m, sec = divmod(int(seg.start), 60)
            lines.append(f"[{m}:{sec:02d}] {txt}")
            if len(lines) >= 60:
                break
        out = "\n".join(lines)
        log(status_cb, f"Discovery: transcribed the source dialogue "
                       f"({len(lines)} lines, lang={getattr(info, 'language', '?')}).")
        return out
    except Exception as exc:  # noqa: BLE001 - transcript is a quality boost, not a gate
        log(status_cb, f"Discovery: source transcription skipped ({exc}).")
        return ""


def _apply_voice_pauses(wav, words, sentences, status_cb=None):
    """Insert the writer's planned pauses (sentence['pause_after']) as real SILENCE into
    the one-take voiceover, shifting all later word timings. Pauses let the original
    audio bed (dual audio) and visual punchlines breathe instead of wall-to-wall talk."""
    counts = [len(s["text"].split()) for s in sentences]
    if sum(counts) != len(words):
        return wav, words
    pauses, cum = [], 0
    for i, sent in enumerate(sentences):
        cum += counts[i]
        p = 0.0
        try:
            p = float(sent.get("pause_after") or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        p = max(0.0, min(1.5, p))
        if p >= 0.15 and i < len(sentences) - 1:
            pauses.append((cum - 1, round(p, 2)))
    total_added = sum(p for _, p in pauses)
    if not pauses or total_added > 8.0:
        return wav, words
    ff = pipeline.find_ffmpeg()
    dur = _video_duration(wav)
    bounds = [round(float(words[idx]["end"]) + 0.03, 3) for idx, _ in pauses]
    fc, parts, prev = [], [], 0.0
    for k, b in enumerate(bounds):
        fc.append(f"[0:a]atrim={prev}:{b},asetpts=PTS-STARTPTS[c{k}]")
        fc.append(f"aevalsrc=0:d={pauses[k][1]}:s=48000[p{k}]")
        parts += [f"[c{k}]", f"[p{k}]"]
        prev = b
    fc.append(f"[0:a]atrim={prev}:{dur + 0.5},asetpts=PTS-STARTPTS[c{len(bounds)}]")
    parts.append(f"[c{len(bounds)}]")
    fc.append("".join(parts) + f"concat=n={len(parts)}:v=0:a=1,aresample=48000[a]")
    out = Path(wav).with_name(Path(wav).stem + "_paused.wav")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(wav),
                    "-filter_complex", ";".join(fc), "-map", "[a]",
                    "-c:a", "pcm_s16le", str(out)], check=True)
    shifted, offset, bi = [], 0.0, 0
    for wi, w in enumerate(words):
        shifted.append({**w, "start": round(float(w["start"]) + offset, 3),
                        "end": round(float(w["end"]) + offset, 3)})
        if bi < len(pauses) and wi == pauses[bi][0]:
            offset += pauses[bi][1]
            bi += 1
    log(status_cb, f"Discovery: inserted {len(pauses)} voiceover pause(s) "
                   f"(+{total_added:.1f}s) so punchlines and original audio can breathe.")
    return out, shifted


def _align_words(wav, script, status_cb=None):
    """Local faster-whisper word timings, aligned 1:1 onto the script tokens
    (hyphenated words merged). Falls back to raw whisper tokens on mismatch."""
    from faster_whisper import WhisperModel
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _info = model.transcribe(str(wav), word_timestamps=True, language="en")
    raw = []
    for seg in segs:
        for w in seg.words:
            raw.append({"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})
    tokens = script.split()

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    out, j = [], 0
    try:
        for tok in tokens:
            tgt = norm(tok)
            acc, s0, e = "", raw[j]["start"], raw[j]["end"]
            while j < len(raw):
                acc += norm(raw[j]["word"])
                e = raw[j]["end"]
                j += 1
                if acc == tgt or len(acc) >= len(tgt):
                    break
            out.append({"word": tok, "start": s0, "end": e})
        if j != len(raw):
            raise ValueError("leftover whisper tokens")
        log(status_cb, f"Discovery: aligned {len(out)} words to the voiceover.")
        return out
    except (IndexError, ValueError):
        log(status_cb, "Discovery: script/whisper mismatch - using raw whisper words for captions.")
        return raw


def _auto_phrases(counts):
    sizes = []
    for c in counts:
        while c > 4:
            sizes.append(3)
            c -= 3
        if c == 4:
            sizes += [2, 2]
        elif c > 0:
            sizes.append(c)
    return sizes


def run_discovery_short(form, status_cb=None, style="process"):
    """Entry point for a Clip Short run WITHOUT a script. style="process" = classic
    Discovery (craft/process docs); style="story" = the MINI mode's automatic discovery
    (user 2026-07-23): skits/stories with Japanese/Korean/Chinese women or couples,
    activated when the mini script AND topic are empty (never a selectable option)."""
    cancel_event = form.get("_cancel_event")

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Run cancelled by user.")

    # An empty account 403s every LLM call, and without this the run still searched,
    # downloaded 21 videos and screened for 20 minutes before failing with "no candidate
    # survived the vision review" (2026-07-25). Two seconds here saves all of it.
    agent_core.assert_wavespeed_balance(status_cb=status_cb)

    hint = str(form.get("gen_topic") or "").strip()
    region = str(form.get("region") or "").strip().lower()
    reasoning_model = str(form.get("reasoning_model") or "") or "google/gemini-3.5-flash"
    log(status_cb, ("MINI DISCOVERY: empty script - hunting a story/skit TikTok with Asian "
                    "women or couples " if style == "story" else
                    "DISCOVERY MODE: no script given - the agent hunts a long process TikTok ")
                   + (f"about '{hint}'." if hint else "on its own."))

    pinned_url = str(form.get("candidate_url") or "").strip()
    if pinned_url:
        # CANDIDATE LIBRARY start: the user already chose this exact video - no search,
        # no 5-candidate gate, straight to analysis/build.
        entry = next((r for r in load_candidate_library() if r.get("url") == pinned_url), None)
        m_url = re.match(r"https?://www\.tiktok\.com/@([^/]+)/video/(\d+)", pinned_url)
        candidates = [{
            "id": str((entry or {}).get("id") or (m_url.group(2) if m_url else "pinned")),
            "author": str((entry or {}).get("author") or (m_url.group(1) if m_url else "")),
            "likes": int((entry or {}).get("likes") or 0),
            "dur": int((entry or {}).get("dur") or 0),
            "desc": str((entry or {}).get("desc") or ""), "query": "library",
            "url": pinned_url,
            "local_file": str((entry or {}).get("file") or ""),
        }]
        log(status_cb, f"Discovery: using the saved library candidate "
                       f"@{candidates[0]['author']} - no search needed.")
    else:
        queries = _plan_queries(hint, reasoning_model, status_cb, style=style, region=region)
        _check_cancel()
        # story mode searches WIDE (deeper scroll, max 2 clips per creator) and then
        # text-prescreens the whole pool - special beats popular (user 2026-07-25).
        candidates = _search_long(queries, status_cb,
                                  min_seconds=40 if style == "story" else None,
                                  want=25 if style == "story" else 12,
                                  per_author=2 if style == "story" else 99)
        if style == "story":
            _check_cancel()
            candidates = _prescreen_story(candidates, reasoning_model, status_cb)
    if not candidates:
        raise RuntimeError("Discovery: no long TikTok candidates found - try a different topic.")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = agent_core.PROJECTS_DIR / f"_discovery_tmp_{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    # Screen a WIDE pool and rank it; the user then picks ONE of the best five.
    accepted = []
    _tried_cap = 1 if pinned_url else (MAX_CANDIDATES_TRIED_STORY if style == "story"
                                       else MAX_CANDIDATES_TRIED)
    _want_accepted = 1 if pinned_url else (10 if style == "story" else 5)
    for cand in candidates[:_tried_cap]:
        if len(accepted) >= _want_accepted:
            break
        _check_cancel()
        log(status_cb, f"Discovery: trying @{cand['author']} ({cand['dur']}s, "
                       f"{cand['likes']:,} likes) - {cand['desc'][:80]}")
        _lf = Path(str(cand.get("local_file") or ""))
        if cand.get("local_file") and _lf.exists():
            # pinned library candidate whose video already lives in a project folder
            import shutil as _sh
            path = work / f"cand_{cand['id']}.mp4"
            _sh.copy2(_lf, path)
            log(status_cb, "Discovery: reusing the saved candidate video (no download).")
        else:
            path = clip_scraper.download_full(cand["url"], work, name=f"cand_{cand['id']}",
                                              status_cb=status_cb)
        if not path or not _is_portrait(path):
            log(status_cb, "Discovery: skipped (download failed or not portrait 9:16).")
            continue
        tech = {}
        if style == "story" and not pinned_url:
            # objective gate BEFORE paying for a vision call: black bars = unusable as a
            # 9:16 short, no scene changes = a talking head with no visual story.
            tech = _probe_technical(path, status_cb)
            if tech.get("letterbox", 0) > 0.22:
                log(status_cb, f"Discovery: skipped @{cand['author']} - "
                               f"{tech['letterbox']*100:.0f}% letterbox/black bars.")
                continue
            # Calibrated on the real 2026-07-25 pool: a one-camera prank with strong
            # reactions measures ~2 cuts/min and is still good, so cuts are a RANKING
            # signal, not a gate. Only a literally single-shot video is rejected here.
            if tech.get("cuts_per_min", 0) < 1.0:
                log(status_cb, f"Discovery: skipped @{cand['author']} - single static shot "
                               "(no scene change in the first minute).")
                continue
        sheet, total = _frame_sheet(path, work, status_cb, tag=cand["id"])
        info = _vision_stages(sheet, total, cand, reasoning_model, status_cb, style=style)
        _min_appeal = 0 if pinned_url else (7 if style == "story" else 6)
        if (not pinned_url and not info.get("is_process"))                 or float(info.get("appeal") or 0) < _min_appeal                 or len(info["stages"]) < 3:
            log(status_cb, "Discovery: rejected by vision review - next candidate.")
            continue
        accepted.append({"cand": cand, "src": path, "info": info, "sheet": sheet,
                         "tech": tech})
        log(status_cb, f"Discovery: candidate {len(accepted)}/{_want_accepted} accepted - "
                       f"{info.get('topic_title')}"
                       + (f" [{tech.get('cuts_per_min')} cuts/min]" if tech else ""))
    if not accepted:
        raise RuntimeError("Discovery: no candidate survived the vision review - "
                           "rerun or give a topic hint.")
    # FINAL RANKING (user 2026-07-25): vision appeal alone was flat (everything scored 8),
    # so combine it with the text "special" score and the measured scene density, then keep
    # the best FIVE with a category spread so the picks are not five of the same thing.
    def _rank(a):
        appeal = float(a["info"].get("appeal") or 0)
        special = float(a["cand"].get("special") or 0)
        cuts = float((a.get("tech") or {}).get("cuts_per_min") or 0)
        return appeal + special + min(cuts, 25.0) / 5.0
    accepted.sort(key=_rank, reverse=True)
    if not pinned_url and style == "story" and len(accepted) > 5:
        picked, per_cat = [], {}
        for a in accepted:
            cat = str(a["cand"].get("category") or "other")
            if per_cat.get(cat, 0) >= 2:
                continue
            per_cat[cat] = per_cat.get(cat, 0) + 1
            picked.append(a)
            if len(picked) >= 5:
                break
        for a in accepted:               # top up if the spread left gaps
            if len(picked) >= 5:
                break
            if a not in picked:
                picked.append(a)
        accepted = picked
        log(status_cb, "Discovery: shortlist = " + ", ".join(
            f"{a['info'].get('topic_title')} (rank {_rank(a):.1f})" for a in accepted))
    _ffprev = pipeline.find_ffmpeg()

    def _mk_prev(a):
        try:
            dst = work / f"prev_{a['cand']['id']}.mp4"
            a["preview"] = str(_browser_preview(a["src"], dst, _ffprev))
        except Exception:  # noqa: BLE001
            a["preview"] = ""
    from concurrent.futures import ThreadPoolExecutor
    log(status_cb, f"Discovery: building {len(accepted)} browser previews (H.264)...")
    with ThreadPoolExecutor(max_workers=3) as _ex:
        list(_ex.map(_mk_prev, accepted))
    _record_candidates(accepted, style)   # persistent candidate library (every pick ever shown)

    # USER APPROVAL GATE: the user must PICK ONE of the found candidates on the run page
    # (frame sheets + stages shown) before any script/TTS money is spent.
    sel = 0
    gate = form.get("_discovery_gate")
    if callable(gate) and not pinned_url:
        decision = gate({"candidates": [{
            "index": i,
            "title": str(a["info"].get("topic_title") or ""),
            "author": a["cand"]["author"], "likes": a["cand"]["likes"],
            "dur": a["cand"]["dur"], "url": a["cand"]["url"],
            "appeal": a["info"].get("appeal"), "sheet": str(a["sheet"]),
            "video": str(a.get("preview") or ""),
            "premise": str(a["info"].get("premise") or ""),
            "stages": [f"{s['start']:.0f}-{s['end']:.0f}s: {s['action']}"
                       for s in a["info"]["stages"]],
        } for i, a in enumerate(accepted)]})
        m = re.match(r"pick:(\d+)", str(decision or ""))
        sel = int(m.group(1)) if m else 0
        sel = max(0, min(sel, len(accepted) - 1))
        log(status_cb, f"Discovery: user picked candidate {sel + 1} - "
                       f"{accepted[sel]['info'].get('topic_title')}")
    chosen, src, analysis = accepted[sel]["cand"], accepted[sel]["src"], accepted[sel]["info"]

    if style == "story":
        # the writer dubs the ACTUAL dialogue - transcribe the chosen source first
        analysis["transcript"] = _transcribe_source(src, status_cb)
    plan = _write_script(analysis, hint, reasoning_model, status_cb, style=style)
    script = plan["script"]
    slug = re.sub(r"[^a-z0-9_]+", "_", str(plan.get("slug") or plan.get("title") or "discovery")
                  .lower()).strip("_")[:48] or "discovery"
    slug = f"{slug}_{stamp}"
    project_dir = agent_core.PROJECTS_DIR / slug
    (project_dir / "input").mkdir(parents=True, exist_ok=True)
    log(status_cb, f"PROJECT_DIR|{project_dir}")
    _mark_candidate_picked(chosen.get("id"), slug)
    clip_dir = project_dir / "seedance 2.0"
    clip_dir.mkdir(exist_ok=True)
    (project_dir / "input" / "script.txt").write_text(script, encoding="utf-8")
    src_final = clip_dir / "_discovery_source.mp4"
    Path(src).replace(src_final)
    # TikTok sources are HEVC - unplayable (black) in the timeline editor's player.
    # Re-encode the project source to full-quality H.264 once; all cutting and the
    # editor work from this file afterwards.
    try:
        ffp0 = pipeline.find_ffprobe(pipeline.find_ffmpeg())
        _c = subprocess.run([ffp0, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name", "-of", "csv=p=0", str(src_final)],
                            capture_output=True, text=True)
        if (_c.stdout or "").strip().lower() != "h264":
            _tmp = src_final.with_name("_discovery_source_h264.mp4")
            subprocess.run([pipeline.find_ffmpeg(), "-y", "-loglevel", "error",
                            "-i", str(src_final), "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                            "-movflags", "+faststart", str(_tmp)], check=True)
            _tmp.replace(src_final)
            log(status_cb, "Discovery: source re-encoded to H.264 (playable in the editor).")
    except Exception as exc:  # noqa: BLE001
        log(status_cb, f"Discovery: H.264 source conversion skipped ({exc}).")
    (project_dir / "input" / "discovery_report.json").write_text(json.dumps({
        "source": chosen, "analysis": analysis, "plan": plan}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    # Keep ALL initial downloads + where they came from (user rule 2026-07-23: "die
    # initial downloads, also woher die clips kommen, sollen gesaved bleiben").
    dl_dir = project_dir / "initial downloads"
    dl_dir.mkdir(exist_ok=True)
    sources = []
    for j, a in enumerate(accepted):
        entry = {k: a["cand"].get(k) for k in ("id", "url", "author", "likes", "dur", "desc")}
        entry["title"] = str(a["info"].get("topic_title") or "")
        entry["chosen"] = (j == sel)
        if j == sel:
            entry["file"] = "seedance 2.0/_discovery_source.mp4"
            _update_candidate_file(a["cand"].get("id"), str(src_final))
        else:
            p = Path(a["src"])
            if p.exists():
                safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(entry.get("author") or "cand"))[:40]
                dest = dl_dir / f"cand_{j}_{safe}.mp4"
                try:
                    p.replace(dest)
                    entry["file"] = f"initial downloads/{dest.name}"
                    _update_candidate_file(a["cand"].get("id"), str(dest))
                except OSError:
                    entry["file"] = str(p)
        sources.append(entry)
    (dl_dir / "sources.json").write_text(
        json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- voiceover + word timings (same recipe as the hand-build factory)
    _check_cancel()
    wav = agent_core.generate_project_voiceover(script, project_dir, form, status_cb=status_cb)
    if not wav:
        raise RuntimeError("Discovery: voiceover generation failed (TTS disabled or errored).")
    words = _align_words(wav, script, status_cb)
    if style == "story":
        wav, words = _apply_voice_pauses(wav, words, plan["sentences"], status_cb)
    (project_dir / "input" / "word_timings.json").write_text(json.dumps(words), encoding="utf-8")
    total_vo = _video_duration(wav)

    # ---- sentence spans over the voiceover (gapless; each ends where the next starts)
    sentences = plan["sentences"]
    counts = [len(s["text"].split()) for s in sentences]
    aligned = sum(counts) == len(words)
    bounds, wi = [], 0
    if aligned:
        for c in counts:
            bounds.append((float(words[wi]["start"]), float(words[wi + c - 1]["end"])))
            wi += c
    else:  # fallback: split the voiceover evenly across sentences
        step = total_vo / len(sentences)
        bounds = [(i * step, (i + 1) * step) for i in range(len(sentences))]
    spans = [(bounds[i][0] if i else 0.0,
              bounds[i + 1][0] if i + 1 < len(bounds) else total_vo)
             for i in range(len(bounds))]

    # ---- caption track (word timings RELATIVE to the row start - render re-anchors them).
    # Built from whatever word list we have: script-aligned tokens normally, raw whisper
    # tokens on a mismatch - the short must never render caption-less.
    caption_track = []
    # word-by-word rule (user 2026-07-23): 4+ char words alone, short words grouped
    sizes = pipeline.caption_chunk_sizes([w["word"] for w in words])
    wi = 0
    for n in sizes:
        ch = words[wi:wi + n]
        wi += n
        if not ch:
            continue
        s0 = float(ch[0]["start"])
        caption_track.append({
            "start": round(s0, 3), "end": round(float(ch[-1]["end"]) + 0.04, 3),
            "text": " ".join(w["word"] for w in ch),
            "word_timings": [{"word": w["word"], "start": round(float(w["start"]) - s0, 3),
                              "end": round(float(w["end"]) - s0, 3)} for w in ch]})

    # ---- recut the long source: each sentence gets footage from ITS process stage
    ff = pipeline.find_ffmpeg()
    stages = analysis["stages"]
    stage_cursor = {}
    scenes = []
    # story mode keeps the source's own audio as a quiet bed under the voiceover
    ffp = pipeline.find_ffprobe(ff)
    _probe = subprocess.run([ffp, "-v", "error", "-select_streams", "a", "-show_entries",
                             "stream=codec_type", "-of", "csv=p=0", str(src_final)],
                            capture_output=True, text=True)
    _src_has_audio = style == "story" and "audio" in (_probe.stdout or "")
    for i, sent in enumerate(sentences):
        _check_cancel()
        a, b = spans[i]
        d = round(b - a, 2)
        st = stages[sent["stage"]]
        st_start, st_end = float(st["start"]), float(st["end"])
        stage_len = max(0.5, st_end - st_start)
        t0 = stage_cursor.get(sent["stage"], st_start)
        t0 = min(max(st_start, t0), max(st_start, st_end - 0.5))
        # If the remaining stage footage is much shorter than the sentence, restart at
        # the stage start (a little repetition beats freeze frames or wrong footage).
        if st_end - t0 < min(d * 0.5, stage_len):
            t0 = st_start
        avail = max(0.5, st_end - t0)
        # The cut must NEVER run past its stage (user: the SAW showed while the voice
        # was still on "boiled"). Sentence longer than the stage footage -> take what
        # the stage has and SLOW it to cover the sentence; freeze-pad only as a last resort.
        if avail >= d - 0.01:
            take, rate = d, 1.0
        else:
            rate = max(0.5, round(avail / d, 3))
            take = avail
        name = f"disc_{i:02d}.mp4"
        # RETENTION EDIT for story mode (user 2026-07-23, reference-video analysis):
        # a 4-7s sentence over ONE static cut feels slow - split it into ~2s JUMP CUTS
        # that leap forward through the beat (skipping dead air between sub-cuts), and
        # keep the ORIGINAL AUDIO in the clip so the mixer can lay it quietly under the
        # voiceover (dual audio). Falls back to the single slowed cut when the beat is
        # too short to jump around in.
        did_subcuts = False
        if style == "story" and d >= 2.4 and avail >= d + 1.0:
            n_sub = max(2, min(4, int(round(d / 2.2))))
            base = d / n_sub
            gap = min(1.2, max(0.0, (avail - d) / max(1, n_sub - 1)))
            subs, t = [], t0
            for k in range(n_sub):
                sd = round(base, 2) if k < n_sub - 1 else round(d - base * (n_sub - 1), 2)
                subs.append((round(t, 2), max(0.4, sd)))
                t += sd + gap
            stage_cursor[sent["stage"]] = min(t, st_end)
            cmd = [ff, "-y", "-loglevel", "error"]
            for (sa, sd) in subs:
                cmd += ["-ss", str(sa), "-t", str(round(sd + 0.05, 2)), "-i", str(src_final)]
            vparts, aparts, fc = [], [], []
            for k in range(n_sub):
                fc.append(f"[{k}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                          f"crop=1080:1920,fps=30,setpts=PTS-STARTPTS[v{k}]")
                if _src_has_audio:
                    fc.append(f"[{k}:a]aresample=48000,asetpts=PTS-STARTPTS[a{k}]")
                vparts.append(f"[v{k}]")
                aparts.append(f"[a{k}]")
            if _src_has_audio:
                fc.append("".join(v + a for v, a in zip(vparts, aparts))
                          + f"concat=n={n_sub}:v=1:a=1[v][a]")
            else:
                fc.append("".join(vparts) + f"concat=n={n_sub}:v=1:a=0[v]")
            cmd += ["-filter_complex", ";".join(fc), "-map", "[v]"]
            if _src_has_audio:
                cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "128k"]
            cmd += ["-t", str(d + 0.05), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "19", str(clip_dir / name)]
            try:
                subprocess.run(cmd, check=True)
                did_subcuts = True
                log(status_cb, f"Discovery: clip {i} retention-cut into {n_sub} jump cuts.")
            except subprocess.CalledProcessError:
                log(status_cb, f"Discovery: jump-cut build failed for clip {i} - "
                               "falling back to a single cut.")
        if not did_subcuts:
            stage_cursor[sent["stage"]] = t0 + take
            vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
            if rate < 0.999:
                vf += f",setpts=PTS/{rate}"
            vf += ",fps=30"
            out_len = take / rate
            if out_len < d - 0.05:
                vf += f",tpad=stop_mode=clone:stop_duration={d - out_len + 0.2:.2f}"
            cmd = [ff, "-y", "-loglevel", "error", "-ss", str(round(t0, 2)),
                   "-t", str(round(take + 0.1, 2)), "-i", str(src_final),
                   "-vf", vf, "-t", str(d + 0.05)]
            if style == "story" and _src_has_audio and rate >= 0.999:
                cmd += ["-c:a", "aac", "-b:a", "128k"]   # dual audio: keep the original bed
            else:
                cmd += ["-an"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                    str(clip_dir / name)]
            subprocess.run(cmd, check=True)
            if rate < 0.999:
                log(status_cb, f"Discovery: stage {sent['stage']} shorter than its sentence - "
                               f"clip {i} slowed to {rate:.2f}x to stay inside the stage.")
        scenes.append({"id": f"{i+1:02d}", "name": f"Stage {sent['stage']}",
                       "script": sent["text"], "exact_voice_text": sent["text"],
                       "start": round(a, 2), "end": round(b, 2),
                       "clip": name, "asset": name, "seedance": True,
                       "seedance_start_trim": 0.0, "render_caption": False,
                       "overlays": [], "blur_captions": False, "timeline_speed": 1.0})
    scenes[-1]["end"] = round(total_vo, 2)
    log(status_cb, f"Discovery: recut the source into {len(scenes)} stage-matched segments.")
    _check_cancel()
    _verify_recut(scenes, sentences, stages, src_final, clip_dir, work,
                  reasoning_model, status_cb)

    # ---- config + local SFX pass (hook riser -> impact word) + render
    config = {
        "project_slug": slug, "title": str(plan.get("title") or analysis.get("topic_title") or slug),
        # scrape marker is CRITICAL: without it pipeline.build_sfx_segments runs the old
        # keyword planner ON TOP of ai_content_sfx -> doubled transitions + random foley.
        "clip_source": "scrape",
        "resolution": [1080, 1920], "fps": 30, "duration": round(total_vo, 2),
        "scenes": scenes, "use_seedance_clips": True,
        "audio_path": str(wav), "speech_audio_in_final": True,
        "animated_captions": True, "render_captions": bool(caption_track),
        "timeline_editor_render": True, "timeline_caption_track": caption_track,
        "pipeline_version": "v0.2",
        "hook_keywords": [str(k) for k in (plan.get("hook_keywords") or [])][:10],
        "caption_max_words": 3, "caption_uppercase": True,
        "canonical_words": words, "impact_word": str(plan.get("impact_word") or ""),
        "sfx_enabled": True, "render_sfx_enabled": True, "custom_sfx": [],
        "hook_riser_file": "hook_riser3", "hook_riser_full_hook": True,
        "background_music_enabled": False,
        # DUAL AUDIO (story style, reference-video analysis): the cut clips keep the
        # source's own sound and the mixer lays it quietly under the voiceover.
        "mix_seedance_audio_with_speech": _src_has_audio,
        "seedance_audio_volume_with_speech": 0.12,
        "smart_overlays": [], "timeline_overlays_managed": True,
        "output_basename": f"{slug}_v1",
    }
    agent_core.apply_caption_style_from_form(config, form)
    (project_dir / "config").mkdir(exist_ok=True)
    n_sfx = agent_core.place_editor_sfx(config, reasoning_model=reasoning_model,
                                        status_cb=status_cb)
    # User rule 2026-07-23: discovery SFX = ONLY transition sounds (transition library:
    # swipe/bright whoosh; combos swapped for a clean whoosh first) + the hook riser
    # (pinned to hook_riser3 via config). Everything else (reactions, impacts, dings,
    # pops, body risers...) is dropped completely.
    events = list(config.get("ai_content_sfx") or [])
    plain = [e for e in events if e.get("category") in ("swipe_whoosh", "bright_whoosh")]
    for i, e in enumerate(events):
        if e.get("category") == "whoosh_hit_combo" and plain:
            donor = plain[i % len(plain)]
            e["path"] = donor["path"]
            e["category"] = donor["category"]
            e["duration"] = donor["duration"]
            e["source_trim"] = donor.get("source_trim", 0)
            e["playback_rate"] = donor.get("playback_rate", 1)
    _KEEP = {"swipe_whoosh", "bright_whoosh", "hook_riser"}
    events = [e for e in events if str(e.get("category") or "") in _KEEP]
    config["ai_content_sfx"] = events
    log(status_cb, f"Discovery: placed {len(config['ai_content_sfx'])} local SFX "
                   "(hook riser #3 + transition whooshes only).")
    (project_dir / "config" / "project.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    _check_cancel()
    output = pipeline.render_video(config)
    log(status_cb, f"Discovery render complete: {Path(output).name}")
    try:  # tidy the temp download dir (chosen source was moved into the project)
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return {"title": config["title"], "project_dir": str(project_dir), "video": str(output)}


# ====================================================================== MINI TOPIC MODE
# Mini Story WITHOUT a script (user rule 2026-07-22): the user gives ONE topic (e.g.
# "old noodle vending machine"). The agent scrapes real portrait footage of that ONE
# subject FIRST, then writes a <=130-token mini script that only tells what the found
# material can actually show ("der agent soll ein script machen wos auch genug material
# hat"), and builds the short from that pool. Same local finishing as discovery.

def _mini_queries(topic, form, reasoning_model, status_cb=None):
    langs = [c.strip() for c in str(form.get("search_languages") or "").split(",") if c.strip()]
    lang_hint = (" Search languages to mix: " + ", ".join(langs) + ".") if langs else \
        " Mix English with Japanese/Chinese when the topic is Asian."
    data = agent_core._post_llm_json(reasoning_model, [
        {"role": "system", "content":
            "You plan TikTok searches for a 20-25s mini documentary about ONE concrete subject. "
            "The goal is a COHERENT footage cluster: many short clips of the SAME kind of thing "
            "(one machine, one place, one dish), not a broad topic collage. "
            "Return JSON: {\"subject\": \"the one concrete subject\", "
            "\"queries\": [6 search strings]}. No hashtags." + lang_hint},
        {"role": "user", "content": f"Topic: {topic}"}],
        max_tokens=500, temperature=0.5)
    queries = [str(q).strip() for q in (data.get("queries") or []) if str(q).strip()][:6]
    if not queries:
        raise RuntimeError("Mini topic: query planner returned nothing.")
    subject = str(data.get("subject") or topic)
    log(status_cb, f"Mini topic '{subject}': " + " | ".join(queries))
    return subject, queries


def _mini_pool_sheet(clips, work, status_cb=None):
    """One labeled contact sheet (3 frames per clip) for the single vision pass."""
    from PIL import Image, ImageDraw
    ff = pipeline.find_ffmpeg()
    W, H, LBL, PER = 200, 356, 26, 3
    canvas = Image.new("RGB", (W * PER, (H + LBL) * len(clips)), (8, 8, 8))
    dr = ImageDraw.Draw(canvas)
    y = 0
    for idx, p in enumerate(clips):
        d = _video_duration(p)
        dr.text((6, y + 6), f"[{idx}] {p.name} ({d:.1f}s)", fill=(120, 255, 150))
        y += LBL
        for k in range(PER):
            t = d * (0.12 + 0.76 * k / (PER - 1))
            fp = Path(work) / f"_mp_{idx}_{k}.jpg"
            subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(t), "-i", str(p),
                            "-frames:v", "1", "-vf", f"scale={W}:{H}", str(fp)], check=True)
            canvas.paste(Image.open(fp), (k * W, y))
        y += H
    sheet = Path(work) / "mini_pool_sheet.jpg"
    canvas.save(sheet, quality=85)
    log(status_cb, f"Mini topic: built pool sheet of {len(clips)} clips.")
    return sheet


def _mini_script(subject, descs, reasoning_model, status_cb=None):
    clip_lines = "\n".join(f"[{i}] {d}" for i, d in descs.items())
    data = agent_core._post_llm_json(reasoning_model, [
        {"role": "system", "content":
            "You write 20-25s viral mini-documentary narrations (max 130 tokens TOTAL) about ONE "
            "real subject, built strictly from an available footage pool.\n"
            "Return JSON: {\"title\": str, \"slug\": \"kebab-case\", "
            "\"sentences\": [{\"text\": str, \"clips\": [clip indices that SHOW this line]}], "
            "\"hook_keywords\": [4-8 UPPERCASE words], \"impact_word\": \"one word of sentence 1\"}.\n"
            "HARD RULES:\n"
            "1. VOICE = VIDEO: a sentence may only say what its assigned clips visibly show.\n"
            "2. HOOK: one breath, concrete stake/contradiction anchored to the strongest clip "
            "(patterns: a machine that should not exist, a price that makes no sense). NEVER "
            "open with generic lines like this-ancient or in-Japan.\n"
            "3. 5-8 sentences, <=130 tokens total, one coherent story about the ONE subject, "
            "clear payoff at the end. Only use clips marked usable. No dashes (commas instead), "
            "every sentence ends with . ! or ?"},
        {"role": "user", "content": f"Subject: {subject}\nFootage pool:\n{clip_lines}"}],
        max_tokens=900, temperature=0.5)
    sentences = []
    for s in (data.get("sentences") or []):
        txt = re.sub(r"\s+", " ", str(s.get("text") or "")).strip()
        clips = []
        for c in (s.get("clips") or []):
            try:
                clips.append(int(c))
            except (TypeError, ValueError):
                continue
        if txt:
            if not re.search(r"[.!?]$", txt):
                txt += "."
            sentences.append({"text": txt, "clips": clips})
    if len(sentences) < 4:
        raise RuntimeError("Mini topic: script writer returned too few sentences.")
    data["sentences"] = sentences
    data["script"] = " ".join(s["text"] for s in sentences)
    log(status_cb, f"Mini topic script ({len(sentences)} sentences): {data.get('title')}")
    return data


def run_mini_topic_short(form, status_cb=None):
    """Mini Story from a TOPIC only: material-first scripting over one subject cluster."""
    cancel_event = form.get("_cancel_event")

    def _check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise pipeline.PipelineCancelled("Run cancelled by user.")

    topic = str(form.get("gen_topic") or "").strip()
    if not topic:
        raise RuntimeError("Mini Story without a script needs a topic.")
    reasoning_model = str(form.get("reasoning_model") or "") or "google/gemini-3.5-flash"
    log(status_cb, f"MINI TOPIC MODE: no script - scraping material for '{topic}' first.")

    subject, queries = _mini_queries(topic, form, reasoning_model, status_cb)
    _check_cancel()
    import tiktok_login
    import clip_scraper
    stamp = time.strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9_]+", "_", subject.lower()).strip("_")[:40] or "mini_topic"
    slug = f"{slug}_{stamp}"
    project_dir = agent_core.PROJECTS_DIR / slug
    clip_dir = project_dir / "seedance 2.0"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "input").mkdir(exist_ok=True)
    log(status_cb, f"PROJECT_DIR|{project_dir}")
    _mark_candidate_picked(chosen.get("id"), slug)

    seen, picked = set(), []
    for q in queries:
        try:
            items = tiktok_login.search_sync(q, want=10, status_cb=status_cb,
                                             sort="MOST_LIKED", timeout_s=240)
        except Exception as exc:  # noqa: BLE001
            log(status_cb, f"Mini topic search '{q}' failed: {exc.__class__.__name__}")
            continue
        for it in items or []:
            vid = str(it.get("id") or "")
            author = it.get("author")
            author = str((author.get("uniqueId") if isinstance(author, dict) else author) or "")
            if not vid or vid in seen or not author:
                continue
            seen.add(vid)
            likes = int((it.get("stats") or {}).get("diggCount") or 0)
            picked.append({"id": vid, "author": author, "likes": likes})
    picked.sort(key=lambda x: -x["likes"])
    log(status_cb, f"Mini topic: {len(picked)} unique results, downloading top 16...")
    pool = []
    for i, it in enumerate(picked[:16]):
        _check_cancel()
        url = f"https://www.tiktok.com/@{it['author']}/video/{it['id']}"
        p = clip_scraper.download_raw(url, clip_dir, i, status_cb=status_cb)
        if p and _is_portrait(p):
            pool.append(Path(p))
        elif p:
            Path(p).unlink(missing_ok=True)
    if len(pool) < 5:
        raise RuntimeError(f"Mini topic: only {len(pool)} portrait clips found - not enough "
                           "material for this topic.")
    log(status_cb, f"Mini topic: {len(pool)} portrait clips in the pool.")

    import scrape_v2
    sheet = _mini_pool_sheet(pool, project_dir / "input", status_cb)
    vdata = scrape_v2._vision_json(
        "You see a labeled contact sheet: each row = one TikTok clip ([index] name (dur), 3 "
        f"frames). The mini short is about: {subject}.\n"
        "Return JSON: {\"clips\": {\"0\": {\"desc\": \"what is LITERALLY visible\", "
        "\"usable\": true/false}, ...}, \"enough_material\": true/false}.\n"
        "usable=false for: static images, text/meme cards, anime/cartoons, talking heads, "
        "off-subject clips. desc must only name visible things - the narration is written "
        "from it.", str(sheet), max_tokens=2500, temperature=0.1,
        reasoning_model=reasoning_model)
    cinfo = vdata.get("clips") or {}
    descs = {}
    for k, v in cinfo.items():
        if str(k).isdigit() and isinstance(v, dict) and v.get("usable"):
            descs[int(k)] = str(v.get("desc") or "")
    if len(descs) < 4 or not vdata.get("enough_material", True):
        raise RuntimeError("Mini topic: not enough usable on-subject material - try another topic.")
    log(status_cb, f"Mini topic: {len(descs)}/{len(pool)} clips usable.")

    plan = _mini_script(subject, descs, reasoning_model, status_cb)
    script = plan["script"]
    (project_dir / "input" / "script.txt").write_text(script, encoding="utf-8")
    (project_dir / "input" / "mini_topic_report.json").write_text(json.dumps({
        "topic": topic, "subject": subject, "queries": queries,
        "clips": {str(k): v for k, v in descs.items()}, "plan": plan},
        indent=2, ensure_ascii=False), encoding="utf-8")

    _check_cancel()
    wav = agent_core.generate_project_voiceover(script, project_dir, form, status_cb=status_cb)
    if not wav:
        raise RuntimeError("Mini topic: voiceover generation failed.")
    words = _align_words(wav, script, status_cb)
    total_vo = _video_duration(wav)
    sentences = plan["sentences"]
    counts = [len(s["text"].split()) for s in sentences]
    aligned = sum(counts) == len(words)
    bounds, wi = [], 0
    if aligned:
        for c in counts:
            bounds.append((float(words[wi]["start"]), float(words[wi + c - 1]["end"])))
            wi += c
    else:
        step = total_vo / len(sentences)
        bounds = [(i * step, (i + 1) * step) for i in range(len(sentences))]
    spans = [(bounds[i][0] if i else 0.0,
              bounds[i + 1][0] if i + 1 < len(bounds) else total_vo)
             for i in range(len(bounds))]
    caption_track = []
    # word-by-word rule (user 2026-07-23): 4+ char words alone, short words grouped
    sizes = pipeline.caption_chunk_sizes([w["word"] for w in words])
    wi = 0
    for n in sizes:
        ch = words[wi:wi + n]
        wi += n
        if not ch:
            continue
        s0 = float(ch[0]["start"])
        caption_track.append({"start": round(s0, 3), "end": round(float(ch[-1]["end"]) + 0.04, 3),
            "text": " ".join(w["word"] for w in ch),
            "word_timings": [{"word": w["word"], "start": round(float(w["start"]) - s0, 3),
                              "end": round(float(w["end"]) - s0, 3)} for w in ch]})

    ff = pipeline.find_ffmpeg()
    scenes = []
    last_clip = None
    rot = 0
    usable_idx = sorted(descs.keys())
    for i, sent in enumerate(sentences):
        _check_cancel()
        a, b = spans[i]
        d = round(b - a, 2)
        cands = [c for c in (sent.get("clips") or []) if c in descs] or usable_idx
        pick = next((c for c in cands if pool[c].name != last_clip), cands[0])
        src = pool[pick]
        src_d = _video_duration(src)
        t0 = min(max(0.3, 0.3 + (rot % 3) * 1.7), max(0.0, src_d - d - 0.1))
        rot += 1
        name = f"mini_{i:02d}.mp4"
        subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(round(t0, 2)),
                        "-t", str(d + 0.05), "-i", str(src),
                        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                               "crop=1080:1920,fps=30",
                        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                        str(clip_dir / name)], check=True)
        last_clip = src.name
        scenes.append({"id": f"{i+1:02d}", "name": f"Line {i+1}", "script": sent["text"],
            "exact_voice_text": sent["text"], "start": round(a, 2), "end": round(b, 2),
            "clip": name, "asset": name, "seedance": True, "seedance_start_trim": 0.0,
            "render_caption": False, "overlays": [], "blur_captions": False,
            "timeline_speed": 1.0})
    scenes[-1]["end"] = round(total_vo, 2)
    log(status_cb, f"Mini topic: cut {len(scenes)} scenes from the {subject} pool.")

    config = {
        "project_slug": slug, "title": str(plan.get("title") or subject),
        "clip_source": "scrape",
        "resolution": [1080, 1920], "fps": 30, "duration": round(total_vo, 2),
        "scenes": scenes, "use_seedance_clips": True,
        "audio_path": str(wav), "speech_audio_in_final": True,
        "animated_captions": True, "render_captions": bool(caption_track),
        "timeline_editor_render": True, "timeline_caption_track": caption_track,
        "pipeline_version": "v0.2",
        "hook_keywords": [str(k) for k in (plan.get("hook_keywords") or [])][:10],
        "caption_max_words": 3, "caption_uppercase": True,
        "canonical_words": words, "impact_word": str(plan.get("impact_word") or ""),
        "sfx_enabled": True, "render_sfx_enabled": True, "custom_sfx": [],
        "hook_riser_file": "hook_riser3", "hook_riser_full_hook": True,
        "background_music_enabled": False,
        "smart_overlays": [], "timeline_overlays_managed": True,
        "output_basename": f"{slug}_v1",
    }
    agent_core.apply_caption_style_from_form(config, form)
    (project_dir / "config").mkdir(exist_ok=True)
    agent_core.place_editor_sfx(config, reasoning_model=reasoning_model, status_cb=status_cb)
    # Same SFX whitelist as discovery (user rule 2026-07-23): ONLY transition whooshes
    # (combos swapped for a clean whoosh first) + the pinned hook riser #3.
    events = list(config.get("ai_content_sfx") or [])
    plain = [e for e in events if e.get("category") in ("swipe_whoosh", "bright_whoosh")]
    for i, e in enumerate(events):
        if e.get("category") == "whoosh_hit_combo" and plain:
            donor = plain[i % len(plain)]
            e.update({"path": donor["path"], "category": donor["category"],
                      "duration": donor["duration"],
                      "source_trim": donor.get("source_trim", 0),
                      "playback_rate": donor.get("playback_rate", 1)})
    events = [e for e in events
              if str(e.get("category") or "") in ("swipe_whoosh", "bright_whoosh", "hook_riser")]
    config["ai_content_sfx"] = events
    (project_dir / "config" / "project.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    _check_cancel()
    output = pipeline.render_video(config)
    log(status_cb, f"Mini topic render complete: {Path(output).name}")
    return {"title": config["title"], "project_dir": str(project_dir), "video": str(output)}
