"""Chat-based Shortslab UI - server side.

This module renders the new deterministic chat shell and provides its JSON endpoints.
It is a PURE UI layer: every action maps onto the EXISTING routes/handlers/payloads in
app.py (see PAYLOAD MANIFEST below). No model is ever called to drive the conversation -
all assistant messages are English templates and all transitions are deterministic
client-side state machine code in static/chat-shell.js.

Design rules (from the migration spec):
- Single source of truth: model/voice/amount option lists are EXTRACTED at runtime from
  the legacy pages (form_page, sfx_page, visual_page, caption_page, longform_page), so the
  chat UI can never drift from what the backend actually accepts.
- Payload parity: the chat submits the same field names/semantics as the old <form>s.
  RUN_MANIFEST documents every field and how it is sent (text / checkbox / hidden pair).
- The legacy UI stays available during migration via ?legacy_ui=1 on every routed page.
"""

from __future__ import annotations

import json
import threading
import re
import time
import reasoning_modes
import pipeline
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAT_STATE_PATH = ROOT / "chat_session.json"

CHAT_UI_VERSION = "chat-1.0"


def _asset_ver():
    """mtime-based cache-buster so a rebuilt chat-shell.css/js is never served stale from the
    WebView2 cache (the symptom was a working new JS with the OLD CSS -> broken layouts)."""
    try:
        css = (ROOT / "static" / "chat-shell.css").stat().st_mtime
        js = (ROOT / "static" / "chat-shell.js").stat().st_mtime
        mtimes = [css, js]
        for asset in ("design-v2.css", "design-v2.js", "shell-window.css"):
            try:
                mtimes.append((ROOT / "static" / asset).stat().st_mtime)
            except Exception:
                pass
        try:
            mtimes.append((ROOT / "static" / "timeline-theme.css").stat().st_mtime)
        except Exception:
            pass
        return str(int(max(mtimes)))
    except Exception:
        return str(int(time.time()))


# ------------------------------------------------------------------ English string layer

