import sys
import re

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "r", encoding="utf-8") as f:
    content = f.read()

# Remove ErrorActionPreference = "Stop"
content = content.replace('$ErrorActionPreference = "Stop"\n', "")

# Also make Get-NetTCPConnection safe using try-catch without relying on SilentlyContinue if it fails
safe_kill = """try {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
    if ($listener) {
        $pidToKill = $listener.OwningProcess
        if ($pidToKill) {
            Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
    }
} catch {
    # Port not listening, nothing to kill
}"""

content = re.sub(
    r"\$listener = Get-NetTCPConnection -LocalPort \$port -State Listen -ErrorAction SilentlyContinue\nif \(\$listener\) \{\n    \$pidToKill = \$listener\.OwningProcess\n    if \(\$pidToKill\) \{\n        Stop-Process -Id \$pidToKill -Force -ErrorAction SilentlyContinue\n    \}\n    Start-Sleep -Seconds 1\n\}",
    safe_kill,
    content,
    flags=re.MULTILINE
)

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated launch_app.ps1")
