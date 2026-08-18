"""Single source of truth for model reasoning UI, validation and WaveSpeed payloads."""
from __future__ import annotations

from contextvars import ContextVar


def _opts(values):
    return [{"value": value, "label": label} for value, label in values]


CLAUDE = _opts((("low", "Low"), ("medium", "Medium"), ("high", "High"),
                ("max", "Maximum"), ("xhigh", "Extra High")))
GEMINI = _opts((("minimal", "Minimal"), ("low", "Low"), ("medium", "Medium"),
                ("high", "High")))
GPT55 = _opts((("low", "Low"), ("medium", "Medium"), ("high", "High"),
               ("xhigh", "Extra High")))
GPT56 = _opts((("standard", "Standard"), ("pro", "Pro")))
# DeepSeek exposes exactly three effort levels (low / high / max) since the V4-Pro GA build
# 0813. Any other string is accepted by the endpoint and then silently ignored, so listing
# "medium" here would hand the user a setting that does nothing.
DEEPSEEK_PRO = _opts((("low", "Low"), ("high", "High"), ("max", "Maximum")))
# Flash is NOT given the upper two on purpose. Measured on one logic prompt with a 16k ceiling:
# Pro answered at every level, while Flash at "high" and "max" burned the full 16k inside
# reasoning_content and returned message.content EMPTY (finish_reason=length) - the exact
# failure that killed every Kimi K3 run. At "low" it answers normally.
DEEPSEEK_FLASH = _opts((("low", "Low"),))

REASONING_CONFIG = {
    **{model: {"supported": True, "defaultValue": "standard", "options": GPT56,
               "apiMode": "responses-pro"}
       for model in ("openai/gpt-5.6-sol", "openai/gpt-5.6-terra", "openai/gpt-5.6-luna")},
    "openai/gpt-5.5": {"supported": True, "defaultValue": "medium", "options": GPT55,
                        "apiMode": "reasoning-object"},
    "anthropic/claude-opus-4.8": {"supported": True, "defaultValue": "high",
                                  "options": CLAUDE, "apiMode": "reasoning-object"},
    "anthropic/claude-sonnet-5": {"supported": True, "defaultValue": "high",
                                  "options": CLAUDE, "apiMode": "reasoning-object"},
    "anthropic/claude-fable-5": {"supported": True, "defaultValue": "high",
                                 "options": CLAUDE, "apiMode": "reasoning-object"},
    "google/gemini-3.5-flash": {"supported": True, "defaultValue": "medium",
                                "options": GEMINI, "apiMode": "reasoning-object"},
    "google/gemini-3.1-flash-lite": {"supported": True, "defaultValue": "medium",
                                     "options": GEMINI, "apiMode": "reasoning-object"},
    # https://wavespeed.ai/llm/google/gemini-3.5-flash-lite
    "google/gemini-3.5-flash-lite": {"supported": True, "defaultValue": "medium",
                                     "options": GEMINI, "apiMode": "reasoning-object"},
    "google/gemini-3.1-pro-preview": {"supported": True, "defaultValue": "high",
                                      "options": GEMINI, "apiMode": "reasoning-object"},
    # https://wavespeed.ai/llm/moonshotai/kimi-k3 - no exposed reasoning-effort control
    "moonshotai/kimi-k3": {"supported": False, "defaultValue": None, "options": [],
                           "apiMode": "none"},
    # https://wavespeed.ai/llm/deepseek/deepseek-v4-pro and .../deepseek-v4-flash-0731 -
    # thinking / non-thinking with a reasoning_effort control; TEXT ONLY, no vision, so the
    # vision steps (scrape matching, retime, video review) must never be pointed at these.
    "deepseek/deepseek-v4-pro": {"supported": True, "defaultValue": "high",
                                 "options": DEEPSEEK_PRO, "apiMode": "reasoning-object"},
    "deepseek/deepseek-v4-flash-0731": {"supported": True, "defaultValue": "low",
                                        "options": DEEPSEEK_FLASH, "apiMode": "reasoning-object"},
}

# Models that cannot see. Every vision step in this app (scrape segment description, hook
# scoring, VFX target detection, SFX frame review) sends a contact sheet to whatever model the
# user picked in the dropdown. A text-only model does not error on an image block - it invents
# an answer, so the run silently judges clips it never saw. Vision call sites ask here first and
# swap in VISION_FALLBACK_MODEL instead.
TEXT_ONLY_MODELS = {"deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash-0731"}
VISION_FALLBACK_MODEL = "google/gemini-3.1-pro-preview"


def is_vision_capable(model_id):
    return str(model_id or "") not in TEXT_ONLY_MODELS


def vision_model_for(model_id):
    """The model a vision call should really use for the user's selection."""
    return model_id if is_vision_capable(model_id) else VISION_FALLBACK_MODEL


_selected_mode = ContextVar("wavespeed_reasoning_mode", default=(None, None))


def config_for_model(model_id):
    return REASONING_CONFIG.get(str(model_id or ""))


def validate_reasoning_mode(model_id, selected=None):
    cfg = config_for_model(model_id)
    if not cfg:
        return None
    valid = {row["value"] for row in cfg["options"]}
    return selected if selected in valid else cfg["defaultValue"]


def build_reasoning_payload(model_id, selected=None):
    cfg = config_for_model(model_id)
    if not cfg:
        return {}
    mode = validate_reasoning_mode(model_id, selected)
    if cfg["apiMode"] == "responses-pro":
        return {} if mode == "standard" else {"reasoning": {"mode": "pro", "effort": "medium"}}
    return {"reasoning": {"effort": mode}}


def get_wavespeed_endpoint(model_id, selected=None):
    cfg = config_for_model(model_id)
    mode = validate_reasoning_mode(model_id, selected)
    return "responses" if cfg and cfg["apiMode"] == "responses-pro" and mode == "pro" else "chat-completions"


def responses_input(messages):
    """Translate Chat Completions text/vision blocks to Responses API input blocks."""
    out = []
    for message in messages or []:
        content = message.get("content", "")
        if isinstance(content, list):
            blocks = []
            for block in content:
                kind = block.get("type")
                if kind in ("text", "input_text"):
                    blocks.append({"type": "input_text", "text": str(block.get("text") or "")})
                elif kind in ("image_url", "input_image"):
                    image = block.get("image_url")
                    if isinstance(image, dict):
                        image = image.get("url")
                    if image:
                        blocks.append({"type": "input_image", "image_url": image})
            content = blocks
        out.append({"role": message.get("role", "user"), "content": content})
    return out


def set_current_reasoning_mode(model_id, selected=None):
    mode = validate_reasoning_mode(model_id, selected)
    _selected_mode.set((str(model_id or ""), mode))
    return mode


def current_reasoning_mode(model_id):
    stored_model, stored_mode = _selected_mode.get()
    return validate_reasoning_mode(model_id, stored_mode if stored_model == str(model_id or "") else None)


def public_config():
    return REASONING_CONFIG
