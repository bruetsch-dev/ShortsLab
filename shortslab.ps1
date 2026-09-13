# Open Shortslab - safe to press any number of times.  -Restart stops it first.
#
# Written for a Stream Deck button, where the whole point is pressing it without thinking. The
# existing start.bat launches unconditionally, so a second press leaves a second server fighting
# for the same port.
#
# The port is READ FROM THE RUNNING PROCESS, not guessed from a list. A fixed list looks obvious
# and is wrong the moment the launcher picks another number: this machine had instances on 7899,
# 7900, 7901, 7902 and 7903 at the same time, and a list of three ports would have started a
# sixth one while five were already serving.
#
# WHY -Restart EXISTS. The server runs with a hidden window, so closing the browser leaves it
# running; pressing the plain button then reopens the browser on the SAME old process. After a
# code change that looks exactly like the change did nothing - which is what happened: four
# instances from the evening before were still serving while the fix sat on disk unused.

param([switch]$Restart)

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startPort = 7899                 # only used when nothing is running at all

function Get-ShortslabProcesses {
    # Every python process running THIS app.py, with the port it was told to bind.
    $found = @()
    foreach ($p in Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue) {
        $cmd = [string]$p.CommandLine
        if ($cmd -notmatch 'app\.py') { continue }
        $port = if ($cmd -match '--port\s+(\d+)') { [int]$Matches[1] } else { 7865 }
        $found += [pscustomobject]@{ Pid = $p.ProcessId; Port = $port }
    }
    return $found
}

function Test-Shortslab([int]$port) {
    try { return (Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 }
    catch { return $false }
}

if ($Restart) {
    $running = @(Get-ShortslabProcesses)
    if ($running.Count) {
        # A render can be in flight, and there is no way to ask a hidden window. Say what will be
        # lost and let the answer decide, rather than discovering it afterwards.
        [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null
        $ports = ($running | ForEach-Object { $_.Port } | Sort-Object -Unique) -join ', '
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "Stop $($running.Count) running Shortslab server(s) on port $ports and start a fresh one?" +
            [Environment]::NewLine + [Environment]::NewLine +
            "Anything still rendering will be lost.",
            "Restart Shortslab", [System.Windows.Forms.MessageBoxButtons]::OKCancel,
            [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
        foreach ($proc in $running) { Stop-Process -Id $proc.Pid -Force -ErrorAction SilentlyContinue }
        # Wait for the ports to actually free up; a fresh bind on a port still in TIME_WAIT fails.
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 400
            if (-not (Get-ShortslabProcesses)) { break }
        }
    }
}

$live = $null
foreach ($proc in (Get-ShortslabProcesses | Sort-Object Port)) {
    if (Test-Shortslab $proc.Port) { $live = $proc.Port; break }
}

if (-not $live) {
    Start-Process -FilePath "py" `
        -ArgumentList @("-3.11", "app.py", "--host", "127.0.0.1", "--port", "$startPort") `
        -WorkingDirectory $appDir -WindowStyle Hidden | Out-Null

    # Poll rather than sleep a fixed guess: a cold start after a reboot takes several seconds,
    # a warm one is nearly instant, and one fixed wait is wrong for both.
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Shortslab $startPort) { $live = $startPort; break }
    }
}

if ($live) {
    Start-Process "http://127.0.0.1:$live/"
} else {
    # Say what happened instead of opening a browser at a dead port.
    [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null
    [System.Windows.Forms.MessageBox]::Show(
        "Shortslab did not answer within 20 seconds. Start it once from start.bat to see the error.",
        "Shortslab") | Out-Null
    exit 1
}
