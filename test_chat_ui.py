"""Offline tests for the chat-based Shortslab UI (no paid API calls; NO_PAID_API guard active).

Covers (migration spec sections 39/41/42):
- payload parity: the chat manifest covers exactly the legacy /run form fields
- option parity: chat options are extracted from the REAL legacy pages
- shell rendering for every deep-linked flow + assets + job views
- chat-state persistence roundtrip
- projects-list / jobs-list payload shape
- English-only audit of every new UI string / shell markup
- paid-API guard blocks agent_core + pipeline entrypoints
"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["SHORTSLAB_NO_PAID_API"] = "1"          # spec 42: tests run under the guard

import app                                          # noqa: E402
import chat_ui                                      # noqa: E402
import agent_core                                   # noqa: E402
import pipeline                                     # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


META_NAMES = {"viewport", "google"}                 # <meta name=...> artifacts, not form fields
# fields that exist in shared page chrome (preview modal etc.), not the create form itself
CHROME_NAMES = {"media_path"}


def legacy_form_fields():
    html = app.form_page(clear=False).decode("utf-8", "replace")
    # only fields inside the actual create <form>
    m = re.search(r'<form id="short-form".*?</form>', html, re.S)
    frag = m.group(0) if m else html
    names = set(re.findall(r'name="([a-zA-Z_0-9]+)"', frag))
    return names - META_NAMES - CHROME_NAMES


def manifest_fields():
    man = chat_ui.RUN_MANIFEST
    return (set(man["always"].keys()) | set(man["state_hidden"]) | set(man["text"])
            | set(man["check"]) | set(man["file"]))


def test_payload_parity():
    legacy = legacy_form_fields()
    chat = manifest_fields()
    missing = legacy - chat
    extra = chat - legacy
    check("run manifest covers every legacy form field", not missing, f"missing={sorted(missing)}")
    check("run manifest has no unknown fields", not extra, f"extra={sorted(extra)}")

    # masters parity: every legacy field name is used by the chat submit
    sfx = app.sfx_page().decode("utf-8", "replace")
    vis = app.visual_page().decode("utf-8", "replace")
    cap = app.caption_page().decode("utf-8", "replace")
    for name, html, fields in (
            ("sfx", sfx, ["video_file", "reasoning_model", "sfx_amount"]),
            ("visual", vis, ["video_file", "reasoning_model", "vfx_amount", "add_characters"]),
            ("captions", cap, ["video_file", "caption_max_words", "caption_center_y"])):
        for f in fields:
            check(f"{name} master keeps field {f}", f'name="{f}"' in html)


def test_option_extraction():
    opts = chat_ui.extract_legacy_options()
    check("reasoning models extracted (>=6)", len(opts["reasoning_model"]) >= 6,
          str(len(opts["reasoning_model"])))
    vals = [o["value"] for o in opts["reasoning_model"]]
    for expect in ("anthropic/claude-opus-4.8", "openai/gpt-5.5",
                   "google/gemini-3.5-flash", "google/gemini-3.1-pro-preview"):
        check(f"reasoning option {expect}", expect in vals)
    check("video models extracted (>=5)", len(opts["video_model"]) >= 5)
    check("image models extracted (>=2)", len(opts["image_model"]) >= 2)
    check("all 30 TTS voices extracted", len(opts["tts_voice"]) >= 30, str(len(opts["tts_voice"])))
    check("sfx amount has low/medium/high",
          {o["value"] for o in opts["sfx_amount"]} == {"low", "medium", "high"})
    check("vfx amount has low/medium/high",
          {o["value"] for o in opts["vfx_amount"]} == {"low", "medium", "high"})
    check("preset fields extracted", len(opts["preset_fields"]) >= 20, str(len(opts["preset_fields"])))
    check("longform model options extracted", len(opts["longform_model"]) >= 1)
    check("caption selects extracted", len(opts["caption_max_words"]) >= 1
          and len(opts["caption_center_y"]) >= 1)


def test_shell_renders():
    for initial, marker in (
            ({}, "chat-boot"),
            ({"view": "assets"}, '"view": "assets"'),
            ({"flow": "sfx"}, '"flow": "sfx"'),
            ({"flow": "visual"}, '"flow": "visual"'),
            ({"flow": "captions"}, '"flow": "captions"'),
            ({"flow": "viraltrans"}, '"flow": "viraltrans"'),
            ({"flow": "reddit"}, '"flow": "reddit"'),
            ({"flow": "longform"}, '"flow": "longform"'),
            ({"job": "12345"}, '"job": "12345"'),
            ({"project": "some_slug"}, '"project": "some_slug"'),
            ({"new": True}, '"new": true')):
        html = chat_ui.chat_shell_page(initial).decode("utf-8")
        check(f"shell renders initial={initial}", marker in html and "chat-shell.js" in html)
    html = chat_ui.chat_shell_page({}).decode("utf-8")
    # the Legacy UI sidebar button was removed; the ?legacy_ui=1 route still works server-side
    leg = app.form_page().decode("utf-8", "replace")   # legacy form still renders
    check("legacy form route still available", 'id="short-form"' in leg)
    check("shell is standalone document", html.startswith("<!DOCTYPE html>"))


def test_chat_state_roundtrip():
    state = {"flow": "script", "step": "voice", "values": {"script": "Hello", "tts_voice": "Charon"},
             "completed": ["script", "hook", "source", "reasoning"], "jobId": None}
    check("chat-state save", chat_ui.save_chat_state(state))
    loaded = chat_ui.load_chat_state()
    check("chat-state load returns same flow/step",
          loaded.get("flow") == "script" and loaded.get("step") == "voice")
    check("chat-state preserves values",
          (loaded.get("values") or {}).get("tts_voice") == "Charon")


def test_projects_jobs_payloads():
    d = chat_ui.projects_list_payload()
    check("projects-list payload shape", isinstance(d.get("projects"), list)
          and "hidden_count" in d)
    if d["projects"]:
        p = d["projects"][0]
        for k in ("slug", "title", "edited", "failed", "has_video", "counters",
                  "results_url", "has_timeline"):
            check(f"project item has {k}", k in p)
    j = chat_ui.jobs_list_payload()
    check("jobs-list payload shape", isinstance(j.get("jobs"), list))


GERMAN_MARKERS = [
    "Erstellen", "Einstellungen", "Vorschau", "Projekt ", "Stimme", "Hintergrund",
    "hochladen", "Herunterladen", "Weiter", "Zurücл", "Zurück", "Abbrechen", "Fertig",
    "Bewertung", "Szene", "Lauf ", "Verbinden", "ausgeblendet", "Titel",
]


def test_english_only():
    blob = json.dumps(chat_ui.UI_STRINGS, ensure_ascii=False)
    hits = [g for g in GERMAN_MARKERS if g in blob]
    check("UI strings are English-only", not hits, str(hits))
    shell = chat_ui.chat_shell_page({}).decode("utf-8")
    hits2 = [g for g in GERMAN_MARKERS if g in shell]
    check("shell markup is English-only", not hits2, str(hits2))
    js = (chat_ui.ROOT / "static" / "chat-shell.js").read_text(encoding="utf-8")
    hits3 = [g for g in GERMAN_MARKERS if g in js]
    check("chat-shell.js is English-only", not hits3, str(hits3))


def test_no_ai_in_chat():
    js = (chat_ui.ROOT / "static" / "chat-shell.js").read_text(encoding="utf-8")
    for bad in ("llm.wavespeed", "/v1/chat/completions", "anthropic.com/v1", "openai.com/v1",
                "generativelanguage.googleapis"):
        check(f"chat JS contains no model endpoint ({bad})", bad not in js)
    py = (chat_ui.ROOT / "chat_ui.py").read_text(encoding="utf-8")
    check("chat_ui.py never calls post_json_url", "post_json_url" not in py)


def test_paid_guard():
    try:
        agent_core.post_json_url("https://llm.wavespeed.ai/v1/chat/completions", {"model": "x"})
        check("guard blocks agent_core.post_json_url", False)
    except agent_core.PaidAPIBlockedError:
        check("guard blocks agent_core.post_json_url", True)
    except Exception as exc:
        check("guard blocks agent_core.post_json_url", False, repr(exc))
    try:
        pipeline.api_key()
        check("guard blocks pipeline.api_key", False)
    except RuntimeError as exc:
        check("guard blocks pipeline.api_key", "NO_PAID_API" in str(exc), str(exc))


def test_legacy_routes_still_render():
    for name, fn in (("form", lambda: app.form_page()), ("assets", lambda: app.assets_page()),
                     ("sfx", app.sfx_page), ("visual", app.visual_page),
                     ("captions", app.caption_page), ("viraltrans", app.viraltrans_page),
                     ("reddit", app.reddit_page), ("longform", app.longform_page)):
        try:
            out = fn()
            check(f"legacy page {name} still renders", bool(out and len(out) > 500))
        except Exception as exc:
            check(f"legacy page {name} still renders", False, repr(exc))


if __name__ == "__main__":
    for t in (test_payload_parity, test_option_extraction, test_shell_renders,
              test_chat_state_roundtrip, test_projects_jobs_payloads, test_english_only,
              test_no_ai_in_chat, test_paid_guard, test_legacy_routes_still_render):
        print(f"\n== {t.__name__} ==")
        t()
    print(f"\n{'ALL PASSED' if not FAIL else str(FAIL) + ' FAILED'} ({PASS} passed)")
    sys.exit(1 if FAIL else 0)
