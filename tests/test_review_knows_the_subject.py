"""The reviewer has to know what the VIDEO is about, not only the line it is filling.

Measured on the convenience-store Short (2026-09-04): the beat "somebody tried to fit every minor
emergency into one BUILDING" was filled with a tour of Hong Kong's Monster Building at relevance
8. Nothing in the prompt said the video was about Japanese convenience stores, so a building
matched "building" - the literal-word failure the rest of the prompt already warns against.
"""
import inspect

import scrape_v4


def test_the_subject_appears_before_the_line():
    text = scrape_v4.footage_review_prompt("s", "q", "a line about shirts", "Japanese konbini")
    assert text.index("THE VIDEO IS ABOUT: Japanese konbini") < \
        text.index("THE LINE THIS SHOT HAS TO CARRY")


def test_word_matching_is_named_as_the_failure():
    text = scrape_v4.footage_review_prompt("s", "q", "a line", "a subject")
    assert "matches a WORD in the line but belongs to a different subject" in text
    assert "score it 0-3" in text


def test_without_a_subject_the_prompt_says_nothing_about_one():
    """A caller that has no topic must not send the model an empty promise."""
    text = scrape_v4.footage_review_prompt("s", "q", "a line")
    assert "THE VIDEO IS ABOUT" not in text
    assert "matches a WORD" not in text
    assert "THE LINE THIS SHOT HAS TO CARRY" in text


def test_a_long_subject_is_trimmed():
    text = scrape_v4.footage_review_prompt("s", "q", "line", "x" * 400)
    assert "x" * 121 not in text


def test_every_review_call_passes_the_subject():
    body = inspect.getsource(scrape_v4)
    assert body.count("topic=_topic") == 3, "one per review call site"
    assert '_topic = str((config or {}).get("topic")' in body


def test_the_reviewer_accepts_the_subject():
    assert "topic" in inspect.signature(scrape_v4._vision_source_review).parameters
