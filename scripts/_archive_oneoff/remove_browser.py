import sys
import re

with open("d:/data/AutoShortsAntigravity/app.py", "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace("threading.Thread(target=launch_browser, args=(args.host, args.port), daemon=True).start()", "# threading.Thread(target=launch_browser, args=(args.host, args.port), daemon=True).start()")

with open("d:/data/AutoShortsAntigravity/app.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Removed launch_browser from app.py")
