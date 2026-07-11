import json
import os
import unittest
from unittest import mock

import reasoning_modes as rm


class ReasoningModeTests(unittest.TestCase):
    def test_all_model_families_and_defaults(self):
        expected = {
            "openai/gpt-5.6-sol": (["standard", "pro"], "standard"),
            "openai/gpt-5.6-terra": (["standard", "pro"], "standard"),
            "openai/gpt-5.6-luna": (["standard", "pro"], "standard"),
            "openai/gpt-5.5": (["low", "medium", "high", "xhigh"], "medium"),
            "anthropic/claude-opus-4.8": (["low", "medium", "high", "max", "xhigh"], "high"),
            "anthropic/claude-sonnet-5": (["low", "medium", "high", "max", "xhigh"], "high"),
            "anthropic/claude-fable-5": (["low", "medium", "high", "max", "xhigh"], "high"),
            "google/gemini-3.5-flash": (["minimal", "low", "medium", "high"], "medium"),
            "google/gemini-3.1-flash-lite": (["minimal", "low", "medium", "high"], "medium"),
            "google/gemini-3.1-pro-preview": (["minimal", "low", "medium", "high"], "high"),
        }
        for model, (values, default) in expected.items():
            cfg = rm.config_for_model(model)
            self.assertEqual(values, [o["value"] for o in cfg["options"]])
            self.assertEqual(default, cfg["defaultValue"])

    def test_unsupported_and_switch_validation(self):
        self.assertIsNone(rm.config_for_model("some/unsupported-model"))
        self.assertIsNone(rm.validate_reasoning_mode("some/unsupported-model", "high"))
        self.assertEqual("high", rm.validate_reasoning_mode("openai/gpt-5.5", "high"))
        self.assertEqual("standard", rm.validate_reasoning_mode("openai/gpt-5.6-sol", "xhigh"))

    def test_payload_mapping(self):
        self.assertEqual({}, rm.build_reasoning_payload("openai/gpt-5.6-sol", "standard"))
        self.assertEqual({"reasoning": {"mode": "pro", "effort": "medium"}},
                         rm.build_reasoning_payload("openai/gpt-5.6-sol", "pro"))
        self.assertEqual({"reasoning": {"effort": "xhigh"}},
                         rm.build_reasoning_payload("openai/gpt-5.5", "xhigh"))
        self.assertEqual({"reasoning": {"effort": "max"}},
                         rm.build_reasoning_payload("anthropic/claude-fable-5", "max"))
        self.assertEqual({"reasoning": {"effort": "minimal"}},
                         rm.build_reasoning_payload("google/gemini-3.5-flash", "minimal"))
        self.assertEqual({}, rm.build_reasoning_payload("unsupported", "high"))

    def test_endpoint(self):
        self.assertEqual("responses", rm.get_wavespeed_endpoint("openai/gpt-5.6-terra", "pro"))
        self.assertEqual("chat-completions", rm.get_wavespeed_endpoint("openai/gpt-5.6-terra", "standard"))

    @mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": "test"}, clear=False)
    def test_final_real_request_uses_responses_and_normalizes(self):
        import importlib
        import sys
        with mock.patch.dict(sys.modules, {"pipeline": mock.MagicMock()}):
            agent_core = importlib.import_module("agent_core")

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({"output_text": "done"}).encode()

        captured = {}
        def fake_open(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return Response()

        with mock.patch.object(agent_core.urllib.request, "urlopen", side_effect=fake_open):
            result = agent_core.post_json_url(agent_core.WAVESPEED_LLM_API, {
                "model": "openai/gpt-5.6-sol", "reasoning_mode": "pro",
                "messages": [{"role": "user", "content": "hello"}], "max_tokens": 40})
        self.assertTrue(captured["url"].endswith("/v1/responses"))
        self.assertEqual({"mode": "pro", "effort": "medium"}, captured["body"]["reasoning"])
        self.assertNotIn("messages", captured["body"])
        self.assertEqual("done", result["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
