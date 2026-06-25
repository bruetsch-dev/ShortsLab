import sys
import re

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "r", encoding="utf-8") as f:
    content = f.read()

# Replace profileDir logic
profile_repl = """# Clean up old profile dirs to prevent disk space bloat
$oldProfiles = Get-ChildItem -Path $appDir -Filter "browser-profile-*" -Directory
foreach ($profile in $oldProfiles) {
    Remove-Item -Path $profile.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# Generate a unique profile directory for this session so Edge doesn't delegate to a background process
$profileDir = Join-Path $appDir "browser-profile-$(Get-Random)"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null"""

content = re.sub(
    r"\$profileDir = Join-Path \$appDir \"browser-profile-v2\".*?New-Item -ItemType Directory -Force -Path \$profileDir \| Out-Null",
    profile_repl,
    content,
    flags=re.MULTILINE | re.DOTALL
)

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated launch_app.ps1 with unique profile dir")
