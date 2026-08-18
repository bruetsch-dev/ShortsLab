import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_core


class ScriptCreatorVarietyTests(unittest.TestCase):
    def test_generated_fact_blocks_become_searchable_paragraphs(self):
        data = {
            "script": "This fallback should not flatten the chapter plan.",
            "hook": "Three things in Japan just make sense.",
            "visual_blocks": [
                "First, a machine wraps wet umbrellas.",
                "Second, ramen shops sell tickets before you sit down.",
                "Third, Otohime buttons cover restroom noise.",
            ],
        }
        script = agent_core._generated_fact_script_with_blocks(data)
        self.assertEqual(len(script.split("\n\n")), 4)
        self.assertIn("Otohime", script.split("\n\n")[-1])

    def test_token_trim_preserves_existing_chapter_boundaries(self):
        script = "Hook sentence.\n\nFirst visual sentence. More detail.\n\nSecond visual sentence."
        trimmed = agent_core._trim_script_to_token_limit(script, 12)
        self.assertIn("\n\n", trimmed)

    def test_similarity_recognizes_paraphrase_but_not_new_subject(self):
        repeated = agent_core._script_text_similarity(
            "Japanese schools inspect students' natural hair every morning.",
            "Every morning Japanese schools inspect the natural hair of students.",
        )
        fresh = agent_core._script_text_similarity(
            "Japanese schools inspect students' natural hair every morning.",
            "Remote vending machines use live cameras to call local repair crews.",
        )
        self.assertGreater(repeated, fresh)
        self.assertGreater(repeated, 0.68)

    def test_user_topic_is_locked_and_duplicate_result_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.json"
            old = "A vending machine on every corner hides Japan's strangest retail habit. " * 8
            history.write_text(json.dumps([{
                "requested_topic": "Japanese vending machines",
                "topic": "Vending Machines",
                "script": old,
            }]), encoding="utf-8")
            fresh = "Mountain vending machines alert a local owner when winter snow blocks the product door. " * 8
            replies = [
                {"topic": "Vending Machines", "script": old, "hook_keywords": []},
                {"topic": "Remote Vending", "script": fresh, "hook_keywords": ["snow"]},
            ]
            prompts = []

            def fake_post(model, messages, max_tokens, temperature, timeout=180):
                prompts.append(messages[-1]["content"])
                return replies.pop(0)

            with patch.object(agent_core, "_SCRIPT_TOPIC_HISTORY", history), \
                 patch.object(agent_core, "_post_llm_json", side_effect=fake_post):
                result = agent_core.generate_viral_script("Japanese vending machines")

            self.assertEqual(result["script"], fresh.strip())
            self.assertEqual(len(prompts), 2)
            self.assertIn('USER-LOCKED TOPIC: "Japanese vending machines"', prompts[0])
            self.assertIn("Rejected draft to avoid", prompts[1])

    def test_custom_instructions_are_passed_as_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = Path(tmp) / "history.json"
            captured = []

            def fake_post(model, messages, max_tokens, temperature, timeout=180):
                captured.append(messages[-1]["content"])
                return {"topic": "Tea", "script": "Tea workers repeat one visible ritual every morning. " * 12,
                        "hook_keywords": []}

            with patch.object(agent_core, "_SCRIPT_TOPIC_HISTORY", history), \
                 patch.object(agent_core, "_post_llm_json", side_effect=fake_post):
                agent_core.generate_viral_script(
                    "Japanese tea ceremonies",
                    instructions="Use three facts and make the language less exaggerated.",
                )

            self.assertIn("USER SCRIPT DIRECTIONS", captured[0])
            self.assertIn("Use three facts and make the language less exaggerated.", captured[0])
            self.assertIn("MAY override the default tone", captured[0])

    def test_script_creator_uses_selected_reasoning_model(self):
        called = []

        def fake_post(model, messages, max_tokens, temperature, timeout=180):
            called.append(model)
            return {"topic": "Night Ritual", "script": "A visible Japanese night ritual surprises visitors. " * 12,
                    "hook_keywords": []}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(agent_core, "_SCRIPT_TOPIC_HISTORY", Path(tmp) / "history.json"), \
             patch.object(agent_core, "_post_llm_json", side_effect=fake_post):
            result = agent_core.generate_viral_script(
                reasoning_model="openai/gpt-5.6-luna")

        self.assertEqual(called, ["openai/gpt-5.6-luna"])
        self.assertEqual(result["reasoning_model"], "openai/gpt-5.6-luna")

    def test_default_prompt_demands_plain_spoken_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = []

            def fake_post(model, messages, max_tokens, temperature, timeout=180):
                captured.extend(m["content"] for m in messages if isinstance(m.get("content"), str))
                return {"topic": "Trains", "script": "People quietly form exact lines before every train arrives. " * 12,
                        "hook_keywords": []}

            with patch.object(agent_core, "_SCRIPT_TOPIC_HISTORY", Path(tmp) / "history.json"), \
                 patch.object(agent_core, "_post_llm_json", side_effect=fake_post):
                agent_core.generate_viral_script("Japanese train etiquette")

            whole = "\n".join(captured)
            self.assertIn("RESEARCH DEEPLY, WRITE SIMPLY", whole)
            self.assertIn("words a 13-year-old understands", whole)
            self.assertIn("ONE IDEA PER SENTENCE", whole)

    def test_reference_regeneration_keeps_original_as_binding_topic(self):
        captured = []

        def fake_post(model, messages, max_tokens, temperature, timeout=180):
            captured.append(messages[-1]["content"])
            return {"script": "Three simpler facts about the same vending-machine topic."}

        original = "Japanese vending machines stay operational in remote mountain towns."
        with patch.object(agent_core, "_post_llm_json", side_effect=fake_post):
            result = agent_core.regenerate_script_from_reference(
                original, "Use three facts and make it less specific.")

        self.assertIn(original, captured[0])
        self.assertIn("binding topic and factual reference", captured[0])
        self.assertIn("Use three facts and make it less specific.", captured[0])
        self.assertEqual(result["script"], "Three simpler facts about the same vending-machine topic.")


if __name__ == "__main__":
    unittest.main()
