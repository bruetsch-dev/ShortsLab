"""Instagram login-based Reels discovery - the third scrape backend next to TikTok + X.

Same pattern as twitter_login.py: the user logs in to instagram.com ONCE inside a Chromium
window we control (dedicated persistent profile, `instagram-profile/`), and from then on we
drive that logged-in session to run real keyword searches on the Instagram WEB APP and
harvest the Reels it returns. No official API and no API key involved: we navigate the
normal search page and read the JSON the web app itself fetches (fbsearch / clips
endpoints), which requires nothing beyond the user's own session cookies.

Each Reel is normalized into the exact item schema `clip_scraper._item_meta` already
understands (`_platform: "instagram"`), so the whole existing quality pipeline (metadata
pre-filter -> yt-dlp download with session cookies -> gates -> OCR -> vision matcher) is
reused unchanged.

THREADING: like X, ALL Playwright work is funneled through ONE dedicated worker thread
(`_EXECUTOR`, thread name prefix "igscrape"), so `search_async()` can run concurrently
with the TikTok and X searches without cross-thread Playwright errors.

Public surface used by clip_scraper / app:
    available() / is_ready() / status()
    login(status_cb, timeout_s)          -> headed one-time login (blocking)
    search_async(query, want, status_cb) -> concurrent.futures.Future -> [item, ...]
    search_stats() / reset_search_stats()
    export_cookies_txt(path)             -> cookies.txt for yt-dlp (instagram.com domains)
    close_session()
"""

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import scrape_browser_preview

try:
    from playwright.sync_api import sync_playwright
except Exception:                       # playwright not installed
    sync_playwright = None

import tiktok_login as _tt              # reuse the off-screen taskbar hider (same parking band)

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("INSTAGRAM_PROFILE_DIR") or (ROOT / "instagram-profile"))
_STATE_DIR = ROOT / "generated_assets" / "instagram"
COOKIES_TXT = _STATE_DIR / "instagram_cookies.txt"
_MARKER = _STATE_DIR / "logged_in.json"

# cookies that only exist for a logged-in Instagram session
SESSION_COOKIE_NAMES = ("sessionid",)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_LOCALE = os.environ.get("INSTAGRAM_LOCALE", "ja-JP").strip() or "ja-JP"

# XHR endpoints the Instagram web app uses for keyword search / reels feeds. The schema
# drifts, so extraction walks the whole JSON tree instead of hard-coding paths.
# Instagram moved hashtag/search results to the GraphQL endpoint (2026-07); the old REST
# markers (fbsearch/top_serp/sections/clips) no longer fire for a tag page, so `graphql` MUST
# be listened for or every IG search returns 0 items and Instagram gets auto-disabled.
_SEARCH_XHR_MARKERS = ("fbsearch", "top_serp", "/api/v1/clips/", "sections/", "hashtag",
                       "graphql", "web_info", "tags/web_info")

_SEARCH_STATS = {"searches": 0, "items": 0, "login_wall": 0, "captcha": 0,
                 "timeline_responses": 0, "health_checks": 0, "unavailable": 0,
                 "nsfw_skipped": 0}

# PRE-DOWNLOAD NSFW GATE: dropped at metadata time (never downloaded) when the caption,
# hashtags or username hit obvious adult terms. (Mirrored in twitter_login.py - keep in sync.)
NSFW_RE = re.compile(
    r"(?i)\b(porn|nsfw|xxx|onlyfans|fansly|hentai|nudes?|lewd|bdsm|fetish|camgirl|escort|"
    r"stripper|milf)\b|18\s*[+＋]|18plus|エロ|裏垢|無修正|アダルト|オフパコ|セフレ|av女優|"
    r"風俗|デリヘル|パパ活|えっち|性感")
_EXECUTOR = None
_EXEC_LOCK = threading.Lock()
_SESSION = [None]                       # only ever touched from the executor thread


def search_stats():
    return dict(_SEARCH_STATS)


