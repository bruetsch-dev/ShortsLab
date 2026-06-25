$ErrorActionPreference = "Stop"; $listener = Get-NetTCPConnection -LocalPort 9999 -State Listen -ErrorAction SilentlyContinue; Write-Host "DONE"
