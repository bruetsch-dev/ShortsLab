"""Seedance 2.5 challenge-story planner, approval gate, generator, and editor.

The first stage writes the story and renders Seed TTS only. Video generation is a
separate stage: after the user approves that audio, the approved file is aligned,
eight time-aware Seedance requests are submitted, and the returned clips are edited.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import agent_core
import ai_core
import caption_agent
import pipeline
import voice_align


TOPIC_MODEL = "google/gemini-3.7-flash"
SCRIPT_MODEL = "google/gemini-3.7-flash"
SEEDANCE_MODEL = "bytedance/seedance-2.5/text-to-video-turbo"
SEED_TTS_MODEL = "bytedance/seed-speech-tts-2.0"
SEED_TTS_VOICE = "tim_en"
# 1.14 was the old delivery. 1.25 keeps the quick short-form read going and 1.05 lifts the master
# slightly, since the editor mixes the Seedance native sound UNDER this track at 0.90.
SEED_TTS_SPEED = 1.25
SEED_TTS_VOLUME = 1.05
SEEDANCE_ENDPOINT = (
    "https://api.wavespeed.ai/api/v3/"
    "bytedance/seedance-2.5/text-to-video-turbo"
)
CHARACTER_URL_TOKEN = "{{CHARACTER_SHEET_URL}}"

EXAMPLE_DIRECTIONS = (
    "I brought a Spartan to a modern gym",
    "I took a Viking to IKEA",
    "I brought Napoleon to McDonald's",
    "I put a Roman senator on a Zoom call",
    "I took a medieval knight through airport security",
    "I brought a pirate onto a cruise ship",
    "I took Cleopatra to Sephora",
    "I brought a caveman to a supermarket",
    "I took a samurai to Black Friday at Walmart",
    "I took Genghis Khan to a driving test",
    "I challenged a Spartan to a modern 5K",
    "I entered a Viking hot-dog eating contest",
    "I made a Roman gladiator play Fortnite",
    "I survived one day as a Roman slave",
    "I spent a night in a Viking longhouse",
    "I survived a medieval prison",
    "I had a job interview with Julius Caesar",
    "I went on a date with a Spartan woman",
    "I toured a Pharaoh's palace",
    "I ranked history's toughest eras to survive",
)

TOPIC_SYSTEM = """You are the idea editor for a fast first-person vertical comedy channel with a
recurring lead character. The character sheet arrives later; never describe a face.

Return exactly 20 premises that a viewer cannot scroll past. Each premise collides two specific,
already-recognizable things:
- a named historical figure, people, or trade (e.g. "a Mongol horse archer", "Queen Elizabeth I",
  "a 1970s Soviet cosmonaut", "an Aztec ballplayer"), and
