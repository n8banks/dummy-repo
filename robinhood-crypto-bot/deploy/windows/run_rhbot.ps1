# Runs the bot on Windows and restarts it if it crashes.
# Register it to start at boot (run once in an admin PowerShell, from the repo root):
#
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\deploy\windows\run_rhbot.ps1`""
#   $t = New-ScheduledTaskTrigger -AtStartup
#   $s = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
#        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
#   Register-ScheduledTask -TaskName rhbot -Action $a -Trigger $t -Settings $s `
#        -User $env:USERNAME -RunLevel Limited
#
# Credentials: set RH_API_KEY / RH_PRIVATE_KEY as *user* environment variables
# (System Properties > Environment Variables), not in this file.

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
if (-not (Test-Path .venv)) {
    py -3 -m venv .venv
    .\.venv\Scripts\pip install -q -r requirements.txt
}
$env:RHBOT_DATA = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $env:RHBOT_DATA | Out-Null

while ($true) {
    # Paper trading. Add --live only after you trust the paper results.
    .\.venv\Scripts\python -m rhbot trade --interval 1d --strategy tsmom --daily-budget 25 --budget-cap 500 *>> "$env:RHBOT_DATA\rhbot.log"
    Start-Sleep -Seconds 30
}
