"""Native Shortslab window.

Runs the app's HTTP server IN THIS PROCESS and shows it in a real OS window (WebView2 via
pywebview) - no external browser, no tabs, no address bar. It IS its own app. Closing the
window quits Shortslab. If pywebview / the WebView2 runtime is unavailable, it degrades to
opening the app in the default browser so the app still works everywhere.
"""
import argparse
import ctypes
import os
import socket
import sys
import threading
import time
import urllib.request

import app  # defines Handler + QuietServer and installs crash logging at import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def _enable_context_menus():
    """Give the native window its right-click menu back (copy/paste in the script box).

    pywebview's WebView2 backend ties FOUR unrelated things to debug mode in one line each:

        settings.AreBrowserAcceleratorKeysEnabled = _state['debug']
        settings.AreDefaultContextMenusEnabled    = _state['debug']   # <- the one we want
        settings.AreDevToolsEnabled              = _state['debug']
        settings.IsStatusBarEnabled              = _state['debug']

    So a release window has no context menu ANYWHERE - you cannot right-click-paste a script.
    (webview.settings['SHOW_DEFAULT_MENUS'] looks like the knob for this but is read only by the
    macOS backend.) Starting with debug=True would fix it and drag in devtools, browser hotkeys
    and a hover status bar; instead we re-enable the single setting right after pywebview has
    applied its own, inside ITS handler - which is the UI thread, the only thread allowed to
    touch CoreWebView2.Settings.

    Best effort: on any pywebview version where this no longer fits, the app runs exactly as
    before, just without the context menu.
    """
    try:
        from webview.platforms import edgechromium
    except Exception:
        return False
    original = getattr(edgechromium.EdgeChrome, "on_webview_ready", None)
    if original is None or getattr(original, "_shortslab_ctxmenu", False):
        return False

    def on_webview_ready(self, sender, args):
        # finally, not a plain sequence: pywebview applies the settings and THEN navigates, so an
        # exception out of its load_url would leave the context menu switched off behind us.
        try:
            original(self, sender, args)
        finally:
            try:
                core = getattr(sender, "CoreWebView2", None)
                if core is not None:        # None when init failed and pywebview bailed out early
                    core.Settings.AreDefaultContextMenusEnabled = True
            except Exception as exc:                               # pragma: no cover - GUI path
                print("[native] could not enable context menus:", exc)

    on_webview_ready._shortslab_ctxmenu = True
    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    return True


def _cancel_active_jobs():
    """Tell in-process workers to stop before the native window disappears."""
    try:
        with app.JOB_LOCK:
            jobs = list(app.JOBS.values())
        for job in jobs:
            event = job.get("cancel_event") if isinstance(job, dict) else None
            if event:
                event.set()
    except Exception:
        pass


def _close_scraper_sessions():
    """Close Playwright contexts so their Chromium child processes do not survive Shortslab."""
    closers = []
    for module_name in ("tiktok_login", "twitter_login", "instagram_login",
                        "higgsfield_login"):
        try:
            module = __import__(module_name)
            close = getattr(module, "close_session", None)
            if callable(close):
                worker = threading.Thread(target=close, daemon=True,
                                          name=f"close-{module_name}")
                worker.start()
                closers.append(worker)
        except Exception:
            pass
    # Do not let a wedged browser/session keep an already-closed app window alive invisibly.
    deadline = time.monotonic() + 4.0
    for worker in closers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))


def _shutdown_everything(server):
    _cancel_active_jobs()
    _close_scraper_sessions()
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def _app_icon():
    """The app's .ico for the window + taskbar (falls back to png / None)."""
    for name in ("app_icon.ico", "start_icon.ico", "app_icon.png"):
        path = os.path.join(ROOT, "static", name)
        if os.path.exists(path):
            return path
    return None