- one concrete modern place, brand, object, job or ritual (e.g. "a Formula 1 pit stop", "an IKEA
  returns desk", "airport security", "a Costco food court", "a Tinder date").

Every premise must pass all five of these:
1. VISIBLE - it is a chain of filmable actions, not a topic, a feeling, or an explanation.
2. SPECIFIC - both halves are named. "Ancient warrior meets the modern world" is not a premise.
3. ESCALATING - something concrete gets worse, bigger, dirtier, louder or more public as it runs.
4. PAYOFF - the final shot reverses or tops the opening image. Write that reversal into the hook.
5. SELF-EXPLANATORY - it is gripping from the title alone, with no caption and no extra context.

Title: first person, present tense, 6-12 words, naming BOTH halves of the collision.
Hook: one sentence naming the funniest visible beat AND the final reversal.

Spread the engines across the 20, and never let two ideas share a setting or an era:
- 5-7 "visitor": the historical figure dropped into an ordinary modern situation.
- 5-7 "survival": the modern lead dropped into one brutal, specific historical day or job.
- 3-5 "challenge": a named contest, test, inspection, interview, date, or ranking.
- 2-4 "ranking": a ranked list the lead actually lived through, with a controversial winner.
Vary the register as well: dry understatement, deadpan bragging, escalating panic,
mock-serious documentary.

The calibration list in the user message is deliberately narrow - it is mostly ONE engine
(bring a figure to a modern place) plus a few survival and ranking entries. Treat it as the floor,
not the target: if more than a third of your titles copy its exact "I took X to Y" shape, rewrite
them before answering.

Banned, because each one reads as filler: "a day in the life", "the secret life of", "history's
greatest/toughest/hardest", "what if", "life as", "top 10", "things you didn't know", pure
exposition, invisible actions, copyrighted fictional characters, real living private people, and
cruelty or gore played straight.

Return JSON only:
{"topics":[{"title":"...","hook":"the funniest visible beat and the final reversal",
"format":"visitor|survival|challenge|ranking"}]}
"""

TOPIC_COUNT = 20
# The exact shape of the ideas this screen used to show: a title that promises nothing, so the
# user has to read the hook to find out whether there is an idea at all. These are dropped before
# they reach the screen and the writer is asked to replace them.
TOPIC_FILLER = re.compile(
    r"\b(?:a day in the life|day in the life|the secret life of|secret history of|"
    r"history'?s (?:greatest|toughest|hardest|most)|what if|life as|"
    r"top\s*\d+|\d+ things|things you (?:did|didn'?t) know)\b", re.I)

SCRIPT_SYSTEM = """You are the head writer and shot director for a vertical first-person comedy
short. Write one complete 60-90 second story and its Seedance clip plan from the chosen premise.
The same main character appears throughout and is supplied to the video model as @image1, a
character-sheet reference.

SCRIPT RULES
- 120-180 spoken words, quick American-English delivery, escalating events, concrete times or
  chapter markers when useful, and one clean joke or reversal per paragraph.
- Hook: a question or sharp setup, two short sentences of absurdity, then "Here's what happened."
  or "Here's what that looks like." Never say "I did it" or "I spent one day there" in the hook.
- Keep continuity exact: era, wardrobe, props, secondary characters, injuries, dirt, and time of
  day carry forward. End with a callback or dry modern comparison. No CTA.
- The narration describes the story; characters in the generated footage do not need to speak
  the narration.
- Split the complete script across clips[].narration in the same order. Keep the eight narration
  sections reasonably even so each one fits under its own 10-second generated clip.

CLIP RULES
- Produce EXACTLY EIGHT clips in this order: Hook, Day 1, Day 2, Day 3, Day 4, Day 5, Day 6,
  Ending. There is NEVER a Day 7: do not write those words in the script, labels, narration,
  prompts, background signs, or dialogue. The Ending is a payoff/callback without a day label.
  Each clip is exactly 10 seconds. Every prompt describes multiple
  timed shots whose durations total exactly 10 seconds.
- Every prompt starts with a shared photorealistic live-action style sentence, names @image1 as
  the exact recurring main character, and tells the model to preserve that face, body, and fixed
  identifying details. Adapt only period clothing while preserving the character's signature
  feature from the sheet.
- Describe visible action, reaction, camera position/motion, location, lighting, continuity, and
  synchronized diegetic sound. No captions, logos, watermarks, split screens, or narration text.
- Prompts are standalone: repeat all details needed by that clip. Use @image1 in every prompt.
- The eight generations are the complete source budget. Put useful cutaways inside their timed
  multi-shot prompts; never add a ninth B-roll generation.

Return JSON only:
{"title":"...","script":"...","voice_direction":"...","continuity":"...",
"clips":[{"label":"...","narration":"the lines this clip covers","prompt":"..."}]}
"""


def _clean_line(value) -> str:
    return " ".join(str(value or "").split()).strip()


def custom_topic(title: str) -> dict:
    """Wrap a premise the user typed so the planner treats it exactly like a chosen card.

    It carries ``custom`` so the script writer is told to keep the premise word for word instead
    of "improving" it into one of its own.
    """
    clean = _clean_line(title)
    if not clean:
        raise RuntimeError("Type your own topic first.")
    return {"title": clean, "hook": "Written by the user - use it word for word.",
            "format": "yours", "custom": True}


def resolve_topic(own_topic: str, raw_topic_json: str) -> dict:
    """Which premise a preview runs on: the user's own words, or the card they selected."""
    if _clean_line(own_topic):
        return custom_topic(own_topic)
    try:
        topic = json.loads(raw_topic_json or "{}")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("The selected topic is invalid.") from exc
    if not isinstance(topic, dict) or not _clean_line(topic.get("title")):
        raise RuntimeError("Choose a topic or write your own first.")
    return topic


def _usable_topics(out, seen: set, limit: int) -> list[dict]:
    """Normalize the writer's rows, dropping the ones that only look like ideas."""
    topics = []
    for raw in (out or {}).get("topics") or []:
        if not isinstance(raw, dict):
            continue
        title = _clean_line(raw.get("title"))
        key = title.casefold()
        if not title or key in seen or TOPIC_FILLER.search(title):
            continue
        seen.add(key)
        topics.append({
            "title": title,
            "hook": _clean_line(raw.get("hook")),
            "format": _clean_line(raw.get("format")) or "challenge",
        })
        if len(topics) == limit:
            break
    return topics


def _ask_for_topics(model: str, ask: str) -> dict:
    return agent_core._post_llm_json(
        model,
        [{"role": "system", "content": TOPIC_SYSTEM},
         {"role": "user", "content": ask}],
        max_tokens=6000, temperature=0.9, timeout=420,
    ) or {}


def generate_topics(direction: str = "", model: str = TOPIC_MODEL) -> list[dict]:
    """Create 20 normalized choices. This is an LLM call, never a video API call."""
    seed = "\n".join(f"- {item}" for item in EXAMPLE_DIRECTIONS)
    lead = f"Optional direction: {_clean_line(direction) or '(open — surprise me)'}"
    topics = _usable_topics(_ask_for_topics(model, (
        f"{lead}\n\nCalibration examples (one narrow engine - derive the range, not the shape; "
        f"do not repeat or lightly reword any of them):\n{seed}"
    )), set(), TOPIC_COUNT)
    if len(topics) < TOPIC_COUNT:
        # The filler filter removed rows, or the writer repeated itself. One repair pass is
        # cheaper than showing the user 17 cards and calling the set "20 ideas".
        missing = TOPIC_COUNT - len(topics)
        accepted = "\n".join(f"- {row['title']}" for row in topics) or "(none yet)"
        topics.extend(_usable_topics(_ask_for_topics(model, (
            f"{lead}\n\nThese ideas are already accepted - do not repeat or paraphrase them:\n"
            f"{accepted}\n\nReturn exactly {missing} more in the same JSON shape, obeying every "
            "rule and avoiding the banned filler phrasings."
        )), {row["title"].casefold() for row in topics}, missing))
    if len(topics) != TOPIC_COUNT:
        raise RuntimeError(
            f"The topic writer returned {len(topics)} usable ideas; expected {TOPIC_COUNT}.")
    return topics


def build_preview(topic: dict, direction: str, character_path, model: str = SCRIPT_MODEL) -> dict:
    """Write the script and clip drafts without contacting Seedance."""
    path = Path(character_path)
    if not path.is_file():
        raise RuntimeError("Upload a character-sheet image first.")
    if not isinstance(topic, dict) or not _clean_line(topic.get("title")):
        raise RuntimeError("Choose a topic card or write your own topic first.")
    if topic.get("custom"):
        topic = custom_topic(topic.get("title"))
    character_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ask = (
        f"Chosen topic: {json.dumps(topic, ensure_ascii=False)}\n"
        f"Original direction: {_clean_line(direction) or '(none)'}\n"
        + ("The user wrote this premise themselves. Use their title and premise exactly as given: "
           "do not rename it, swap the historical figure, move it to another setting, or replace it "
           "with a safer idea. Return their title unchanged in the \"title\" field. Everything "
           "below still governs the script and the clip plan.\n"
           if topic.get("custom") else "")
        + "The attached character sheet will be available to Seedance as @image1. Infer no private "
        "identity facts from it; refer to it only as the recurring main character."
    )
    out = agent_core._post_llm_json(
        model,
        [{"role": "system", "content": SCRIPT_SYSTEM},
         {"role": "user", "content": ask}],
        max_tokens=10000, temperature=0.65, timeout=600,
    ) or {}
    script = str(out.get("script") or "").strip()
    clips = []
    for index, raw in enumerate((out.get("clips") or [])[:8], 1):
        if not isinstance(raw, dict):
            continue
        prompt = _clean_line(raw.get("prompt"))
        if not prompt:
            continue
        if "@image1" not in prompt:
            prompt = (
                "Use @image1 as the exact recurring main character; preserve the same face, "
                "body proportions, and signature identifying details. " + prompt
            )
        clips.append({
            "index": index,
            "label": _clean_line(raw.get("label")) or f"Clip {index}",
            "narration": str(raw.get("narration") or "").strip(),
            "prompt": prompt,
        })
    if not script:
        raise RuntimeError("The writer returned no script.")
    if len(clips) != 8:
        raise RuntimeError(f"The writer returned {len(clips)} usable clips; expected exactly 8.")
    canonical_labels = ["Hook", "Day 1", "Day 2", "Day 3", "Day 4", "Day 5", "Day 6", "Ending"]
    for clip, label in zip(clips, canonical_labels):
        clip["label"] = label
    forbidden = json.dumps(out, ensure_ascii=False).casefold()
    if re.search(r"\bday\s*[:#-]?\s*(?:7|seven)\b", forbidden):
        raise RuntimeError("The writer introduced Day 7, which this format forbids.")

    return {
        "dry_run": False,
        "video_api_called": False,
        "generation_state": "voice_pending_approval",
        "model": SEEDANCE_MODEL,
        "endpoint": SEEDANCE_ENDPOINT,
        "topic": topic,
        # A premise the user typed is also the title they get: the writer may not rename it.
        "title": (_clean_line(topic.get("title")) if topic.get("custom")
                  else (_clean_line(out.get("title")) or _clean_line(topic.get("title")))),
        "script": script,
        "voice_direction": str(out.get("voice_direction") or "").strip(),
        "continuity": str(out.get("continuity") or "").strip(),
        "clips": clips,
        "requests": [],
        "generation_count": 8,
        "edit_plan": {
            "source_order": ["Hook", "Day 1", "Day 2", "Day 3", "Day 4", "Day 5", "Day 6", "Ending"],
            "max_sources": 8,
            "target_seconds": "voiceover length (normally 55-70s)",
            "cut_style": "hard cuts; internal generated shots average 1.8-2.5s",
            "captions": "1-2 words, bold white, active keyword orange, centered at 65% frame height",
            "day_labels": ["Day 1", "Day 2", "Day 3", "Day 4", "Day 5", "Day 6"],
            "audio": "voiceover foreground; synchronized Seedance sound at -20 dB; no generated dialogue",
            "ending": "final generation is the payoff; no Day 7 label",
        },
        "character_sheet": {
            "filename": path.name,
            "sha256": character_sha,
            "path": str(path.resolve()),
            "request_token": CHARACTER_URL_TOKEN,
            "sent_as": "reference_images[0] / @image1 in every prompt",
        },
    }


def _run(command, error):
    done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if done.returncode != 0:
        raise RuntimeError(f"{error}: {done.stderr.strip()[-500:]}")


def _duration(path) -> float:
    probe = str(pipeline.find_ffprobe(pipeline.find_ffmpeg()))
    done = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return max(0.0, float(done.stdout.strip()))
    except (TypeError, ValueError):
        return 0.0


def _narration_timeline(preview: dict, voice_seconds: float) -> list[dict]:
    """Allocate the measured voiceover across the eight authored narration sections.

    Seedance sources are capped at ten seconds. Long narration sections are therefore capped
    first and the remaining time is redistributed across the other sections. This keeps the
    final cut equal to the real voiceover duration while preserving the writer's relative beat
    lengths instead of imposing eight arbitrary equal slices.
    """
    clips = list((preview or {}).get("clips") or [])[:8]
    if len(clips) != 8:
        raise RuntimeError("The voiceover timeline needs exactly 8 narration sections.")
    weights = [max(1, len(re.findall(r"\b[\w']+\b", str(row.get("narration") or ""))))
               for row in clips]
    remaining = float(voice_seconds)
    durations = [0.0] * 8
    open_indices = set(range(8))
    while open_indices:
        weight_sum = sum(weights[i] for i in open_indices)
        capped = [i for i in open_indices
                  if remaining * weights[i] / max(1, weight_sum) > 10.0 + 1e-9]
        if not capped:
            for i in open_indices:
                durations[i] = remaining * weights[i] / max(1, weight_sum)
            break
        for i in capped:
            durations[i] = 10.0
            remaining -= 10.0
            open_indices.remove(i)
    if remaining < -1e-6 or any(value > 10.0001 for value in durations):
        raise RuntimeError("The voiceover is too long for eight 10-second generations.")
    timeline = []
    cursor = 0.0
    for index, (clip, seconds) in enumerate(zip(clips, durations), 1):
        end = voice_seconds if index == 8 else cursor + seconds
        timeline.append({
            "index": index, "label": str(clip.get("label") or f"Clip {index}"),
            "start": round(cursor, 3), "end": round(end, 3),
            "seconds": round(end - cursor, 3),
        })
        cursor = end
    return timeline


def generate_preview_voiceover(preview: dict, stage_dir, status_cb=None,
                               cancel_event=None) -> dict:
    """Generate the reviewable Seed TTS master without creating video timings."""
    script = str((preview or {}).get("script") or "").strip()
    if not script:
        raise RuntimeError("The challenge script is missing.")
    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)
    voice = pipeline.generate_speech_gemini(
        script, stage / "seed_voiceover.mp3", model=SEED_TTS_MODEL,
        voice=SEED_TTS_VOICE, language="en",
        voice_instruction=str((preview or {}).get("voice_direction") or ""),
        tts_speed=SEED_TTS_SPEED, volume=SEED_TTS_VOLUME,
        cancel_event=cancel_event, status_cb=status_cb,
    )
    seconds = _duration(voice)
    if not 20.0 <= seconds <= 80.0:
        raise RuntimeError(f"Unexpected voiceover duration: {seconds:.2f}s.")
    preview["voiceover"] = {
        "path": str(Path(voice).resolve()), "duration": round(seconds, 3),
        "model": SEED_TTS_MODEL, "voice": SEED_TTS_VOICE,
        "speed": SEED_TTS_SPEED, "volume": SEED_TTS_VOLUME,
    }
    preview.setdefault("edit_plan", {})["target_seconds"] = round(seconds, 3)
    preview["edit_plan"]["timing"] = (
        "Pending approval; word timestamps and clip windows are created only after approval"
    )
    return preview


