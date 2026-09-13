"""Resuming a partial image-prompt checkpoint has to CONTINUE, not stop.

A sketch explainer whose prompt generation was interrupted at 40 of 304 lines could never be
finished: the resume branch ran the model loop zero times, so the run fell straight through to
missing-slot recovery (60 lines at most) and then to the deterministic emergency template for
everything left. Two thirds of the project's art came from that template, and re-running changed
nothing because the checkpoint resumed the same 40 again.
"""

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_core
import longform_video as lv


def _lines(count):
    return [{"start": i * 3.0, "text": f"narration line number {i}"} for i in range(count)]


class PromptCheckpointResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lf_resume_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.original_post = agent_core.post_json_url
        self.addCleanup(setattr, agent_core, "post_json_url", self.original_post)
        # The model is stubbed, but generate_image_prompts refuses to start without a key. Set
        # one here rather than depending on the developer's environment: another test in the
        # suite clears it, so this passed alone and failed under discovery.
        patcher = mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _checkpoint(self, lines, done):
        path = self.tmp / "ck.json"
        path.write_text(json.dumps({
            "line_count": len(lines),
            "first_ts": lv.fmt_ts(lines[0]["start"]),
            "last_ts": lv.fmt_ts(lines[-1]["start"]),
            "prompts": [{"timestamp": lv.fmt_ts(lines[i]["start"]),
                         "prompt": f"resumed doodle {i}"} for i in range(done)],
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def _stub_model(self):
        """Answers like the real STAGE-3 conversation: the transcript arrives once, then each
        'next' continues where the previous reply stopped."""
        state = {"pool": None, "served": 0, "asked": None}

        def post(url, payload, timeout=600):
            first_user = [m for m in payload["messages"] if m["role"] == "user"][0]["content"]
            stamps = re.findall(r"\[\d+:\d\d\.\d\]", first_user)
            if state["pool"] is None:
                state["pool"], state["asked"] = stamps, len(stamps)
            chunk = state["pool"][state["served"]:state["served"] + 20]
            state["served"] += len(chunk)
            body = "\n".join(f"{stamp} doodle for {stamp}" for stamp in chunk)
            return {"choices": [{"message": {"content": body}}]}

        agent_core.post_json_url = post
        return state

    def test_a_partial_checkpoint_is_finished_by_the_model(self):
        lines = _lines(304)
        checkpoint = self._checkpoint(lines, 40)
        state = self._stub_model()
        logs = []
        prompts = lv.generate_image_prompts(lines, status_cb=logs.append,
                                            checkpoint_path=checkpoint)
        self.assertEqual(len(prompts), len(lines))
        self.assertEqual(sum(1 for line in logs if "safe local fallback" in line), 0,
                         "the emergency template ran; the model was never asked to continue")

    def test_the_model_is_asked_only_for_the_missing_lines(self):
        """Replaying the whole transcript would make it answer for line 1 again and duplicate
        everything the checkpoint already holds."""
        lines = _lines(304)
        checkpoint = self._checkpoint(lines, 40)
        state = self._stub_model()
        lv.generate_image_prompts(lines, status_cb=lambda _m: None, checkpoint_path=checkpoint)
        self.assertEqual(state["asked"], 264)

    def test_the_resumed_prompts_are_kept_and_nothing_is_duplicated(self):
        lines = _lines(120)
        checkpoint = self._checkpoint(lines, 25)
        self._stub_model()
        prompts = lv.generate_image_prompts(lines, status_cb=lambda _m: None,
                                            checkpoint_path=checkpoint)
        self.assertEqual(sum(1 for p in prompts if str(p["prompt"]).startswith("resumed")), 25)
        self.assertEqual(len({p["timestamp"] for p in prompts}), len(prompts))

    def test_a_fresh_run_still_starts_from_the_whole_transcript(self):
        lines = _lines(60)
        state = self._stub_model()
        prompts = lv.generate_image_prompts(lines, status_cb=lambda _m: None,
                                            checkpoint_path=self.tmp / "missing.json")
        self.assertEqual(state["asked"], 60)
        self.assertEqual(len(prompts), 60)


if __name__ == "__main__":
    unittest.main()
