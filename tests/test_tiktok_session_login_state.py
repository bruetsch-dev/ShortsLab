"""A session that is not signed in must say so, not quietly lose half the run.

Measured on the pufferfish run (2026-09-03): `is_ready()` reported a connected TikTok account
while `sessionid` was gone from the browser profile - it only checks a marker file. The profile's
export still held the cookies, so the login could be put back without asking the user for
anything.
"""
import json

import tiktok_login

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".tiktok.com\tTRUE\t/\tTRUE\t1798391943\tsessionid\tabc123\n"
    ".tiktok.com\tTRUE\t/\tTRUE\t1798391943\tsid_tt\tdef456\n"
    ".tiktok.com\tTRUE\t/\tFALSE\t0\tttwid\tsession-only\n"
)


def test_cookies_survive_a_round_trip():
    parsed = tiktok_login._cookies_from_netscape(NETSCAPE)
    assert {c["name"] for c in parsed} == {"sessionid", "sid_tt", "ttwid"}
    assert tiktok_login._has_session_cookie(parsed)
    session = [c for c in parsed if c["name"] == "sessionid"][0]
    assert session["domain"] == ".tiktok.com" and session["secure"] is True
    assert session["expires"] == 1798391943
    # A session cookie has no expiry at all rather than an expiry of zero.
    assert "expires" not in [c for c in parsed if c["name"] == "ttwid"][0]
    back = tiktok_login._cookies_to_netscape(parsed)
    assert tiktok_login._has_session_cookie(tiktok_login._cookies_from_netscape(back))


def test_junk_lines_are_skipped():
    assert tiktok_login._cookies_from_netscape("") == []
    assert tiktok_login._cookies_from_netscape("# just a comment\n\nnot\ttab\tdelimited\n") == []


class _Ctx:
    def __init__(self, cookies):
        self._cookies = list(cookies)

    def cookies(self):
        return list(self._cookies)

    def add_cookies(self, cookies):
        self._cookies.extend(cookies)


def _session(cookies, status):
    obj = tiktok_login.Session.__new__(tiktok_login.Session)
    obj._ctx = _Ctx(cookies)
    obj._status_cb = status.append
    return obj


def test_a_lost_login_is_restored_from_the_saved_export(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok_login, "COOKIES_TXT", tmp_path / "cookies.txt")
    monkeypatch.setattr(tiktok_login, "_MARKER", tmp_path / "logged_in.json")
    monkeypatch.setattr(tiktok_login, "_STATE_DIR", tmp_path)
    tiktok_login.COOKIES_TXT.write_text(NETSCAPE, encoding="utf-8")
    said = []
    session = _session([{"name": "ttwid", "value": "x", "domain": ".tiktok.com"}], said)
    session._restore_or_report_login()
    assert session.logged_in()
    assert any("restored it from the saved cookies" in m for m in said)
    assert tiktok_login._MARKER.exists()


def test_without_a_usable_export_the_app_stops_claiming_a_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok_login, "COOKIES_TXT", tmp_path / "missing.txt")
    monkeypatch.setattr(tiktok_login, "_MARKER", tmp_path / "logged_in.json")
    tiktok_login._MARKER.write_text(json.dumps({"at": "2026-09-03 13:37:12"}), encoding="utf-8")
    said = []
    session = _session([{"name": "ttwid", "value": "x", "domain": ".tiktok.com"}], said)
    session._restore_or_report_login()
    assert not session.logged_in()
    assert any("NOT signed in" in m for m in said)
    assert not tiktok_login._MARKER.exists(), "a stale marker keeps the UI lying about the login"


def test_a_healthy_session_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok_login, "COOKIES_TXT", tmp_path / "cookies.txt")
    said = []
    session = _session([{"name": "sessionid", "value": "x", "domain": ".tiktok.com"}], said)
    session._restore_or_report_login()
    assert session.logged_in() and said == []
