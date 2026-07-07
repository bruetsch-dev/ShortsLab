"""TikTok login-based clip discovery - the primary backend alongside X/Twitter.

No API keys. The user logs in to TikTok ONCE inside a Chromium window we control
(a dedicated, persistent browser profile), and from then on we drive that logged-in
session to run real keyword searches and collect native TikTok item objects. Those
objects are the exact schema `clip_scraper._item_meta` already understands, so the
whole existing quality pipeline (metadata pre-filter -> download -> black-bar / text /
stability gates -> vision matcher) is reused unchanged.

Why a dedicated profile and not the user's everyday Chrome: modern Chrome/Edge encrypt
their cookie store with App-Bound Encryption, so yt-dlp's --cookies-from-browser fails
with "Failed to decrypt with DPAPI". Owning the browser profile sidesteps that entirely -
we read cookies straight from the Playwright context and hand them to yt-dlp as a plain
Netscape cookies.txt for the actual (watermark-free) download.

Public surface used by clip_scraper:
    available()                      -> is Playwright importable
    is_ready()                       -> have we logged in at least once
    status()                         -> dict for the UI
    login(status_cb, timeout_s)      -> headed one-time login, returns bool
    logout()                         -> wipe the saved session
    ensure_session(status_cb)        -> open/reuse the session on the worker thread (bool)
    search_sync(query, ...)          -> run a search on the worker thread, block for items
    export_cookies_txt(path)         -> write cookies.txt for yt-dlp
    close_session()                  -> tear the shared session down (on the worker thread)
"""

import concurrent.futures
import os
import json
import subprocess
import time
import threading
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception:                       # playwright not installed
    sync_playwright = None

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("TIKTOK_PROFILE_DIR") or (ROOT / "tiktok-profile"))
# cookies.txt + login marker live under generated_assets/ (already gitignored).
_STATE_DIR = ROOT / "generated_assets" / "tiktok"
COOKIES_TXT = _STATE_DIR / "tiktok_cookies.txt"
_MARKER = _STATE_DIR / "logged_in.json"

# cookies that only exist for a logged-in TikTok web session
SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# TikTok aggressively blocks HEADLESS browsers - even a logged-in session returns 0 search
# results in headless mode (empty feed / login-wall). So search runs HEADED by default (a small
# Chromium window opens during scraping), matching the working headed login. Set TIKTOK_HEADLESS=1
# to force headless (faster, no window, but usually returns nothing).
_HEADLESS_SEARCH = (os.environ.get("TIKTOK_HEADLESS", "0").strip().lower()
                    in ("1", "true", "yes"))
_LOCALE = os.environ.get("TIKTOK_LOCALE", "en-US").strip() or "en-US"

_LOCK = threading.Lock()
_SESSION = [None]                       # the shared Session; ONLY touched on the worker thread
# Playwright's SYNC api is thread-affine, and a half-closed driver POISONS the calling thread:
# every later sync_playwright().start() there dies with "Sync API inside the asyncio loop".
# Cure (same as twitter_login): ALL Playwright work is funneled through ONE dedicated worker
# thread that outlives job runs - the session is created, searched, cookie-read and closed only
# there, and survives across runs (no per-run reopen churn, no cross-thread closes).
_EXECUTOR = None
_EXEC_LOCK = threading.Lock()
# per-run search health: lets the caller fail FAST when the backend returns nothing at all
# (expired login / headless block / captcha) instead of grinding through every bucket.
_SEARCH_STATS = {"searches": 0, "items": 0, "login_wall": 0}


def search_stats():
    """Cumulative search health for this run: {searches, items, login_wall}."""
    return dict(_SEARCH_STATS)


def reset_search_stats():
    _SEARCH_STATS.update(searches=0, items=0, login_wall=0)


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
    names = {str(c.get("name") or "").lower() for c in (cookies or [])
             if "tiktok" in str(c.get("domain") or "").lower()}
    return any(n in names for n in SESSION_COOKIE_NAMES)


def is_ready():
    """True if Playwright is available and a TikTok login has been saved at least once."""
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


def _cookies_to_netscape(cookies):
    """Serialize Playwright cookies into a Netscape cookies.txt that yt-dlp accepts."""
    lines = ["# Netscape HTTP Cookie File", "# Written by tiktok_login.py", ""]
    for c in cookies or []:
        domain = str(c.get("domain") or "")
        if "tiktok" not in domain.lower():
            continue
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        include_sub = "TRUE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires")
        try:
            expires = str(int(expires)) if expires and float(expires) > 0 else "0"
        except (TypeError, ValueError):
            expires = "0"
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        lines.append("\t".join([domain, include_sub, path, secure, expires, name, value]))
    return "\n".join(lines) + "\n"


