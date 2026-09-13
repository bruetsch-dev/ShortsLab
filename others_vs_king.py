"""Build the no-narration ``Others doing X`` Clip Short format.

The edit is deliberately searched in two independent passes.  Ordinary attempts establish a
clear visual pattern; a separately ranked payoff source breaks that pattern at the end.  Keeping
the pools separate prevents a spectacular result from being spent in the setup, which is the
entire retention mechanism of the reference format.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import agent_core
import caption_remover
import clip_scraper
import pipeline
import scrape_v2


NORMAL_SORTS = ("RELEVANCE", "MOST_LIKED", "MOST_VIEWED")
PAYOFF_SORTS = ("MOST_LIKED", "MOST_VIEWED", "RELEVANCE")
# The audible main drop in the reference master is at 41.79s. The earlier 41.23s transient is a
# lead-in hit; aligning that weaker transient made the payoff feel roughly half a second late.
REFERENCE_DROP_SECONDS = 46.79
AUTO_ACTIONS = (
    "high jumping", "ski jumping", "cliff diving", "parkour jumping",
    "skateboard jumping", "basketball dunking", "gymnastics vaulting",
    "trampoline tricks", "wakeboard jumping", "roller skating jumps",
)


def _log(cb, message):
    if cb:
        cb(str(message))


def clean_action(value):
    value = re.sub(r"\b(?:tiktok|instagram|reels?|video|compilation)\b", " ",
                   str(value or ""), flags=re.I)
    value = re.sub(r"[^\w\- ]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()[:70]


def action_queries(action, payoff=False):
    """Search phrases describe visible behaviour, never the name of the platform."""
    action = clean_action(action)
    if re.search(r"\bhigh\s*jump", action, flags=re.I):
        if payoff:
            return ["dog extreme vertical jump", "dog jumping high catch ball",
                    "dog jumping for hanging toy", "insane vertical jump touch",
                    "professional vertical jump reach", "highest touch challenge"]
        return ["jump touch height challenge", "how high can you jump challenge",
                "vertical jump touch challenge", "jumping to touch hanging target",
                "jump and touch challenge", "highest touch challenge"]
    if payoff:
        stems = (f"insane {action}", f"best {action} ever", f"professional {action}",
                 f"extreme {action}", f"unexpected {action}", f"world record {action}",
                 f"animal {action}")
    else:
        stems = (action, f"{action} challenge", f"people {action}",
                 f"{action} attempts", f"{action} fail", f"friends {action}")
    return list(dict.fromkeys(q.strip() for q in stems if q.strip()))


def _used_actions():
    used = set()
    for cfg in agent_core.PROJECTS_DIR.glob("*/config/project.json"):
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("clip_short_format") or "") == "others_vs_king":
            used.add(clean_action(data.get("others_action")).casefold())
    return used


def choose_action(requested=""):
    requested = clean_action(requested)
    if requested:
        return requested
    used = _used_actions()
    return next((a for a in AUTO_ACTIONS if a.casefold() not in used), AUTO_ACTIONS[0])


def _source_id(item):
    return str(item.get("id") or item.get("videoId") or item.get("aweme_id") or "").strip()


def _llm_plan(requested, reasoning_model, status_cb):
    """The selected app model is the format architect; deterministic queries are only fallback."""
    requested = clean_action(requested)
    used = sorted(a for a in _used_actions() if a)
    prompt = f"""Plan an 'Others doing X -> unbelievable final performer' vertical social edit.
User action: {requested or 'choose it yourself'}
Previously used actions (do not repeat when choosing): {used[-30:]}

Choose ONE simple, highly filmable physical action. It must have plentiful ordinary attempts on
TikTok/Instagram and a realistic chance of a spectacular, funny, animal, professional or record
payoff showing the SAME action. Search strings must be organic platform searches and must not
contain the words TikTok, Instagram, reels, video or compilation.

Define one concrete VISUAL CONTRACT shared by every clip: the same visible goal, object and body
movement must be readable without captions. Do not reduce a social challenge to a broad sport.
For example, "high jump" in this format means ordinary people jumping toward a clearly visible
high target, followed by an exceptional person/animal attempting that same kind of target; it does
not mean an unrelated montage of track-and-field athletes.

