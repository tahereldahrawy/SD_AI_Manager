# Register a daily Windows Scheduled Task that emails due-bill reminders.
# Run once (from the project root). Re-running updates the existing task.
# Default: every day at 08:00. Pass a time as HH:mm to override.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$py = Join-Path $root ".venv\Scripts\python.exe"
$script = Join-Path $root "scripts\send_reminders.py"
$time = if ($args.Count -ge 1) { $args[0] } else { "08:00" }

if (-not (Test-Path $py)) { throw "venv python not found at $py — run run.ps1 once first." }

$action  = New-ScheduledTaskAction -Execute $py -Argument "`"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $time
Register-ScheduledTask -TaskName "SubscriptionManager-Reminders" `
    -Action $action -Trigger $trigger -Description "Email due subscription bills" -Force | Out-Null

Write-Host "Registered task 'SubscriptionManager-Reminders' to run daily at $time."
Write-Host "Configure SMTP + recipient in the app Settings page first."
