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
CREATE_URL = os.environ.get("HIGGSFIELD_CREATE_URL") or "https://higgsfield.ai/create/image"
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

    def _dismiss_overlays(self, page):
        for sel in ("button:has-text('Accept all')", "button:has-text('Allow all')",
                    "button:has-text('Got it')", "button[aria-label='Close']",
                    "div[aria-label='Close'] button"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=1200)
            except Exception:
                pass

    def _type_prompt(self, page, prompt):
        """Type the prompt into the most likely prompt field. Returns True if it landed text."""
        selectors = [
            "textarea[placeholder*='prompt' i]",
            "textarea[placeholder*='describe' i]",
            "textarea[placeholder*='imagine' i]",
            "textarea[name='prompt']",
            "div[contenteditable='true']",
            "textarea",
            "input[type='text'][placeholder*='prompt' i]",
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=1500)
                    try:
                        el.fill("")
                    except Exception:
                        pass
                    el.type(prompt, delay=6)
                    return True
            except Exception:
                continue
        return False

    def _set_aspect(self, page, aspect):
        if not aspect:
            return
        for sel in (f"button:has-text('{aspect}')", f"[role='option']:has-text('{aspect}')",
                    f"[data-value='{aspect}']", f"label:has-text('{aspect}')"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=1200)
                    return
            except Exception:
                continue

    def _set_model(self, page, model):
        if not model:
            return
        for sel in (f"button:has-text('{model}')", f"[role='option']:has-text('{model}')",
                    f"[data-value*='{model}' i]"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=1200)
                    return
            except Exception:
                continue

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
            try:
                page.goto(CREATE_URL, timeout=60000, wait_until="domcontentloaded")
            except Exception as exc:
                _status(cb, f"Higgsfield: navigation to create page failed ({exc.__class__.__name__}).")
            page.wait_for_timeout(1500)
            self._dismiss_overlays(page)
            self._set_model(page, model)
            self._set_aspect(page, aspect)
            if not self._type_prompt(page, prompt):
                _status(cb, "Higgsfield: could not find the prompt input (needs calibration). "
                            "Run: python higgsfield_login.py probe")
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
    wait = max(30.0, float(timeout_s) + 60.0)
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