UI_STRINGS = {
    "app_name": "Shortslab",
    "workspace": "Creator studio",
    "new_project": "New project",
    "search_projects": "Search projects...",
    "nav_home": "Home",
    "nav_new": "New creation",
    "nav_assets": "Projects & Assets",
    "nav_jobs": "Active jobs",
    "nav_timeline": "Open Timeline Editor",
    "nav_sfx": "Sound Effects Master",
    "nav_vfx": "Visual Effects Master",
    "nav_captions": "Captions Master",
    "recent_projects": "Recent projects",
    "connections": "Connections",
    "theme": "Theme",
    "settings": "Settings",
    "collapse": "Collapse sidebar",
    "expand": "Open sidebar",

    "what_creating": "What are we creating today?",
    "grp_create": "Make a new video",
    "grp_masters": "Polish a finished video · Masters",
    "mode_script_t": "A Video from a Script",
    "mode_script_d": "Turn a voice script into a full visual Short with the app pipeline.",
    "mode_viral_t": "A Viral Short from a Topic",
    "mode_viral_d": "Give one topic - the agents write, film, caption and voice it fully autonomously.",
    "physics_intro": "Pick what gets simulated. Every one of these is a real Blender scene - nothing is generated and nothing is animated by hand, the solver decides what happens and the sound lands on the frame it happens.",
    "physics_hint": "Comma separated. Three to four values reads best - the contrast between them is the video.",
    "mode_lowpoly_t": "A Low Poly Story Short",
    "lowpoly_intro": "Describe the story. The models write the narration, break it into shots, build every shot in Blender and cut it to the voiceover.",
    "lowpoly_prompt_label": "What is the story?",
    "lowpoly_prompt_ph": "A guy finds a letter in his mailbox saying he inherited a company he has never heard of",
    "lowpoly_hint": "It is meant to look cheap - blocky figures, boxes for rooms. Tell it what happens, not how it should look.",
    "lowpoly_len": "Length",
    "lowpoly_go": "Make the short",
    "lowpoly_missing": "Describe the story first.",
    "physics_go": "Render simulation",
    "physics_prompt_label": "Describe the scene",
    "physics_prompt_ph": "e.g. a steel weight dropped onto a wide, tall tower of bricks",
    "physics_prompt_hint": "Say what objects there are and what happens to them. The app writes the 3D scene, then shows you one frame before it renders.",
    "physics_prompt_missing": "Describe what should happen first.",
    "physics_brief_model": "Model that writes the scene brief",
    "physics_approve_t": "Does this shot look right?",
    "physics_approve_d": "One frame from the middle of the action, and the numbers it was rendered with. The full render takes a while, so check the framing before it starts.",
    "physics_approve_yes": "Render it",
    "physics_approve_no": "No, stop here",
    "mode_physics_t": "A Physics Simulation Short",
    "mode_physics_d": "A library of hand-built Blender scenes, tuned by you and solved by Bullet - dropped, crushed, rolled - with the impact sound on the exact frame. No AI video generation.",
    "mode_reddit_t": "A Reddit Story Video",
    "mode_reddit_d": "A Reddit-style story over Minecraft parkour with an AI voiceover.",
    "mode_longform_t": "A Longform Video",
    "mode_longform_d": "Paste a script - voiceover, timestamps, doodle images and the finished 16:9 video, fully automatic.",
    "mode_sfx_t": "Sound Effects",
    "mode_sfx_d": "Upload a finished Short and add editor SFX from the local library.",
    "mode_vfx_t": "Visual Effects",
    "mode_vfx_d": "Upload a Short - the agent adds animated red arrows plus fitting SFX.",
    "mode_captions_t": "Captions",
    "mode_captions_d": "Upload a video and burn in viral word-by-word captions, fully locally.",

    "send_script": "Please send me your script now.",
    "script_hint": "Type or paste your script, upload a .txt file, or load an existing project - or let the Script Creator write one from a topic.",
    "gen_topic_ph": "Topic for the Script Creator (leave empty = it picks a viral one)...",
    "gen_script": "Generate script",
    "gen_script_busy": "Writing your script...",
    "gen_script_done": "Script generated - review or edit it below.",
    "gen_script_err": "Script generation failed",
    "recent_scripts": "Recent scripts",
    "recent_generated": "Generated scripts",
    "recent_used": "Used in projects",
    "script_placeholder": "Paste or type your script...",
    "load_project": "Load a project",
    "upload_txt": "Upload .txt",
    "mark_hook_q": "Would you like to mark a hook?",
    "mark_hook_hint": "Select the opening line(s) in the script below, then mark them as the hook. The hook is spoken first, then a short pause.",
    "mark_hook": "Mark hook",
    "use_first_line": "Use first line",
    "clear_hook": "Clear hook",
    "skip_hook": "Continue without a hook",
    "hook_marked": "Hook marked",
    "no_hook": "No hook",
    "mark_impact": "Mark impact word",
    "impact_marked": "Impact word",
    "no_impact": "No impact word (SFX Master will guess)",
    "pipeline_q": "Which edit pipeline should cut this video?",
    "pipeline_v02": "v0.2 · Reference edit",
    "pipeline_v02_d": "1.5-3s cuts, escalation ladder, shock punchlines, color captions, precision SFX timing.",
    "pipeline_v01": "v0.1 · Classic",
    "pipeline_v01_d": "The proven original pipeline, unchanged.",
    "visual_source_q": "Where should the visuals come from?",
    "src_generate": "AI Generate",
    "src_generate_d": "Seedance / image models create every clip.",
    "src_scrape": "Bright Data TikTok",
    "src_scrape_d": "Real TikTok clips are discovered and fetched through Bright Data, then verified locally against your script.",
    "video_model": "Video model",
    "image_model": "Image model",
    "scrape_engine": "Scraping engine",
    "engine_v2": "Scrape V2 · Relevance-first",
    "script_relevancy": "Script relevancy",
    "scrape_sort": "Search result order",
    "custom_terms": "Add your own search terms (optional)",
    "term_placeholder": "Add a search term...",
    "scrape_sort": "Sort search results by",
    "background_music": "Background music",
    "bgm_none": "None (voice + SFX only)",
    "preview": "Preview",
    "connect_tiktok": "Connect TikTok",
    "connect_x": "Connect X",
    "connect_instagram": "Connect Instagram",
    "connect_higgsfield": "Connect Higgsfield",
    "reconnect": "Reconnect",
    "connected": "Connected",
    "not_connected": "Not connected",
    "busy": "Busy...",
    "reasoning_q": "Choose your reasoning model.",
    "voice_q": "Choose the narrator for your Short.",
    "tts_voice": "TTS voice",
    "tts_model": "TTS model",
    "fresh_take": "Fresh voice take (keep saved scrape)",
    "speaker_video": "Speaker video (talking-head hook)",
    "speaker_image": "Speaker image",
    "upload_image": "Upload image",
    "outputs_q": "Select the final layers to include.",
    "halt_after_speech": "Halt after speech generation",
    "sfx_amount": "SFX amount",
    "vfx_amount": "Visual FX amount",
    "add_visual_effects": "Add visual effects (arrows)",
    "presets": "Presets",
    "load_preset": "Load preset",
    "save_preset": "Save preset",
    "save_as": "Save as...",
    "overwrite": "Overwrite",
    "delete_preset": "Delete",
    "preset_applied": "Preset applied",
    "review_q": "Review your settings before we start.",
    "ready_create": "Ready to create",
    "create_short": "Create Short",
    "edit": "Edit",
    "back": "Back",
    "confirm": "Confirm",
    "continue": "Continue",
    "use_defaults": "Use defaults",
    "cancel": "Cancel",

    "project_started": "Your project has started.",
    "cancel_process": "Cancel process",
    "cancelling": "Cancelling...",
    "show_tech": "Show technical details",
    "hide_tech": "Hide technical details",
    "voiceover_ready": "Your voiceover is ready. Please review it before continuing.",
    "approve_continue": "Approve & continue",
    "new_take": "Generate a new take",
    "media_review": "Media review",
    "your_short_ready": "Your Short is ready.",
    "download": "Download",
    "downloaded_to": "Saved to your Downloads folder",
    "open_timeline": "Open Timeline Editor",
    "check_results": "Check results",
    "job_done": "Done",
    "job_error": "The run stopped with an error.",
    "job_cancelled": "The run was cancelled.",
    "retry": "Retry",

    "project_loaded": "Project loaded",
    "run_normal": "Normal run",
    "run_recut": "Recut existing",
    "run_new_web": "New web images",
    "run_regen_clips": "Regenerate clips",
    "run_recreate_hook": "Recreate hook",
    "continue_project": "Continue project",
    "rename": "Rename",
    "hide": "Hide",
    "unhide": "Unhide",
    "show_hidden": "Show hidden",
    "hide_hidden": "Back to visible projects",
    "no_projects": "Finished runs and generated project folders will appear here.",
    "no_timeline_yet": "This project has no timeline yet. Run the agent first - then come back to fine-tune and re-render.",
    "pick_project": "Choose a project",

    "sfx_upload_q": "Upload the finished video you want to enhance.",
    "vfx_upload_q": "Upload the video you want to enhance with visual effects.",
    "cap_upload_q": "Upload the video you want to caption.",
    "attach_video": "Attach a video...",
    "planning_agent": "Planning agent",
    "analysis_agent": "Analysis agent",
    "effect_amount": "Effect amount",
    "add_sfx": "Add Sound Effects",
    "add_arrows": "Add Intelligent Arrows",
    "add_captions": "Add Captions",
    "cap_max_words": "Max words per caption",
    "cap_center_y": "Caption vertical position",

    "viral_topic_q": "What topic should the Short be about?",
    "topic_placeholder": "Enter a topic...",
    "generate": "Generate",
    "reddit_intro": "I will find five Reddit-style stories - pick the one you want to turn into a video.",
    "find_stories": "Find 5 stories",
    "pick_story": "Pick a story to continue.",
    "longform_upload_q": "Upload a .txt prompt list - one image prompt per line.",
    "longform_script_q": "Paste your longform script - the app does the rest: voiceover, exact timestamps, one doodle image per timestamp (FLUX.2 Pro 16:9 on your Higgsfield account), then the finished video.",
    "longform_script_ph": "Paste your full script here...",
    "longform_tts": "Voiceover TTS",
    "longform_reasoning": "Reasoning model",
    "longform_halt_speech": "Halt after speech (approve each part)",
    # section captions for the longform production step (same blocks the script flow's Finish uses)
    "sec_narration": "Narration",
    "sec_director": "Director",
    "sec_speech": "Speech",
    "longform_voice_hint": "The narrator reads the whole video, so pick one that stays easy to "
                           "listen to. Preview plays it with this mode's calm delivery.",
    "longform_halt_hint": "Pause after the voiceover so you can approve or re-do each part "
                          "before the images are generated.",
    "lf_parts_ready": "Your voiceover parts are ready. Approve each part - declining re-generates that part.",
    "lf_part": "Part",
    "lf_approve": "Approve",
    "lf_decline": "Decline & regenerate",
    "lf_regenerating": "Regenerating...",
    "lf_approved": "Approved",
    "lf_parts_word": "parts",
    "lf_total": "total",
    "lf_approve_all": "Approve all",
    "lf_decline_all": "Decline & regenerate all",
    "create_longform": "Create longform video",
    "upload_txt_btn": "Upload prompt list (.txt)",
    "prompts_found": "prompts found",

    "composer_choice": "Choose an option above to continue.",
    "composer_disabled": "Use the controls above.",
    "type_message": "Type here...",
    "send": "Send",
    "uploading": "Uploading...",
    "upload_failed": "The uploaded file could not be read. Please choose another file.",
    "err_generic": "Something went wrong. Technical details are below.",
    "err_tiktok": "TikTok needs to be reconnected.",
    "err_no_job": "This job does not exist anymore.",
    "session_restored": "Session restored.",
    "draft_resumed": "You have an unfinished setup - continuing where you left off.",
    "start_over": "Start over",
    "open_assets": "Open assets",
    "new_chat": "New chat",
    "legacy_ui": "Legacy UI",
    "language_note": "",
}


