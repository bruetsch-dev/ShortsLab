import sys
import re

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "r", encoding="utf-8") as f:
    content = f.read()

# Replace the listener check with a kill block
kill_repl = """$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $pidToKill = $listener.OwningProcess
    if ($pidToKill) {
        Stop-Process -Id $pidToKill -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

$pyProcess = Start-Process `
    -FilePath "py" `
    -ArgumentList @("-B", "app.py", "--host", "127.0.0.1", "--port", [string]$port) `
    -WorkingDirectory $appDir `
    -WindowStyle Hidden `
    -PassThru
"""

content = re.sub(
    r"\$pyProcess = \$null\n\$listener = Get-NetTCPConnection -LocalPort \$port -State Listen -ErrorAction SilentlyContinue\nif \(-not \$listener\) \{\n    \$pyProcess = Start-Process `\n        -FilePath \"py\" `\n        -ArgumentList @\(\"-B\", \"app\.py\", \"--host\", \"127\.0\.0\.1\", \"--port\", \[string\]\$port\) `\n        -WorkingDirectory \$appDir `\n        -WindowStyle Hidden `\n        -PassThru\n\}",
    kill_repl,
    content,
    flags=re.MULTILINE
)

with open("d:/data/AutoShortsAntigravity/launch_app.ps1", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated launch_app.ps1")
