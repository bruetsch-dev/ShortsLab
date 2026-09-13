"""A generator watermark is evidence, not an opinion.

The footage reviewer is already told to reject AI-generated imagery. On the "things from the
future" Short (2026-09-04) it accepted, at relevance 10, a sushi-train clip with a visible "Sora"
mark burned into the corner - so a clip nobody filmed opened a Short about real Japanese
technology. The OCR that finds burned-in captions can simply read the mark.
"""
import numpy as np
from PIL import Image

import scrape_v4


class _Row(list):
    pass


def _ocr_returning(*texts):
    def ocr(_frame, **_kw):
        return [[[[0, 0], [10, 0], [10, 10], [0, 10]], text, 0.9] for text in texts], None
    return ocr


def _frames(n=2):
    return [Image.fromarray(np.zeros((40, 40, 3), dtype=np.uint8)) for _ in range(n)]


def test_a_sora_mark_is_found(monkeypatch):
    monkeypatch.setattr(scrape_v4.clip_scraper, "_get_ocr", lambda: _ocr_returning("Sora"))
    assert scrape_v4.synthetic_watermark(_frames()) == "Sora"


def test_the_other_generators_are_known(monkeypatch):
    for name in ("Runway", "Kling AI", "Veo", "Pika", "Hailuo", "made with AI"):
        monkeypatch.setattr(scrape_v4.clip_scraper, "_get_ocr", lambda n=name: _ocr_returning(n))
        assert scrape_v4.synthetic_watermark(_frames()), name


def test_ordinary_caption_text_is_not_a_watermark(monkeypatch):
    monkeypatch.setattr(scrape_v4.clip_scraper, "_get_ocr",
                        lambda: _ocr_returning("本庄第一中学校", "Tokyo station", "3 things"))
    assert scrape_v4.synthetic_watermark(_frames()) == ""


def test_a_low_confidence_read_is_ignored(monkeypatch):
    def ocr(_frame, **_kw):
        return [[[[0, 0]], "sora", 0.2]], None
    monkeypatch.setattr(scrape_v4.clip_scraper, "_get_ocr", lambda: ocr)
    assert scrape_v4.synthetic_watermark(_frames()) == ""


def test_no_ocr_installed_is_not_an_accusation(monkeypatch):
    monkeypatch.setattr(scrape_v4.clip_scraper, "_get_ocr", lambda: None)
    assert scrape_v4.synthetic_watermark(_frames()) == ""


def test_a_broken_recognizer_does_not_break_the_run(monkeypatch):
    def ocr(_frame, **_kw):
        raise RuntimeError("model missing")
    monkeypatch.setattr(scrape_v4.clip_scraper, "_get_ocr", lambda: ocr)
    assert scrape_v4.synthetic_watermark(_frames()) == ""


def test_a_marked_window_is_rejected_whatever_the_verdict_said():
    import inspect
    body = inspect.getsource(scrape_v4._vision_source_review)
    assert 'mark = str(getattr(candidate, "synthetic_mark", "") or "")' in body
    assert "made by an AI tool, not filmed" in body


def test_a_candidate_defaults_to_unmarked():
    assert scrape_v4.Candidate("s", "q", "p", 0, 1, 1.0, "", "available").synthetic_mark == ""


def test_the_prompt_names_the_celebrity_tell():
    """A watermark is not the only evidence, and OCR cannot see composition.

    Measured on the convenience-store Short (2026-09-04): the hook was Beyonce holding an ice
    cream inside a 7-Eleven, in a split-screen panel, scored 8 out of 10. No tool mark to read -
    the tell is that the moment could not have been filmed by a passer-by.
    """
    text = scrape_v4.footage_review_prompt("s", "q", "a line", "Japanese convenience stores")
    assert "WORLD-FAMOUS person somewhere they would never plausibly be" in text
    assert "could have been filmed by a passer-by with a phone" in text


def test_the_prompt_rejects_split_screens():
    text = scrape_v4.footage_review_prompt("s", "q", "a line")
    assert "split screen" in text and "the edit needs the whole frame" in text
