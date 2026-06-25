import sys
import re

with open("d:/data/AutoShortsAntigravity/pipeline.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update submit_wavespeed_clip to accept scene-level overrides
clip_repl = """def submit_wavespeed_clip(image_url, prompt, duration, config, scene, key):
    wavespeed = config.get("wavespeed", {})
    model = scene.get("video_model", wavespeed.get("video_model", DEFAULT_VIDEO_MODEL))
    if scene.get("max_duration") is not None:
        duration = min(duration, float(scene["max_duration"]))
    payload = {
        "aspect_ratio": wavespeed.get("video_aspect_ratio", wavespeed.get("aspect_ratio", "9:16")),
        "duration": int(clamp(int(math.ceil(duration)), 4, 15)),
        "enable_web_search": bool(scene.get("video_enable_web_search", wavespeed.get("video_enable_web_search", False))),
        "generate_audio": bool(wavespeed.get("video_generate_audio", True)),
        "image": image_url,
        "prompt": prompt,
        "resolution": scene.get("video_resolution", wavespeed.get("video_resolution", "480p")),
        "seed": int(scene.get("seed", wavespeed.get("seed", -1))),
    }"""
content = re.sub(
    r"def submit_wavespeed_clip\(image_url, prompt, duration, config, scene, key\):\n    wavespeed = config\.get\(\"wavespeed\", \{\}\)\n    model = wavespeed\.get\(\"video_model\", DEFAULT_VIDEO_MODEL\)\n    payload = \{\n        \"aspect_ratio\": wavespeed\.get\(\"video_aspect_ratio\", wavespeed\.get\(\"aspect_ratio\", \"9:16\"\)\),\n        \"duration\": int\(clamp\(int\(math\.ceil\(duration\)\), 4, 15\)\),\n        \"enable_web_search\": bool\(wavespeed\.get\(\"video_enable_web_search\", False\)\),\n        \"generate_audio\": bool\(wavespeed\.get\(\"video_generate_audio\", True\)\),\n        \"image\": image_url,\n        \"prompt\": prompt,\n        \"resolution\": wavespeed\.get\(\"video_resolution\", \"480p\"\),\n        \"seed\": int\(scene\.get\(\"seed\", wavespeed\.get\(\"seed\", -1\)\)\),\n    \}",
    clip_repl,
    content,
    flags=re.MULTILINE
)

# 2. Update submit_wavespeed_image to use lowest quality
image_repl = """def submit_wavespeed_image(prompt, config, key):
    wavespeed = config.get("wavespeed", {})
    model = wavespeed.get("image_model", DEFAULT_IMAGE_MODEL)
    payload = {
        "aspect_ratio": wavespeed.get("aspect_ratio", "9:16"),
        "enable_base64_output": False,
        "enable_sync_mode": False,
        "output_format": wavespeed.get("output_format", "png"),
        "prompt": prompt,
        "quality": "low",
        "resolution": "1k",
    }"""
content = re.sub(
    r"def submit_wavespeed_image\(prompt, config, key\):\n    wavespeed = config\.get\(\"wavespeed\", \{\}\)\n    model = wavespeed\.get\(\"image_model\", DEFAULT_IMAGE_MODEL\)\n    payload = \{\n        \"aspect_ratio\": wavespeed\.get\(\"aspect_ratio\", \"9:16\"\),\n        \"enable_base64_output\": False,\n        \"enable_sync_mode\": False,\n        \"output_format\": wavespeed\.get\(\"output_format\", \"png\"\),\n        \"prompt\": prompt,\n        \"quality\": wavespeed\.get\(\"quality\", \"high\"\),\n        \"resolution\": wavespeed\.get\(\"resolution\", \"1k\"\),\n    \}",
    image_repl,
    content,
    flags=re.MULTILINE
)

with open("d:/data/AutoShortsAntigravity/pipeline.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated pipeline.py")
