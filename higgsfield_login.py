"""Higgsfield login-based image generation - the backend for the "Longform Images" mode.

No API keys. The user logs in to their Higgsfield account ONCE inside a Chromium window
we control (a dedicated, persistent browser profile), and from then on we drive that
logged-in session to generate 16:9 images with FLUX.2 Pro (unlimited on the user's plan),
one prompt at a time (up to a few in flight at once), and download each finished image
named by the timestamp that prefixes its prompt (e.g. "[0:06]" -> "[0-06].png").

Why a dedicated profile and not the user's everyday Chrome: same reason as tiktok_login -
we own the browser profile so the persistent Clerk/Higgsfield session cookies survive
between runs and we never have to touch the user's encrypted Chrome cookie store.

This mirrors tiktok_login.py exactly (persistent profile, off-screen background session via
its CDP window helpers, one dedicated Playwright worker thread) and REUSES its window
helpers so a scrape and an image run share the same "nothing visible in the taskbar" polish.

Public surface used by app.py / longform_images.py:
    available()                         -> is Playwright importable
    is_ready()                          -> have we logged in at least once
    status()                            -> dict for the UI
    login(status_cb, timeout_s)         -> headed one-time login, returns bool
    logout()                            -> wipe the saved session
    ensure_session(status_cb)           -> open/reuse the session on the worker thread (bool)
    generate_sync(prompt, out_path,...) -> generate ONE image on the worker thread (path|None)
    generate_batch(items, out_dir, ...) -> generate MANY (concurrency in flight), yields per item
    close_session()                     -> tear the shared session down (on the worker thread)

    parse_prompt_lines(text)            -> [{index, timestamp, key, prompt}] from a prompt .txt
    sanitize_timestamp(ts)              -> Windows-safe filename stem for a "[m:ss]" timestamp

CALIBRATION NOTE: the exact create-page URL, the prompt input, the model/aspect controls and
the "generate" button live behind the user's login, so the click-path constants below
(CREATE_URL, the selector lists) are best-effort and are meant to be locked against the LIVE
Higgsfield UI once the user has connected (run `python higgsfield_login.py probe`). The
result image itself is captured from the network (any generated image URL Higgsfield serves),
which is far more robust than scraping a specific DOM node.
"""

import concurrent.futures
import os
import re
import json
import time
import threading
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception:                       # playwright not installed
    sync_playwright = None

# Reuse tiktok_login's proven window plumbing (off-screen park / on-screen place / taskbar hide /
# stale-profile kill). Importing it is cheap and keeps ONE implementation of the CDP dance.
try:
    import tiktok_login as _tt
except Exception:                       # pragma: no cover - tiktok_login always ships alongside
    _tt = None

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("HIGGSFIELD_PROFILE_DIR") or (ROOT / "higgsfield-profile"))
_STATE_DIR = ROOT / "generated_assets" / "higgsfield"
_MARKER = _STATE_DIR / "logged_in.json"

# Higgsfield authenticates through Clerk; a signed-in session sets these cookies. `__client_uat`
# is "0" when signed out and a unix timestamp when signed in, and `__session` is the JWT.
SESSION_COOKIE_NAMES = ("__session", "__client_uat", "hf_session", "authjs.session-token")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_LOCALE = os.environ.get("HIGGSFIELD_LOCALE", "en-US").strip() or "en-US"

# --- calibration points (override via env; locked against the live UI after Connect) ----------
LOGIN_URL = os.environ.get("HIGGSFIELD_LOGIN_URL") or "https://higgsfield.ai/"
CREATE_URL = os.environ.get("HIGGSFIELD_CREATE_URL") or "https://higgsfield.ai/ai/image?model=flux_2"
DEFAULT_MODEL = os.environ.get("HIGGSFIELD_MODEL") or "FLUX.2 Pro"
DEFAULT_ASPECT = os.environ.get("HIGGSFIELD_ASPECT") or "16:9"
# an image URL "looks generated" when it points at Higgsfield's own media/CDN storage. Kept broad
# on purpose - the newest large image that appears AFTER we click generate is the one we keep.
_GEN_URL_RE = re.compile(
    r"(higgsfield|hgsfld|cloudfront|storage\.googleapis|r2\.cloudflarestorage|amazonaws|"
    r"supabase|blob\.core\.windows)", re.I)
_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|webp)(\?|$)", re.I)

_HEADLESS = (os.environ.get("HIGGSFIELD_HEADLESS", "0").strip().lower() in ("1", "true", "yes"))

_LOCK = threading.Lock()
_SESSION = [None]                       # the shared Session; ONLY touched on the worker thread
_EXECUTOR = None
_EXEC_LOCK = threading.Lock()


# --------------------------------------------------------------------------- helpers

def _status(cb, msg):
    if cb:
        try:
            cb(msg)
        except Exception:
            pass
    else:
        print(msg)


def available():
    return sync_playwright is not None


def _has_session_cookie(cookies):
    for c in cookies or []:
        if "higgsfield" not in str(c.get("domain") or "").lower():
            continue
        name = str(c.get("name") or "")
        val = str(c.get("value") or "").strip()
        if name in ("__client_uat",):
            if val and val != "0":
                return True
        elif name in SESSION_COOKIE_NAMES and val:
            return True
    return False


def is_ready():
    """True if Playwright is available and a Higgsfield login has been saved at least once."""
    return bool(available() and _MARKER.exists() and PROFILE_DIR.exists())


def status():
    info = {"available": available(), "ready": is_ready(),
            "profile": str(PROFILE_DIR), "logged_in_at": None}
    try:
        if _MARKER.exists():
            info["logged_in_at"] = json.loads(_MARKER.read_text("utf-8")).get("at")
    except Exception:
        pass
    return info


def _write_marker():
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _MARKER.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S")}), "utf-8")


# --------------------------------------------------------------- prompt-file parsing

_TS_RE = re.compile(r"^\s*[\[\(]?\s*(\d{1,2})\s*[:.]\s*(\d{1,2})\s*[\]\)]?\s*(.*)$")


def sanitize_timestamp(ts):
    """A "[m:ss]" timestamp -> a Windows-safe filename stem, e.g. "[0:06]" -> "[0-06]".
    Windows forbids ':' in filenames; we keep the visible brackets the user asked for."""
    stem = str(ts or "").strip()
    stem = stem.replace(":", "-").replace(".", "-")
    stem = re.sub(r'[<>:"/\\|?*]', "-", stem)          # any remaining illegal char
    stem = stem.strip().strip(".")
    return stem or "image"