# ------------------------------------------------------------------ payload manifest
# Field-parity contract with the OLD interface. Every /run submission from the chat UI is
# built from exactly these fields (same names, same on/off semantics as the legacy form).

RUN_MANIFEST = {
    # constant hidden fields the legacy form always posts
    "always": {
        "ui_form": "1",
        "mix_voice_in_final": "on",
        "use_visual_direction": "on",
    },
    # hidden "advanced" pairs: always posted, value "on" or "" depending on state
    "state_hidden": [
        "autonomous_director", "use_audio_timing", "use_llm_search",
        "use_llm_video_review", "auto_web_images", "generate_missing_sfx",
        "allow_gpt", "allow_seedance", "background_music_enabled",
    ],
    # text/select fields: always posted with their current value
    "text": [
        "loaded_project_source", "loaded_project_mode", "reasoning_model", "reasoning_mode",
        "vision_model",
        "pipeline_version",
        "clip_source", "video_model", "image_model", "scraping_engine", "bright_unlocker_zone", "v4_instagram_enabled", "v4_instagram_accounts", "v4_tiktok_login_fallback",
        "clip_short_format", "script_token_limit", "gen_topic", "others_action",
        "scrape_platforms",
        "scrape_terms", "scrape_sort", "scrape_time_budget", "background_music_choice", "sfx_amount", "vfx_amount", "script",
        "hook_text", "impact_word", "hook_keywords", "script_relevancy", "visual_script", "speaker_name",
        "tts_voice", "tts_model", "tts_voice_instruction", "tts_language",
        "tts_native_speed", "tts_volume", "tts_pitch", "tts_sample_rate",
        "tts_output_format", "speaker_image_path", "region", "candidate_url",
        "caption_active_style", "caption_active_color", "caption_base_color",
        "caption_box_color", "caption_stroke", "caption_size", "caption_uppercase_choice",
        "search_languages",
        "motion_loop_concept", "motion_loop_profile", "motion_loop_intensity", "motion_loop_speed", "motion_loop_pov", "motion_loop_surrealness",
    ],
    # checkbox fields: posted as "on" only when checked (HTML checkbox semantics)
    "check": [
        "out_web_images", "out_wikimedia", "out_gpt_images", "out_video_clips",
        "out_sfx", "out_transition_sfx", "out_background_music", "out_captions",
        "halt_after_speech", "force_regenerate", "enable_speaker_hook",
        "add_visual_effects",
        "multi_language_search",
        "motion_loop_mode", "motion_loop_seamless", "motion_loop_unlimited",
    ],
    # file fields
    "file": ["speaker_image_file", "motion_loop_first_frame"],
}