def export_cookies_txt(path=None, cookies=None):
    """Write the current session cookies to a Netscape cookies.txt for yt-dlp.
    Uses the shared session if `cookies` is not supplied. Returns the path or None."""
    path = Path(path or COOKIES_TXT)
    if cookies is None:
        cookies = _session_cookies_threadsafe()
        if cookies is None:
            return None
    if not _has_session_cookie(cookies):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_cookies_to_netscape(cookies), "utf-8")
    return str(path)


# --------------------------------------------------------------------------- login

def login(status_cb=None, timeout_s=300):
    """Open a real Chromium window, let the user sign in to TikTok, and persist the
    session into PROFILE_DIR. Blocks until a session cookie appears or timeout. Headed."""
    if not available():
        _status(status_cb, "TikTok login: Playwright is not installed (pip install playwright "
                           "&& playwright install chromium).")
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # CRITICAL: if a zombie Chromium still holds this profile (leftover off-screen scrape
    # window), the new browser DELEGATES to it - and the "new" login window then opens in
    # THAT process, inheriting its off-screen -2400,-2400 position. To the user, clicking
    # Connect/Reconnect does exactly NOTHING. Kill the leftovers first.
    _kill_stale_profile_processes()
    _status(status_cb, "Opening a Chromium window - log in to your TikTok account in it. "
                       "This window stays connected; you only do this once.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--window-position=120,60"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _place_window_visible(ctx, 120, 60)   # force ON-SCREEN even if a scrape saved it off-screen
            try:
                page.goto("https://www.tiktok.com/login", timeout=60000)
            except Exception:
                try:
                    page.goto("https://www.tiktok.com/", timeout=60000)
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
                export_cookies_txt(cookies=ctx.cookies())
                _write_marker()
                _status(status_cb, "TikTok login captured - the session is saved. You can close "
                                   "the window; future scrapes reuse it automatically.")
            else:
                _status(status_cb, "TikTok login timed out / window closed before sign-in completed.")
            return ok
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def logout():
    """Forget the saved session (delete profile + cookies)."""
    import shutil
    close_session()
    for target in (PROFILE_DIR, _MARKER, COOKIES_TXT):
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except Exception:
            pass
    return True


# --------------------------------------------------------------------------- search

def _extract_items_from_payload(payload):
    """Pull TikTok item objects out of a /api/search/* JSON response (both shapes)."""
    items = []
    if not isinstance(payload, dict):
        return items
    # video tab: {"item_list":[ item, ... ]}
    for it in (payload.get("item_list") or []):
        if isinstance(it, dict) and (it.get("id") or it.get("video")):
            items.append(it)
    # general tab: {"data":[ {"type":1,"item":{...}}, ... ]}
    for row in (payload.get("data") or []):
        if isinstance(row, dict):
            it = row.get("item") or row.get("aweme_info")
            if isinstance(it, dict) and (it.get("id") or it.get("video")):
                items.append(it)
    return items


def _hide_offscreen_from_taskbar():
    """Windows only: strip the TASKBAR BUTTON from our off-screen scrape window so nothing shows
    or blinks in the taskbar. Only windows parked DEEP off-screen (left AND top <= -2000, i.e. the
    -2400,-2400 position we launch at - no real monitor sits there) get the WS_EX_TOOLWINDOW style
    (hide -> restyle -> show-without-activate is the required Win32 dance). Returns True if at
    least one window was found."""
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return False
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    SW_HIDE, SW_SHOWNA = 0, 8
    found = [False]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            # CRITICAL: minimized windows report sentinel positions like -32000/-25600, which a
            # naive "deep negative" check matches - that restyled EVERY minimized window on the
            # system. Skip iconic windows and match ONLY our exact -2400,-2400 parking band.
            if user32.IsIconic(hwnd):
                return True
            rect = ctypes.wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            if -2600 <= rect.left <= -2200 and -2600 <= rect.top <= -2200:
                found[0] = True
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                if not (style & WS_EX_TOOLWINDOW):
                    user32.ShowWindow(hwnd, SW_HIDE)
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                          (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                    user32.ShowWindow(hwnd, SW_SHOWNA)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return False
    return found[0]


def _profile_pids(profile_dir):
    """PIDs of the chrome.exe process(es) holding `profile_dir` open. The BROWSER process (the one
    launched with --user-data-dir=<profile>) owns the top-level window, so matching windows by these
    PIDs reliably identifies OUR scrape window regardless of where it currently sits on screen."""
    if os.name != "nt":
        return set()
    try:
        marker = str(profile_dir).replace("'", "''")
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" -ErrorAction Stop | "
               f"Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*{marker}*' }} | "
               "Select-Object -ExpandProperty ProcessId")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {int(x) for x in (out.stdout or "").split() if x.strip().isdigit()}
    except Exception:
        return set()


def _enforce_offscreen_hidden(pids):
    """CDP-INDEPENDENT hide: for every top-level window owned by one of `pids`, force it far
    off-screen (SetWindowPos) AND strip its taskbar button (WS_EX_TOOLWINDOW). This is the fix for
    the case where the CDP `Browser.setWindowBounds` park silently failed - then the window stays at
    the profile's restored ON-screen position, the -2400 band never matches, and the window shows in
    the taskbar. Matching by PID (our profile's browser) means we only ever touch OUR window, never
    the user's real Chrome. Idempotent: once parked + restyled, every later call is a no-op."""
    if os.name != "nt" or not pids:
        return False
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return False
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    SW_HIDE, SW_SHOWNA = 0, 8
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
    GW_OWNER = 4
    acted = [False]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            if user32.GetWindow(hwnd, GW_OWNER):    # owned popup/dialog, not the main window
                return True
            # Safety against (rare) PID recycling: only ever touch a real Chromium top-level window
            # (class "Chrome_WidgetWin_1"), never some unrelated app that inherited a freed PID.
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if "Chrome_WidgetWin" not in buf.value:
                return True
            acted[0] = True
            if not user32.IsIconic(hwnd):
                rect = ctypes.wintypes.RECT()
                # Park at -3200 (not -2400): on a >100% display Windows virtualizes the coordinate
                # DOWN (e.g. 125% scaling turns -2400 into -1920), which could leave part of the
                # window on a monitor. -3200 stays fully off-screen even after that scaling. The
                # "already parked" guard uses a loose -1500 so the watcher never re-moves it (no
                # flicker) once it has landed off-screen.
                if (user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        and not (rect.left <= -1500 and rect.top <= -1500)):
                    user32.SetWindowPos(hwnd, 0, -3200, -3200, 0, 0,
                                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (style & WS_EX_TOOLWINDOW):
                user32.ShowWindow(hwnd, SW_HIDE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                      (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                user32.ShowWindow(hwnd, SW_SHOWNA)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return False
    return acted[0]


def _spawn_taskbar_watcher(pids, stop_event, interval=0.7):
    """Keep OUR off-screen scrape window(s) off-screen AND out of the taskbar for the whole session.
    Defends against the persistent profile restoring an on-screen position or the site re-showing the
    window after the initial hide (the reason the one-shot hide kept 'coming back'). Cheap: after the
    first pass every call is a no-op until something re-shows the window. Daemon; stops on stop_event."""
    def _loop():
        while not stop_event.is_set():
            try:
                _enforce_offscreen_hidden(pids)
            except Exception:
                pass
            stop_event.wait(interval)
    t = threading.Thread(target=_loop, name="taskbar-hide", daemon=True)
    t.start()
    return t


def _park_window_offscreen(ctx, env_visible="TIKTOK_WINDOW_VISIBLE"):
    """Force the persistent context's OS window FAR off-screen via CDP. `--window-position` is only
    a hint for a fresh profile - a persistent profile RESTORES the last saved window bounds, so after
    an interactive login (which showed the window on-screen) the scrape window reappears on-screen
    and blinks in the taskbar. CDP `Browser.setWindowBounds` overrides that regardless of the saved
    bounds; then `_hide_offscreen_from_taskbar()` (which keys on the -2400,-2400 band) can strip the
    taskbar button. Best-effort + silent."""
    if os.environ.get(env_visible, "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        info = cdp.send("Browser.getWindowForTarget") or {}
        wid = info.get("windowId")
        if wid is not None:
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                "left": -2400, "top": -2400, "width": 1280, "height": 900, "windowState": "normal"}})
    except Exception:
        pass


def _place_window_visible(ctx, left=120, top=60, width=1280, height=900):
    """Force the persistent context's window ON-SCREEN + focused via CDP, overriding any off-screen
    bounds the profile saved from a previous BACKGROUND scrape - otherwise an interactive login could
    open invisibly at -2400,-2400 and the user could never sign in. Best-effort + silent."""
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        info = cdp.send("Browser.getWindowForTarget") or {}
        wid = info.get("windowId")
        if wid is not None:
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
                "left": left, "top": top, "width": width, "height": height, "windowState": "normal"}})
        try:
            page.bring_to_front()
        except Exception:
            pass
    except Exception:
        pass


