import sys

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "r", encoding="utf-8") as f:
    content = f.read()

correct_block = """$port = 7865

$shortcutPath = Join-Path $appDir "Start Autonomous Shorts Agent.lnk"
$iconPath = Join-Path $appDir "static\\app_icon.ico"

if (-not (Test-Path $iconPath)) {
    $iconPath = Join-Path $appDir "static\\start_icon.ico"
}

# Clean up old profile dirs to prevent disk space bloat
$oldProfiles = Get-ChildItem -Path $appDir -Filter "browser-profile-*" -Directory
foreach ($profile in $oldProfiles) {
    Remove-Item -Path $profile.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# Generate a unique profile directory for this session so Edge doesn't delegate to a background process
$profileDir = Join-Path $appDir "browser-profile-$(Get-Random)"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null"""

target_replace = '''$port = 7865
# Clean up old profile dirs to prevent disk space bloat
$oldProfiles = Get-ChildItem -Path $appDir -Filter "browser-profile-*" -Directory
foreach ($profile in $oldProfiles) {
    Remove-Item -Path $profile.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# Generate a unique profile directory for this session so Edge doesn't delegate to a background process
$profileDir = Join-Path $appDir "browser-profile-$(Get-Random)"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null'''

content = content.replace(target_replace, correct_block)

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "w", encoding="utf-8") as f:
    f.write(content)
print("Restored missing variable definitions in launch_app.ps1")