def timestamp_approved_voice(preview: dict, output_dir=None, status_cb=None) -> list[dict]:
    """Align the approved master and turn real word boundaries into eight cut windows."""
    log = status_cb or (lambda _message: None)
    voice_meta = (preview or {}).get("voiceover") or {}
    voice = Path(str(voice_meta.get("path") or ""))
    if not voice.is_file():
        raise RuntimeError("Approve a generated voiceover before creating video timings.")
    clips = list((preview or {}).get("clips") or [])
    if len(clips) != 8:
        raise RuntimeError("Challenge timing needs exactly 8 authored clips.")
    seconds = float(voice_meta.get("duration") or _duration(voice))
    if not 20.0 <= seconds <= 80.0:
        raise RuntimeError(f"Unexpected voiceover duration: {seconds:.2f}s.")

    log("Approval received. Creating word timestamps from the approved Seed voiceover...")
    asr_words = voice_align.transcribe_words(
        str(voice), language="en", status_cb=log, speed=SEED_TTS_SPEED) or []
    words = voice_align.align_script_to_words(str(preview.get("script") or ""), asr_words)
    words = [row for row in words if isinstance(row, dict)
             and row.get("start") is not None and row.get("end") is not None]
    if len(words) < 8:
        raise RuntimeError("The approved voiceover could not be aligned reliably.")

    weights = [max(1, len(re.findall(r"\b[\w']+\b", str(row.get("narration") or ""))))
               for row in clips]
    total_weight = sum(weights)
    boundaries = [0]
    running = 0
    for weight in weights[:-1]:
        running += weight
        wanted = round(len(words) * running / max(1, total_weight))
        boundaries.append(max(boundaries[-1] + 1,
                              min(len(words) - (8 - len(boundaries)), wanted)))
    boundaries.append(len(words))

    timeline = []
    for index, clip in enumerate(clips):
        left, right = boundaries[index], boundaries[index + 1]
        start = 0.0 if index == 0 else float(words[left]["start"])
        end = seconds if index == 7 else float(words[right]["start"])
        duration = end - start
        if duration <= 0 or duration > 10.0001:
            raise RuntimeError(
                f"{clip.get('label') or f'Clip {index + 1}'} needs {duration:.2f}s; "
                "each Seedance source can supply at most 10 seconds. Regenerate a faster voice take."
            )
        timeline.append({
            "index": index + 1,
            "label": str(clip.get("label") or f"Clip {index + 1}"),
            "start": round(start, 3), "end": round(end, 3),
            "seconds": round(duration, 3),
            "spoken_text": " ".join(str(row.get("word") or "") for row in words[left:right]).strip(),
            "word_start": left, "word_end": right,
        })

    preview["word_timestamps"] = words
    preview["cut_timeline"] = timeline
    preview["generation_state"] = "approved_and_timed"
    preview.setdefault("edit_plan", {})["timing"] = (
        "Word-aligned from the user-approved Seed TTS master"
    )
    if output_dir:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "approved_voice_timestamps.json").write_text(
            json.dumps({"words": words, "cut_timeline": timeline}, indent=2,
                       ensure_ascii=False), encoding="utf-8")
    return timeline