def _kill_stale_profile_processes():
    """Windows only: force-kill any Chromium process still holding PROFILE_DIR open. Needed
    because a crashed run / cross-thread-poisoned session / killed process can leave the browser
    alive without Playwright knowing about it - the NEXT launch_persistent_context() against the
    same profile then silently delegates the URL to that zombie and exits instantly, which
    Playwright surfaces as a confusing TargetClosedError."""
    if os.name != "nt":
        return
    try:
        marker = str(PROFILE_DIR).replace("'", "''")
        cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" -ErrorAction Stop | "
            f"Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*{marker}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


class Session:
    """A reusable logged-in TikTok browser session. One persistent context for a whole run."""

    def __init__(self, headless=None, status_cb=None):
        self._p = None
        self._ctx = None
        self._status_cb = status_cb
        self.headless = _HEADLESS_SEARCH if headless is None else bool(headless)
        self._pids = set()
        self._hide_stop = threading.Event()
        self._watcher = None
        self._open()

    def _open(self):
        # A crashed run, a cross-thread-poisoned session, or a killed process can leave the
        # PREVIOUS Chromium instance still holding PROFILE_DIR open. A new launch against the
        # same profile then silently DELEGATES to that zombie and exits immediately, which
        # Playwright reports as TargetClosedError - kill any such leftover first.
        _kill_stale_profile_processes()
        try:
            self._p = sync_playwright().start()
            args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                    "--no-default-browser-check", "--mute-audio"]
            if not self.headless:
                # TikTok blocks HEADLESS, so we must run a real (headed) browser - but the user does NOT
                # want to see a window. Push it FAR off-screen: it still renders normally (off-screen is
                # not headless and not minimized), so TikTok serves results, but nothing is visible.
                # Disable occlusion/background throttling so the off-screen window isn't slowed down.
                # Set TIKTOK_WINDOW_VISIBLE=1 to watch it (debugging).
                if os.environ.get("TIKTOK_WINDOW_VISIBLE", "").strip().lower() not in ("1", "true", "yes"):
                    args += ["--window-position=-2400,-2400", "--window-size=1280,900",
                             "--disable-backgrounding-occluded-windows",
                             "--disable-renderer-backgrounding",
                             "--disable-background-timer-throttling",
                             "--disable-features=CalculateNativeWinOcclusion"]
            self._ctx = self._p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=self.headless, user_agent=_UA, locale=_LOCALE,
                viewport={"width": 1280, "height": 900}, args=args)
        except Exception:
            # Don't leak a started-but-unused Playwright driver connection on a failed launch -
            # its dispatcher thread lingering has been observed to poison the NEXT sync_playwright()
            # call in this same thread ("Sync API inside the asyncio loop").
            if self._p is not None:
                try:
                    self._p.stop()
                except Exception:
                    pass
                self._p = None
            raise
        visible = os.environ.get("TIKTOK_WINDOW_VISIBLE", "").strip().lower() in ("1", "true", "yes")
        if not self.headless and not visible:
            # CDP-force the window off-screen first (a persistent profile can restore an on-screen
            # position from a prior interactive login), THEN strip its blinking taskbar button.
            _park_window_offscreen(self._ctx, "TIKTOK_WINDOW_VISIBLE")
            for _wait in (0.4, 1.2, 2.0):
                time.sleep(_wait)
                if _hide_offscreen_from_taskbar():
                    break
            # ROBUST enforcement (CDP-independent): identify our window by the profile's browser PID
            # and force it off-screen + out of the taskbar, then keep enforcing for the whole session.
            # This catches the failure the band-based hide above cannot: a CDP park that silently
            # failed leaves the window ON-screen (and in the taskbar), where the -2400 band never
            # matches. THIS is why the windows kept showing.
            for _ in range(6):
                self._pids = _profile_pids(PROFILE_DIR)
                if self._pids:
                    break
                time.sleep(0.5)
            if self._pids:
                _enforce_offscreen_hidden(self._pids)
                self._watcher = _spawn_taskbar_watcher(self._pids, self._hide_stop)

    def cookies(self):
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def logged_in(self):
        return _has_session_cookie(self.cookies())

    def search(self, query, want=12, status_cb=None, sort="MOST_LIKED", max_scrolls=8,
               timeout_s=None):
        """Run a logged-in keyword search and return up to ~want native TikTok item dicts."""
        cb = status_cb or self._status_cb
        query = str(query or "").strip()
        if not query:
            return []
        deadline = (time.monotonic() + max(0.1, float(timeout_s))
                    if timeout_s is not None else None)
        if not self.headless:
            _hide_offscreen_from_taskbar()      # windows can be re-created between searches
        collected = []
        seen = set()

        def _absorb(payload):
            for it in _extract_items_from_payload(payload):
                vid = str(it.get("id") or "")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                it["_source"] = "tiktok_login"   # tells clip_scraper.backend_download to use yt-dlp
                # synthesize the webpage url so yt-dlp can fetch the (clean) mp4
                if not it.get("webVideoUrl"):
                    author = it.get("author") if isinstance(it.get("author"), dict) else {}
                    uid = author.get("uniqueId") or author.get("unique_id") or ""
                    if uid:
                        it["webVideoUrl"] = f"https://www.tiktok.com/@{uid}/video/{vid}"
                collected.append(it)

        page = self._ctx.new_page()

        def _on_response(resp):
            try:
                url = resp.url
                if "/api/search/" in url and "/full" in url:
                    _absorb(resp.json())
            except Exception:
                pass

        page.on("response", _on_response)
        try:
            url = "https://www.tiktok.com/search/video?q=" + _quote(query)
            try:
                nav_ms = 45000 if deadline is None else max(
                    1000, min(45000, int((deadline - time.monotonic()) * 1000)))
                page.goto(url, timeout=nav_ms, wait_until="domcontentloaded")
            except Exception as exc:
                _status(cb, f"TikTok search: navigation failed for {query!r} ({exc.__class__.__name__}).")
            # let the first XHR settle, then scroll ADAPTIVELY: a dead query fails FAST (settle +
            # one probe scroll ~4s instead of a fixed 8-scroll ~14s), and a productive query stops
            # as soon as two consecutive scrolls add nothing new (results stagnated).
            page.wait_for_timeout(1800)
            self._maybe_dismiss_overlays(page)
            scrolls = 0
            stagnant = 0
            last_n = len(collected)
            while (len(collected) < want and scrolls < max_scrolls and stagnant < 2
                   and (deadline is None or time.monotonic() < deadline)):
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(1100)
                scrolls += 1
                if len(collected) <= last_n:
                    stagnant += 1
                    if not collected:
                        break                     # nothing at all after settle + probe -> dead query
                else:
                    stagnant = 0
                last_n = len(collected)
            # last-resort: parse the embedded hydration JSON if XHRs were blocked
            if not collected:
                _absorb(self._hydration_items(page))
            # If we got nothing, is TikTok showing a login wall / captcha (session dead or
            # headless blocked)? Record it so the caller can fail fast with a clear message.
            if not collected:
                try:
                    low = (page.content() or "").lower()
                    if ("log in to tiktok" in low or "login-modal" in low
                            or "verify to continue" in low or "/captcha" in low
                            or "secsdk-captcha" in low):
                        _SEARCH_STATS["login_wall"] += 1
                except Exception:
                    pass
        finally:
            try:
                page.close()
            except Exception:
                pass
        _SEARCH_STATS["searches"] += 1
        _SEARCH_STATS["items"] += len(collected)
        if collected:
            self._refresh_cookies_quietly()
        if str(sort or "").upper() == "MOST_LIKED":
            def _likes(item):
                stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else (
                    item.get("stats") if isinstance(item.get("stats"), dict) else {})
                try:
                    return int(item.get("diggCount") or item.get("digg_count")
                               or item.get("likeCount") or stats.get("diggCount")
                               or stats.get("digg_count") or stats.get("likeCount") or 0)
                except (TypeError, ValueError):
                    return 0
            collected.sort(key=_likes, reverse=True)
        _status(cb, f"TikTok search {query!r}: collected {len(collected)} candidate item(s).")
        return collected[:max(0, int(want))]

    def _maybe_dismiss_overlays(self, page):
        # best-effort close of cookie / login nags that can cover the feed
        for sel in ("button:has-text('Accept all')", "button:has-text('Allow all')",
                    "div[aria-label='Close'] button", "button[aria-label='Close']"):
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=1500)
            except Exception:
                pass

    def _hydration_items(self, page):
        try:
            raw = page.evaluate(
                "() => { const e = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');"
                " return e ? e.textContent : null; }")
            if not raw:
                return {}
            data = json.loads(raw)
            scope = (((data or {}).get("__DEFAULT_SCOPE__") or {})
                     .get("webapp.search-detail") or {})
            rows = scope.get("data") or scope.get("item_list") or []
            return {"data": rows} if rows and isinstance(rows[0], dict) and "item" in rows[0] \
                else {"item_list": rows}
        except Exception:
            return {}

    def _refresh_cookies_quietly(self):
        try:
            export_cookies_txt(cookies=self.cookies())
            _write_marker()
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


