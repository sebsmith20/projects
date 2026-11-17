<#
.SYNOPSIS
    Registers a Windows Task Scheduler job to run monitor.py --once every 15 minutes.
    Run this script from PowerShell inside the t212-analyst directory.
#>

param(
    [string]$TaskName = "T212MarketMonitor",
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [int]$IntervalMinutes = 15
)

$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$MonitorScript = Join-Path $ProjectDir "monitor.py"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Virtual environment python not found at $PythonExe. Create .venv and install requirements.txt first."
    exit 1
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$MonitorScript`" --once" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Trading 212 read-only market & portfolio monitor (human-in-the-loop)" `
    -Force

Write-Host "Scheduled task '$TaskName' registered to run every $IntervalMinutes minutes."
Write-Host "Manage it in Task Scheduler or run: Get-ScheduledTask -TaskName $TaskName"