def build_timed_requests(preview: dict, character_url: str) -> list[dict]:
    """Build the final WaveSpeed payloads from approved, word-aligned timing."""
    clips = list((preview or {}).get("clips") or [])
    timeline = list((preview or {}).get("cut_timeline") or [])
    if len(clips) != 8 or len(timeline) != 8:
        raise RuntimeError("Approve and timestamp the voiceover before building video requests.")
    if not str(character_url).startswith(("http://", "https://")):
        raise RuntimeError("The character sheet was not uploaded to WaveSpeed.")
    requests = []
    for clip, timing in zip(clips, timeline):
        usable = float(timing["seconds"])
        spoken = _clean_line(timing.get("spoken_text") or clip.get("narration"))
        timing_note = (
            f"EDIT TIMING: this 10-second source is cut at {usable:.2f} seconds in the final edit. "
            f"The matching approved voiceover section is: {json.dumps(spoken, ensure_ascii=False)}. "
            f"Show the decisive action and visual payoff before {usable:.2f} seconds; use the "
            "remaining source time only as a safe continuation. "
        )
        prompt = timing_note + str(clip.get("prompt") or "")
        if "@image1" not in prompt:
            prompt += " Use @image1 as the exact recurring main character."
        requests.append({
            "clip": int(clip.get("index") or len(requests) + 1),
            "label": str(clip.get("label") or f"Clip {len(requests) + 1}"),
            "method": "POST", "url": SEEDANCE_ENDPOINT,
            "cut_window": dict(timing),
            "body": {
                "prompt": prompt, "reference_images": [character_url],
                "aspect_ratio": "9:16", "resolution": "720p",
                "duration": 10, "generate_audio": True,
            },
        })
    return requests