def _quote(s):
    import urllib.parse
    return urllib.parse.quote(str(s), safe="")


# ------------------------------------------------------------- worker-thread plumbing
# ALL Playwright work runs on this ONE dedicated thread (see note at _EXECUTOR). Job
# threads only ever talk to it through futures, so a session can never be created in
# one thread and closed from another - the exact pattern that poisoned job threads
# with "Sync API inside the asyncio loop" and made every later reopen fail.

def _executor():
    global _EXECUTOR
    with _EXEC_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tiktok-scrape")
        return _EXECUTOR


def _on_worker_thread():
    return threading.current_thread().name.startswith("tiktok-scrape")


def _session_on_worker(status_cb=None):
    if _SESSION[0] is None:
        try:
            _SESSION[0] = Session(status_cb=status_cb)
        except Exception as exc:            # noqa: BLE001
            _status(status_cb, f"TikTok session failed to open ({exc.__class__.__name__}: {exc}).")
            _SESSION[0] = None
    return _SESSION[0]


def _search_on_worker(query, want, status_cb, sort, timeout_s=None):
    sess = _session_on_worker(status_cb)
    if sess is None:
        return []
    try:
        return sess.search(query, want=want, status_cb=status_cb, sort=sort,
                           timeout_s=timeout_s) or []
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"TikTok search failed ({exc.__class__.__name__}: {exc}).")
        # a dead browser poisons every later search on this session - drop it so the
        # next call rebuilds a fresh one (still on this same worker thread)
        try:
            _SESSION[0].close()
        except Exception:
            pass
        _SESSION[0] = None
        return []