Return strict JSON: {{"action":"gerund phrase","visual_contract":"one precise visible action",
"setup_label":"OTHERS ...","payoff_label":"THE KING OF ...",
"setup_queries":[6 strings],"payoff_queries":[7 strings]}}"""
    try:
        plan = agent_core._post_llm_json(
            reasoning_model,
            [{"role": "system", "content": "You are the search architect and retention editor for a visual short."},
             {"role": "user", "content": prompt}],
            max_tokens=900, temperature=.35, timeout=300) or {}
    except Exception as exc:
        _log(status_cb, f"Others mode: LLM search plan unavailable ({type(exc).__name__}); using safe fallback.")
        plan = {}
    # A user-entered action is a lock, not a suggestion. The LLM may expand how it searches,
    # but it may only choose the action when the field was intentionally left blank.
    action = requested or clean_action(plan.get("action")) or choose_action("")
    setup = [clean_action(q) for q in (plan.get("setup_queries") or [])]
    payoff = [clean_action(q) for q in (plan.get("payoff_queries") or [])]
    setup = [q for q in dict.fromkeys(setup) if q][:8] or action_queries(action)
    payoff = [q for q in dict.fromkeys(payoff) if q][:9] or action_queries(action, True)
    contract = clean_action(plan.get("visual_contract")) or action
    return {"action": action, "visual_contract": contract,
            "setup_queries": setup, "payoff_queries": payoff,
            "setup_label": str(plan.get("setup_label") or f"OTHERS {action}")[:44],
            "payoff_label": str(plan.get("payoff_label") or f"THE KING OF {action}")[:44]}


def _search_pool(action, payoff, platforms, deadline, status_cb, queries=None):
    queries = list(queries or action_queries(action, payoff=payoff))
    sorts = PAYOFF_SORTS if payoff else NORMAL_SORTS
    seen, rows = set(), []
    target = 24 if payoff else 20
    # Breadth before depth. The old loop exhausted the entire target on query #1, so six
    # supposedly different search angles produced twenty near-identical tabletop clips.
    for sort in sorts:
        for query_index, query in enumerate(queries):
            if time.monotonic() >= deadline:
                break
            added_for_query = 0
            # Give both connected platforms an opportunity instead of letting TikTok's faster
            # result page fill the whole pool before Instagram is inspected.
            for platform in platforms:
                try:
                    results = clip_scraper.backend_search(
                        query, 8, status_cb=None, sort=sort,
                        platforms=[platform], deadline=deadline) or []
                except Exception as exc:
                    _log(status_cb, f"Others mode: '{query}' on {platform} [{sort}] failed "
                                    f"({type(exc).__name__}); continuing.")
                    continue
                for item in results:
                    sid = _source_id(item)
                    if not sid or sid in seen:
                        continue
                    seen.add(sid)
                    rows.append({"id": sid, "query": query, "sort": sort, "item": item,
                                 "platform": str(item.get("platform") or platform)})
                    added_for_query += 1
                    if added_for_query >= 3 or len(rows) >= target:
                        break
                if added_for_query >= 3 or len(rows) >= target:
                    break
            if len(rows) >= target:
                break
        if len(rows) >= target:
            break
    return rows


def _llm_edit(normal_rows, payoff_rows, action, reasoning_model, status_cb):
    """Choose order and winner from frame-grounded reviews, not metadata popularity."""
    def compact(row):
        return {k: row.get(k) for k in ("id", "platform", "reason", "clarity", "wow",
                                        "surprise", "start", "end", "label")}
    prompt = f"""Edit an 'Others -> final king' Short about {action}.
Every candidate below was already inspected from real frames and the reason states what is visible.
Choose 4-6 UNIQUE setup ids that clearly repeat the same action. Order them so skill/oddness rises.
Choose exactly one UNIQUE payoff id from the payoff list. It must be the most instantly astonishing
execution, never merely popular, and it cannot appear in setup_ids.

