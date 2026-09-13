"""A download that is not a video must never occupy a source slot.

Measured on the delivered run `japan_has_hotels_where_you_can_stay`: of 70 "successful"
downloads, 9 were multi-megabyte files containing no MP4 header at all - random bytes where
`ftyp` belongs. Every one of them was accepted as a source, took a place in its chapter's pool,
and was only unmasked much later by a duration probe reporting "unreadable or empty file". By
then the search record was paid for and the chapter had stopped looking, so the selector was
left choosing between too few sources and had to reuse footage.

The check below was validated against all 67 proxies still on disk from that run: it catches
9 of 9 corrupt files and lets through 58 of 58 good ones.
"""

import tempfile
import unittest
from pathlib import Path

import scrape_v3


def write(tmp, name, payload):
    path = Path(tmp) / name
    path.write_bytes(payload)
    return path


class ContainerCheckTests(unittest.TestCase):
    def test_a_real_mp4_header_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = write(tmp, "a.mp4", bytes(4) + b"ftypisom" + bytes(64))
            self.assertTrue(scrape_v3._is_playable_video(good))

    def test_the_measured_corruption_is_caught(self):
        """What the CDN actually returned: megabytes with no box structure."""
        with tempfile.TemporaryDirectory() as tmp:
            junk = write(tmp, "b.mp4", bytes(range(256)) * 400)
            self.assertFalse(scrape_v3._is_playable_video(junk))

    def test_an_html_error_page_saved_as_mp4_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = write(tmp, "c.mp4", b"<!DOCTYPE html><html><body>403</body></html>")
            self.assertFalse(scrape_v3._is_playable_video(page))

    def test_an_empty_or_truncated_file_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(scrape_v3._is_playable_video(write(tmp, "d.mp4", b"")))
            self.assertFalse(scrape_v3._is_playable_video(write(tmp, "e.mp4", b"ftyp")))

    def test_a_missing_file_is_not_playable(self):
        self.assertFalse(scrape_v3._is_playable_video(Path("does-not-exist.mp4")))

    def test_the_other_containers_are_not_rejected(self):
        """Rejecting a good download is worse than accepting a bad one - it costs a source."""
        cases = {
            "mkv": bytes([0x1A, 0x45, 0xDF, 0xA3]),
            "avi": b"RIFF",
            "flv": bytes([0x46, 0x4C, 0x56, 0x01]),
            "ogg": b"OggS",
            "mpg": bytes([0x00, 0x00, 0x01, 0xBA]),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, magic in cases.items():
                path = write(tmp, name + ".mp4", magic + bytes(32))
                self.assertTrue(scrape_v3._is_playable_video(path), name)

    def test_a_moov_first_mp4_passes(self):
        """Faststart files put moov before mdat; both are valid opening boxes."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "f.mp4", bytes(4) + b"moov" + bytes(64))
            self.assertTrue(scrape_v3._is_playable_video(path))


class WiringTests(unittest.TestCase):
    def body(self):
        source = open(scrape_v3.__file__, encoding="utf-8").read()
        block = source[source.index("def gather_chapter_sources("):]
        return block[:block.index("\ndef ", 10)]

    def test_the_download_loop_checks_before_accepting_the_source(self):
        body = self.body()
        self.assertIn("_is_playable_video(dest)", body)
        # and the slot must be released, not merely skipped silently
        self.assertIn("taking the next candidate", body)

    def test_the_bad_file_is_deleted(self):
        """Leaving it on disk lets the reuse pass pick it up on the next run."""
        body = self.body()
        check = body[body.index("_is_playable_video(dest)"):]
        check = check[:check.index("seen_source_ids.add")]
        self.assertIn("unlink()", check)

    def test_it_happens_before_the_source_is_recorded(self):
        """Scoped to the download loop: `seen_source_ids` is also touched by the reuse pass
        further up, so comparing first occurrences in the whole function proves nothing."""
        loop = self.body()
        loop = loop[loop.index("for query, item in candidates:"):]
        self.assertLess(loop.index("_is_playable_video(dest)"),
                        loop.index("seen_source_ids.add(source_id)"))


if __name__ == "__main__":
    unittest.main()
