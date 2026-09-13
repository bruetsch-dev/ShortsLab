"""Search needs the login. Download does not - and a dead measurement had made it look required.

A note in `backend_download` said "yt-dlp CANNOT fetch TikTok any more", on the strength of the
capsule-hotel run (2026-09-03): 116 calls, 116 failures. So the logged-in session went FIRST for
every TikTok candidate - a single-threaded browser, a cookie export and a rate-limit risk per
clip - and in V4 `_download_tiktok_post_media` refused outright when no session was ready, so a
run could discover posts through Scrape.do and download none of them.

Re-measured 2026-09-09 against eight post ids from a finished run, with no cookie file at all:

    8 of 8 resolved; one downloaded whole - 30.3 MB, a real MP4 (ftypisom), in four seconds

The installed yt-dlp is 2026.08.19 and its TikTok extractor was last touched 2026-08-27 - the fix
landed after the measurement that was still being quoted, and nobody looked again.

The session stays as a fallback for the day TikTok closes the public path again.
"""
from pathlib import Path

import clip_scraper


def _wire(monkeypatch, tmp_path, ytdlp=None, session=None, ready=True):
    order = []

    def _yt(url, dest, status_cb=None, max_seconds=16.0):
        order.append(f"ytdlp:{'whole' if not max_seconds else int(max_seconds)}")
        if ytdlp:
            Path(dest).write_bytes(b"\x00\x00\x00\x20ftypisom" + b"0" * (100 * 1024))
            return dest
        return None

    def _sess(url, dest, status_cb=None):
        order.append("session")
        if session:
            Path(dest).write_bytes(b"\x00\x00\x00\x20ftypisom" + b"0" * (100 * 1024))
            return dest
        return None

    monkeypatch.setattr(clip_scraper, "_ytdlp_fetch", _yt)
    monkeypatch.setattr(clip_scraper, "tiktok_backend_ready", lambda: ready)
    monkeypatch.setattr(clip_scraper.tiktok_login, "download_sync", _sess)
    monkeypatch.setattr(clip_scraper, "_tiktok_session_media_fetch",
                        lambda item, dest, status_cb=None: order.append("signed") or None)
    return order


def test_the_public_extractor_goes_first_for_tiktok(monkeypatch, tmp_path):
    order = _wire(monkeypatch, tmp_path, ytdlp=True, session=True)
    item = {"webVideoUrl": "https://www.tiktok.com/@a/video/1"}
    assert clip_scraper.backend_download(item, tmp_path / "a.mp4")
    assert order == ["ytdlp:16"], "the logged-in session is being tried before the open path"


def test_the_session_still_catches_what_the_public_path_misses(monkeypatch, tmp_path):
    order = _wire(monkeypatch, tmp_path, ytdlp=False, session=True)
    item = {"webVideoUrl": "https://www.tiktok.com/@a/video/1"}
    assert clip_scraper.backend_download(item, tmp_path / "a.mp4")
    assert order == ["ytdlp:16", "session"]


def test_no_session_is_no_longer_a_dead_end(monkeypatch, tmp_path):
    """This is the case that mattered: Scrape.do finds the posts, nobody is logged in."""
    order = _wire(monkeypatch, tmp_path, ytdlp=True, ready=False)
    item = {"webVideoUrl": "https://www.tiktok.com/@a/video/1"}
    assert clip_scraper.backend_download(item, tmp_path / "a.mp4")
    assert order == ["ytdlp:16"]


def test_a_source_download_asks_for_the_whole_post(monkeypatch, tmp_path):
    """The 16-second default belongs to the preview use. A V4 window can start at 37s, so a
    truncated file there is not a cheaper download - it is different footage."""
    order = _wire(monkeypatch, tmp_path, ytdlp=True)
    item = {"webVideoUrl": "https://www.tiktok.com/@a/video/1"}
    assert clip_scraper.backend_download(item, tmp_path / "a.mp4", max_seconds=None)
    assert order == ["ytdlp:whole"]


def test_other_platforms_are_unchanged(monkeypatch, tmp_path):
    order = _wire(monkeypatch, tmp_path, ytdlp=True)
    item = {"webVideoUrl": "https://www.instagram.com/reel/abc/"}
    assert clip_scraper.backend_download(item, tmp_path / "a.mp4")
    assert order == ["ytdlp:16"]


def test_nothing_delivers_and_the_signed_url_is_the_last_resort(monkeypatch, tmp_path):
    order = _wire(monkeypatch, tmp_path)
    item = {"webVideoUrl": "https://www.tiktok.com/@a/video/1"}
    assert clip_scraper.backend_download(item, tmp_path / "a.mp4") is None
    assert order == ["ytdlp:16", "session", "signed"]
