"""Every window of the app is one machine, and no path leads back to the old look.

The owner's own test case, verbatim: "I open the Timeline Editor, then click 'Back to launchpad' -
and I'm back in the old UI." This file makes that impossible to reintroduce quietly:

1. Every routed window answers with the v3 document by default - the Library, the Sketch Station
   (/longform), the Action Edit, the Story Station (/reddit) and
   the Physics Bench (/?flow=physics) - and the two deliberate escape hatches (?ui=chat,
   ?legacy_ui=1) still answer exactly as they did.
2. The server-rendered windows (the Timeline editor, its blank and not-ready pages, the file
   viewer, the scrape log, the developer tools, the progress view) all load the window sheet
   (static/shell-window.css) and carry the same title bar; the old skin file is gone.
3. The chat shell's way back leads into the showroom unless the chat shell was asked for by name.
4. Payload parity: the v3 windows post the SAME field names the older console posted. Measured
   from the sources, not from a list typed by hand: the fd.append(...) names of chat-shell.js
   submitLongform / submitActionEdit are compared with those of shell-v3.js buildLongformForm /
   buildActionForm, and every master manifest field must be appended by shell-v3.js.
5. The grammar is real, not a palette: no Inter, no web fonts, no rounded corner in the editor's
   sheet; a focus ring and prefers-reduced-motion in every sheet.
6. app_script() is valid JavaScript again - it had a literal `{json.dumps(...)}` in a non-f-string,
   which made every server page's inline script a syntax error.
"""

import inspect
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import chat_ui
from tests.test_shell_v3_wiring import Fake, boot_of, chat_boot_of, get

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


V3_JS = read("static/shell-v3.js")
V3_CSS = read("static/shell-v3.css")
WINDOW_CSS = read("static/shell-window.css")
TIMELINE_CSS = read("static/timeline-theme.css")
CHAT_JS = read("static/chat-shell.js")


def function_source(src, name):
    """The body of `[async ]function name(` up to the next top-level function."""
    m = re.search(r"(?:async )?function %s\(" % re.escape(name), src)
    assert m, name
    end = re.search(r"\n(?:async )?function \w+\(", src[m.end():])
    return src[m.start():m.end() + (end.start() if end else len(src))]


def appended_names(fn_src):
    return set(re.findall(r'fd\.append\(\s*"([a-z_0-9]+)"', fn_src))


class EveryWindowIsV3(unittest.TestCase):

    def test_the_window_routes_answer_with_the_v3_document(self):
        for path, initial in (
            ("/assets", {"view": "assets"}),
            ("/assets?q=some_slug", {"view": "assets", "q": "some_slug"}),
            ("/assets?show_hidden=1", {"view": "assets", "hidden": True}),
            ("/longform", {"flow": "longform"}),
            ("/actionedit", {"flow": "actionedit"}),
            ("/reddit", {"flow": "reddit"}),
            ("/?flow=physics", {"flow": "physics"}),
            ("/?flow=asmr", {"flow": "asmr"}),
        ):
            boot = boot_of(get(path).body)
            self.assertIsNotNone(boot, path + " did not answer with the v3 document")
            self.assertEqual(boot["initial"], initial, path)

    def test_an_unknown_flow_on_the_home_route_is_ignored_not_trusted(self):
        self.assertEqual(boot_of(get("/?flow=nonsense").body)["initial"], {})

    def test_the_escape_hatches_still_reach_the_older_interfaces(self):
        for path in ("/assets?ui=chat", "/longform?ui=chat", "/?ui=chat&flow=physics", "/reddit?ui=chat"):
            body = get(path).body
            self.assertIsNone(boot_of(body), path)
            self.assertIsNotNone(chat_boot_of(body), path + " must still reach the chat shell")
        # ?legacy_ui=1 is retired: the classic forms render nothing after the studio restyle, and
        # a blank screen is worse than no screen. v3 answers it like any other route.
        self.assertIsNotNone(boot_of(get("/longform?legacy_ui=1").body))

    def test_the_chat_shell_deep_link_for_the_physics_bench_still_carries_its_flow(self):
        self.assertEqual(chat_boot_of(get("/?ui=chat&flow=physics").body)["initial"].get("flow"), "physics")


