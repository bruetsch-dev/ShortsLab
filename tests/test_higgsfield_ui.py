"""Regression tests for the live Higgsfield image-generator controls."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import higgsfield_login as hf


class _Keyboard:
    def press(self, _key):
        pass


class _Element:
    def __init__(self, text="", visible=True, attributes=None, click_state=None):
        self.text = text
        self.visible = visible
        self.value = ""
        self.clicks = 0
        self.attributes = dict(attributes or {})
        self.click_state = click_state

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text

    def click(self, **_kwargs):
        self.clicks += 1
        if self.click_state is not None:
            self.attributes["aria-checked"] = self.click_state

    def fill(self, value):
        self.value = value

    def input_value(self):
        return self.value

    def press(self, _key):
        pass

    def type(self, value, **_kwargs):
        self.value = value

    def get_attribute(self, name):
        return self.attributes.get(name)

    def is_checked(self):
        return str(self.attributes.get("checked") or "").lower() == "true"

    def evaluate(self, _script):
        return " ".join(filter(None, [self.text, self.attributes.get("aria-label", "")]))


class _Page:
    def __init__(self, mapping):
        self.mapping = mapping
        self.keyboard = _Keyboard()

    def query_selector_all(self, selector):
        return list(self.mapping.get(selector, []))

    def query_selector(self, selector):
        values = self.query_selector_all(selector)
        return values[0] if values else None

    def wait_for_timeout(self, _milliseconds):
        pass


class HiggsfieldUiTest(unittest.TestCase):
    def setUp(self):
        # These methods do not depend on a running browser/context.
        self.session = object.__new__(hf.Session)

    def test_prompt_uses_visible_editor_instead_of_first_hidden_clone(self):
        hidden = _Element(visible=False)
        visible = _Element(visible=True)
        page = _Page({"[role='textbox'][contenteditable='true']": [hidden, visible]})

        self.assertTrue(self.session._type_prompt(page, "real prompt"))
        self.assertEqual(hidden.value, "")
        self.assertEqual(visible.value, "real prompt")

    def test_selected_model_is_verified_without_opening_its_popup(self):
        selected = _Element("FLUX.2 Pro")
        page = _Page({"main button": [selected]})

        self.assertTrue(self.session._set_model(page, "FLUX.2 Pro"))
        self.assertEqual(selected.clicks, 0,
                         "clicking the selected model opens a popup over the prompt")

    def test_aspect_control_opens_and_selects_16_by_9(self):
        current = _Element("3:4")
        wanted = _Element("16:9")
        page = _Page({"button[aria-haspopup='listbox']": [current],
                      "[role='option']": [wanted]})

        self.assertTrue(self.session._set_aspect(page, "16:9"))
        self.assertEqual(current.clicks, 1)
        self.assertEqual(wanted.clicks, 1)

    def test_unlimited_switch_is_enabled_and_verified(self):
        unlimited = _Element(attributes={"aria-label": "Unlimited", "aria-checked": "false"},
                             click_state="true")
        page = _Page({"[role='switch']": [unlimited]})

        self.assertTrue(self.session._set_unlimited(page))
        self.assertEqual(unlimited.clicks, 1)
        self.assertEqual(unlimited.get_attribute("aria-checked"), "true")

    def test_missing_unlimited_switch_is_a_hard_failure(self):
        unrelated = _Element(attributes={"aria-label": "Enhance", "aria-checked": "false"})
        page = _Page({"[role='switch']": [unrelated]})

        self.assertFalse(self.session._set_unlimited(page))
        self.assertEqual(unrelated.clicks, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
