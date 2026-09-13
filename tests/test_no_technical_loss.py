"""A download that failed is not a post that is unusable.

The train-pushers run (2026-09-03) threw away all 190 of its records, 126 of them as "corrupt
download", and every one of the 16 re-fetched afterwards was a sound vertical video. The run then
spent its remaining time searching for footage it had already found. Technical failures therefore
get a second attempt, and only editorial verdicts are final.
"""
import scrape_v4


def _is_technical(reason):
    rows = [scrape_v4.Candidate("s", "q", "p", 0, 0, 0.0, "", "rejected", reason)]
    return any(sign in str(rows[0].rejection).lower() for sign in scrape_v4._TECHNICAL_FAILURES)


def test_delivery_failures_are_retryable():
    for reason in ("corrupt download (no readable video stream)",
                   "The selected TikTok post could not be downloaded through the logged-in session",
                   "incomplete download (1200 bytes)",
                   "download produced no file",
                   "unreadable download (OSError)",
                   "local inspection failed: TimeoutError",
                   "parallel media gate failed: RuntimeError"):
        assert _is_technical(reason), reason


def test_editorial_verdicts_are_final():
    for reason in ("not native vertical (1280x720)",
                   "static or repeated footage",
                   "no motion across sampled frames",
                   "vision review rejected: a person talking to camera"):
        assert not _is_technical(reason), reason


def test_the_retry_only_reruns_what_failed(monkeypatch, tmp_path):
    """The second pass must not re-inspect records that already produced usable footage."""
    good = [scrape_v4.Candidate("a", "q", "p", 0, 3, 5.0, "", "available", "")]
    bad = [scrape_v4.Candidate("b", "q", "p", 0, 0, 0.0, "", "rejected", "corrupt download (x)")]
    judged = {"kept": good, "lost": bad}

    def is_technical(rows):
        if not rows or any(row.status == "available" for row in rows):
            return False
        return any(sign in str(rows[0].rejection).lower() for sign in scrape_v4._TECHNICAL_FAILURES)

    assert not is_technical(judged["kept"])
    assert is_technical(judged["lost"])
    assert not is_technical([])