def reset_search_stats():
    _SEARCH_STATS.update(searches=0, items=0, login_wall=0, captcha=0,
                         timeline_responses=0, health_checks=0, unavailable=0,
                         nsfw_skipped=0)


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
    names = {str(c.get("name") or "") for c in (cookies or [])
             if "instagram" in str(c.get("domain") or "").lower()}
    return any(n in names for n in SESSION_COOKIE_NAMES)


def is_ready():
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
    lines = ["# Netscape HTTP Cookie File", "# Written by instagram_login.py", ""]
    for c in cookies or []:
        domain = str(c.get("domain") or "")
        if "instagram" not in domain.lower():
            continue
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires")
        try:
            expires = str(int(expires)) if expires and float(expires) > 0 else "0"
        except (TypeError, ValueError):
            expires = "0"
        lines.append("\t".join([domain, "TRUE", path, secure, expires,
                                str(c.get("name") or ""), str(c.get("value") or "")]))
    return "\n".join(lines) + "\n"


def export_cookies_txt(path=None, cookies=None):
    path = Path(path or COOKIES_TXT)
    if cookies is None:
        # Playwright call - MUST run on the worker thread (see twitter_login for the
        # greenlet cross-thread crash this avoids).
        cookies = _session_cookies_threadsafe()
        if cookies is None:
            return str(path) if path.exists() else None
    if not _has_session_cookie(cookies):
        return str(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_cookies_to_netscape(cookies), "utf-8")
    return str(path)


def _kill_stale_profile_processes():
    """Kill any Chromium zombie still holding instagram-profile (same failure mode as
    TikTok: the next launch delegates to the zombie and dies with TargetClosedError)."""
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
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


# --------------------------------------------------------------------------- login

def login(status_cb=None, timeout_s=300):
    """Open a real Chromium window, let the user sign in to Instagram, persist the session.
    Blocking."""
    if not available():
        _status(status_cb, "Instagram login: Playwright is not installed (pip install "
                           "playwright && playwright install chromium).")
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _kill_stale_profile_processes()
    _status(status_cb, "Opening a Chromium window - log in to your Instagram account in it. "
                       "You only do this once.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--window-position=160,80"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _tt._place_window_visible(ctx, 160, 80)   # force ON-SCREEN even if a scrape parked it
            try:
                page.goto("https://www.instagram.com/accounts/login/", timeout=60000)
            except Exception:
                try:
                    page.goto("https://www.instagram.com/", timeout=60000)
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
                if not ctx.pages:
                    break
                time.sleep(2.0)
            if ok:
                export_cookies_txt(cookies=ctx.cookies())
                _write_marker()
                _status(status_cb, "Instagram login captured - the session is saved. Future "
                                   "scrapes search Instagram Reels alongside TikTok and X.")
            else:
                _status(status_cb, "Instagram login timed out / window closed before sign-in "
                                   "completed.")
            return ok
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def logout():
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

_HASHTAG_RE = re.compile(r"#([^\s#]{1,60})")


def instagram_search_url(query):
    """Route native hashtags to Instagram's tag surface instead of its keyword box."""
    from urllib.parse import quote
    value = str(query or "").strip()
    if value.startswith("#"):
        tag = value.lstrip("#").strip().split()[0] if value.lstrip("#").strip() else ""
        return f"https://www.instagram.com/explore/tags/{quote(tag, safe='')}/"
    return f"https://www.instagram.com/explore/search/keyword/?q={quote(value, safe='')}"


def _extract_reels(payload):
    """Recursively pull Reels (media_type == 2 videos with a shortcode) out of any
    Instagram web-API response. The schema drifts across endpoints (fbsearch top_serp,
    clips feed, hashtag sections), so we walk the whole tree and recognise media dicts
    by shape instead of hard-coding response paths."""
    found = {}

    def _walk(node):
        if isinstance(node, dict):
            code = node.get("code")
            mtype = node.get("media_type")
            if code and mtype == 2 and (node.get("video_versions") or node.get("video_duration")):
                found.setdefault(str(code), node)
            # some payloads nest the actual media under a "media" key
            inner = node.get("media")
            if isinstance(inner, dict):
                _walk(inner)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)
    items = []
    for code, m in found.items():
        cap = m.get("caption") if isinstance(m.get("caption"), dict) else {}
        desc = str((cap or {}).get("text") or "")
        user = m.get("user") if isinstance(m.get("user"), dict) else {}
        uname = str(user.get("username") or "")
        # NSFW gate BEFORE the item ever reaches ranking/download
        if NSFW_RE.search(desc + " " + uname):
            _SEARCH_STATS["nsfw_skipped"] += 1
            continue
        try:
            likes = int(m.get("like_count") or 0)
        except (TypeError, ValueError):
            likes = 0
        try:
            views = int(m.get("play_count") or m.get("view_count") or m.get("ig_play_count") or 0)
        except (TypeError, ValueError):
            views = 0
        try:
            dur_ms = int(float(m.get("video_duration") or 0) * 1000)
        except (TypeError, ValueError):
            dur_ms = 0
        try:
            created = int(m.get("taken_at") or 0)
        except (TypeError, ValueError):
            created = 0
        width = height = 0
        try:
            width = int(m.get("original_width") or 0)
            height = int(m.get("original_height") or 0)
        except (TypeError, ValueError):
            pass
        if (not width or not height) and isinstance(m.get("video_versions"), list):
            for vv in m["video_versions"]:
                if isinstance(vv, dict) and vv.get("width") and vv.get("height"):
                    width, height = int(vv["width"]), int(vv["height"])
                    break
        tags = [{"name": t} for t in _HASHTAG_RE.findall(desc)[:12]]
        items.append({
            "id": str(m.get("pk") or code),
            "desc": desc,
            "webVideoUrl": f"https://www.instagram.com/reel/{code}/",
            "_source": "instagram_login",     # backend_download -> yt-dlp
            "_platform": "instagram",         # scaled like-gate + reporting
            "video": {"width": width, "height": height, "duration": dur_ms},
            "stats": {"diggCount": likes, "playCount": views},
            "createTime": created,
            "author": {"uniqueId": uname, "nickname": uname},
            "hashtags": tags,
        })
    return items


