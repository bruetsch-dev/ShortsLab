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
    webview.create_window("Shortslab", url, width=1440, height=920, min_size=(1024, 680))
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
    try:
        server.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    main()
