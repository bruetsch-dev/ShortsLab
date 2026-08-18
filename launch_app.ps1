
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonLauncher = "py"
$pythonVersion = "3.11"
$url = "http://127.0.0.1:7865/"
$port = 7865

$shortcutPath = Join-Path $appDir "Start Autonomous Shorts Agent.lnk"
$iconPath = Join-Path $appDir "static\app_icon.ico"

if (-not (Test-Path $iconPath)) {
    $iconPath = Join-Path $appDir "static\start_icon.ico"
}

# Clean up the old per-session random profiles. A FRESH profile on every start made Edge re-run
# its first-run auto-sign-in each launch - THAT was the "we're syncing your browser data" flyout
# (and the translate prompt) appearing every single time the app opened.
$oldProfiles = Get-ChildItem -Path $appDir -Filter "browser-profile-*" -Directory -ErrorAction SilentlyContinue
foreach ($profile in $oldProfiles) {
    Remove-Item -Path $profile.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# ONE persistent, dedicated app profile instead (any non-default --user-data-dir already forces a
# separate Edge process, so the per-session randomness was never needed). On first creation we
# pre-seed the profile preferences: sign-in DISABLED (kills the auto-sign-in + sync flyout for
# good) and translate prompts off - scoped to THIS app profile only; normal Edge is untouched.
$profileDir = Join-Path $appDir "browser-profile"
if (-not (Test-Path (Join-Path $profileDir "Default"))) {
    New-Item -ItemType Directory -Force -Path (Join-Path $profileDir "Default") | Out-Null
    $prefs = '{"signin":{"allowed":false,"allowed_on_next_startup":false},"sync_promo":{"show_on_first_run_allowed":false},"translate":{"enabled":false},"credentials_enable_service":false}'
    Set-Content -Path (Join-Path $profileDir "Default\Preferences") -Value $prefs -Encoding utf8
}

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

# Kill any leftover browser windows that still use OUR app profile. If one lingers (old app
# window still open, Edge background mode), the new msedge.exe would just hand the URL to it
# and exit instantly - Wait-Process below would then kill the fresh server immediately and the
# window would show ERR_CONNECTION_REFUSED.
try {
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe' OR Name='chrome.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*browser-profile*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 600
} catch {
    # CIM unavailable or nothing to kill
}

$logDir = Join-Path $appDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$outLog = Join-Path $logDir "server.out.log"
$errLog = Join-Path $logDir "server.err.log"

# ======================= NATIVE APP WINDOW (preferred, no browser) =======================
# Shortslab runs in its OWN window (WebView2 via pywebview) - not a browser tab, no address bar.
# run_native.py starts the HTTP server AND the window in ONE process; closing the window quits
# the app. Only fall through to the browser flow below if pywebview / WebView2 is unavailable.
# Set SHORTSLAB_NO_NATIVE=1 to force the browser flow.
& $pythonLauncher "-$pythonVersion" -c "import webview" *> $null
$hasWebview = ($LASTEXITCODE -eq 0)
if ($hasWebview -and (-not $env:SHORTSLAB_NO_NATIVE) -and (Test-Path (Join-Path $appDir "run_native.py"))) {
    # kill a leftover native instance so we don't stack windows / double-bind the port
    try {
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*run_native.py*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 300
    } catch {}
    Start-Process `
        -FilePath $pythonLauncher `
        -ArgumentList @("-$pythonVersion", "-X", "faulthandler", "-B", "run_native.py", "--host", "127.0.0.1", "--port", [string]$port) `
        -WorkingDirectory $appDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog
    exit 0
}

# ======================= BROWSER FALLBACK (pywebview not available) =======================
# Capture the server's stdout/stderr so a crash (native fault, MemoryError, Playwright/onnxruntime
# abort) leaves a trace instead of vanishing with the hidden window. -X faulthandler dumps C-level
# faults; the app also writes logs/crash.log itself.
$pyProcess = Start-Process `
    -FilePath $pythonLauncher `
    -ArgumentList @("-$pythonVersion", "-X", "faulthandler", "-B", "app.py", "--host", "127.0.0.1", "--port", [string]$port) `
    -WorkingDirectory $appDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru


$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    if ($pyProcess.HasExited) { break }
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

# Open the app in a standalone app-window (--app, no browser chrome - it looks like its OWN app,
# not a browser tab). PREFER CHROME over Edge: Edge's renderer kept crashing on media-heavy pages
# and it nags with sign-in/sync flyouts. Set SHORTSLAB_BROWSER to a full exe path to force one.
$browserCandidates = @()
if ($env:SHORTSLAB_BROWSER) { $browserCandidates += $env:SHORTSLAB_BROWSER }
if ($env:ProgramFiles) {
    $browserCandidates += Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
}
if (${env:ProgramFiles(x86)}) {
    $browserCandidates += Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"
}
if ($env:LocalAppData) {
    $browserCandidates += Join-Path $env:LocalAppData "Google\Chrome\Application\chrome.exe"
}
# Edge as the fallback (always present on Windows) so the app still opens if Chrome isn't installed.
if (${env:ProgramFiles(x86)}) {
    $browserCandidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"
}
if ($env:ProgramFiles) {
    $browserCandidates += Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"
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
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-features=msImplicitSignin,msSeamlessWebToBrowserSignIn,msFirstRunExperience,TranslateUI",
    "--disable-extensions"
) -join " "

$browserProcess = Start-Process -FilePath $browser -ArgumentList $arguments -PassThru

if ($pyProcess -and $browserProcess) {
    # Keep the server alive as long as ANY browser process is still using our app profile.
    # (The spawned msedge.exe may delegate to another process and exit early, so waiting on
    # that single PID alone is not reliable.)
    try { Wait-Process -Id $browserProcess.Id -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 2
    while ($true) {
        $alive = $null
        try {
            $alive = Get-CimInstance Win32_Process -Filter "Name='msedge.exe' OR Name='chrome.exe'" -ErrorAction Stop |
                Where-Object { $_.CommandLine -and $_.CommandLine -like "*browser-profile*" } |
                Select-Object -First 1
        } catch { break }
        if (-not $alive) { break }
        Start-Sleep -Seconds 3
    }
    Stop-Process -Id $pyProcess.Id -Force -ErrorAction SilentlyContinue
}
