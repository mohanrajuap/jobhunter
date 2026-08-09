<#
.SYNOPSIS
    Registers the daily jobhunter run in Windows Task Scheduler.

.DESCRIPTION
    Creates a task that runs `python -m jobhunter run` every weekday at the given time.
    Defaults to dry-run mode; pass -Live once you've watched a few runs and trust it.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Time "08:00"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Time "07:30" -Live

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Remove
#>
param(
    [string]$Time = "08:00",
    [string]$TaskName = "JobHunter Daily",
    [switch]$Live,
    [switch]$Remove,
    [switch]$IncludeWeekends
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "No scheduled task named '$TaskName'." -ForegroundColor Yellow
    }
    return
}

# Prefer the venv interpreter if there is one — it has the dependencies.
$python = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    throw "Could not find a Python interpreter on PATH. Install Python 3.10+ and retry."
}

if (-not (Test-Path (Join-Path $projectRoot "config\config.yaml"))) {
    Write-Host "WARNING: config\config.yaml does not exist yet." -ForegroundColor Yellow
    Write-Host "         Copy config\config.example.yaml to config\config.yaml first," -ForegroundColor Yellow
    Write-Host "         or set it up in the UI (python run_gui.py)." -ForegroundColor Yellow
}

$mode = if ($Live) { "--live" } else { "--dry-run" }
$arguments = "-m jobhunter run $mode"

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $projectRoot

$daysOfWeek = if ($IncludeWeekends) {
    @("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")
} else {
    @("Monday","Tuesday","Wednesday","Thursday","Friday")
}
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $daysOfWeek -At $Time

# StartWhenAvailable matters on a laptop: if the machine was asleep at 08:00 the run
# happens when it wakes rather than being skipped for the day.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replacing existing task '$TaskName'." -ForegroundColor Yellow
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Daily automated job search and application" | Out-Null

Write-Host ""
Write-Host "Scheduled '$TaskName'" -ForegroundColor Green
Write-Host "  Runs      : $Time on $($daysOfWeek -join ', ')"
Write-Host "  Command   : $python $arguments"
Write-Host "  Directory : $projectRoot"
Write-Host "  Mode      : $(if ($Live) { 'LIVE - applications will be submitted' } else { 'DRY RUN - nothing is submitted' })"
Write-Host ""
Write-Host "Run it now to test:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove it:           powershell -File scripts\install_windows_task.ps1 -Remove"
