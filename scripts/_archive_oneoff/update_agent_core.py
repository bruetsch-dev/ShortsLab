import sys
import re

with open("d:/data/AutoShortsAntigravity/agent_core.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update SEEDANCE_VIDEO_MODELS
new_models = """SEEDANCE_VIDEO_MODELS = {
    "seedance-2.0": "bytedance/seedance-2.0/image-to-video-spicy",
    "seedance-v1.5-pro": "bytedance/seedance-v1.5-pro/image-to-video",
    "ltx-2.3": "wavespeed-ai/ltx-2.3/image-to-video",
    "happyhorse-1.1": "alibaba/happyhorse-1.1/image-to-video",
}"""
content = re.sub(
    r"SEEDANCE_VIDEO_MODELS = \{\s*\"seedance-2.0\": \"bytedance/seedance-2.0/image-to-video\",\s*\"seedance-v1.5-pro\": \"bytedance/seedance-v1.5-pro/image-to-video\",\s*\}",
    new_models,
    content,
    flags=re.MULTILINE
)

# 2. Update apply_speaker_hook_to_config
hook_repl = """    first["video_prompt"] = clean_text(plan.get("seedance_prompt") or "")
    first["video_model"] = "bytedance/seedance-2.0/image-to-video-spicy"
    first["video_resolution"] = "480p"
    first["video_enable_web_search"] = True
    first["max_duration"] = 9.0
    first["shots"] = [{"at": 0.0, "use_clip": True}]"""
content = re.sub(
    r"    first\[\"video_prompt\"\] = clean_text\(plan\.get\(\"seedance_prompt\"\) or \"\"\)\n    first\[\"shots\"\] = \[\{\"at\": 0\.0, \"use_clip\": True\}\]",
    hook_repl,
    content,
    flags=re.MULTILINE
)

# 3. Update run_project to read `video_model` from form
content = content.replace('seedance_model_choice = "seedance-2.0"', 'seedance_model_choice = form.get("video_model", "seedance-2.0")')

# 4. Set global resolution and web search based on choice
project_config_repl = """    config["wavespeed"]["seedance_model"] = seedance_model_choice
    config["wavespeed"]["video_model"] = SEEDANCE_VIDEO_MODELS.get(seedance_model_choice)
    config["wavespeed"]["video_resolution"] = "720p" if seedance_model_choice == "happyhorse-1.1" else "480p"
    config["wavespeed"]["video_enable_web_search"] = (seedance_model_choice == "seedance-2.0")
    config["wavespeed"]["reasoning_model"] = form.get("reasoning_model", "openai/gpt-5.5")"""
content = re.sub(
    r"    config\[\"wavespeed\"\]\[\"seedance_model\"\] = seedance_model_choice\n    config\[\"wavespeed\"\]\[\"video_model\"\] = SEEDANCE_VIDEO_MODELS\.get\(seedance_model_choice\)\n    config\[\"wavespeed\"\]\[\"reasoning_model\"\] = form\.get\(\"reasoning_model\", \"openai/gpt-5\.5\"\)",
    project_config_repl,
    content,
    flags=re.MULTILINE
)


with open("d:/data/AutoShortsAntigravity/agent_core.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated agent_core.py")
