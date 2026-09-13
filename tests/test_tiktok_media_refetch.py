"""Chrome does not keep large media bodies, so the download must ask for the file again.

Measured on the pufferfish run (2026-09-03): 49 of 116 sources were rejected as "could not be
downloaded". On the ones re-checked by hand, EVERY media response failed with
"Protocol error (Network.getResponseBody): No data found" - while the very same URLs served
complete files of 4.9 to 70 MB when re-requested through the browser context. Re-fetching turned
2 of 14 undeliverable posts into 14 of 14.
"""
import types

import tiktok_login

MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * (200 * 1024)


class _Response:
    def __init__(self, url, ctype="video/mp4", body=None, raises=False):
        self.url = url
        self.headers = {"content-type": ctype}
        self._body = body
        self._raises = raises

    def body(self):
        if self._raises:
            raise RuntimeError("Protocol error (Network.getResponseBody): No data found")
        return self._body


class _Page:
    def __init__(self, responses):
        self._responses = responses
        self._handler = None

    def on(self, _event, handler):
        self._handler = handler

    def goto(self, *_a, **_k):
        for response in self._responses:
            self._handler(response)

    def wait_for_selector(self, *_a, **_k):
        return None

    def evaluate(self, *_a, **_k):
        return None

    def wait_for_timeout(self, *_a, **_k):
        return None

    def close(self):
        return None


class _Request:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, **kwargs):
        self.asked.append((url, kwargs))
        status, body = self.answers.get(url, (404, b""))
        return types.SimpleNamespace(status=status, body=lambda: body)


class _Ctx:
    def __init__(self, page, request):
        self._page = page
        self.request = request

    def new_page(self):
        return self._page


def _session(responses, answers):
    session = tiktok_login.Session.__new__(tiktok_login.Session)
    request = _Request(answers)
    session._ctx = _Ctx(_Page(responses), request)
    session._maybe_dismiss_overlays = lambda _page: None
    return session, request


def test_an_evicted_body_is_refetched_through_the_context(tmp_path):
    url = "https://v16-webapp-prime.tiktok.com/video/tos/x/abc/?a=1988"
    session, request = _session([_Response(url, raises=True)], {url: (200, MP4)})
    dest = tmp_path / "clip.mp4"
    assert session.download_video("https://www.tiktok.com/@a/video/1", dest) == dest
    assert dest.read_bytes() == MP4
    assert request.asked[0][0] == url
    assert request.asked[0][1]["headers"]["referer"] == "https://www.tiktok.com/"


def test_a_body_that_is_still_there_is_used_without_a_second_fetch(tmp_path):
    url = "https://v16-webapp-prime.tiktok.com/video/tos/x/abc/?a=1988"
    session, request = _session([_Response(url, body=MP4)], {})
    dest = tmp_path / "clip.mp4"
    assert session.download_video("https://www.tiktok.com/@a/video/1", dest) == dest
    assert request.asked == []


def test_it_moves_on_to_the_next_url_when_one_will_not_serve(tmp_path):
    bad = "https://v16-webapp-prime.tiktok.com/video/tos/x/bad/?a=1"
    good = "https://v16-webapp-prime.tiktok.com/video/tos/x/good/?a=1"
    session, request = _session([_Response(bad, raises=True), _Response(good, raises=True)],
                                {bad: (403, b""), good: (200, MP4)})
    dest = tmp_path / "clip.mp4"
    assert session.download_video("https://www.tiktok.com/@a/video/1", dest) == dest
    assert [u for u, _ in request.asked] == [bad, good]


def test_a_page_that_requests_no_media_fails_fast(tmp_path):
    said = []
    session, request = _session([], {})
    assert session.download_video("https://www.tiktok.com/@a/video/1", tmp_path / "c.mp4",
                                  status_cb=said.append) is None
    assert request.asked == []
    assert any("requested no media" in m for m in said)


def test_a_body_that_is_not_a_video_is_refused(tmp_path):
    url = "https://v16-webapp-prime.tiktok.com/video/tos/x/abc/?a=1"
    session, _request = _session([_Response(url, raises=True)],
                                 {url: (200, b"<html>nope</html>" + b"x" * (100 * 1024))})
    dest = tmp_path / "clip.mp4"
    assert session.download_video("https://www.tiktok.com/@a/video/1", dest) is None
    assert not dest.exists()


def test_non_video_responses_are_not_treated_as_media(tmp_path):
    """A text/plain body on a /video/tos/ URL is not the file."""
    text = "https://v16-webapp.tiktokcdn-eu.com/video/tos/x/manifest/?a=1"
    session, request = _session([_Response(text, ctype="text/plain", raises=True)], {})
    assert session.download_video("https://www.tiktok.com/@a/video/1", tmp_path / "c.mp4") is None
    assert request.asked == []
