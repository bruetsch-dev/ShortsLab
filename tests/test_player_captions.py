"""The player must show captions the way the render does: small groups, never split mid-word.

Reported with a screenshot 2026-09-02: "warum zum kack werden captions von mir so im player
angezeigt?" - most of the script on screen at once, wrapped through the middle of a word.

Two causes, both measured on `japanese_convenience_stores_look_effortless_but_the`:

* the editor payload sent `caption_track` as one row PER SENTENCE - 60 to 90 characters - and the
  preview draws one whole row at a time. The render splits every sentence with
  `build_caption_chunks` and shows one word at a time (measured: a 74-character sentence became
  8 chunks). The preview never did that split, so the two disagreed completely.
* the caption box used `overflow-wrap:anywhere` with `word-break:break-word`, which breaks inside
  a word. The render never breaks a word.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import pipeline


SENTENCE = "Japanese convenience stores look effortless, but the 3am shift is ruthless."
WORDS = [{"word": w, "start": i * 0.36, "end": i * 0.36 + 0.34}
         for i, w in enumerate(SENTENCE.replace(",", "").replace(".", "").split())]


class PreviewMatchesTheRender(unittest.TestCase):

    def test_a_sentence_becomes_several_short_rows(self):
        chunks = pipeline.build_caption_chunks(SENTENCE, 4.43, 3, True, word_times=WORDS)
        self.assertGreater(len(chunks), 3, "the sentence was not split at all")
        longest = max(len(" ".join(str(w.get("text") or w.get("word") or "")
                                   for w in (c.get("words") or []))) for c in chunks)
        self.assertLess(longest, 40, f"a preview row is still {longest} characters wide")

    def test_the_payload_splits_the_track_with_the_render_s_own_function(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("_cap_pipeline.build_caption_chunks(", source)

    def test_the_payload_reads_the_key_the_chunks_actually_use(self):
        """The chunks name a word "text"; reading only "word" made the split a no-op."""
        chunks = pipeline.build_caption_chunks(SENTENCE, 4.43, 3, True, word_times=WORDS)
        self.assertIn("text", (chunks[0].get("words") or [{}])[0])
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('str(w.get("text") or w.get("word") or "")', source)

    def test_a_failed_split_falls_back_to_the_sentence_rows(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("caption_preview = caption_track", source)
        self.assertIn("if chunked:\n            caption_preview = chunked", source)

    def test_the_sentence_track_itself_is_left_alone(self):
        """Other consumers - and the captions_editable flag - still read caption_track."""
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"caption_preview": caption_preview,', source)
        self.assertNotIn("caption_track = chunked", source)

    def test_the_player_reads_the_preview_track(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("for(var i=0;i<captionPreview.length;i++){", source)


class CaptionsDoNotBreakInsideAWord(unittest.TestCase):

    def test_the_box_breaks_between_words(self):
        with open(app.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("overflow-wrap:break-word; word-break:normal; hyphens:none;", source)
        self.assertNotIn(".tl-preview-caption { position:absolute; left:7%; right:7%; top:72%; "
                         "z-index:6; display:none; max-height:32%; overflow:hidden; "
                         "overflow-wrap:anywhere;", source)


if __name__ == "__main__":
    unittest.main()