class Session:
    """A logged-in Instagram browser session. MUST only be used from the executor thread."""

    def __init__(self, status_cb=None):
        self._p = None
        self._ctx = None
        self._status_cb = status_cb
        self._pids = set()
        self._hide_stop = threading.Event()
        self._watcher = None
        self._open()

    def _open(self):
        _kill_stale_profile_processes()
        self._p = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--no-first-run",
                "--no-default-browser-check", "--mute-audio"]
        visible = os.environ.get("INSTAGRAM_WINDOW_VISIBLE", "").strip().lower() in ("1", "true", "yes")
        if not visible:
            # same off-screen trick as TikTok/X: headed (Instagram degrades headless
            # sessions hard) but parked far off-screen with anti-throttle flags; the
            # taskbar button is stripped below.
            args += ["--window-position=-2400,-2400", "--window-size=1280,900",
                     "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding",
                     "--disable-background-timer-throttling",
                     "--disable-features=CalculateNativeWinOcclusion"]
        self._ctx = self._p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, user_agent=_UA, locale=_LOCALE,
            viewport={"width": 1280, "height": 900}, args=args)
        if not visible:
            _tt._park_window_offscreen(self._ctx, "INSTAGRAM_WINDOW_VISIBLE")
            for _wait in (0.4, 1.2, 2.0):
                time.sleep(_wait)
                if _tt._hide_offscreen_from_taskbar():
                    break
            # robust, CDP-independent enforcement by browser PID + session-long watcher
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

    def search(self, query, want=12, status_cb=None, max_scrolls=12, timeout_s=None,
               sort="MOST_LIKED"):
        cb = status_cb or self._status_cb
        query = str(query or "").strip()
        if not query:
            return []
        deadline = (time.monotonic() + max(0.1, float(timeout_s))
                    if timeout_s is not None else None)
        _tt._hide_offscreen_from_taskbar()
        collected, seen = [], set()
        xhr_seen = [False]

        def _absorb(payload):
            for it in _extract_reels(payload):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                collected.append(it)

        page = self._ctx.new_page()

        def _on_response(resp):
            try:
                if any(m in resp.url for m in _SEARCH_XHR_MARKERS):
                    if not xhr_seen[0]:
                        xhr_seen[0] = True
                        _SEARCH_STATS["timeline_responses"] += 1
                    _absorb(resp.json())
            except Exception:
                pass

        page.on("response", _on_response)
        try:
            url = instagram_search_url(query)
            try:
                nav_ms = 45000 if deadline is None else max(
                    1000, min(45000, int((deadline - time.monotonic()) * 1000)))
                page.goto(url, timeout=nav_ms, wait_until="domcontentloaded")
                scrape_browser_preview.capture(page, "Instagram", query, sort)
            except Exception as exc:
                _status(cb, f"Instagram search: navigation failed for {query!r} "
                            f"({exc.__class__.__name__}).")
            page.wait_for_timeout(2600)
            scrape_browser_preview.capture(page, "Instagram", query, sort, force=True)
            scrolls, stagnant, last_n = 0, 0, len(collected)
            while (len(collected) < want and scrolls < max_scrolls and stagnant < 4
                   and (deadline is None or time.monotonic() < deadline)):
                page.mouse.wheel(0, 2600)
                page.wait_for_timeout(1200)
                scrape_browser_preview.capture(page, "Instagram", query, sort, force=True)
                scrolls += 1
                if len(collected) <= last_n:
                    stagnant += 1
                    if not collected:
                        break
                else:
                    stagnant = 0
                last_n = len(collected)
            # Hashtag grids sometimes hydrate without firing one of the currently known JSON
            # endpoints. Parse embedded JSON first, then retain visible Reel links as a minimal
            # fallback so a dense tag grid is never reported as a false zero-result search.
            if not collected:
                try:
                    for script in page.locator('script[type="application/json"]').all():
                        raw = script.text_content() or ""
                        if raw and len(raw) < 25_000_000:
                            try:
                                _absorb(json.loads(raw))
                            except Exception:
                                continue
                except Exception:
                    pass
            if not collected:
                try:
                    for anchor in page.locator('a[href*="/reel/"]').all()[:max(1, int(want))]:
                        href = str(anchor.get_attribute("href") or "")
                        match = re.search(r"/reel/([^/?#]+)/?", href)
                        if not match or match.group(1) in seen:
                            continue
                        code = match.group(1)
                        seen.add(code)
                        try:
                            desc = str(anchor.locator("img").first.get_attribute("alt") or "")
                        except Exception:
                            desc = ""
                        if NSFW_RE.search(desc):
                            _SEARCH_STATS["nsfw_skipped"] += 1
                            continue
                        collected.append({
                            "id": code, "desc": desc,
                            "webVideoUrl": f"https://www.instagram.com/reel/{code}/",
                            "_source": "instagram_login", "_platform": "instagram",
                            "video": {"width": 0, "height": 0, "duration": 0},
                            "stats": {"diggCount": 0, "playCount": 0}, "createTime": 0,
                            "author": {"uniqueId": "", "nickname": ""},
                            "hashtags": [{"name": t} for t in _HASHTAG_RE.findall(desc)[:12]],
                        })
                except Exception:
                    pass
            if not collected:
                try:
                    # Detect a REAL block by the REDIRECT URL, never by substring in the page
                    # HTML: Instagram's inline JS bundle contains the literals "captcha" and
                    # "challenge" on EVERY page, so matching page.content() flagged a captcha on
                    # a perfectly healthy #japan grid and disabled Instagram for the whole run.
                    url = (page.url or "").lower()
                    if "/accounts/login" in url or "/accounts/suspended" in url:
                        _SEARCH_STATS["login_wall"] += 1
                    if "/challenge" in url or "/checkpoint" in url:
                        _SEARCH_STATS["captcha"] += 1
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
            try:
                export_cookies_txt(cookies=self.cookies())
            except Exception:
                pass
        # Instagram search has no server-side sort orders -> rank locally like X does.
        sort_mode = str(sort or "MOST_LIKED").upper()
        metric = "diggCount" if sort_mode == "MOST_LIKED" else "playCount"
        if sort_mode in {"MOST_LIKED", "MOST_VIEWED"}:
            collected.sort(key=lambda item: int((item.get("stats") or {}).get(metric) or 0),
                           reverse=True)
        elif sort_mode == "MOST_RECENT":
            collected.sort(key=lambda item: int(item.get("createTime") or 0), reverse=True)
        return collected[:max(0, int(want))]

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