def parse_prompt_lines(text):
    """Parse a prompt .txt (one prompt per line, each starting with a "[m:ss]" timestamp).

    Returns a list of {index, timestamp, key, prompt}:
      timestamp - the normalized "[m:ss]" label (for display),
      key       - the sanitized filename stem (e.g. "[0-06]"),
      prompt    - the prompt text WITH the timestamp prefix stripped off.
    Blank lines are skipped. Lines without a leading timestamp still generate, keyed by their
    line number so nothing is silently dropped."""
    items = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _TS_RE.match(line)
        if m:
            mm, ss, rest = m.group(1), m.group(2), (m.group(3) or "").strip()
            label = f"[{int(mm)}:{int(ss):02d}]"
            prompt = rest
        else:
            label = f"[line{len(items) + 1}]"
            prompt = line
        if not prompt:
            continue
        items.append({
            "index": len(items),
            "timestamp": label,
            "key": sanitize_timestamp(label),
            "prompt": prompt,
        })
    return items


# --------------------------------------------------------------------------- login

def login(status_cb=None, timeout_s=300):
    """Open a real Chromium window, let the user sign in to Higgsfield, and persist the
    session into PROFILE_DIR. Blocks until a session cookie appears or timeout. Headed."""
    if not available():
        _status(status_cb, "Higgsfield login: Playwright is not installed (pip install playwright "
                           "&& playwright install chromium).")
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # Kill any zombie Chromium still holding this profile - otherwise the "new" login window
    # delegates to it and inherits its off-screen position (see tiktok_login for the full story).
    if _tt is not None:
        _kill_stale()
    _status(status_cb, "Opening a Chromium window - log in to your Higgsfield account in it. "
                       "This window stays connected; you only do this once.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1360, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--window-position=120,60"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if _tt is not None:
                _tt._place_window_visible(ctx, 120, 60)   # force ON-SCREEN even if a run parked it off
            try:
                page.goto(LOGIN_URL, timeout=60000)
            except Exception:
                pass
            deadline = time.time() + max(30, int(timeout_s))
            ok = False
            while time.time() < deadline:
                try:
                    if _has_session_cookie(ctx.cookies()):
                        ok = True
                        break
                except Exception:
                    pass
                if not ctx.pages:           # user closed the window
                    break
                time.sleep(2.0)
            if ok:
                _write_marker()
                _status(status_cb, "Higgsfield login captured - the session is saved. You can close "
                                   "the window; future image runs reuse it automatically.")
            else:
                _status(status_cb, "Higgsfield login timed out / window closed before sign-in completed.")
            return ok
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def logout():
    """Forget the saved session (delete profile + marker)."""
    import shutil
    close_session()
    for target in (PROFILE_DIR, _MARKER):
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except Exception:
            pass
    return True


def _kill_stale():
    """Force-kill any Chromium still holding PROFILE_DIR (reuses tiktok_login's routine with our
    profile path)."""
    if _tt is None or os.name != "nt":
        return
    try:
        import subprocess
        marker = str(PROFILE_DIR).replace("'", "''")
        cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" -ErrorAction Stop | "
            f"Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*{marker}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


# --------------------------------------------------------------------------- session

class Session:
    """A reusable logged-in Higgsfield browser session. One persistent context for a whole run.
    Runs HEADED but parked FAR off-screen (like the scrape sessions) so nothing shows in the
    taskbar; set HIGGSFIELD_WINDOW_VISIBLE=1 to watch it while calibrating."""

    def __init__(self, headless=None, status_cb=None):
        self._p = None
        self._ctx = None
        self._status_cb = status_cb
        self.headless = _HEADLESS if headless is None else bool(headless)
        self._pids = set()
        self._hide_stop = threading.Event()
        self._watcher = None
        self._gen_page = None               # the ONE reused generator page (manual-Unlimited mode)
        self._captured = []                 # generated-image URLs seen on the reused page
        self._open()

    def _open(self):
        if _tt is not None:
            _kill_stale()
        try:
            self._p = sync_playwright().start()
            args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                    "--no-default-browser-check", "--mute-audio"]
            visible = os.environ.get("HIGGSFIELD_WINDOW_VISIBLE", "").strip().lower() in (
                "1", "true", "yes")
            if not self.headless and not visible:
                args += ["--window-position=-2400,-2400", "--window-size=1360,900",
                         "--disable-backgrounding-occluded-windows",
                         "--disable-renderer-backgrounding",
                         "--disable-background-timer-throttling",
                         "--disable-features=CalculateNativeWinOcclusion"]
            self._ctx = self._p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=self.headless, user_agent=_UA, locale=_LOCALE,
                viewport={"width": 1360, "height": 900}, args=args)
        except Exception:
            if self._p is not None:
                try:
                    self._p.stop()
                except Exception:
                    pass
                self._p = None
            raise
        if not self.headless and not visible and _tt is not None:
            _tt._park_window_offscreen(self._ctx, "HIGGSFIELD_WINDOW_VISIBLE")
            for _wait in (0.4, 1.2, 2.0):
                time.sleep(_wait)
                if _tt._hide_offscreen_from_taskbar():
                    break
            # ROBUST, CDP-independent enforcement by browser PID + a session-long watcher (same fix
            # as tiktok_login: catches the case where the CDP park failed and the window stayed
            # on-screen / in the taskbar).
            for _ in range(6):
                self._pids = _tt._profile_pids(PROFILE_DIR)
                if self._pids:
                    break
                time.sleep(0.5)
            if self._pids:
                _tt._enforce_offscreen_hidden(self._pids)
                self._watcher = _tt._spawn_taskbar_watcher(self._pids, self._hide_stop)

    def cookies(self):
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def logged_in(self):
        return _has_session_cookie(self.cookies())

    # ---- the generation click-path (calibration-pending; see module docstring) --------------

    @staticmethod
    def _elements(page, selector):
        """All matches, not only the first hidden clone rendered by the current React UI."""
        try:
            return page.query_selector_all(selector)
        except Exception:
            try:
                one = page.query_selector(selector)
                return [one] if one else []
            except Exception:
                return []

    @staticmethod
    def _text(el):
        try:
            return re.sub(r"\s+", " ", el.inner_text() or "").strip()
        except Exception:
            return ""

    def _dismiss_overlays(self, page):
        """Close every visible consent/onboarding dialog.

        Higgsfield can stack the cookie dialog and an "Organize. Share. Create together" modal.
        The old query_selector clicked only the first Close button, leaving the second dialog over
        the prompt.  Playwright then reported the textbox as visible but could not click it.
        """
        selectors = (
            "[role='button']:has-text('Accept all')",
            "[role='button']:has-text('Allow all')",
            "[role='button']:has-text('Got it')",
            "[role='button']:has-text('Maybe later')",
            "[role='button']:has-text('Save & Close')",
            "button[aria-label='Dismiss']", "[role='button'][aria-label='Dismiss']",
            "[role='dialog'] button[aria-label='Close']",
            "[role='dialog'] button:has-text('Close')",
            "[role='dialog'] [role='button'][aria-label='Close']",
            "div[role='dialog'] button:has-text('×')",
        )
        for _round in range(3):
            clicked = False
            for sel in selectors:
                for el in self._elements(page, sel):
                    try:
                        if el and el.is_visible():
                            el.click(timeout=1500)
                            clicked = True
                    except Exception:
                        continue
            if not clicked:
                break
            try:
                page.wait_for_timeout(250)
            except Exception:
                pass

    def _type_prompt(self, page, prompt):
        """Type the prompt into the most likely prompt field. Returns True if it landed text."""
        selectors = [
            "[role='textbox'][contenteditable='true']",
            "div[contenteditable='true'][role='textbox']",
            "[contenteditable='true'][data-placeholder*='describe' i]",
            "textarea[placeholder*='prompt' i]",
            "textarea[placeholder*='describe' i]",
            "textarea[placeholder*='imagine' i]",
            "textarea[name='prompt']",
            "div[contenteditable='true']",
            "textarea",
            "[role='textbox']",
            "input[type='text'][placeholder*='prompt' i]",
        ]
        deadline = time.time() + 12.0
        while time.time() < deadline:
            self._dismiss_overlays(page)
            for sel in selectors:
                for el in self._elements(page, sel):
                    try:
                        if not el or not el.is_visible():
                            continue
                        el.click(timeout=2000)
                        # fill works for both textarea/input and contenteditable and fires the
                        # input event React needs. It is also much faster than typing 700 chars.
                        el.fill(prompt)
                        landed = ""
                        try:
                            landed = el.input_value()
                        except Exception:
                            landed = self._text(el)
                        if landed.strip():
                            return True
                    except Exception:
                        # Some editor wrappers reject fill but accept real keyboard input.
                        try:
                            el.press("Control+A")
                            el.type(prompt, delay=2)
                            return True
                        except Exception:
                            continue
            try:
                page.wait_for_timeout(500)
            except Exception:
                break
        return False

    def _set_aspect(self, page, aspect):
        if not aspect:
            return True
        wanted = str(aspect).strip()
        # Already selected: do not click it again (clicking a selected control merely opens its
        # popup and leaves an overlay over the prompt).
        for el in self._elements(page, "button[aria-haspopup='listbox']"):
            try:
                if el.is_visible() and self._text(el) == wanted:
                    return True
            except Exception:
                continue
        # Find the ratio trigger by its current ratio (3:4 on Higgsfield's new page), open it,
        # then choose the exact listbox option.
        ratio_re = re.compile(r"^\d{1,2}:\d{1,2}$")
        for trigger in self._elements(page, "button[aria-haspopup='listbox']"):
            try:
                current = self._text(trigger)
                if not trigger.is_visible() or not ratio_re.fullmatch(current):
                    continue
                trigger.click(timeout=1500)
                page.wait_for_timeout(250)
                for option in self._elements(page, "[role='option']"):
                    if option.is_visible() and self._text(option) == wanted:
                        option.click(timeout=1500)
                        return True
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
            except Exception:
                continue
        return False

    def _set_model(self, page, model):
        if not model:
            return True
        wanted = re.sub(r"\s+", " ", str(model)).strip().lower()
        # Critical: when FLUX.2 Pro is already selected, never click its button. The old code did,
        # opening the model popup; the following prompt click was then blocked by that popup.
        for el in self._elements(page, "main button"):
            try:
                if el.is_visible() and self._text(el).lower() == wanted:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _control_state(el):
        """Return True/False for a switch-like control, or None if it exposes no state."""
        for name in ("aria-checked", "aria-pressed"):
            try:
                value = str(el.get_attribute(name) or "").strip().lower()
            except Exception:
                value = ""
            if value in ("true", "false"):
                return value == "true"
        try:
            value = str(el.get_attribute("data-state") or "").strip().lower()
        except Exception:
            value = ""
        if value in ("checked", "on", "active", "enabled"):
            return True
        if value in ("unchecked", "off", "inactive", "disabled"):
            return False
        try:
            if str(el.get_attribute("type") or "").lower() == "checkbox":
                return bool(el.is_checked())
        except Exception:
            pass
        return None

    def _wait_for_generator_bar(self, page, timeout_ms=25000):
        """Wait for the create page's bottom control bar (model / aspect / Unlimited / Generate)
        to actually render.

        It appears SECONDS after domcontentloaded - a screenshot at 3.5s still showed only a
        spinner. The old flat 2s wait ran _set_unlimited before the Unlimited toggle existed, so it
        'could not verify Unlimited' and refused to spend credits on a run that would have been
        free. Ready = both a Generate button and an 'Unlimited' label are on the page."""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                ready = page.evaluate(
                    """() => {
                      const gen = [...document.querySelectorAll('button,[role=\"button\"]')]
                        .some(b => /generate/i.test(b.innerText || ''));
                      // The word 'Unlimited' is not a leaf - it labels a role=switch a few DOM
                      // levels up. Ready = a real switch exists whose surrounding text says so.
                      const unl = [...document.querySelectorAll('[role=\"switch\"]')].some(sw => {
                        let n = sw, txt = '';
                        for (let i = 0; i < 4 && n; i++) { txt += ' ' + (n.innerText || ''); n = n.parentElement; }
                        return /unlimited/i.test(txt);
                      });
                      return gen && unl;
                    }""")
            except Exception:
                ready = False
            if ready:
                page.wait_for_timeout(500)   # let the switch settle into its real on/off state
                return True
            page.wait_for_timeout(500)
        return False

    def _find_unlimited_switch(self, page):
        """Return the live 'Unlimited' toggle, or None.

        On the FLUX.2 create bar the real control is an inner <button role="switch"> whose
        OWN text is empty - the word 'Unlimited' sits a couple of DOM levels up next to it
        (label span is a sibling of the switch's wrapper). Matching on the element's own text
        or its single parent misses it and lands on the inert outer wrapper (data-state
        'closed'), which is why an earlier version clicked something that never toggled. So we
        match on the text of up to a few ancestors, and only accept an element that exposes a
        real on/off state (aria-checked / data-state on|off) so we never grab a popover trigger.
        """
        for el in self._elements(page, "[role='switch']"):
            try:
                if not el.is_visible():
                    continue
                ctx = el.evaluate(
                    """node => {
                      let n = node, txt = (node.getAttribute('aria-label') || '');
                      for (let i = 0; i < 4 && n; i++) { txt += ' ' + (n.innerText || ''); n = n.parentElement; }
                      return txt;
                    }""")
            except Exception:
                ctx = ""
            if "unlimited" in str(ctx or "").lower() and self._control_state(el) is not None:
                return el
        return None

    def _set_unlimited(self, page):
        """Enable Higgsfield's Unlimited switch and verify it turned on. Return True only when
        the toggle reads on; absence/ambiguity is a hard False so the caller refuses to spend.

        Unlimited is separate from the FLUX model selection. Missing it silently charges credits
        even on a plan that includes unlimited FLUX generations, so this must be verified, not
        best-effort. aria-checked/data-state give a clean on/off read (confirmed live: off->on,
        and the Generate button drops its 'Generate 1' credit cost when it flips on).
        """
        switch = self._find_unlimited_switch(page)
        if switch is None:
            return False
        if self._control_state(switch) is True:
            return True
        # Click, then poll: the switch animates and the bar re-renders, so a single quick
        # check reads the stale pre-click state. Re-find between attempts in case React
        # replaced the node during the re-render.
        for _ in range(3):
            try:
                switch.click(timeout=1500)
            except Exception:
                break
            for _ in range(8):                  # ~2.4s of polling per click
                page.wait_for_timeout(300)
                if self._control_state(switch) is True:
                    return True
            again = self._find_unlimited_switch(page)
            if again is not None:
                switch = again
                if self._control_state(switch) is True:
                    return True
        return False

    # ---- manual-Unlimited, single-reused-page generation ------------------------------------
    # Higgsfield resets the Unlimited switch on every fresh page load, and DataDome throws a
    # bot-check CAPTCHA on an automated Generate click. Both are solved by NOT reopening the page
    # per image: we open ONE visible page, the USER turns Unlimited on (and clears any DataDome
    # verification) once, and then every prompt is generated on that same page - the Unlimited
    # state and the DataDome trust cookie both persist, so nothing has to be automated around the
    # bot-check. We never touch a CAPTCHA ourselves.

    def _attach_capture(self, page):
        def _maybe_add(url):
            if not url or not _IMG_EXT_RE.search(url) or not _GEN_URL_RE.search(url):
                return
            if url not in self._captured:
                self._captured.append(url)

        def _on_resp(resp):
            try:
                url = resp.url
                ctype = ""
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                except Exception:
                    pass
                if "image" in ctype or _IMG_EXT_RE.search(url):
                    _maybe_add(url)
                elif "json" in ctype:
                    try:
                        _scan_json_for_images(resp.json(), _maybe_add)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", _on_resp)

    def _has_captcha(self, page):
        """True while a DataDome / captcha-delivery verification is mounted over the page."""
        try:
            return bool(page.evaluate(
                """() => [...document.querySelectorAll('iframe')]
                     .some(f => /captcha-delivery|datadome/i.test(f.src || ''))
                   || !!document.querySelector('[id*=datadome i],[class*=datadome i]')"""))
        except Exception:
            return False

    def open_generator(self, model=None, aspect=None, status_cb=None):
        """Open (once) the single reused generator page and set model + aspect. The Unlimited
        switch is deliberately NOT touched here - the user sets it. Returns True on success."""
        cb = status_cb or self._status_cb
        model = model or DEFAULT_MODEL
        aspect = aspect or DEFAULT_ASPECT
        if self._gen_page is None:
            self._gen_page = self._ctx.new_page()
            self._attach_capture(self._gen_page)
        page = self._gen_page
        try:
            page.goto(CREATE_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as exc:            # noqa: BLE001
            _status(cb, f"Higgsfield: could not open the generator ({exc.__class__.__name__}).")
            return False
        page.wait_for_timeout(1200)
        self._dismiss_overlays(page)
        self._wait_for_generator_bar(page)
        self._dismiss_overlays(page)
        self._set_model(page, model)
        self._set_aspect(page, aspect)
        return True

    def unlimited_is_on(self):
        page = self._gen_page
        if page is None:
            return False
        sw = self._find_unlimited_switch(page)
        return sw is not None and self._control_state(sw) is True

    def wait_for_user_unlimited(self, status_cb=None, cancel_check=None, timeout_s=1200):
        """Bring the window forward and WAIT until the USER turns Unlimited ON (and clears any
        DataDome verification). Returns True once Unlimited reads on with no CAPTCHA showing."""
        cb = status_cb or self._status_cb
        page = self._gen_page
        if page is None:
            return False
        deadline = time.time() + timeout_s
        nagged = 0.0
        while time.time() < deadline:
            if cancel_check and cancel_check():
                return False
            if self.unlimited_is_on() and not self._has_captcha(page):
                _status(cb, "Unlimited is ON - starting image generation.")
                return True
            now = time.time()
            if now - nagged > 8:
                nagged = now
                if self._has_captcha(page):
                    _status(cb, "Higgsfield shows a quick verification - please complete it in the "
                                "window, then flip the 'Unlimited' switch on.")
                else:
                    _status(cb, "Waiting for you: turn ON the 'Unlimited' switch in the Higgsfield "
                                "window - generation then starts automatically.")
            try:
                page.wait_for_timeout(1500)
            except Exception:
                break
        return False

    def generate_reuse(self, prompt, out_path, timeout_s=300, status_cb=None):
        """Generate ONE image on the already-open, user-prepared generator page. Never navigates
        (that would reset Unlimited). Returns the path, None on a plain timeout, or the sentinels
        'UNLIMITED_OFF' / 'CAPTCHA' so the caller can pause for the user instead of spending."""
        cb = status_cb or self._status_cb
        page = self._gen_page
        if page is None:
            return None
        if self._has_captcha(page):
            return "CAPTCHA"
        if not self.unlimited_is_on():
            return "UNLIMITED_OFF"
        if not self._type_prompt(page, prompt):
            _status(cb, "Higgsfield: could not enter the prompt.")
            return None
        cut = len(self._captured)
        if not self._click_generate(page):
            _status(cb, "Higgsfield: generate button not found.")
            return None
        _status(cb, f"Higgsfield: generating {os.path.basename(str(out_path))} ...")
        deadline = time.time() + max(30, int(timeout_s))
        chosen = None
        while time.time() < deadline:
            if self._has_captcha(page):
                return "CAPTCHA"
            fresh = self._captured[cut:]
            if fresh:
                page.wait_for_timeout(1500)          # let the queue settle on the final asset
                fresh = self._captured[cut:]
                chosen = fresh[-1]
                break
            page.wait_for_timeout(1000)
        if not chosen:
            _status(cb, f"Higgsfield: timed out waiting for the image for {out_path}.")
            return None
        if self._download(page, chosen, out_path):
            return str(out_path)
        _status(cb, f"Higgsfield: failed to download the finished image ({chosen}).")
        return None

    # ---- concurrent generation: K pages, each owns its own result -----------------------------
    # Every page has its OWN response listener + captured list, so the image a page produces is
    # unambiguously that page's prompt - completion order never matters and frames can't be
    # mis-mapped. All pages live in the one trusted, Unlimited context (the anchor cleared the
    # DataDome check + turned Unlimited on once; worker pages auto-enable Unlimited).

    @staticmethod
    def _make_capture(caps):
        def _on_resp(resp):
            try:
                url = resp.url
                ctype = ""
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                except Exception:
                    pass
                def _add(u):
                    if u and _IMG_EXT_RE.search(u) and _GEN_URL_RE.search(u) and u not in caps:
                        caps.append(u)
                if "image" in ctype or _IMG_EXT_RE.search(url):
                    _add(url)
                elif "json" in ctype:
                    try:
                        _scan_json_for_images(resp.json(), _add)
                    except Exception:
                        pass
            except Exception:
                pass
        return _on_resp

    def _page_unlimited(self, page):
        sw = self._find_unlimited_switch(page)
        return sw is not None and self._control_state(sw) is True

    def _prepare_extra_page(self, model, aspect, status_cb=None):
        """Open + prepare an extra worker page (navigate, model/aspect, auto-Unlimited). The
        DataDome trust cookie is context-wide, so an extra page inherits the anchor's trust and
        does not re-challenge. Returns the page, or None if Unlimited could not be verified."""
        cb = status_cb or self._status_cb
        try:
            page = self._ctx.new_page()
            page.goto(CREATE_URL, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            self._dismiss_overlays(page)
            self._wait_for_generator_bar(page)
            self._dismiss_overlays(page)
            self._set_model(page, model)
            self._set_aspect(page, aspect)
            if not self._set_unlimited(page):
                _status(cb, "Higgsfield: an extra generation slot could not enable Unlimited - "
                            "keeping fewer slots (never spending).")
                try:
                    page.close()
                except Exception:
                    pass
                return None
            return page
        except Exception:
            return None

    def generate_pool(self, items, k=4, timeout_s=300, status_cb=None, cancel_check=None,
                      on_done=None):
        """Generate `items` (each {idx, prompt, path}) with up to k generations in flight across
        k pages. Calls on_done(idx, path|None) as each finishes. Returns {idx: path|None}.
        Pauses the whole pool for the user if a page loses Unlimited or hits a DataDome check."""
        cb = status_cb or self._status_cb
        model, aspect = DEFAULT_MODEL, DEFAULT_ASPECT
        slots = []
        if self._gen_page is not None:                  # reuse the anchor the user prepared
            caps = []
            self._gen_page.on("response", self._make_capture(caps))
            slots.append({"page": self._gen_page, "caps": caps, "idx": None})
        while len(slots) < max(1, int(k)):
            if cancel_check and cancel_check():
                break
            page = self._prepare_extra_page(model, aspect, status_cb=cb)
            if page is None:
                break
            caps = []
            page.on("response", self._make_capture(caps))
            slots.append({"page": page, "caps": caps, "idx": None})
        _status(cb, f"Higgsfield: {len(slots)} generation slot(s) in flight.")

        queue = list(items)
        results = {}
        tick = slots[0]["page"]

        def _pause_for_user():
            _status(cb, "Paused - re-enable Unlimited / finish the verification in the Higgsfield "
                        "window; generation resumes automatically.")
            self.wait_for_user_unlimited(status_cb=cb, cancel_check=cancel_check, timeout_s=1800)
            for s in slots:                             # re-arm Unlimited on every worker page
                if not self._page_unlimited(s["page"]):
                    self._set_unlimited(s["page"])

        while queue or any(s["idx"] is not None for s in slots):
            if cancel_check and cancel_check():
                break
            # assign free slots
            for s in slots:
                if s["idx"] is not None or not queue:
                    continue
                page = s["page"]
                if self._has_captcha(page) or not self._page_unlimited(page):
                    _pause_for_user()
                    if cancel_check and cancel_check():
                        break
                idx, prompt, path = queue[0]
                if not self._type_prompt(page, prompt):
                    queue.pop(0)
                    results[idx] = None
                    if on_done:
                        on_done(idx, None)
                    continue
                queue.pop(0)
                s["cut"] = len(s["caps"])
                s["idx"], s["path"] = idx, path
                self._click_generate(page)
                s["deadline"] = time.time() + max(30, int(timeout_s))
                _status(cb, f"Higgsfield: generating {os.path.basename(str(path))} ...")
            # collect finished slots
            for s in slots:
                if s["idx"] is None:
                    continue
                page = s["page"]
                fresh = s["caps"][s["cut"]:]
                if fresh:
                    page.wait_for_timeout(300)
                    fresh = s["caps"][s["cut"]:]
                    url = fresh[-1]
                    ok = self._download(page, url, s["path"])
                    results[s["idx"]] = s["path"] if ok else None
                    if on_done:
                        on_done(s["idx"], s["path"] if ok else None)
                    s["idx"] = None
                elif time.time() > s["deadline"]:
                    _status(cb, f"Higgsfield: timed out waiting for {os.path.basename(str(s['path']))}.")
                    results[s["idx"]] = None
                    if on_done:
                        on_done(s["idx"], None)
                    s["idx"] = None
            try:
                tick.wait_for_timeout(500)
            except Exception:
                break
        return results

    def _click_generate(self, page):
        for sel in ("button:has-text('Generate')", "button:has-text('Create')",
                    "button:has-text('Imagine')", "button[type='submit']",
                    "button:has-text('Render')"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible() and el.is_enabled():
                    el.click(timeout=2000)
                    return True
            except Exception:
                continue
        # last resort: Ctrl+Enter often submits the prompt box
        try:
            page.keyboard.press("Control+Enter")
            return True
        except Exception:
            return False

    def _download(self, page, url, out_path):
        """Fetch the result image WITH the session's auth (page.request reuses context cookies)."""
        try:
            resp = page.request.get(url, timeout=60000)
            if resp.ok:
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_bytes(resp.body())
                return True
        except Exception:
            pass
        return False

    def generate(self, prompt, out_path, aspect=None, model=None, timeout_s=300, status_cb=None):
        """Generate ONE image for `prompt` and save it to out_path. Returns the path or None.
        The finished image is captured from the network (the newest generated image URL that
        appears after we click generate), which survives UI churn better than DOM-scraping."""
        cb = status_cb or self._status_cb
        aspect = aspect or DEFAULT_ASPECT
        model = model or DEFAULT_MODEL
        page = self._ctx.new_page()
        captured = []
        pre_submit = {"cut": 0}

        def _maybe_add(url):
            if not url or not _IMG_EXT_RE.search(url):
                return
            if not _GEN_URL_RE.search(url):
                return
            if url not in captured:
                captured.append(url)

        def _on_resp(resp):
            try:
                url = resp.url
                ctype = ""
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                except Exception:
                    pass
                if "image" in ctype or _IMG_EXT_RE.search(url):
                    _maybe_add(url)
                elif "json" in ctype:
                    try:
                        _scan_json_for_images(resp.json(), _maybe_add)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", _on_resp)
        try:
            def prepare_generator(reload=False):
                """Open a clean generator and prove its controls are usable before spending.

                Higgsfield's July 2026 UI occasionally leaves a model/onboarding popup mounted
                over the editor.  One clean reload is enough to recover from that transient
                state; silently submitting the site's default 3:4 ratio is never acceptable for
                a 16:9 longform timeline.
                """
                try:
                    if reload:
                        page.reload(timeout=60000, wait_until="domcontentloaded")
                    else:
                        page.goto(CREATE_URL, timeout=60000, wait_until="domcontentloaded")
                except Exception as exc:
                    _status(cb, "Higgsfield: navigation to the image generator failed "
                                f"({exc.__class__.__name__}).")
                    return False
                page.wait_for_timeout(1200)
                self._dismiss_overlays(page)
                # The control bar renders late; wait for it before touching model/aspect/Unlimited,
                # otherwise _set_unlimited runs on a page that has only a spinner.
                self._wait_for_generator_bar(page)
                self._dismiss_overlays(page)
                # CREATE_URL selects FLUX.2 Pro directly.  _set_model deliberately verifies the
                # selected label without clicking it, because clicking it opens a blocking popup.
                if not self._set_model(page, model):
                    page.wait_for_timeout(1000)
                    if not self._set_model(page, model):
                        _status(cb, f"Higgsfield: {model} is not selected on {page.url}.")
                        return False
                if not self._set_aspect(page, aspect):
                    _status(cb, f"Higgsfield: could not select the required {aspect} aspect ratio.")
                    return False
                if not self._set_unlimited(page):
                    _status(cb, "Higgsfield: Unlimited mode is unavailable or could not be "
                                "verified as enabled; refusing to spend credits.")
                    return False
                return self._type_prompt(page, prompt)

            if not prepare_generator():
                _status(cb, "Higgsfield: generator controls were not ready; reloading once...")
                if not prepare_generator(reload=True):
                    _status(cb, "Higgsfield: could not prepare the prompt/model/aspect controls "
                                f"after recovery (page: {page.url}).")
                    return None
            # everything captured up to now is pre-existing gallery art; only NEW urls count.
            pre_submit["cut"] = len(captured)
            if not self._click_generate(page):
                _status(cb, "Higgsfield: could not find the generate button (needs calibration).")
                return None
            _status(cb, f"Higgsfield: generating {os.path.basename(str(out_path))} ...")
            deadline = time.time() + max(30, int(timeout_s))
            chosen = None
            while time.time() < deadline:
                fresh = captured[pre_submit["cut"]:]
                if fresh:
                    # give the queue a moment to settle on the final (largest/last) asset
                    page.wait_for_timeout(1500)
                    fresh = captured[pre_submit["cut"]:]
                    chosen = fresh[-1]
                    break
                page.wait_for_timeout(1000)
            if not chosen:
                _status(cb, f"Higgsfield: timed out waiting for the image for {out_path}.")
                return None
            if self._download(page, chosen, out_path):
                return str(out_path)
            _status(cb, f"Higgsfield: failed to download the finished image ({chosen}).")
            return None
        finally:
            try:
                page.off("response", _on_resp)
            except Exception:
                pass
            try:
                page.close()
            except Exception:
                pass

    def close(self):
        try:
            self._hide_stop.set()           # stop the taskbar-hide watcher
        except Exception:
            pass
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._p:
                self._p.stop()
        except Exception:
            pass
        self._ctx = self._p = None


def _scan_json_for_images(data, add):
    """Walk a JSON payload and hand any string that looks like a generated-image URL to `add`."""
    stack = [data]
    seen = 0
    while stack and seen < 5000:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
        elif isinstance(cur, str):
            if cur.startswith("http") and _IMG_EXT_RE.search(cur):
                add(cur)


# ------------------------------------------------------------- worker-thread plumbing
# ALL Playwright work runs on this ONE dedicated thread (same rule as tiktok_login: the sync
# API is thread-affine and a half-closed driver poisons its thread). Job threads only talk to
# it through futures.

def _executor():
    global _EXECUTOR
    with _EXEC_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="higgsfield")
        return _EXECUTOR


def _session_on_worker(status_cb=None):
    if _SESSION[0] is None:
        try:
            _SESSION[0] = Session(status_cb=status_cb)
        except Exception as exc:            # noqa: BLE001
            _status(status_cb, f"Higgsfield session failed to open ({exc.__class__.__name__}: {exc}).")
            _SESSION[0] = None
    # The marker only says that this profile was logged in once. Session cookies can expire
    # months later; treating the marker as live authentication made the UI say Connected while
    # Higgsfield showed Login, then the longform scheduler burned ten generation attempts.
    if _SESSION[0] is not None and not _SESSION[0].logged_in():
        _status(status_cb, "Higgsfield login has expired - reconnect Higgsfield, then resume. "
                           "The voiceover, transcript and image prompts are already saved.")
        try:
            _SESSION[0].close()
        except Exception:
            pass
        _SESSION[0] = None
        try:
            _MARKER.unlink(missing_ok=True)
        except OSError:
            pass
    return _SESSION[0]


def ensure_session(status_cb=None, timeout_s=120.0):
    """Open (or reuse) the logged-in session on the worker thread. True when ready."""
    if not is_ready():
        return False
    try:
        return (_executor().submit(_session_on_worker, status_cb)
                .result(timeout=timeout_s) is not None)
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield session failed to open ({exc.__class__.__name__}: {exc}).")
        return False


def _generate_on_worker(prompt, out_path, aspect, model, timeout_s, status_cb):
    sess = _session_on_worker(status_cb)
    if sess is None:
        return None
    try:
        return sess.generate(prompt, out_path, aspect=aspect, model=model,
                             timeout_s=timeout_s, status_cb=status_cb)
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield generate failed ({exc.__class__.__name__}: {exc}).")
        try:
            _SESSION[0].close()
        except Exception:
            pass
        _SESSION[0] = None
        return None


def generate_sync(prompt, out_path, aspect=None, model=None, timeout_s=300, status_cb=None):
    """Generate ONE image on the worker thread and block for the result path (or None)."""
    if not is_ready():
        return None
    # Session.generate's own deadline only covers WAITING for the image. After it comes the
    # download (page.request.get, 60s), plus up to ~2.5s of loop overshoot - so the worst case is
    # timeout_s + ~62.5s. Waiting only timeout_s + 60 lost that race by two seconds: we would give
    # up while the worker was still writing a perfectly good file, and the caller would re-generate
    # an image that had in fact just landed. The margin has to clear the download.
    wait = max(30.0, float(timeout_s) + 90.0)
    try:
        return (_executor().submit(_generate_on_worker, prompt, str(out_path), aspect, model,
                                   timeout_s, status_cb).result(timeout=wait))
    except concurrent.futures.TimeoutError:
        _status(status_cb, f"Higgsfield generate timed out for {out_path}.")
        return None
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield generate failed ({exc.__class__.__name__}: {exc}).")
        return None


def generate_batch(items, out_dir, concurrency=4, aspect=None, model=None, ext="png",
                   status_cb=None, cancel_check=None, on_done=None, timeout_s=300):
    """Generate images for a list of parsed prompt items (from parse_prompt_lines).

    Because Playwright's sync API is single-threaded, "concurrency" is realised by keeping up to
    `concurrency` Higgsfield generations IN FLIGHT on the one session (the site queues them
    server-side) rather than by OS threads. This first version submits/awaits per image; the
    hook is here so the wave-based concurrency can be tightened once the click-path is locked
    against the live UI.

    Returns a list of {item, path|None}. Calls on_done(item, path) after each. Honours
    cancel_check() (a callable -> bool) between images."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    total = len(items)
    if not ensure_session(status_cb=status_cb):
        _status(status_cb, "Higgsfield is not connected - click Connect Higgsfield first.")
        return results
    for i, item in enumerate(items):
        if cancel_check and cancel_check():
            _status(status_cb, "Cancelled.")
            break
        out_path = out_dir / f"{item['key']}.{ext}"
        _status(status_cb, f"[{i + 1}/{total}] {item['timestamp']} -> {out_path.name}")
        path = generate_sync(item["prompt"], out_path, aspect=aspect, model=model,
                             timeout_s=timeout_s, status_cb=status_cb)
        results.append({"item": item, "path": path})
        if on_done:
            try:
                on_done(item, path)
            except Exception:
                pass
    return results


# ---- manual-Unlimited session (visible window, one reused page) -----------------------------
# The longform image batch uses THIS instead of generate_batch: it opens a VISIBLE Higgsfield
# window, waits for the user to flip Unlimited on (and clear any DataDome check), then generates
# every image on the one page the user prepared - so Unlimited and the trust cookie both persist.

def _open_visible_on_worker(status_cb=None):
    # A manual run needs the window ON-SCREEN; if a prior off-screen session is around, drop it.
    if _SESSION[0] is not None:
        try:
            _SESSION[0].close()
        except Exception:
            pass
        _SESSION[0] = None
    os.environ["HIGGSFIELD_WINDOW_VISIBLE"] = "1"
    return _session_on_worker(status_cb)


def _begin_manual_on_worker(model, aspect, status_cb, cancel_check, timeout_s):
    sess = _open_visible_on_worker(status_cb)
    if sess is None:
        return False
    if not sess.open_generator(model=model, aspect=aspect, status_cb=status_cb):
        return False
    return sess.wait_for_user_unlimited(status_cb=status_cb, cancel_check=cancel_check,
                                        timeout_s=timeout_s)


def begin_manual_session(model=None, aspect=None, status_cb=None, cancel_check=None,
                         timeout_s=1200):
    """Open a VISIBLE Higgsfield generator and block until the user has turned Unlimited on.
    True when ready to generate; False on timeout/cancel/failure."""
    if not is_ready():
        _status(status_cb, "Higgsfield is not connected - click Connect Higgsfield first.")
        return False
    try:
        return bool(_executor().submit(_begin_manual_on_worker, model, aspect, status_cb,
                                       cancel_check, timeout_s).result(timeout=timeout_s + 60))
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield manual session failed ({exc.__class__.__name__}: {exc}).")
        return False


def _generate_shared_on_worker(prompt, out_path, timeout_s, status_cb):
    sess = _SESSION[0]
    if sess is None:
        return None
    try:
        return sess.generate_reuse(prompt, out_path, timeout_s=timeout_s, status_cb=status_cb)
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield generate failed ({exc.__class__.__name__}: {exc}).")
        return None


def generate_shared_sync(prompt, out_path, timeout_s=300, status_cb=None):
    """Generate ONE image on the manual session's reused page. Returns a path, None, or the
    sentinels 'UNLIMITED_OFF' / 'CAPTCHA' (caller should pause for the user, then retry)."""
    wait = max(30.0, float(timeout_s) + 90.0)
    try:
        return (_executor().submit(_generate_shared_on_worker, prompt, str(out_path),
                                   timeout_s, status_cb).result(timeout=wait))
    except concurrent.futures.TimeoutError:
        _status(status_cb, f"Higgsfield generate timed out for {out_path}.")
        return None
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield generate failed ({exc.__class__.__name__}: {exc}).")
        return None


def _generate_pool_on_worker(items, k, timeout_s, status_cb, cancel_check, on_done):
    sess = _SESSION[0]
    if sess is None:
        return {}
    try:
        return sess.generate_pool(items, k=k, timeout_s=timeout_s, status_cb=status_cb,
                                  cancel_check=cancel_check, on_done=on_done)
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield pool failed ({exc.__class__.__name__}: {exc}).")
        return {}


def generate_pool_sync(items, k=4, timeout_s=300, status_cb=None, cancel_check=None, on_done=None):
    """Run the whole item list through the K-in-flight pool on the worker thread. `on_done` is
    invoked (on the worker thread) as each image finishes. Returns {idx: path|None}."""
    try:
        return _executor().submit(_generate_pool_on_worker, list(items), int(k), timeout_s,
                                  status_cb, cancel_check, on_done).result()
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"Higgsfield pool failed ({exc.__class__.__name__}: {exc}).")
        return {}


def wait_for_user_unlimited_sync(status_cb=None, cancel_check=None, timeout_s=1200):
    """Re-block for the user to turn Unlimited back on mid-batch (it flipped off / a CAPTCHA
    appeared). True once ready again."""
    try:
        return bool(_executor().submit(
            lambda: (_SESSION[0].wait_for_user_unlimited(
                status_cb=status_cb, cancel_check=cancel_check, timeout_s=timeout_s)
                if _SESSION[0] is not None else False)).result(timeout=timeout_s + 60))
    except Exception:
        return False


def close_session():
    global _EXECUTOR
    with _EXEC_LOCK:
        ex = _EXECUTOR
    if ex is None:
        return

    def _close():
        if _SESSION[0] is not None:
            try:
                _SESSION[0].close()
            except Exception:
                pass
            _SESSION[0] = None
    try:
        ex.submit(_close).result(timeout=30)
    except Exception:
        pass


# --------------------------------------------------------------------------- CLI

def _probe():
    """Open the create page in the connected session and dump its inputs/buttons so the
    click-path selectors can be locked against the live Higgsfield UI."""
    if not is_ready():
        print("not logged in - run: python higgsfield_login.py login")
        return
    sess = Session(headless=False, status_cb=print)
    try:
        page = sess._ctx.new_page()
        page.goto(CREATE_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        info = page.evaluate(
            """() => {
              const grab = (els, keys) => Array.from(els).slice(0, 40).map(e => {
                const o = { tag: e.tagName.toLowerCase() };
                keys.forEach(k => { const v = e.getAttribute(k); if (v) o[k] = v; });
                const t = (e.innerText || e.value || '').trim(); if (t) o.text = t.slice(0, 40);
                return o;
              });
              return {
                url: location.href,
                textareas: grab(document.querySelectorAll('textarea,[contenteditable=\"true\"]'),
                                ['placeholder','name','aria-label']),
                buttons: grab(document.querySelectorAll('button,[role=\"button\"]'),
                              ['aria-label','type','data-value']),
              };
            }"""
        )
        print(json.dumps(info, indent=2)[:6000])
    finally:
        sess.close()


if __name__ == "__main__":
    import sys
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").strip().lower()
    if cmd == "login":
        ok = login(status_cb=print)
        print("LOGGED IN" if ok else "NOT LOGGED IN")
        sys.exit(0 if ok else 1)
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "logout":
        logout()
        print("logged out")
    elif cmd == "probe":
        _probe()
    elif cmd == "unlimited":
        # Live-verify the Unlimited toggle ends up ON - WITHOUT clicking Generate (no credits).
        if not is_ready():
            print("not logged in - run: python higgsfield_login.py login")
            sys.exit(1)
        sess = Session(headless=False, status_cb=print)
        try:
            page = sess._ctx.new_page()
            page.goto(CREATE_URL, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            sess._dismiss_overlays(page)
            bar = sess._wait_for_generator_bar(page)
            sess._dismiss_overlays(page)
            ok = sess._set_unlimited(page)
            print(json.dumps({"generator_bar_ready": bool(bar), "unlimited_on": bool(ok)}, indent=2))
            sys.exit(0 if ok else 1)
        finally:
            sess.close()
    elif cmd == "parse":
        p = sys.argv[2] if len(sys.argv) > 2 else ""
        items = parse_prompt_lines(Path(p).read_text("utf-8")) if p else []
        for it in items[:10]:
            print(it["timestamp"], "->", it["key"] + ".png", "|", it["prompt"][:60])
        print(f"... {len(items)} prompt(s)")
    elif cmd == "gen":
        if not ensure_session(status_cb=print):
            print("not logged in - run: python higgsfield_login.py login")
            sys.exit(1)
        prompt = sys.argv[2] if len(sys.argv) > 2 else "a cheerful doodle cartoon, 16:9"
        out = sys.argv[3] if len(sys.argv) > 3 else str(_STATE_DIR / "_probe.png")
        res = generate_sync(prompt, out, status_cb=print)
        print("SAVED:", res)
        close_session()
    else:
        print("usage: python higgsfield_login.py [login|status|logout|probe|parse <txt>|gen <prompt> <out>]")