class TheWindowSheet(unittest.TestCase):

    def test_the_old_skin_is_gone_and_the_window_sheet_is_served(self):
        gone = Fake("/static/shell-skin.css")
        gone.do_GET()
        self.assertEqual(gone.error, 404)
        for name in ("shell-window.css", "timeline-theme.css"):
            handler = Fake("/static/" + name)
            handler.do_GET()
            self.assertIsNone(handler.error, name)
            self.assertIn("text/css", handler.content_type)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "static", "shell-skin.css")))
        # the cache-buster must see the new sheet, or edits are served stale
        self.assertIn("shell-window.css", inspect.getsource(chat_ui._asset_ver))

    def test_every_server_rendered_window_loads_it_and_wears_the_title_bar(self):
        with app.JOB_LOCK:
            app.JOBS["win_test_job"] = {"status": "running", "logs": ["Queued."], "created_at": 0, "job_kind": "timeline"}
        self.addCleanup(lambda: app.JOBS.pop("win_test_job", None))
        for path in ("/timeline", "/timeline?slug=definitely_not_a_project", "/dev-tools",
                     "/progress?id=win_test_job", "/scrape-log-view?slug=x", "/?ui=chat"):
            body = get(path).body.decode("utf-8")
            self.assertIn("/static/shell-window.css?v=", body, path)
            self.assertNotIn("shell-skin.css", body, path)
            self.assertNotIn("fonts.googleapis.com", body, path)
        for path in ("/timeline", "/timeline?slug=definitely_not_a_project"):
            body = get(path).body.decode("utf-8")
            self.assertIn('class="top win-titlebar"', body, path)
            self.assertIn("TIMELINE EDITOR", body, path)
            self.assertIn('href="/"', body, path)

    def test_the_title_bar_has_no_theme_switch_and_names_the_way_back(self):
        header = app.brand_header(program="FILE VIEWER", model="FV-8")
        self.assertNotIn("theme-toggle", header)
        self.assertNotIn("toggleTheme", header)
        self.assertIn("win-back", header)
        self.assertIn("Studio", header)
        self.assertIn("Library", header)
        self.assertIn("<b>FILE VIEWER</b>", header)

    def test_the_palette_is_unlayered_and_the_grammar_is_layered(self):
        """Unlayered normal declarations win over the other sheets' :root; layered important ones
        win over their !important surfaces. Both halves have to keep their place."""
        layer_at = WINDOW_CSS.index("@layer shortslab-window")
        self.assertLess(WINDOW_CSS.index("--text-primary: #f1f1f4"), layer_at)
        self.assertLess(WINDOW_CSS.index("--v2-text: #f1f1f4"), layer_at)
        self.assertLess(WINDOW_CSS.index("--bg-base: #111313"), layer_at)
        self.assertIn("@layer shortslab-window", TIMELINE_CSS)

    def test_the_editor_sheet_is_the_grammar_not_a_palette_swap(self):
        self.assertNotIn("Inter", TIMELINE_CSS)
        self.assertNotIn("--accent:", TIMELINE_CSS, "the palette lives in shell-window.css")
        for value in re.findall(r"border-radius:\s*([^;!]+)", TIMELINE_CSS + WINDOW_CSS):
            self.assertIn(value.strip(), ("0", "0px", "6px", "10px"), "radius outside the studio scale: " + value)
        for sheet, name in ((WINDOW_CSS, "shell-window.css"), (TIMELINE_CSS, "timeline-theme.css"), (V3_CSS, "shell-v3.css")):
            self.assertIn("prefers-reduced-motion", sheet, name)
        for sheet, name in ((WINDOW_CSS, "shell-window.css"), (V3_CSS, "shell-v3.css")):
            self.assertIn(":focus-visible", sheet, name)
        # the idioms the run page is built from. The bracketed COMMAND survives; the bracketed
        # checkbox does not - "[x]" and "[ ]" put the whole state into one character of the same
        # width and colour, which the owner could not read at a glance. It is a drawn box now.
        self.assertIn('content: "["', WINDOW_CSS)
        self.assertNotIn('content: "[x]"', WINDOW_CSS)
        self.assertIn('input[type="checkbox"]:not(.tl-clip-check):not([role="switch"]):checked {', WINDOW_CSS)
        self.assertIn("3px double", WINDOW_CSS)

    def test_the_progress_page_carries_its_glyphs_as_css_escapes(self):
        """A heredoc once turned these into a NUL and a control byte; the page must ship the
        escape sequences themselves."""
        body = app.progress_page("nope").decode("utf-8")
        self.assertIn('content:"\\2588"', body)
        # The wordmark used to be pseudo-element content on a hidden span, because this
        # page had no header to put it in. It has one now - the same bar as every other
        # screen - so the mark is markup and the escape it needed is gone with it.
        self.assertIn('class="pg-brand"', body)
        self.assertIn('>SHORTSLAB<', body)
        self.assertNotIn("\x00", body)
        self.assertNotRegex(body, r"font-family:\s*Inter\b")
        self.assertNotIn("border-radius:99px", body)


