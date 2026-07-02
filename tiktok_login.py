"""TikTok login-based clip discovery - the Apify replacement.

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
    get_session(status_cb)           -> a reusable logged-in Session (or None)
    export_cookies_txt(path)         -> write cookies.txt for yt-dlp
    close_session()                  -> tear the shared session down
"""

import os
import json
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
_SESSION = [None]                       # the shared Session, lazily created per run
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
        sess = _SESSION[0]
        if sess is None:
            return None
        cookies = sess.cookies()
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
    _status(status_cb, "Opening a Chromium window - log in to your TikTok account in it. "
                       "This window stays connected; you only do this once.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
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


class Session:
    """A reusable logged-in TikTok browser session. One persistent context for a whole run."""

    def __init__(self, headless=None, status_cb=None):
        self._p = None
        self._ctx = None
        self._status_cb = status_cb
        self.headless = _HEADLESS_SEARCH if headless is None else bool(headless)
        self._open()

    def _open(self):
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
        if not self.headless:
            # remove the taskbar button of the off-screen window (it kept blinking for attention)
            for _wait in (0.4, 1.2, 2.0):
                time.sleep(_wait)
                if _hide_offscreen_from_taskbar():
                    break

    def cookies(self):
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def logged_in(self):
        return _has_session_cookie(self.cookies())

    def search(self, query, want=12, status_cb=None, sort="MOST_LIKED", max_scrolls=8):
        """Run a logged-in keyword search and return up to ~want native TikTok item dicts."""
        cb = status_cb or self._status_cb
        query = str(query or "").strip()
        if not query:
            return []
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
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
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
            while len(collected) < want and scrolls < max_scrolls and stagnant < 2:
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
        _status(cb, f"TikTok search {query!r}: collected {len(collected)} candidate item(s).")
        return collected[:max(want, len(collected))]

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


def get_session(status_cb=None):
    """Return the shared logged-in Session, creating it on first use. None if not ready."""
    if not is_ready():
        return None
    with _LOCK:
        if _SESSION[0] is None:
            try:
                _SESSION[0] = Session(status_cb=status_cb)
            except Exception as exc:        # noqa: BLE001
                _status(status_cb, f"TikTok session failed to open ({exc.__class__.__name__}: {exc}).")
                _SESSION[0] = None
                return None
        return _SESSION[0]


def close_session():
    with _LOCK:
        if _SESSION[0] is not None:
            try:
                _SESSION[0].close()
            except Exception:
                pass
            _SESSION[0] = None


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
        sess = get_session()
        if not sess:
            print("not logged in - run: python tiktok_login.py login")
            sys.exit(1)
        items = sess.search(q, want=10, status_cb=print)
        for it in items[:10]:
            a = (it.get("author") or {})
            print("-", it.get("id"), "@" + str(a.get("uniqueId")), "|",
                  str(it.get("desc"))[:50])
        close_session()
    else:
        print("usage: python tiktok_login.py [login|status|search <q>|logout]")
