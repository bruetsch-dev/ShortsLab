import sys
import re

with open("d:/data/AutoShortsAntigravity/app.py", "r", encoding="utf-8") as f:
    content = f.read()

repl = """    if wavespeed.get("seedance_model"):
        state["seedance_model"] = str(wavespeed.get("seedance_model"))
        state["video_model"] = state["seedance_model"]
    elif "v1.5" in video_model or "1.5" in video_model:
        state["seedance_model"] = "seedance-v1.5-pro"
        state["video_model"] = "seedance-v1.5-pro"
    else:
        state["seedance_model"] = "seedance-2.0"
        state["video_model"] = "seedance-2.0" """

content = re.sub(
    r"    if wavespeed\.get\(\"seedance_model\"\):\n        state\[\"seedance_model\"\] = str\(wavespeed\.get\(\"seedance_model\"\)\)\n    elif \"v1\.5\" in video_model or \"1\.5\" in video_model:\n        state\[\"seedance_model\"\] = \"seedance-v1\.5-pro\"\n    else:\n        state\[\"seedance_model\"\] = \"seedance-2\.0\"",
    repl,
    content,
    flags=re.MULTILINE
)

with open("d:/data/AutoShortsAntigravity/app.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Fixed app.py state parsing")