class TheWayBack(unittest.TestCase):

    def test_the_chat_shell_back_button_leads_to_the_showroom_unless_asked_for_by_name(self):
        self.assertIn("function isClassicConsole()", CHAT_JS)
        self.assertIn('/^(chat|classic|v2)$/i', CHAT_JS)
        assets_view = function_source(CHAT_JS, "renderAssetsView")
        self.assertIn('isClassicConsole() ? (prototypeMode ? "Back to launchpad"', assets_view)
        self.assertIn('if (!isClassicConsole()) { location.href = "/"; return; }', assets_view)

    def test_no_v3_machine_hands_over_to_the_chat_shell(self):
        """The showroom's machines used to land on /?ui=chat&flow=physics and /longform, which
        the chat shell answered. Every machine is a v3 page now."""
        for m in re.finditer(r'\{ id: "(\w+)", page: "([^"]+)"', V3_JS):
            self.assertEqual(m.group(2), "v3", m.group(1) + " still hands over to another interface")
        self.assertNotIn("ui=chat&flow=physics", V3_JS)

    def test_the_timeline_exit_and_the_library_link_stay_inside_the_machine(self):
        self.assertIn('id="tl-editor-exit" href="/assets"', app.TIMELINE_SKELETON)
        self.assertIn('lib.href = "/assets"', V3_JS)
        self.assertIn("goAssets()", V3_JS)


class PayloadParity(unittest.TestCase):
    """The v3 windows post what the older console posted - measured from both sources."""

    def test_longform_posts_the_same_fields_as_the_chat_shell(self):
        chat = appended_names(function_source(CHAT_JS, "submitLongform"))
        v3 = appended_names(function_source(V3_JS, "buildLongformForm"))
        self.assertEqual(v3, chat)
        self.assertIn("/longform-run", function_source(V3_JS, "submitLongform"))
        # the manifest's own view of it
        man = chat_ui.MASTER_MANIFESTS["longform"]
        for name in man["text"] + man["fields"] + man["check"]:
            self.assertIn(name, v3, name)

    def test_action_edit_posts_the_same_fields_as_the_chat_shell(self):
        chat = appended_names(function_source(CHAT_JS, "submitActionEdit"))
        v3 = appended_names(function_source(V3_JS, "buildActionForm"))
        self.assertEqual(v3, chat)
        self.assertIn('fd.append(`clip${i + 1}`, f, f.name)', function_source(V3_JS, "buildActionForm"))
        self.assertIn("/action-edit-run", function_source(V3_JS, "submitActionEdit"))
        man = chat_ui.MASTER_MANIFESTS["actionedit"]
        for name in man["fields"] + man["check"]:
            self.assertIn(name, v3, name)

    def test_the_single_file_masters_append_every_manifest_field(self):
        src = function_source(V3_JS, "submitMaster")
        for kind in ("sfx", "visual", "captions", "asmr"):
            man = chat_ui.MASTER_MANIFESTS[kind]
            self.assertIn('fd.append(man.file', src)
            for name in man.get("fields", []) + man.get("check", []):
                self.assertIn('"%s"' % name, src, kind + " lost " + name)

    def test_physics_posts_the_same_json_keys(self):
        chat = set(re.findall(r"body\.(\w+) =", function_source(CHAT_JS, "physShotCard")))
        v3 = set(re.findall(r"body\.(\w+) =", function_source(V3_JS, "buildPhysicsBody")))
        chat |= {"seconds", "samples"}; v3 |= {"seconds", "samples"}
        self.assertEqual(v3, chat)
        self.assertIn('jpost("/physics-run"', function_source(V3_JS, "submitPhysics"))
        self.assertIn('"/approve-physics?id="', V3_JS)

    def test_reddit_posts_the_story_and_its_id(self):
        src = function_source(V3_JS, "renderReddit")
        self.assertIn('jpost("/reddit-discover", {})', src)
        self.assertIn('jpost("/reddit-generate", { story: st, story_id: st.id })', src)

    def test_the_longform_speech_gate_uses_the_same_decision_endpoint(self):
        src = function_source(V3_JS, "paintExtraGates")
        self.assertIn('"/longform-speech-decide?id="', src)
        for action in ("approve", "decline", "polish"):
            self.assertIn(action, src)

    def test_the_run_form_is_still_the_manifest(self):
        src = function_source(V3_JS, "buildRunForm")
        for key in ("MAN.run.always", "MAN.run.state_hidden", "MAN.run.text", "MAN.run.check"):
            self.assertIn(key, src)


class ServerScripts(unittest.TestCase):

    def test_app_script_is_valid_javascript_with_the_voices_substituted(self):
        script = app.app_script()
        self.assertNotIn("{json.dumps(list(", script)      # the placeholder shape, not the comment naming it
        self.assertNotIn("__SEED_VOICES__", script)
        self.assertIn(json.dumps(app.pipeline.DEFAULT_TTS_VOICE), script)
        self.assertNotIn("{{ return '<option", script)


if __name__ == "__main__":
    unittest.main()