SETUP CANDIDATES: {json.dumps([compact(r) for r in normal_rows], ensure_ascii=False)}
PAYOFF CANDIDATES: {json.dumps([compact(r) for r in payoff_rows], ensure_ascii=False)}
Return strict JSON: {{"setup_ids":["..."],"payoff_id":"...","payoff_label":"THIS DOG etc.",
"editor_reason":"one sentence"}}"""
    try:
        return agent_core._post_llm_json(
            reasoning_model,
            [{"role": "system", "content": "You are the final retention editor. Use only grounded candidate evidence."},
             {"role": "user", "content": prompt}],
            max_tokens=800, temperature=.1, timeout=300) or {}
    except Exception as exc:
        _log(status_cb, f"Others mode: LLM final edit decision unavailable ({type(exc).__name__}); using grounded scores.")
        return {}


def _download(rows, folder, limit, status_cb):
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for row in rows:
        if len(out) >= limit:
            break
        dest = folder / f"{row['platform']}_{re.sub(r'[^A-Za-z0-9]+', '_', row['id'])[:28]}.mp4"
        try:
            got = scrape_v2.download_proxy_v2(row["item"], dest, None)
        except Exception:
            got = None
        if got and dest.is_file() and dest.stat().st_size > 10_000:
            out.append({**row, "path": str(dest)})
    _log(status_cb, f"Others mode: downloaded {len(out)} candidate(s) for visual inspection.")
    return out


VISION_PROMPT = """Inspect these numbered candidate-window strips from ONE social video.
Target visible action: {action}
Role being judged: {role}
Candidate windows in tile order: {windows}

Return strict JSON:
{{"chosen_window":0,"shows_action":true,"shows_preparation":true,"shows_execution":true,
"shows_outcome":true,"full_action_in_range":true,
"continuous_footage":true,"vertical_usable":true,
"single_attempt":true,"contains_replay":false,"contains_original_edit":false,
"wow_score":0,"clarity_score":0,"surprise_score":0,"has_large_captions":false,
"has_engagement_bait":false,
"subject_label":"THIS PERSON or THIS DOG etc.","visible_evidence":"exact execution and outcome",
"reason":"short visible description"}}

Rules:
- shows_action is true only when the physical action itself is visibly performed.
- full_action_in_range requires the chosen range to visibly contain the action's execution AND
  its outcome (landing, success, failure or reaction). A person merely holding/preparing/reaching
  for an object is false. An object leaving frame without a visible result is false.
- continuous_footage is false for slideshows or a proposed range containing another hard cut.
- single_attempt is true only when the range shows one complete attempt, not several people or
  a replay/loop of the same attempt. contains_replay is true whenever any action is repeated.
- contains_original_edit is true for ANY edit already present in the uploaded source: jump cut,
  angle change, reset to an earlier composition, inserted reaction, replay, montage cut or camera
  transition. A usable range must be one uninterrupted camera take from first to last frame.
- Normal/setup clips need clarity, not greatness. Payoff clips need an immediately astonishing,
  unusually skilled, funny or unexpected execution of THE SAME action.
- Score 0-10 from visible frames only. Never infer action from caption metadata.
- chosen_window is an integer tile index from the supplied list; never invent timestamps.
- If no supplied tile contains the COMPLETE visible action, set chosen_window to -1 and all
  action booleans false. Captions claiming it happened are not evidence.
- has_engagement_bait is true for visible follower/like/comment/subscribe questions, challenges,
  giveaways or other text asking the viewer to interact. Those clips are never usable.