def retime_prompts_for_approved_voice(preview: dict, model: str = SCRIPT_MODEL) -> list[dict]:
    """Rewrite draft shot clocks so they cannot contradict the approved cut windows."""
    clips = list((preview or {}).get("clips") or [])
    timeline = list((preview or {}).get("cut_timeline") or [])
    if len(clips) != 8 or len(timeline) != 8:
        raise RuntimeError("Approved voice timing is required before prompts can be retimed.")
    material = [{
        "index": index + 1, "label": clip.get("label"),
        "approved_voice_start": timing.get("start"),
        "approved_voice_end": timing.get("end"),
        "cut_after_seconds": timing.get("seconds"),
        "spoken_text": timing.get("spoken_text") or clip.get("narration"),
        "draft_prompt": clip.get("prompt"),
    } for index, (clip, timing) in enumerate(zip(clips, timeline))]
    system = """You retime eight Seedance 2.5 video prompts to an approved voiceover.
Return JSON only: {"clips":[{"index":1,"prompt":"..."}, ...]}.
Keep exactly eight rows in order. Each prompt must be a standalone photorealistic live-action
10-second shot plan whose explicitly timed shots total exactly 10 seconds. Preserve @image1 as
the exact recurring character and preserve the draft's story action and continuity. The action
matching spoken_text and its visual payoff MUST finish before cut_after_seconds. Put only safe
reaction, hold, or continuation footage after that boundary. Remove or rewrite every old shot
clock that conflicts with the boundary. Include camera, light, location, visible action and
synchronized diegetic sound. No captions, logos, watermarks, narration text, generated dialogue,
split screens, or Day 7."""
    out = agent_core._post_llm_json(
        model, [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(material, ensure_ascii=False)}],
        max_tokens=10000, temperature=0.35, timeout=600) or {}
    rows = out.get("clips") or []
    if len(rows) != 8:
        raise RuntimeError(f"The timing pass returned {len(rows)} prompts; expected exactly 8.")
    for index, (clip, row) in enumerate(zip(clips, rows), 1):
        prompt = _clean_line(row.get("prompt") if isinstance(row, dict) else "")
        if not prompt:
            raise RuntimeError(f"The timing pass returned no prompt for clip {index}.")
        if "@image1" not in prompt:
            prompt = "Use @image1 as the exact recurring main character. " + prompt
        clip.setdefault("draft_prompt", clip.get("prompt"))
        clip["prompt"] = prompt
    if re.search(r"\bday\s*[:#-]?\s*(?:7|seven)\b",
                 json.dumps(clips, ensure_ascii=False), re.I):
        raise RuntimeError("The timing pass introduced Day 7, which this format forbids.")
    return clips