def _set_taskbar_identity():
    """Give the process its OWN Windows taskbar identity so it shows the Shortslab icon and
    groups on its own, instead of inheriting python.exe's icon."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Shortslab.App")
    except Exception:
        pass


def _free_port(preferred, host="127.0.0.1"):
    """Reuse the preferred port when it's free, else grab any free one."""
    for port in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((host, port))
                return probe.getsockname()[1]
        except OSError:
            continue
    return preferred


def _wait_until_up(url, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except Exception:
            time.sleep(0.15)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7865)
    args = parser.parse_args()

    port = _free_port(args.port, args.host)
    server = app.QuietServer((args.host, port), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{args.host}:{port}"
    print(f"Shortslab server on {url}")
    _wait_until_up(url)

    try:
        import webview
    except Exception as exc:  # pragma: no cover - only when pywebview missing
        import webbrowser
        print(f"pywebview unavailable ({exc}); opening in the default browser instead.")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    _set_taskbar_identity()
    icon = _app_icon()

    class _NativeApi:
        """Exposed to the page as window.pywebview.api. WebView2 does not act on <a download>
        links, so the in-page "Download" button calls save_file() to get a real Save-As dialog."""
        def save_file(self, src_path):
            import os
            import shutil
            try:
                if not src_path or not os.path.isfile(src_path):
                    return {"ok": False, "error": "file not found"}
                windows = getattr(webview, "windows", None) or []
                win = windows[0] if windows else None
                name = os.path.basename(src_path)
                downloads = os.path.join(os.path.expanduser("~"), "Downloads")
                start_dir = downloads if os.path.isdir(downloads) else os.path.expanduser("~")
                dest = win.create_file_dialog(webview.SAVE_DIALOG, directory=start_dir,
                                              save_filename=name) if win else None
                if not dest:
                    return {"ok": False, "cancelled": True}
                if isinstance(dest, (list, tuple)):
                    dest = dest[0]
                shutil.copy2(src_path, dest)
                return {"ok": True, "dest": str(dest)}
            except Exception as exc:  # pragma: no cover - GUI dialog path
                return {"ok": False, "error": str(exc)}

    _enable_context_menus()     # must run before start(): it patches the backend's ready handler
    window = webview.create_window("Shortslab", url, js_api=_NativeApi(),
                                   width=1440, height=920, min_size=(1024, 680))
    closed = threading.Event()

    def _on_window_closed(*_args):
        # Keep the GUI callback non-blocking. Session cleanup can wait on Playwright worker
        # threads, so perform it outside WebView2's event thread.
        if closed.is_set():
            return
        closed.set()
        threading.Thread(target=_shutdown_everything, args=(server,), daemon=True).start()

    # NOTE: we deliberately do NOT hook window.events.closing. Calling window.evaluate_js() (or a
    # modal dialog / time.sleep) from inside WebView2's closing event runs on the UI thread and
    # deadlocks against it, so the window would hang and never close. The unsaved-changes guard
    # lives in the page itself (beforeunload) for the browser; the native window just closes cleanly.

    try:
        window.events.closed += _on_window_closed
    except Exception:
        # Older pywebview versions still make webview.start() return after the last window closes;
        # the unconditional cleanup below remains the fallback.
        pass
    # edgechromium = the WebView2 engine (already installed); pywebview auto-falls-back otherwise.
    # icon = the Shortslab icon for the window + taskbar (so it's not the python.exe logo).
    start_kwargs = {"gui": "edgechromium"}
    if icon:
        start_kwargs["icon"] = icon
    try:
        webview.start(**start_kwargs)
    except TypeError:
        # older pywebview without the icon= kwarg
        try:
            webview.start(gui="edgechromium")
        except Exception:
            webview.start()
    except Exception:
        webview.start()
    _shutdown_everything(server)
    # ThreadPoolExecutors created by scraper integrations use non-daemon worker threads. Even
    # after their browser context is closed they can keep python.exe alive invisibly. At this
    # point the only native window is gone and cleanup has run, so guarantee process termination.
    os._exit(0)


if __name__ == "__main__":
    main()