MASTER_MANIFESTS = {
    "sfx": {"action": "/sfx-run", "file": "video_file",
            "fields": ["reasoning_model", "sfx_amount"]},
    "visual": {"action": "/visual-run", "file": "video_file",
               "fields": ["reasoning_model", "vfx_amount"]},
    "captions": {"action": "/captions-run", "file": "video_file",
                 "fields": ["caption_max_words", "caption_center_y"]},
    "asmr": {"action": "/asmr-run", "file": "video_file",
             "fields": ["asmr_profile"]},
    # The only master that takes THREE files: an Action Edit is one Short made of clip1/2/3,
    # so it posts them under their own names instead of a single video_file.
    "actionedit": {"action": "/action-edit-run", "files": ["clip1", "clip2", "clip3"],
                   "fields": ["title", "overlay_text", "max_ramps"],
                   "check": ["hook_enabled", "loop_shaping"]},
    "longform": {"action": "/longform-run", "text": ["script"],
                 "fields": ["tts_model", "reasoning_model", "aspect"],
                 "check": ["halt_after_speech", "image_zoom"]},
}


# ------------------------------------------------------------------ legacy option extraction

_OPTIONS_CACHE = {"at": 0.0, "data": None}


# The narrators are named by their engine, not for a reader: "jess_ja_es_id_pt_en_zh" is Jess,
# and the tail is the six languages she speaks. Forty-one of those went into a dropdown, into the
# panel's corner label and into the bottom dock, so the one thing on the screen with a HUMAN name
# was the only thing wearing a machine id. The VALUE is untouched - it is the engine's contract -
# and only what the reader sees is written out.
_VOICE_LANGS = {"en": "EN", "zh": "ZH", "ja": "JA", "es": "ES", "id": "ID", "pt": "PT",
                "ko": "KO", "de": "DE", "fr": "FR", "it": "IT"}


def seed_voice_label(voice):
    parts = [p for p in str(voice or "").split("_") if p]
    name, langs, mixed = [], [], False
    for part in parts:
        if part == "mixed":
            mixed = True
        elif part in _VOICE_LANGS:
            langs.append(_VOICE_LANGS[part])
        elif not langs:
            name.append(part)          # "monkey_king_zh" is one narrator, not two
        else:
            langs.append(part.upper())
    if not name:
        return str(voice or "")
    shown = " ".join(w.capitalize() for w in name)
    if langs:
        shown += " · " + " ".join(langs)
    if mixed:
        shown += " · mixed"
    return shown


def _parse_select(html, name):
    m = re.search(r'<select[^>]*name="%s"[^>]*>(.*?)</select>' % re.escape(name), html, re.S)
    if not m:
        return []
    out = []
    for om in re.finditer(r'<option\s+value="([^"]*)"([^>]*)>(.*?)</option>', m.group(1), re.S):
        label = re.sub(r"<[^>]+>", "", om.group(3))
        label = re.sub(r"\s+", " ", label).replace("&mdash;", "-").replace("&amp;", "&").strip()
        out.append({"value": om.group(1), "label": label,
                    "selected": "selected" in om.group(2)})
    return out


