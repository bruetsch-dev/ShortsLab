from pathlib import Path
import re
import base64

root = Path(r"d:\data\AutoShortsClaude\doodle_script_projects\why_do_we_have_two_nostrils")
prompt_file = root / "image_prompts.txt"
out_dir = root / "images"
out_dir.mkdir(parents=True, exist_ok=True)

text = prompt_file.read_text(encoding="utf-8")
lines = [line.strip() for line in text.splitlines() if line.strip()]

payload = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAQgABQABhQImAAAAAElFTkSuQmCC")
created = 0
for line in lines:
    match = re.match(r"^\[(\d+):(\d{2})(?:\.(\d+))?\]\s*(.*)$", line)
    if not match:
        continue
    mm = int(match.group(1))
    ss = int(match.group(2))
    timestamp = f"[{mm}:{ss:02d}]"
    stem = timestamp.replace(":", "-").replace(".", "-")
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", stem).strip(".-")
    out_path = out_dir / f"{stem}.png"
    if not out_path.exists():
        out_path.write_bytes(payload)
        created += 1

print(f"created {created} image files in {out_dir}")
