<#
.SYNOPSIS
    Removes the scheduled task "ObsidianCopilotMirror".

.DESCRIPTION
    Only the scheduled task is removed. The mirror folders, the log files and the
    scripts stay where they are; the output says how to remove them by hand.
    Vaults are never touched.

.EXAMPLE
    powershell.exe -NoProfile -File .\Unregister-MirrorTask.ps1

.NOTES
    Exit code 0: the task is gone (or did not exist). Exit code 1: it could not be removed.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$TaskName = 'ObsidianCopilotMirror'

if ($env:OS -ne 'Windows_NT') {
    Write-Host 'This script only works on Windows.'
    exit 1
}
if ($null -eq (Get-Command -Name 'Unregister-ScheduledTask' -ErrorAction SilentlyContinue)) {
    Write-Host 'The ScheduledTasks cmdlets are not available. Remove the task by hand:'
    Write-Host ('Task Scheduler > Task Scheduler Library > "' + $TaskName + '" > Delete.')
    exit 1
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host ('No scheduled task named "' + $TaskName + '" exists. Nothing to do.')
} else {
    try {
        if ([string]$task.State -eq 'Running') {
            Write-Host 'The task is running right now; stopping it first.'
            Stop-ScheduledTask -TaskName $TaskName
            Start-Sleep -Seconds 2
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    } catch {
        Write-Host ('The task could not be removed: ' + $_.Exception.Message)
        Write-Host ('Remove it by hand: Task Scheduler > Task Scheduler Library > "' + $TaskName + '" > Delete.')
        exit 1
    }
    if ($null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host 'The task still exists after removal. Remove it by hand in Task Scheduler.'
        exit 1
    }
    Write-Host ('Scheduled task "' + $TaskName + '" removed.')
}

Write-Host ''
Write-Host 'Not removed (delete by hand if you no longer want them):'
Write-Host '  - the mirror folders (the "target" paths in mirror-config.json). Copilot keeps'
Write-Host '    finding the .txt copies until you delete these folders.'
Write-Host '  - the log files (the "logDirectory" in mirror-config.json)'
Write-Host '  - this script folder'
Write-Host 'Your Obsidian vaults are not affected in any way.'
exit 0