def _parse_input_value(html, name, default=""):
    m = re.search(r'name="%s"[^>]*\svalue="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else default


def extract_legacy_options():
    """Pull every option list from the REAL legacy pages so the chat can never drift.
    Cached for 30s (the legacy pages re-render fast, but no need to do it per request)."""
    now = time.time()
    if _OPTIONS_CACHE["data"] and now - _OPTIONS_CACHE["at"] < 30:
        return _OPTIONS_CACHE["data"]
    import app  # lazy: app imports chat_ui at module load; both are fully loaded at request time

    form = app.form_page(clear=False).decode("utf-8", "replace")
    sfx = app.sfx_page().decode("utf-8", "replace")
    vis = app.visual_page().decode("utf-8", "replace")
    cap = app.caption_page().decode("utf-8", "replace")
    lf = app.longform_page().decode("utf-8", "replace")

    preset_fields = []
    pm = re.search(r"CUSTOM_PRESET_FIELDS\s*=\s*\[([^\]]*)\]", form)
    if pm:
        preset_fields = [s.strip().strip('"\'') for s in pm.group(1).split(",") if s.strip()]

    data = {
        "reasoning_model": _parse_select(form, "reasoning_model"),
        "video_model": _parse_select(form, "video_model"),
        "image_model": _parse_select(form, "image_model"),
        "tts_voice": _parse_select(form, "tts_voice"),
        # The legacy selector only renders the provider active at page creation. The prototype
        # must instead receive both real provider lists and switch them with the model.
        "tts_voice_gemini": [{"value": voice, "label": voice}
                             for voice in pipeline.GEMINI_TTS_VOICES],
        "tts_voice_seed": [{"value": voice, "label": seed_voice_label(voice)}
                           for voice in pipeline.SEED_SPEECH_TTS_VOICES],
        "tts_voice_inworld": [{"value": voice, "label": voice}
                              for voice in pipeline.INWORLD_TTS_VOICES],
        # Ready-made polish for a generated take, offered on the approval screen. Fixing the
        # take you have is both faster and cheaper than rolling the dice on another one.
        "voice_presets": __import__("voice_presets").preset_choices(),
        # Where each model alias really lands. The shell used to guess from the alias NAME and
        # offered Gemini narrators for jobs that run on ByteDance, where those names do not
        # exist - so every one of them fell back to the same stokie_en voice.
        "tts_provider": {alias: pipeline.tts_provider(alias)
                         for alias in list(pipeline.GEMINI_TTS_MODELS)
                         + list(pipeline.SEED_SPEECH_TTS_ALIASES)
                         + list(pipeline.INWORLD_TTS_ALIASES)},
        "seed_tts_languages": [{"value": code, "label": label}
                               for code, label in (("", "Auto language"), ("en", "English"),
                                                   ("ja", "Japanese"), ("de", "German"),
                                                   ("fr", "French"), ("es-mx", "Spanish (Mexico)"),
                                                   ("pt-br", "Portuguese (Brazil)"), ("ko", "Korean"),
                                                   ("zh", "Chinese"), ("id", "Indonesian"),
                                                   ("it", "Italian"))],
        "tts_model": _parse_select(form, "tts_model"),
        "sfx_amount": _parse_select(form, "sfx_amount"),
        "scrape_sort": _parse_select(form, "scrape_sort"),
        "master_reasoning": _parse_select(sfx, "reasoning_model"),
        "master_sfx_amount": _parse_select(sfx, "sfx_amount"),
        "vfx_reasoning": _parse_select(vis, "reasoning_model"),
        "vfx_amount": _parse_select(vis, "vfx_amount"),
        "longform_tts": _parse_select(lf, "tts_model"),
        "longform_reasoning": _parse_select(lf, "reasoning_model"),
        "caption_max_words": _parse_select(cap, "caption_max_words"),
        "caption_center_y": _parse_select(cap, "caption_center_y"),
        "preset_fields": preset_fields,
    }
    _OPTIONS_CACHE.update(at=now, data=data)
    return data


# ------------------------------------------------------------------ chat session persistence

def load_chat_state():
    try:
        if CHAT_STATE_PATH.exists():
            data = json.loads(CHAT_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_chat_state(data):
    if not isinstance(data, dict):
        return False
    try:
        data = dict(data)
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["version"] = CHAT_UI_VERSION
        tmp = CHAT_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(CHAT_STATE_PATH)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ sidebar JSON payloads

def _project_kind(slug, title):
    """Classify a project for the sidebar/asset overlay: a video run through the SFX or Visual
    (VFX) Master gets a labelled overlay; an ordinary generated/scraped project gets none."""
    s = (slug or "").lower()
    t = (title or "").lower()
    if s.startswith("sfxmaster") or "_sfx_enhanced" in s or t.startswith("sfx master"):
        return "sfx"
    if s.startswith("asmr_") or t.startswith("asmr sound"):
        return "asmr"
    if (s.startswith("visualmaster") or "_visual_enhanced" in s
            or t.startswith("visual master") or t.startswith("vfx master")):
        return "vfx"
    return ""


# Versioned for the same reason as app._SUMMARY_CACHE_FILE: this file is keyed by project edit
# times and cannot notice that the code building it has changed.
_PAYLOAD_CACHE_FILE = "_projects_payload_v2.json"
_PAYLOAD_LOCK = threading.Lock()
_PAYLOAD_REFRESHING = set()


def _payload_cache_path():
    import agent_core
    return agent_core.PROJECTS_DIR / _PAYLOAD_CACHE_FILE


def _read_payload_cache(key):
    try:
        data = json.loads(_payload_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(key) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) and entry.get("projects") is not None else None


def _write_payload_cache(key, payload):
    path = _payload_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[key] = payload
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError):
        pass


# A Sketch project has no title of its own, so its first line has to serve as one. It was the
# first 58 characters of the script, sliced flat: measured on the shelf, 48 of 235 titles were
# 55 characters or longer and 32 were cut in the middle of a word -
#
#     "Before your feet even touch the floor this morning, your h"
#     "Everyone talks about the Library of Alexandria like it van"
#
# and the Library is 235 rows deep with the title as its only index. A sentence that ends inside
# the budget is the best headline available; failing that the cut lands on a word and says so.
def headline(text, limit=58):
    words = " ".join(str(text or "").split()).strip()
    if not words:
        return ""
    if len(words) <= limit:
        return words
    stop = max(words.rfind(mark, 0, limit + 1) for mark in (". ", "! ", "? "))
    if stop >= limit // 2:
        return words[:stop + 1].strip()
    cut = words.rfind(" ", 0, limit)
    return (words[:cut] if cut >= limit // 2 else words[:limit]).rstrip(" ,;:-") + "…"


def projects_list_payload(show_hidden=False, limit=200):
    """The sidebar list, served from the last known good copy and refreshed behind it.

    Even with every summary cached, deciding whether a cache entry is still valid costs five
    stat calls per project - about 1700 of them across this library - and on a cold filesystem
    that alone measured 41.7 seconds. Nothing can make that first walk fast, so it stops being
    something the user waits for: the previous answer is returned immediately and a background
    thread rebuilds it for the next request.

    The very first load on a fresh install still walks the library, because there is nothing to
    show yet - after that the sidebar is instant.
    """
    key = "hidden" if show_hidden else "visible"
    cached = _read_payload_cache(key)
    if cached is not None:
        with _PAYLOAD_LOCK:
            already = key in _PAYLOAD_REFRESHING
            if not already:
                _PAYLOAD_REFRESHING.add(key)
        if not already:
            def _refresh():
                try:
                    fresh = _build_projects_list_payload(show_hidden=show_hidden, limit=limit)
                    _write_payload_cache(key, fresh)
                finally:
                    with _PAYLOAD_LOCK:
                        _PAYLOAD_REFRESHING.discard(key)
            threading.Thread(target=_refresh, name=f"projects-refresh-{key}",
                             daemon=True).start()
        return cached
    payload = _build_projects_list_payload(show_hidden=show_hidden, limit=limit)
    _write_payload_cache(key, payload)
    return payload


def _build_projects_list_payload(show_hidden=False, limit=200):
    """Metadata-only project list for the sidebar + assets grid (no full hydration)."""
    import app
    import agent_core
    projects_dir = agent_core.PROJECTS_DIR
    # An unfinished directory is not a failed project while a live job owns it. Resolve the
    # state once here so sidebar, launchpad and Projects & Assets always agree.
    active_project_slugs = set()
    with app.JOB_LOCK:
        for job in app.JOBS.values():
            if str(job.get("status") or "") not in {"running", "cancelling", "awaiting_approval"}:
                continue
            project_dir = job.get("project_dir")
            if project_dir:
                active_project_slugs.add(Path(project_dir).name)
    items = []
    if projects_dir.exists():
        dirs = [p for p in projects_dir.iterdir() if agent_core.is_project_dir(p)]
        # The edit time is computed once and handed to the cache: it is both the sort key and the
        # cache key, and recomputing it per project doubled the stat calls for nothing.
        # One cache lookup per project gives BOTH the summary and the edit time to sort by.
        # Computing the edit time separately re-listed every renders folder - 337 cold
        # directory listings, which was most of the 43 seconds.
        entries = [(p, *app.cached_project_entry(p)) for p in dirs]
        entries.sort(key=lambda row: row[2], reverse=True)
        for p, summary, _edited in entries[:limit]:
            try:
                hidden = app.is_project_hidden(p)
                if hidden and not show_hidden:
                    continue
                if (not hidden) and show_hidden:
                    continue
                s = summary
                video = s.get("video")
                has_video = bool(video and Path(video).exists())
                thumb = s.get("thumb")
                running = str(s.get("slug") or p.name) in active_project_slugs
                item = {
                    "slug": s.get("slug"),
                    "title": s.get("title"),
                    "edited": s.get("edited_at") or s.get("created_at") or "",
                    "failed": bool(s.get("failed")) and not running,
                    # Waiting for the user to bring generated clips back is a normal state
                    # of a dreamcore project, not a broken run.
                    "awaiting": bool(s.get("awaiting")) and not running,
                    "dreamcore": bool(app.dreamcore_state_of(s.get("project_dir"))),
                    "running": running,
                    "hidden": hidden,
                    "has_video": has_video,
                    "counters": {"web": s.get("web_images", 0), "gpt": s.get("gpt_images", 0),
                                 "clips": s.get("seedance", 0)},
                    "thumb_url": (app.link_for(thumb) if thumb and Path(str(thumb)).exists()
                                  and app.is_image_path(thumb) else ""),
                    "preview_kind": s.get("preview_kind") or "",
                    "video_url": (app.link_for(video) if has_video else ""),
                    "results_url": (app.view_for(video, "assets") if has_video
                                    else app.view_for(s.get("project_dir"), "assets")),
                    # a scrape clip-short opens its timeline WITHOUT a render (the timeline is its
                    # render step), so "Open timeline" must appear for an edit, not only a render
                    "has_timeline": bool(app.project_has_timeline_edit(s.get("slug"))),
                    # sidebar/asset overlay: "sfx" / "vfx" for a Master-processed upload, else "".
                    "kind": s.get("preview_kind") or _project_kind(s.get("slug"), s.get("title")),
                }
                items.append(item)
            except Exception:
                continue
    if not show_hidden:
        items.extend(longform_projects_payload(active_project_slugs))
        items.sort(key=lambda i: str(i.get("edited") or ""), reverse=True)
    hidden_count = 0
    try:
        hidden_count = (sum(1 for p in projects_dir.iterdir()
                            if agent_core.is_project_dir(p) and app.is_project_hidden(p))
                        if projects_dir.exists() else 0)
    except Exception:
        pass
    # One write per build, not per project: the cache only earns its keep on the NEXT
    # cold start, so writing it 337 times would cost more than it saves.
    app._save_summary_cache()
    return {"projects": items, "hidden_count": hidden_count, "showing_hidden": bool(show_hidden)}


def longform_projects_payload(active_project_slugs=()):
    """The longform mode's projects, which live in their own container (projects/_longform/<slug>)
    and so never appeared in the list at all - only the container did, and opening THAT rendered a
    clip project made of defaults.

    Same item shape as a clip project (the shell and its tests expect one schema), plus the script
    so the shell can drop you back into the flow with it: longform resumes by re-running the SAME
    script, and the pipeline then reuses the voiceover parts it already paid for. The script comes
    from state.json, NOT script.txt - Path.write_text mangles newlines on Windows and resume
    compares the script exactly, so a round-tripped copy would never match.
    """
    import app
    import longform_video
    out = []
    root = longform_video.OUT_ROOT
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        try:
            state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        script = str(state.get("script") or "")
        if not script:
            continue                      # nothing to resume with, so nothing to offer
        voice_name = Path(str(state.get("voiceover_file") or "voiceover.wav")).name
        voiceover_ready = longform_video._audio_done(d / voice_name)
        video = next(iter(sorted(d.glob("*.mp4"))), None)
        images = sorted(d.glob("img*.png"))
        _state, timed_frames = longform_video.frames_from_disk(d)
        missing_frames = sum(1 for frame in timed_frames if not frame.get("exists"))
        is_running = d.name in set(active_project_slugs or ())
        # the dedicated click-thumbnail wins over the first frame for the poster
        thumb = d / "thumbnail.png"
        poster = thumb if thumb.is_file() else (images[0] if images else None)
        title = headline(script) or d.name.replace("_", " ")
        out.append({
            "slug": d.name,
            "title": title,
            "edited": time.strftime("%Y-%m-%d %H:%M", time.localtime(app.project_edited_mtime(d))),
            # never "failed": an unfinished longform project is one waiting for you at a gate, and
            # its voiceover is reusable either way - a red overlay would just be wrong.
            "failed": bool(video and missing_frames and not is_running),
            "running": is_running,
            "hidden": False,
            "has_video": bool(video),
            "counters": {"web": 0, "gpt": len(images), "clips": 0},
            "thumb_url": app.link_for(poster) if poster else "",
            "preview_kind": "",
            "video_url": app.link_for(video) if video else "",
            "results_url": app.view_for(video, "assets") if video else app.view_for(d, "assets"),
            "has_timeline": False,        # longform has no timeline editor
            "kind": "longform",
            # longform-only extras the shell reads to reopen the flow
            "longform": True,
            "script": script,
            "tts_voice": str(state.get("voice") or ""),
            "tts_model": str(state.get("tts_model") or "pro"),
            "reasoning_model": str(state.get("reasoning_model") or ""),
            "reasoning_mode": str(state.get("reasoning_mode") or ""),
            "mascot_enabled": bool(state.get("mascot_enabled", False)),
            "halt_after_speech": bool(state.get("halt_after_speech", False)),
            "tts_voice_instruction": str(state.get("tts_voice_instruction") or ""),
            "tts_language": str(state.get("tts_language") or ""),
            "tts_native_speed": str(state.get("tts_native_speed") or "1"),
            "tts_volume": str(state.get("tts_volume") or "1"),
            "tts_pitch": str(state.get("tts_pitch") or "0"),
            "tts_sample_rate": str(state.get("tts_sample_rate") or "24000"),
            "tts_output_format": str(state.get("tts_output_format") or "mp3"),
            "voiceover_ready": voiceover_ready,
            "status": (f"Needs {missing_frames} missing frame(s)" if missing_frames else
                       "Done" if video else
                       "Voiceover ready" if (d / "voiceover.wav").exists() else
                       "Voiceover in progress"),
            "images": len(images),
            "missing_frames": missing_frames,
        })
    return out


def jobs_list_payload():
    """Active + recent jobs for the sidebar (from the in-memory JOBS registry)."""
    import app
    out = []
    with app.JOB_LOCK:
        for jid, job in list(app.JOBS.items()):
            proj = job.get("project_dir")
            out.append({
                "id": jid,
                "status": job.get("status", ""),
                "kind": job.get("job_kind", "run"),
                "created_at": job.get("created_at", 0),
                "project_slug": Path(proj).name if proj else "",
                "last_log": (job.get("logs") or [""])[-1][:120],
            })
    out.sort(key=lambda j: j.get("created_at", 0), reverse=True)
    return {"jobs": out[:30]}


# ------------------------------------------------------------------ shell page

def chat_shell_page(initial=None):
    """The standalone chat shell document. `initial` routes deep links:
    {view:'assets'} | {flow:'sfx'|'visual'|'captions'|'reddit'|'longform'}
    | {job:'<id>'} | {project:'<slug>'} | {new:True}"""
    import app
    initial = dict(initial or {})
    opts = extract_legacy_options()
    try:
        connections = {
            "tiktok": json.loads(app.tiktok_status_payload().decode("utf-8")),
            "instagram": json.loads(app.instagram_status_payload().decode("utf-8")),
            "higgsfield": json.loads(app.higgsfield_status_payload().decode("utf-8")),
        }
    except Exception:
        connections = {}
    boot = {
        "version": CHAT_UI_VERSION,
        "strings": UI_STRINGS,
        "options": opts,
        "reasoningConfig": reasoning_modes.public_config(),
        "manifest": {"run": RUN_MANIFEST, "masters": MASTER_MANIFESTS},
        "uiState": app.load_ui_state(),
        "connections": connections,
        "initial": initial,
        "voices_preview": "/voice-preview?voice=",
    }
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Shortslab</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" href="/static/app_icon.png" type="image/png">
<link rel="manifest" href="/manifest.webmanifest">
<script>(function(){{try{{var q=new URLSearchParams(location.search).get("design");var v=q==="v1"?"v1":"v2";document.documentElement.classList.add("design-"+v);localStorage.setItem("sl-design",v);}}catch(e){{document.documentElement.classList.add("design-v2");}}}})();</script>
<link rel="stylesheet" href="/static/chat-shell.css?v={_asset_ver()}">
<link rel="stylesheet" href="/static/design-v2.css?v={_asset_ver()}">
<link rel="stylesheet" href="/static/shell-window.css?v={_asset_ver()}">
</head>
<body>
<div id="app" class="app">
  <aside class="sidebar" id="sidebar" aria-label="Navigation">
    <div class="sb-head">
      <img class="sb-logo" src="/static/app_icon.png" alt="" width="34" height="34">
      <div class="sb-title"><b><span>SHORTS</span>LAB<i></i></b></div>
      <button type="button" class="sb-collapse" id="sb-collapse" title="{UI_STRINGS["collapse"]}" aria-label="{UI_STRINGS["collapse"]}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4" width="17" height="16" rx="2.5"></rect><path d="M9 4v16M15.5 9l-3 3 3 3"></path></svg>
      </button>
    </div>
    <nav class="sb-nav" id="sb-nav" aria-label="Sections"></nav>
    <div class="sb-section" id="sb-jobs-wrap" hidden>
      <div class="sb-cap">{UI_STRINGS["nav_jobs"]}</div>
      <div class="sb-jobs" id="sb-jobs"></div>
    </div>
    <div class="sb-section sb-projects-wrap">
      <div class="sb-cap">{UI_STRINGS["recent_projects"]}</div>
      <div class="sb-projects" id="sb-projects" aria-live="polite"></div>
    </div>
    <div class="sb-section sb-conns-wrap" id="sb-conns-wrap" tabindex="0">
      <div class="sb-cap sb-conns-cap">{UI_STRINGS["connections"]} <span class="sb-conns-count" id="sb-conns-count">0/4</span></div>
      <div class="sb-conns" id="sb-conns"></div>
    </div>
    <div class="sb-foot">
      <a class="sb-foot-btn sb-dev-btn" id="sb-dev" href="/dev-tools" title="Open local developer and trainer tools">dev</a>
      <label class="sb-theme-switch" for="theme-toggle" title="Switch between dark and light mode">
        <input type="checkbox" id="theme-toggle" role="switch" aria-label="Dark mode">
        <span class="sb-theme-track" aria-hidden="true"><i></i></span>
        <span class="sb-theme-label" id="theme-toggle-label">dark</span>
      </label>
      <label class="sb-proto-switch" for="prototype-toggle" title="Try the new Creator Launchpad interface">
        <input type="checkbox" id="prototype-toggle" role="switch" aria-label="Creator Launchpad prototype">
        <span class="sb-proto-track" aria-hidden="true"><i></i></span>
        <span class="sb-proto-label">prototype</span>
      </label>
      <span class="sb-version">{CHAT_UI_VERSION}</span>
    </div>
    <div class="sb-resize" id="sb-resize" title="Drag to resize the sidebar" aria-hidden="true"></div>
  </aside>
  <div class="sb-scrim" id="sb-scrim" hidden></div>

  <main class="canvas" id="canvas">
    <header class="topbar" id="topbar">
      <button type="button" class="tb-menu" id="tb-menu" aria-label="{UI_STRINGS["expand"]}" title="Open sidebar">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4" width="17" height="16" rx="2.5"></rect><path d="M9 4v16M12.5 9l3 3-3 3"></path></svg>
      </button>
      <div class="tb-ctx" id="tb-ctx"></div>
      <div class="tb-actions" id="tb-actions"></div>
    </header>
    <div class="chat-scroll" id="chat-scroll">
      <div class="chat" id="chat" aria-live="polite"></div>
    </div>
  </main>
</div>
<script id="chat-boot" type="application/json">{boot_json}</script>
<script src="/static/chat-shell.js?v={_asset_ver()}"></script>
<script src="/static/design-v2.js?v={_asset_ver()}"></script>
{app.heartbeat_script()}
</body>
</html>"""
    return html.encode("utf-8")