# ------------------------------------------------------------------ executor plumbing

def _executor():
    global _EXECUTOR
    with _EXEC_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="igscrape")
        return _EXECUTOR


def _on_worker_thread():
    return threading.current_thread().name.startswith("igscrape")


def _session_cookies_threadsafe():
    """Read the session cookies ON the worker thread (None when no session)."""
    if _on_worker_thread():                 # already there - a future would deadlock
        return _SESSION[0].cookies() if _SESSION[0] is not None else None
    def _get():
        return _SESSION[0].cookies() if _SESSION[0] is not None else None
    try:
        return _executor().submit(_get).result(timeout=30)
    except Exception:
        return None


def _session_on_worker(status_cb=None):
    if _SESSION[0] is None:
        try:
            _SESSION[0] = Session(status_cb=status_cb)
        except Exception as exc:  # noqa: BLE001
            _status(status_cb, f"Instagram session failed to open "
                               f"({exc.__class__.__name__}: {exc}).")
            _SESSION[0] = None
    return _SESSION[0]


def _search_on_worker(query, want, status_cb, timeout_s=None, sort="MOST_LIKED"):
    sess = _session_on_worker(status_cb)
    if sess is None:
        return []
    try:
        return sess.search(query, want=want, status_cb=status_cb, timeout_s=timeout_s,
                           sort=sort) or []
    except Exception as exc:  # noqa: BLE001
        _status(status_cb, f"Instagram search failed ({exc.__class__.__name__}: {exc}).")
        return []


