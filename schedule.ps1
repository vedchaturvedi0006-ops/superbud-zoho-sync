# Creates a Windows scheduled task that runs the sync every 15 minutes.
#   powershell -ExecutionPolicy Bypass -File .\schedule.ps1            (create / update)
#   powershell -ExecutionPolicy Bypass -File .\schedule.ps1 -Remove    (stop syncing)
param([int]$Minutes = 15, [switch]$Remove)
$ErrorActionPreference = "Stop"
$name = "Superbud Zoho Sync"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Scheduled task removed."
    exit 0
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $python -Argument "-m sbsync run" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled task '$name' runs every $Minutes minutes. Reports are saved in the logs folder."