def generate_and_edit(preview: dict, project_dir, status_cb=None, cancel_event=None) -> dict:
    """Run post-approval timestamps, eight generations, downloads, and the final edit."""
    log = status_cb or (lambda _message: None)
    project_dir = Path(project_dir)
    work, generated = project_dir / "work", project_dir / "generated"
    for folder in (work, generated, project_dir / "input"):
        folder.mkdir(parents=True, exist_ok=True)

    voice = Path(str(((preview or {}).get("voiceover") or {}).get("path") or ""))
    character = Path(str(((preview or {}).get("character_sheet") or {}).get("path") or ""))
    if not voice.is_file() or not character.is_file():
        raise RuntimeError("The approved voiceover or character sheet is missing.")
    timestamp_approved_voice(preview, work, status_cb=log)
    log("Rewriting all eight shot clocks around the approved word timings...")
    retime_prompts_for_approved_voice(preview)
    key = pipeline.api_key()
    log("Uploading the approved character sheet once for all eight generations...")
    character_url, upload_response = pipeline.upload_media(character, key)
    requests = build_timed_requests(preview, character_url)
    preview["requests"] = requests
    preview["video_api_called"] = True
    preview["generation_state"] = "generating"
    (work / "timed_generation_plan.json").write_text(
        json.dumps({"requests": requests, "character_upload": upload_response}, indent=2,
                   ensure_ascii=False), encoding="utf-8")

    predictions = []
    for index, request in enumerate(requests, 1):
        pipeline.check_cancel(cancel_event=cancel_event)
        log(f"Submitting Seedance clip {index}/8: {request['label']}...")
        response = pipeline.request_json("POST", request["url"], key,
                                         request["body"], timeout=240)
        predictions.append((pipeline.unwrap_id(response), response, request))

    paths, generation_reports = [], []
    for index, (prediction_id, submit_response, request) in enumerate(predictions, 1):
        outputs, result_response = pipeline.poll_wavespeed(
            prediction_id, key, timeout_s=1800, interval_s=3,
            cancel_event=cancel_event, status_cb=log,
            label=f"Seedance clip {index}/8 ({request['label']})")
        dest = generated / f"clip_{index:02d}.mp4"
        pipeline.download_file(outputs[0], dest)
        paths.append(dest)
        generation_reports.append({
            "clip": index, "label": request["label"], "prediction_id": prediction_id,
            "file": str(dest), "submit": submit_response, "result": result_response,
        })
        log(f"Downloaded Seedance clip {index}/8.")

    preview["generation_state"] = "editing"
    report = edit_generated_clips(paths, preview, project_dir, status_cb=log,
                                  cancel_event=cancel_event)
    report["requests"] = requests
    report["generation_reports"] = generation_reports
    report["approval_required"] = True
    report["voice_approved"] = True
    preview["generation_state"] = "complete"
    return report


