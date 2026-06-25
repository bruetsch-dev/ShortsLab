
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = "http://127.0.0.1:7865/"
$port = 7865

$shortcutPath = Join-Path $appDir "Start Autonomous Shorts Agent.lnk"
$iconPath = Join-Path $appDir "static\app_icon.ico"

if (-not (Test-Path $iconPath)) {
    $iconPath = Join-Path $appDir "static\start_icon.ico"
}

# Clean up old profile dirs to prevent disk space bloat
$oldProfiles = Get-ChildItem -Path $appDir -Filter "browser-profile-*" -Directory
foreach ($profile in $oldProfiles) {
    Remove-Item -Path $profile.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# Generate a unique profile directory for this session so Edge doesn't delegate to a background process
$profileDir = Join-Path $appDir "browser-profile-$(Get-Random)"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

# Setup the shortcut so it points to THIS script instead of directly to Edge
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
$shortcut.WorkingDirectory = $appDir
$shortcut.Description = "Autonomous Shorts Agent"
if (Test-Path $iconPath) {
    $shortcut.IconLocation = "$iconPath,0"
}
$shortcut.Save()

try {
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
}

$pyProcess = Start-Process `
    -FilePath "py" `
    -ArgumentList @("-B", "app.py", "--host", "127.0.0.1", "--port", [string]$port) `
    -WorkingDirectory $appDir `
    -WindowStyle Hidden `
    -PassThru


$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 1 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 250
    }
}

if (-not $ready) {
    if ($pyProcess) { Stop-Process -Id $pyProcess.Id -Force -ErrorAction SilentlyContinue }
    throw "Could not reach $url. Check server.err.log in this folder."
}

$browserCandidates = @()
if (${env:ProgramFiles(x86)}) {
    $browserCandidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"
}
if ($env:ProgramFiles) {
    $browserCandidates += Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"
    $browserCandidates += Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
}
if ($env:LocalAppData) {
    $browserCandidates += Join-Path $env:LocalAppData "Google\Chrome\Application\chrome.exe"
}

$browser = $browserCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $browser) {
    Start-Process $url
    exit 0
}

$arguments = @(
    "--app=`"$url`"",
    "--new-window",
    "--window-size=1920,1080",
    "--user-data-dir=`"$profileDir`"",
    "--no-first-run",
    "--disable-extensions"
) -join " "

$browserProcess = Start-Process -FilePath $browser -ArgumentList $arguments -PassThru

if ($pyProcess -and $browserProcess) {
    # Wait for the browser to exit, then kill python
    Wait-Process -Id $browserProcess.Id
    Stop-Process -Id $pyProcess.Id -Force -ErrorAction SilentlyContinue
}
