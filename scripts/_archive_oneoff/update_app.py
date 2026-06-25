import sys
import re

with open("d:/data/AutoShortsAntigravity/app.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add video_model select block
video_model_html = """        <div class="panel">
          <label>Video Model</label>
          <select name="video_model">
            <option value="seedance-2.0"{' selected' if state.get("video_model", state.get("seedance_model", "seedance-2.0")) == "seedance-2.0" else ""}>Seedance 2.0 (Spicy + Web Search)</option>
            <option value="seedance-v1.5-pro"{' selected' if state.get("video_model", state.get("seedance_model")) == "seedance-v1.5-pro" else ""}>Seedance 1.5 Pro</option>
            <option value="ltx-2.3"{' selected' if state.get("video_model") == "ltx-2.3" else ""}>LTX-2.3</option>
            <option value="happyhorse-1.1"{' selected' if state.get("video_model") == "happyhorse-1.1" else ""}>Happy Horse 1.1 (720p)</option>
          </select>
          <div class="hint">Select the primary model used for generating video clips. (Speaker hook is always Seedance 2.0).</div>
        </div>

        <div class="panel">"""

content = content.replace('        <div class="panel">\n          <label>Reasoning Model</label>', video_model_html + '\n          <label>Reasoning Model</label>')

with open("d:/data/AutoShortsAntigravity/app.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated app.py")