def _session_cookies_threadsafe():
    """Current session cookies, fetched on the worker thread (None when no session)."""
    if _on_worker_thread():                 # already there - a future would deadlock
        return _SESSION[0].cookies() if _SESSION[0] is not None else None
    def _get():
        return _SESSION[0].cookies() if _SESSION[0] is not None else None
    try:
        return _executor().submit(_get).result(timeout=30)
    except Exception:
        return None


def ensure_session(status_cb=None, timeout_s=120.0):
    """Open (or reuse) the logged-in session on the worker thread. True when ready."""
    if not is_ready():
        return False
    try:
        return (_executor().submit(_session_on_worker, status_cb)
                .result(timeout=timeout_s) is not None)
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"TikTok session failed to open ({exc.__class__.__name__}: {exc}).")
        return False


def search_sync(query, want=12, status_cb=None, sort="MOST_LIKED", timeout_s=None):
    """Run a logged-in keyword search on the worker thread and block for the result."""
    if not is_ready():
        return []
    wait = 150.0 if timeout_s is None else max(10.0, float(timeout_s) + 30.0)
    try:
        return (_executor().submit(_search_on_worker, query, int(want), status_cb, sort,
                                   timeout_s).result(timeout=wait)) or []
    except concurrent.futures.TimeoutError:
        _status(status_cb, f"TikTok search timed out for {query!r}; moving on.")
        return []
    except Exception as exc:                # noqa: BLE001
        _status(status_cb, f"TikTok search failed ({exc.__class__.__name__}: {exc}).")
        return []


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

if __name__ == "__main__":
    import sys
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "login").strip().lower()
    if cmd == "login":
        ok = login()
        print("LOGGED IN" if ok else "NOT LOGGED IN")
        sys.exit(0 if ok else 1)
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "logout":
        logout()
        print("logged out")
    elif cmd == "search":
        q = sys.argv[2] if len(sys.argv) > 2 else "tokyo street"
        if not ensure_session(status_cb=print):
            print("not logged in - run: python tiktok_login.py login")
            sys.exit(1)
        items = search_sync(q, want=10, status_cb=print)
        for it in items[:10]:
            a = (it.get("author") or {})
            print("-", it.get("id"), "@" + str(a.get("uniqueId")), "|",
                  str(it.get("desc"))[:50])
        close_session()
    else:
        print("usage: python tiktok_login.py [login|status|search <q>|logout]")