def edit_generated_clips(clip_paths, preview: dict, project_dir, status_cb=None,
                         cancel_event=None) -> dict:
    """Turn the eight generated clips into the reference-style finished short.

    This is the downstream path used both for manually returned dry-run generations and, later,
    for clips returned by the live Seedance submitter. It creates narration, uses every source in
    order, mixes native sound below the voice, adds Day 1-6 labels, and burns word captions.
    """
    log = status_cb or (lambda _message: None)
    clips = [Path(p) for p in clip_paths if p and Path(p).is_file()]
    if len(clips) != 8:
        raise RuntimeError(f"Challenge editing needs exactly 8 generated clips; received {len(clips)}.")
    script = str((preview or {}).get("script") or "").strip()
    if not script:
        raise RuntimeError("The dry-run script is missing.")
    project_dir = Path(project_dir)
    work = project_dir / "work"
    renders = project_dir / "renders"
    work.mkdir(parents=True, exist_ok=True)
    renders.mkdir(parents=True, exist_ok=True)
    ffmpeg = str(pipeline.find_ffmpeg())

    voice_meta = (preview or {}).get("voiceover") or {}
    prior_voice = Path(str(voice_meta.get("path") or ""))
    voice_dest = project_dir / "input" / "voiceover.mp3"
    voice_dest.parent.mkdir(parents=True, exist_ok=True)
    if prior_voice.is_file():
        log("Using the approved Seed TTS voiceover from the prompt stage...")
        shutil.copy2(prior_voice, voice_dest)
        voice = voice_dest
    else:
        log("Creating the Seed TTS voiceover before cutting...")
        voice = pipeline.generate_speech_gemini(
            script, voice_dest, model=SEED_TTS_MODEL, voice=SEED_TTS_VOICE, language="en",
            voice_instruction=str((preview or {}).get("voice_direction") or ""),
            tts_speed=SEED_TTS_SPEED, volume=SEED_TTS_VOLUME,
            cancel_event=cancel_event, status_cb=log,
        )
    voice_seconds = _duration(voice)
    if not 20.0 <= voice_seconds <= 80.0:
        raise RuntimeError(f"Unexpected voiceover duration: {voice_seconds:.2f}s.")
    timeline = list((preview or {}).get("cut_timeline") or [])
    if len(timeline) != 8:
        timeline = _narration_timeline(preview, voice_seconds)
    if any(float(row.get("seconds") or 0) <= 0 or float(row.get("seconds") or 0) > 10.0001
           for row in timeline):
        raise RuntimeError("The approved cut timeline contains an invalid source window.")

    log("Trimming all eight generations to their measured voiceover sections...")
    pieces = []
    for index, (source, timing) in enumerate(zip(clips, timeline), 1):
        normalized = work / f"normalized_{index:02d}.mp4"
        ai_core._normalize(source, normalized, ffmpeg)
        piece = work / f"piece_{index:02d}.mp4"
        _run([ffmpeg, "-y", "-i", str(normalized), "-t", f"{timing['seconds']:.6f}",
              "-c", "copy", str(piece)], f"Could not trim clip {index}")
        pieces.append(piece)
    listing = work / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    joined = work / "joined.mp4"
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
          "-c", "copy", str(joined)], "Could not assemble the eight clips")

    log("Mixing native Seedance sound under the narration...")
    mixed = work / "mixed.mp4"
    _run([ffmpeg, "-y", "-i", str(joined), "-i", str(voice),
          "-filter_complex", "[0:a]volume=0.10[amb];[1:a]volume=0.90[vo];"
                             "[amb][vo]amix=inputs=2:duration=first:dropout_transition=0[a]",
          "-map", "0:v:0", "-map", "[a]", "-t", f"{voice_seconds:.6f}",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
          str(mixed)], "Could not mix the narration")

    labels = [{"text": f"Day {day}", "start": timeline[day]["start"],
               "end": timeline[day]["end"]} for day in range(1, 7)]
    log("Burning word captions and Day 1-6 labels...")
    captioned = caption_agent.caption_video(
        mixed, max_words=2, language="en", caption_center_y=0.65,
        caption_config={"caption_active_style": "color", "caption_active_color": "#FF7A00",
                        "caption_base_color": "#FFFFFF", "caption_stroke": "thin",
                        "caption_enter_seconds": 0.04, "caption_exit_seconds": 0.04,
                        "caption_slide_px": 6},
        labels=labels, status_cb=log, cancel_event=cancel_event,
    )
    final = renders / "challenge_short.mp4"
    shutil.copy2(captioned["video"], final)
    return {
        "video": str(final), "title": str((preview or {}).get("title") or "Challenge Short"),
        "project_dir": str(project_dir), "mode": "challenge", "duration": round(voice_seconds, 3),
        "generations": 8, "cuts_expected": "24-36 including generated internal cuts",
        "day_labels": [f"Day {i}" for i in range(1, 7)], "day_7": False,
        "voiceover": {"file": str(voice), "model": SEED_TTS_MODEL,
                      "voice": SEED_TTS_VOICE, "speed": SEED_TTS_SPEED,
                      "volume": SEED_TTS_VOLUME},
        "cut_timeline": timeline,
        "clips": [{"file": str(path), "seconds": timing["seconds"],
                   "seconds_used": timing["seconds"]}
                  for path, timing in zip(clips, timeline)],
    }