"""


def _action_windows(duration, cuts, role):
    """Concrete uncut windows the vision model can actually inspect and select."""
    duration = max(0.0, float(duration or 0.0))
    # The reference lets an attempt breathe: viewers see the setup, jump, result and reaction.
    # Three-second windows repeatedly cut away during the actual action.
    wanted = 9.0 if role == "payoff" else 7.0
    boundaries = [0.0] + [float(c) for c in cuts if .12 < float(c) < duration - .12] + [duration]
    windows = []
    for left, right in zip(boundaries, boundaries[1:]):
        left += .06 if left else 0.0
        right -= .06 if right < duration else 0.0
        span = right - left
        if span < 1.45:
            continue
        if span <= wanted:
            windows.append((left, right))
            continue
        step = max(1.0, wanted * .55)
        cursor = left
        while cursor + 1.45 <= right:
            end = min(right, cursor + wanted)
            windows.append((cursor, end))
            if end >= right - .05:
                break
            cursor += step
    windows = list(dict.fromkeys((round(a, 3), round(b, 3)) for a, b in windows))
    if len(windows) <= 8:
        return windows
    # Evenly sample long uploads; the old first-eight cap never inspected the ending.
    return [windows[round(i * (len(windows) - 1) / 7)] for i in range(8)]


def _window_strip(source, start, end, destination):
    # Sparse eight-frame strips missed short source edits and a late reset in an otherwise static
    # room. Sixteen ordered frames make every candidate auditable as one uninterrupted take.
    frames = scrape_v2._sample_window_frames(str(source), pipeline.find_ffmpeg(), start, end, n=16)
    if not frames or scrape_v2.cv2 is None or scrape_v2.np is None:
        return None
    tiles = []
    for frame in frames:
        h, w = frame.shape[:2]
        scale = 150.0 / max(1, h)
        tiles.append(scrape_v2.cv2.resize(frame, (max(1, int(w * scale)), 150)))
    try:
        tile_width = max(tile.shape[1] for tile in tiles)
        normalized = []
        for tile in tiles:
            if tile.shape[1] < tile_width:
                tile = scrape_v2.cv2.copyMakeBorder(
                    tile, 0, 0, 0, tile_width - tile.shape[1],
                    scrape_v2.cv2.BORDER_CONSTANT, value=(0, 0, 0))
            normalized.append(tile)
        rows = [scrape_v2.np.hstack(normalized[i:i + 4]) for i in range(0, len(normalized), 4)]
        scrape_v2.cv2.imwrite(str(destination), scrape_v2.np.vstack(rows))
        return destination
    except Exception:
        return None


def _window_visual_hashes(source, start, end):
    """Compact fingerprints for catching reposts/crops of the same attempt."""
    frames = scrape_v2._sample_window_frames(
        str(source), pipeline.find_ffmpeg(), float(start), float(end), n=5)
    hashes = []
    for frame in frames or []:
        try:
            value = scrape_v2._dhash(frame)
        except Exception:
            value = ""
        if value:
            hashes.append(value)
    return hashes


def _same_attempt(left, right):
    """True when two social uploads visually contain the same filmed attempt."""
    a = left.get("visual_hashes") or []
    b = right.get("visual_hashes") or []
    if not a or not b:
        return False
    near = 0
    for value in a:
        if min((scrape_v2._hamming_hex(value, other) for other in b), default=999) <= 7:
            near += 1
    return near >= 2


def _unique_attempts(rows, limit=None):
    unique = []
    for row in rows:
        if any(_same_attempt(row, previous) for previous in unique):
            continue
        unique.append(row)
        if limit and len(unique) >= limit:
            break
    return unique


def _review(rows, action, role, project, reasoning_model, status_cb):
    ffmpeg = pipeline.find_ffmpeg()
    _, ffprobe = clip_scraper._ffmpeg_tools()
    strips = project / "review" / role
    strips.mkdir(parents=True, exist_ok=True)
    accepted = []
    for i, row in enumerate(rows):
        path = Path(row["path"])
        try:
            duration = float(clip_scraper._probe_duration(path, ffprobe) or 0)
        except Exception:
            duration = 0
        if duration < 1.8:
            continue
        cuts = clip_scraper.hard_cut_times(str(path), ffmpeg, scan_seconds=min(duration, 180.0)) or []
        windows = _action_windows(duration, cuts, role)
        window_strips = []
        for window_index, (window_start, window_end) in enumerate(windows):
            strip = _window_strip(path, window_start, window_end,
                                  strips / f"source_{i:02d}_window_{window_index:02d}.jpg")
            if strip:
                window_strips.append((window_index, window_start, window_end, strip))
        if not window_strips:
            continue
        sheet = agent_core.create_media_contact_sheet(
            [entry[3] for entry in window_strips], strips / f"source_{i:02d}_windows.jpg",
            title=f"Window tiles 0-{len(window_strips)-1}; each tile is one complete time range")
        if not sheet:
            continue
        window_text = ", ".join(
            f"{tile}=source {start:.2f}-{end:.2f}s"
            for tile, (_original, start, end, _strip) in enumerate(window_strips))
        data = scrape_v2._vision_json(
            VISION_PROMPT.format(action=action, role=role, windows=window_text), sheet, max_tokens=800,
            temperature=0.05, reasoning_model=reasoning_model) or {}
        if not data.get("shows_action") or not data.get("continuous_footage", True):
            continue
        if not data.get("shows_execution") or not data.get("shows_outcome"):
            continue
        if not data.get("full_action_in_range"):
            continue
        description = str(data.get("reason") or "").lower()
        setup_only = re.search(r"\b(?:prepare|prepares|preparing|sets? up|reaches for|holds?)\b",
                               description)
        outcome_seen = re.search(r"\b(?:lands?|landing|fails?|misses?|completes?|finishes?|"
                                 r"catches?|scores?|hits?|success|outcome|reaction)\b", description)
        if setup_only and not outcome_seen:
            continue
        # Existing creator text is not a relevance failure in this format (the reference itself
        # has creator labels). Only framing/action/continuity can reject an otherwise great shot.
        if not data.get("vertical_usable", True):
            continue
        if data.get("has_engagement_bait"):
            continue
        if (not data.get("single_attempt", True) or data.get("contains_replay")
                or data.get("contains_original_edit")):
            continue
        try:
            chosen_index = int(data.get("chosen_window"))
            _original, start, end, _strip = window_strips[chosen_index]
        except (TypeError, ValueError, IndexError):
            continue
        minimum_readable = 4.0 if role == "payoff" else 3.0
        if end - start < minimum_readable:
            continue
        # A selected range may not contain a source edit: the reference lets each attempt read
        # clearly and never repeats a too-short source to fill its slot.
        inside = [c for c in cuts if start + .18 < c < end - .18]
        if inside:
            end = inside[0] - .08
        if end - start < minimum_readable:
            continue
        accepted.append({**row, "start": round(start, 3), "end": round(end, 3),
                         "visual_hashes": _window_visual_hashes(path, start, end),
                         "wow": float(data.get("wow_score") or 0),
                         "clarity": float(data.get("clarity_score") or 0),
                         "surprise": float(data.get("surprise_score") or 0),
                         "label": str(data.get("subject_label") or "").strip()[:28],
                         "reason": str(data.get("reason") or "")[:180]})
    _log(status_cb, f"Others mode: vision kept {len(accepted)}/{len(rows)} {role} candidate(s).")
    return accepted


def _probe_has_audio(path):
    ffprobe = pipeline.find_ffprobe(pipeline.find_ffmpeg())
    cmd = [str(ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries",
           "stream=index", "-of", "csv=p=0", str(path)]
    return bool(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())


def _render_piece(row, destination, label):
    ffmpeg = str(pipeline.find_ffmpeg())
    duration = max(.25, float(row["end"]) - float(row["start"]))
    font = "C\\:/Windows/Fonts/arialbd.ttf"
    safe = str(label).upper().replace("\\", "").replace(":", "\\:").replace("'", "\\'")
    vf = ("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
          "crop=1080:1920,fps=30,format=yuv420p,"
          f"drawtext=fontfile='{font}':text='{safe}':fontcolor=white:fontsize=58:"
          "borderw=5:bordercolor=black:x=(w-text_w)/2:y=95")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(row["start"]),
           "-t", f"{duration:.3f}", "-i", row["path"]]
    if not _probe_has_audio(row["path"]):
        cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest",
            "-movflags", "+faststart", str(destination)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode or not destination.is_file():
        raise RuntimeError(f"Could not render selected {row['platform']} clip: {(result.stderr or '')[-300:]}")


def _caption_clean_selected_range(row, destination, status_cb=None):
    """Trim the chosen take, remove creator captions, then hand that clean take to the renderer.

    Caption cleanup must happen before ``_render_piece`` draws our format label. Running the
    remover on ``others_XX.mp4`` afterwards can mistake OTHERS/KING for creator text and erase the
    format itself. Keeping the clean selected range next to the editable clip also means timeline
    re-renders never need to rerun OCR/inpainting.
    """
    ffmpeg = str(pipeline.find_ffmpeg())
    duration = max(.25, float(row["end"]) - float(row["start"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{float(row['start']):.3f}", "-t", f"{duration:.3f}",
           "-i", str(row["path"]), "-map", "0:v:0", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(destination)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode or not destination.is_file() or destination.stat().st_size < 4096:
        raise RuntimeError(f"Could not prepare selected clip for caption cleanup: "
                           f"{(result.stderr or '')[-300:]}")
    info = {}
    removed = caption_remover.remove_caption_regions(
        destination, pipeline.find_ffmpeg(), seconds=duration,
        # This is the finished deliverable, not a scrape probe: the clip is already selected and
        # the next step burns a format label onto it. With allow_gpu=False the call could only
        # ever return 0, so the branch below that reports "removed burned-in captions" was
        # unreachable and the creator's text shipped underneath our own label.
        status_cb=status_cb, info=info, allow_gpu=True)
    if removed:
        _log(status_cb, f"Others mode: removed burned-in captions before adding the format label "
                        f"({row.get('id')}).")
    elif info.get("refused"):
        _log(status_cb, f"Others mode: creator text covered {float(info.get('covered') or 0):.0%} "
                        f"of selected clip {row.get('id')}; cleanup was safely refused.")
    else:
        _log(status_cb, f"Others mode: no removable creator caption detected in selected clip "
                        f"{row.get('id')}.")
    return {**row, "path": str(destination), "start": 0.0, "end": duration,
            "captions_removed": bool(removed), "caption_cleanup_refused": bool(info.get("refused"))}


def _concat(files, output):
    ffmpeg = str(pipeline.find_ffmpeg())
    listing = output.with_suffix(".concat.txt")
    listing.write_text("".join("file '%s'\n" % str(p).replace("'", "'\\''") for p in files),
                       encoding="utf-8")
    result = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
                             "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags",
                             "+faststart", str(output)], capture_output=True, text=True, timeout=900)
    if result.returncode or not output.is_file():
        raise RuntimeError(f"Could not assemble Others edit: {(result.stderr or '')[-300:]}")


def _add_unified_soundtrack(source, output, duration, payoff_start=None):
    """One coherent music bed replaces the random loudness/style jump between social sources."""
    ffmpeg = str(pipeline.find_ffmpeg())
    music_dir = Path(__file__).resolve().parent / "background music"
    # The dedicated reference track preserves the exact pacing grammar of this format.
    music = music_dir / "others_doing_x_reference.m4a"
    if not music.is_file():
        music = music_dir / "quirky_momentum_112bpm_short_snap.wav"
    if not music.is_file():
        shutil.copy2(source, output)
        return ""
    fade = max(0.0, float(duration) - .45)
    # The reference track's drop is at 41.0s. Start deeper in the song for shorter edits so the
    # drop always lands on the first frame of the final performer instead of at an arbitrary cut.
    music_offset = 0.0
    reference_music = music.name == "others_doing_x_reference.m4a"
    if reference_music and payoff_start is not None:
        music_offset = max(0.0, REFERENCE_DROP_SECONDS - float(payoff_start))
    if reference_music:
        # Use the reference mix itself. Mixing six unrelated TikTok audio beds over it masked the
        # musical drop and made the change of performer feel late even when the video cut was right.
        filters = (f"[1:a]atrim=start={music_offset:.3f}:end={music_offset + float(duration):.3f},"
                   f"asetpts=PTS-STARTPTS,afade=t=out:st={fade:.3f}:d=0.45,"
                   "alimiter=limit=0.96[a]")
    else:
        filters = (f"[0:a]volume=0.25[original];"
                   f"[1:a]atrim=start={music_offset:.3f}:end={music_offset + float(duration):.3f},"
                   f"asetpts=PTS-STARTPTS,volume=0.72,afade=t=out:st={fade:.3f}:d=0.45[music];"
                   "[original][music]amix=inputs=2:duration=first:normalize=0,"
                   "alimiter=limit=0.92[a]")
    result = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-stream_loop", "-1", "-i", str(music), "-filter_complex", filters,
         "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-t", f"{float(duration):.3f}", "-movflags", "+faststart", str(output)],
        capture_output=True, text=True, timeout=900)
    if result.returncode or not output.is_file():
        raise RuntimeError(f"Could not mix the comparison soundtrack: {(result.stderr or '')[-300:]}")
    return str(music)


def run_others_vs_king(fields, status_cb=None):
    reasoning_model = fields.get("reasoning_model") or "google/gemini-3.5-flash-lite"
    plan = _llm_plan(fields.get("others_action") or fields.get("gen_topic"),
                     reasoning_model, status_cb)
    action = plan["action"]
    platforms = [p for p in ("tiktok", "instagram")]
    slug = "others_%s_%s" % (re.sub(r"[^a-z0-9]+", "_", action.lower()).strip("_")[:34],
                              time.strftime("%H%M%S"))
    project = agent_core.PROJECTS_DIR / slug
    raw_normal = project / "downloads" / "normal"
    raw_payoff = project / "downloads" / "payoff"
    clips = project / "seedance 2.0"
    renders = project / "renders"
    for folder in (raw_normal, raw_payoff, clips, renders, project / "config",
                   project / "input", project / "review"):
        folder.mkdir(parents=True, exist_ok=True)
    _log(status_cb, f"PROJECT_DIR|{project}")
    _log(status_cb, f"Others mode: action locked to '{action}'. Searching TikTok + Instagram.")
    deadline = time.monotonic() + max(600, float(fields.get("scrape_time_budget") or 1800))
    normal_rows = _download(_search_pool(action, False, platforms, deadline, status_cb,
                                         queries=plan["setup_queries"]),
                            raw_normal, 12, status_cb)
    payoff_rows = _download(_search_pool(action, True, platforms, deadline, status_cb,
                                         queries=plan["payoff_queries"]),
                            raw_payoff, 14, status_cb)
    visual_contract = plan.get("visual_contract") or action
    normals = _review(normal_rows, visual_contract, "setup", project, reasoning_model, status_cb)
    payoffs = _review(payoff_rows, visual_contract, "payoff", project, reasoning_model, status_cb)
    normals.sort(key=lambda r: (r["clarity"], r["wow"]), reverse=True)
    payoffs.sort(key=lambda r: (r["wow"] + r["surprise"], r["clarity"]), reverse=True)
    grounded = {row["id"]: row for row in normals + payoffs}
    if len(grounded) < 2:
        raise RuntimeError(f"Others mode could verify only {len(grounded)} clip(s) visibly doing "
                           f"'{action}'. The downloads remain saved for a retry with broader queries.")
    if not payoffs:
        # Do not abort a paid scrape merely because every spectacular-query result was also found
        # by the setup pass. Promote the visibly highest-wowscore grounded clip and reserve it.
        payoffs = sorted(grounded.values(), key=lambda r: (r["wow"] + r["surprise"], r["clarity"]),
                         reverse=True)[:1]
        _log(status_cb, "Others mode: payoff query pool overlapped setup; reserving the strongest grounded result.")
    decision = _llm_edit(normals, payoffs, action, reasoning_model, status_cb)
    by_normal = {row["id"]: row for row in normals}
    by_payoff = {row["id"]: row for row in payoffs}
    payoff = by_payoff.get(str(decision.get("payoff_id") or ""), payoffs[0])
    # The two query passes can encounter the same viral upload. The payoff source is owned by
    # the ending and may not appear among the setup clips, even if it also ranked highly there.
    ordered_ids = [str(value) for value in (decision.get("setup_ids") or [])]
    setup_pool = list({row["id"]: row for row in (normals + payoffs)}.values())
    by_setup = {row["id"]: row for row in setup_pool}
    normal_pick = _unique_attempts([by_setup[sid] for sid in ordered_ids
                                    if sid in by_setup and sid != payoff["id"]
                                    and not _same_attempt(by_setup[sid], payoff)])
    # Trust the editor's deliberate length. Filling a valid four-shot choice to six quietly
    # reintroduced the two weak "person merely holds bottle" clips it had intentionally omitted.
    if len(normal_pick) < 4:
        normal_pick = _unique_attempts(normal_pick + [row for row in setup_pool
                                       if row["id"] != payoff["id"]
                                       and row["id"] not in {r["id"] for r in normal_pick}
                                       and not _same_attempt(row, payoff)])
    normal_pick = normal_pick[:min(6, len(setup_pool))]
    if not normal_pick:
        raise RuntimeError(f"Others mode could not form a unique setup before the payoff for '{action}'.")
    selected = normal_pick + [payoff]
    # This format's grammar is intentionally literal. The planner may search creatively, but it
    # may not rename "high jumping" to editorial nonsense such as "flopping".
    normal_label = f"OTHERS {action}...".upper()
    # Keep the format promise visible. Generic model labels such as "THIS MAN" weakened the
    # punchline and made otherwise identical runs feel like unrelated edits.
    payoff_action = re.sub(r"^doing\s+", "", action, flags=re.I).strip()
    final_label = f"KING OF {payoff_action}".upper()
    pieces, scenes, cursor = [], [], 0.0
    cleaned_selection = []
    for index, initial_row in enumerate(selected, 1):
        name = f"others_{index:02d}.mp4"
        label = final_label if index == len(selected) else normal_label
        clean_source = clips / f"others_{index:02d}_clean_source.mp4"
        # Text-heavy clips are not allowed to leak into the finished edit. The remover refuses
        # large masks on purpose because rebuilding a big rectangle damages the picture; try the
        # next already vision-approved candidate instead of aborting the paid scrape or rendering
        # the unclean source. Setup and payoff keep separate fallback pools.
        fallback_pool = payoffs if index == len(selected) else setup_pool
        candidates = [initial_row] + [candidate for candidate in fallback_pool
                                      if candidate.get("id") != initial_row.get("id")]
        row = initial_row
        render_row = None
        for candidate in candidates:
            if any(candidate.get("id") == used.get("id")
                   or _same_attempt(candidate, used) for used in cleaned_selection):
                continue
            prepared = _caption_clean_selected_range(
                candidate, clean_source, status_cb=status_cb)
            if prepared.get("caption_cleanup_refused"):
                _log(status_cb, f"Others mode: replacing text-heavy selected clip "
                                f"{candidate.get('id')} with another verified candidate.")
                continue
            row, render_row = candidate, prepared
            break
        if render_row is None:
            # All alternates were unusable. Preserve the strongest grounded attempt instead of
            # losing the whole run, but record the refusal visibly in project metadata.
            row = initial_row
            render_row = _caption_clean_selected_range(
                row, clean_source, status_cb=status_cb)
            _log(status_cb, "Others mode: no clean alternate remained; keeping the strongest "
                            "grounded attempt for manual replacement in the timeline.")
        cleaned_selection.append(row)
        _render_piece(render_row, clips / name, label)
        duration = float(row["end"]) - float(row["start"])
        scenes.append({"id": f"{index:02d}", "name": "Final payoff" if index == len(selected) else "Normal attempt",
                       "script": "", "exact_voice_text": "", "start": round(cursor, 3),
                       "end": round(cursor + duration, 3), "clip": name, "asset": name,
                       "seedance": True, "seedance_start_trim": 0.0, "render_caption": False,
                       "blur_captions": False, "source_captions_precleaned": True,
                       "caption_cleanup_removed": bool(render_row.get("captions_removed")),
                       "caption_cleanup_refused": bool(render_row.get("caption_cleanup_refused")),
                       "source_platform": row["platform"], "source_query": row["query"],
                       "source_id": row["id"], "selection_reason": row.get("reason", "")})
        cursor += duration
        pieces.append(clips / name)
    selected = cleaned_selection
    payoff = selected[-1]
    raw_output = renders / f"{slug}_picture_cut.mp4"
    output = renders / f"{slug}.mp4"
    _concat(pieces, raw_output)
    payoff_start = cursor - (float(payoff["end"]) - float(payoff["start"]))
    soundtrack = _add_unified_soundtrack(raw_output, output, cursor, payoff_start=payoff_start)
    # The timeline renderer treats the combined original sound as its master audio. Individual
    # visual clips stay independently movable and replaceable in the editor.
    ffmpeg = str(pipeline.find_ffmpeg())
    audio = project / "input" / "voiceover.wav"
    subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(output),
                    "-vn", "-ar", "48000", "-ac", "2", str(audio)], capture_output=True,
                   timeout=900)
    report = {"format": "others_vs_king", "action": action, "llm_plan": plan,
              "llm_edit_decision": decision,
              "soundtrack": soundtrack,
              "normal_queries": plan["setup_queries"], "payoff_queries": plan["payoff_queries"],
              "selected": [{k: r.get(k) for k in ("id", "platform", "query", "start", "end",
                                                    "clarity", "wow", "surprise", "reason")}
                           for r in selected]}
    (project / "review" / "others_search_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    config = {"project_slug": slug, "title": f"Others {action}",
              "clip_short_format": "others_vs_king", "others_action": action,
              "duration": round(cursor, 3), "scenes": scenes, "audio_path": str(audio),
              "timing_audio_path": str(audio), "speech_audio_in_final": True,
              "custom_sfx": [], "sfx_enabled": False, "render_captions": False,
              "animated_captions": False, "use_seedance_clips": True,
              "source_captions_precleaned": True,
              "background_music_choice": Path(soundtrack).name if soundtrack else "none",
              "background_music_baked_into_audio": bool(soundtrack), "audio_master_gain": 1.0,
              "others_search_report": str(project / "review" / "others_search_report.json")}
    config_path = project / "config" / "project.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(status_cb, f"Others mode finished: {len(normal_pick)} setup clips + 1 reserved payoff.")
    return {"title": config["title"], "project_dir": str(project), "video": str(output),
            "config": str(config_path)}