def search_async(query, want=12, status_cb=None, timeout_s=None, sort="MOST_LIKED"):
    """Run a logged-in Instagram Reels search on the dedicated worker thread. Returns a
    Future resolving to a list of normalized items; resolves to [] when not logged in."""
    if not is_ready():
        f = concurrent.futures.Future()
        f.set_result([])
        return f
    return _executor().submit(_search_on_worker, query, int(want), status_cb, timeout_s, sort)


def health_check(timeout_s=45):
    """API-free session/search check using a known broad query."""
    _SEARCH_STATS["health_checks"] += 1
    before = search_stats()
    try:
        items = search_async("#japan", want=3, status_cb=lambda _m: None,
                             timeout_s=timeout_s, sort="MOST_RECENT").result(
                                 timeout=max(5, timeout_s + 5)) or []
    except Exception:
        items = []
    after = search_stats()
    login_problem = after.get("login_wall", 0) > before.get("login_wall", 0)
    captcha = after.get("captcha", 0) > before.get("captcha", 0)
    xhr = after.get("timeline_responses", 0) > before.get("timeline_responses", 0)
    ok = bool(items or (xhr and not login_problem and not captcha))
    if not ok:
        _SEARCH_STATS["unavailable"] += 1
    return {"ok": ok, "items": len(items), "timeline_response": xhr,
            "login_wall": login_problem, "captcha": captcha}


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
        q = sys.argv[2] if len(sys.argv) > 2 else "japan street food"
        items = search_async(q, want=10, status_cb=print).result()
        for it in items[:10]:
            print("-", it["id"], "@" + it["author"]["uniqueId"], "|",
                  f"{it['stats']['diggCount']} likes |", it["desc"][:50])
        close_session()
    else:
        print("usage: python instagram_login.py [login|status|search <q>|logout]")
