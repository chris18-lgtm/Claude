<#
.SYNOPSIS
    Creates or updates the scheduled task "ObsidianCopilotMirror" that runs
    Sync-ObsidianMirror.ps1 at logon and then at a fixed interval.

.DESCRIPTION
    The task runs as the current user, only while that user is logged on, without
    administrator rights and without a stored password.

    Before registering, the script checks the PowerShell execution policy and
    stops with a report if a policy forbids running the mirror script. It does not
    work around any policy.

    After registering, it reads the task back, checks every setting, starts the
    task once and waits for exit code 0.

    Running the script again updates the existing task (idempotent).

.PARAMETER ConfigPath
    Configuration file for the mirror. Default: mirror-config.json next to this script.

.PARAMETER IntervalMinutes
    Minutes between runs. Overrides "syncIntervalMinutes" from the configuration.
    Default 30, minimum 15.

.PARAMETER TimeTriggerOnly
    Register only the repeating time trigger and no logon trigger. Use this only
    if registering the logon trigger is denied on your machine.

.EXAMPLE
    powershell.exe -NoProfile -File .\Register-MirrorTask.ps1

.NOTES
    Exit code 0: task registered, verified and one run finished with exit code 0.
    Exit code 1: anything else. The text output says what is wrong.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [int]$IntervalMinutes = 0,
    [switch]$TimeTriggerOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$TaskName        = 'ObsidianCopilotMirror'
$MarkerName      = '.obsidian-mirror-target'
$MinimumInterval = 15
$MaximumInterval = 1440
$TimeLimitMin    = 15
$LogonDelayMin   = 2

function Write-Step { param([string]$Text) Write-Host ('[..] ' + $Text) }
function Write-Ok   { param([string]$Text) Write-Host ('[OK] ' + $Text) }
function Write-Bad  { param([string]$Text) Write-Host ('[!!] ' + $Text) }

function Stop-WithReport {
    param([string]$Title, [string[]]$Lines)
    Write-Host ''
    Write-Bad $Title
    foreach ($line in $Lines) { Write-Host ('     ' + $line) }
    Write-Host ''
    Write-Host 'Nothing further was changed. See README.md, section "Troubleshooting".'
    exit 1
}

function Get-ConfigValue {
    param($Object, [string]$Name, $Default)
    if ($null -ne $Object) {
        $prop = $Object.PSObject.Properties[$Name]
        if ($null -ne $prop -and $null -ne $prop.Value) { return $prop.Value }
    }
    return $Default
}

function ConvertTo-Minutes {
    # ISO 8601 duration such as PT30M or PT1H30M -> minutes. Returns -1 when empty.
    param($Duration)
    if ([string]::IsNullOrEmpty([string]$Duration)) { return -1 }
    return [System.Xml.XmlConvert]::ToTimeSpan([string]$Duration).TotalMinutes
}

# ---------------------------------------------------------------- preflight ---

Write-Host ('Register-MirrorTask: scheduled task "' + $TaskName + '"')
Write-Host ''

if ($env:OS -ne 'Windows_NT') {
    Stop-WithReport 'This script only works on Windows.' @('The Task Scheduler does not exist on this system.')
}
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Stop-WithReport ('PowerShell runs in ' + $ExecutionContext.SessionState.LanguageMode + ' mode on this machine.') @(
        'An application control policy (AppLocker / App Control) restricts scripts that it does not trust.',
        'Sync-ObsidianMirror.ps1 needs FullLanguage mode and would fail in the scheduled task.',
        'Ask IT whether the script can be trusted by policy (for example by signing it).')
}
if ($null -eq (Get-Command -Name 'Register-ScheduledTask' -ErrorAction SilentlyContinue)) {
    Stop-WithReport 'The ScheduledTasks cmdlets are not available.' @(
        'Register-ScheduledTask was not found. Use the manual steps in README.md, section "Manual task setup".')
}

$scriptDir  = $PSScriptRoot
$syncScript = Join-Path $scriptDir 'Sync-ObsidianMirror.ps1'
if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
    Stop-WithReport 'Sync-ObsidianMirror.ps1 was not found next to this script.' @('Expected: ' + $syncScript)
}
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $scriptDir 'mirror-config.json' }
$ConfigPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigPath)
if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    Stop-WithReport 'Configuration file not found.' @('Expected: ' + $ConfigPath)
}

$config = $null
try {
    $config = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($ConfigPath))
} catch {
    Stop-WithReport 'The configuration file is not valid JSON.' @($ConfigPath, $_.Exception.Message)
}

# --- Interval ----------------------------------------------------------------
$interval = $IntervalMinutes
if ($interval -eq 0) {
    $rawInterval = Get-ConfigValue $config 'syncIntervalMinutes' 30
    $parsed = 0
    if (-not [int]::TryParse([string]$rawInterval, [ref]$parsed)) {
        Stop-WithReport 'syncIntervalMinutes in the configuration is not a whole number.' @('Value: ' + $rawInterval)
    }
    $interval = $parsed
}
if ($interval -lt $MinimumInterval) {
    Stop-WithReport ('The interval of ' + $interval + ' minutes is too short.') @(
        ('The minimum is ' + $MinimumInterval + ' minutes.'),
        'A shorter interval brings no benefit: OneDrive upload and Copilot indexing take far longer than one run of the script.')
}
if ($interval -gt $MaximumInterval) {
    Stop-WithReport ('The interval of ' + $interval + ' minutes is too long.') @(('The maximum is ' + $MaximumInterval + ' minutes (one day).'))
}
Write-Ok ('Interval: every ' + $interval + ' minutes')

# --- A first manual run must have happened -------------------------------------
# The task performs real runs. The first run against real data must be a reviewed
# -WhatIf followed by a manual run; a finished manual run leaves the marker file.
$missing = New-Object System.Collections.ArrayList
foreach ($pair in @(Get-ConfigValue $config 'pairs' @())) {
    $target = [string](Get-ConfigValue $pair 'target' '')
    $name   = [string](Get-ConfigValue $pair 'name' '?')
    if ($target.IndexOf('<') -ge 0) {
        [void]$missing.Add("pair '" + $name + "': the target still contains a placeholder (<...>)")
        continue
    }
    $target = [System.Environment]::ExpandEnvironmentVariables($target)
    if (-not (Test-Path -LiteralPath (Join-Path $target $MarkerName) -PathType Leaf)) {
        [void]$missing.Add("pair '" + $name + "': no marker file in " + $target)
    }
}
if (@(Get-ConfigValue $config 'pairs' @()).Count -eq 0) { [void]$missing.Add('the configuration contains no pairs') }
if ($missing.Count -gt 0) {
    Stop-WithReport 'The mirror has not been run by hand yet.' (@($missing) + @(
        '',
        'Do this first, in this order:',
        '  1. Sync-ObsidianMirror.ps1 -WhatIf     and read the planned copies in the output',
        '  2. Sync-ObsidianMirror.ps1             the first real run',
        '  3. Sync-ObsidianMirror.ps1             again; it must report copied=0 deleted=0',
        'Then run Register-MirrorTask.ps1 again.'))
}
Write-Ok 'Every mirror folder carries its marker file (a manual run has happened)'

# --- Execution policy ----------------------------------------------------------
Write-Host ''
Write-Host 'Execution policy on this machine (Get-ExecutionPolicy -List):'
$policyList = @(Get-ExecutionPolicy -List)
foreach ($entry in $policyList) { Write-Host ('     {0,-14} {1}' -f $entry.Scope, $entry.ExecutionPolicy) }
Write-Host ''

function Get-ScopePolicy {
    param([string]$Scope)
    foreach ($entry in $policyList) {
        if ([string]$entry.Scope -eq $Scope) { return [string]$entry.ExecutionPolicy }
    }
    return 'Undefined'
}

$machinePolicy = Get-ScopePolicy 'MachinePolicy'
$userPolicy    = Get-ScopePolicy 'UserPolicy'
$currentUser   = Get-ScopePolicy 'CurrentUser'
$localMachine  = Get-ScopePolicy 'LocalMachine'

$groupPolicy = $null
$groupScope  = $null
if ($machinePolicy -ne 'Undefined')  { $groupPolicy = $machinePolicy; $groupScope = 'MachinePolicy' }
elseif ($userPolicy -ne 'Undefined') { $groupPolicy = $userPolicy;    $groupScope = 'UserPolicy' }

$addPolicyArgument = $false
if ($null -ne $groupPolicy) {
    # A Group Policy overrides everything, including a -ExecutionPolicy argument.
    if (@('Restricted', 'AllSigned', 'Default') -contains $groupPolicy) {
        Stop-WithReport ('Group Policy sets the execution policy to ' + $groupPolicy + ' (scope ' + $groupScope + ').') @(
            'Sync-ObsidianMirror.ps1 is an unsigned local script and may not run under this policy.',
            'This tool does not work around the policy.',
            'What to ask IT for: either permission to run this specific local script (for example by',
            'signing it with a company code-signing certificate), or a RemoteSigned policy for your account.')
    }
    Write-Ok ('Group Policy (' + $groupScope + ') = ' + $groupPolicy + ': local scripts are allowed')
} else {
    $persisted = $localMachine
    if ($currentUser -ne 'Undefined') { $persisted = $currentUser }
    if ($persisted -eq 'AllSigned') {
        Stop-WithReport 'The execution policy on this machine is AllSigned.' @(
            'Only signed scripts may run. Sync-ObsidianMirror.ps1 is unsigned.',
            'A -ExecutionPolicy argument could technically override this setting, but it was set',
            'deliberately, so this tool does not override it.',
            'What to ask IT for: signing of this script, or a RemoteSigned policy for your account.')
    } elseif (@('Undefined', 'Restricted', 'Default') -contains $persisted) {
        $addPolicyArgument = $true
        Write-Ok ('No Group Policy; persistent policy = ' + $persisted + '. The task will pass "-ExecutionPolicy RemoteSigned" for its own process only.')
    } else {
        Write-Ok ('No Group Policy; persistent policy = ' + $persisted + ': local scripts are allowed, no extra argument needed')
    }
}

# A file downloaded from the internet carries a Zone.Identifier stream. RemoteSigned
# refuses such a file unless it is signed. Report it instead of failing in the task.
$zone = $null
try { $zone = Get-Item -LiteralPath $syncScript -Stream 'Zone.Identifier' -ErrorAction SilentlyContinue } catch { $zone = $null }
if ($null -ne $zone) {
    Stop-WithReport 'Sync-ObsidianMirror.ps1 is marked as "downloaded from the internet".' @(
        'Windows blocks such a script under the RemoteSigned policy, so the scheduled task would fail.',
        'If you have reviewed the script and your company allows running it, remove the mark with',
        '  Unblock-File -LiteralPath "<path>\Sync-ObsidianMirror.ps1"',
        '(same as Properties > Unblock in File Explorer), then run this script again.',
        'If you are not sure whether that is allowed on a managed laptop, ask IT first.')
}

# ------------------------------------------------------------------ register ---

$powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $powershellExe -PathType Leaf)) { $powershellExe = 'powershell.exe' }

$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden'
if ($addPolicyArgument) { $arguments += ' -ExecutionPolicy RemoteSigned' }
$arguments += ' -File "' + $syncScript + '" -ConfigPath "' + $ConfigPath + '"'

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Step 'Building the task definition'
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $scriptDir

$triggers = @()
if (-not $TimeTriggerOnly) {
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $logonTrigger.Delay = 'PT' + $LogonDelayMin + 'M'          # give OneDrive time to start
    $triggers += $logonTrigger
}
# No -RepetitionDuration: an empty duration means "repeat indefinitely".
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($interval) -RepetitionInterval (New-TimeSpan -Minutes $interval)
$triggers += $repeatTrigger

# No -WakeToRun: the machine is never woken for a run.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMin)

$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

$description = 'One-way mirror of Obsidian notes (.md) to .txt for Microsoft 365 Copilot. Runs Sync-ObsidianMirror.ps1. Remove with Unregister-MirrorTask.ps1.'

Write-Step ('Registering the task for ' + $userId)
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
        -Description $description -Force | Out-Null
} catch {
    $message = $_.Exception.Message
    $lines = @('Windows answered: ' + $message, '')
    if ($message -match 'denied|0x80070005') {
        if (-not $TimeTriggerOnly) {
            $lines += 'Some machines do not let standard users create a logon trigger.'
            $lines += 'You can try again without it:  Register-MirrorTask.ps1 -TimeTriggerOnly'
            $lines += 'The mirror then starts at the first interval tick after logon instead of two minutes after logon.'
            $lines += ''
        }
        $lines += 'If that is denied as well, creating scheduled tasks is restricted for your account.'
        $lines += 'Try the manual steps in README.md, section "Manual task setup". If the Task Scheduler'
        $lines += 'window refuses too, ask IT for the right to create a task in your own user context.'
    } else {
        $lines += 'See README.md, section "Manual task setup", for creating the task by hand.'
    }
    Stop-WithReport 'The task could not be registered.' $lines
}
Write-Ok 'Task registered'

# -------------------------------------------------------------------- verify ---

Write-Step 'Reading the task back and checking its settings'
$task = Get-ScheduledTask -TaskName $TaskName
$problems = New-Object System.Collections.ArrayList

function Test-Setting {
    param([string]$Label, [bool]$Condition, [string]$Found)
    if ($Condition) {
        Write-Ok $Label
    } else {
        Write-Bad ($Label + '  -- found: ' + $Found)
        [void]$problems.Add($Label)
    }
}

$s = $task.Settings
Test-Setting 'May start on battery power'            (-not $s.DisallowStartIfOnBatteries)  ([string]$s.DisallowStartIfOnBatteries)
Test-Setting 'Keeps running when switching to battery' (-not $s.StopIfGoingOnBatteries)    ([string]$s.StopIfGoingOnBatteries)
Test-Setting 'Runs a missed start as soon as possible' ([bool]$s.StartWhenAvailable)       ([string]$s.StartWhenAvailable)
Test-Setting 'Does not wake the computer'            (-not $s.WakeToRun)                   ([string]$s.WakeToRun)
$multi = [string]$s.MultipleInstances
Test-Setting 'Does not start a second instance'      ($multi -eq 'IgnoreNew' -or $multi -eq '2') $multi
Test-Setting ('Stops a run after ' + $TimeLimitMin + ' minutes') ((ConvertTo-Minutes $s.ExecutionTimeLimit) -eq $TimeLimitMin) ([string]$s.ExecutionTimeLimit)

$foundRepeat = $false
$foundLogon  = $false
foreach ($trigger in @($task.Triggers)) {
    $className = [string]$trigger.CimClass.CimClassName
    if ($className -eq 'MSFT_TaskLogonTrigger') {
        $foundLogon = $true
        Test-Setting ('Logon trigger delayed by ' + $LogonDelayMin + ' minutes') ((ConvertTo-Minutes $trigger.Delay) -eq $LogonDelayMin) ([string]$trigger.Delay)
    }
    $repetition = $trigger.Repetition
    if ($null -ne $repetition -and -not [string]::IsNullOrEmpty([string]$repetition.Interval)) {
        $foundRepeat = $true
        Test-Setting ('Repeats every ' + $interval + ' minutes') ((ConvertTo-Minutes $repetition.Interval) -eq $interval) ([string]$repetition.Interval)
        Test-Setting 'Repeats indefinitely (no duration)' ([string]::IsNullOrEmpty([string]$repetition.Duration)) ([string]$repetition.Duration)
    }
}
Test-Setting 'Has a repeating trigger' $foundRepeat 'none'
if (-not $TimeTriggerOnly) { Test-Setting 'Has a logon trigger' $foundLogon 'none' }
Test-Setting 'Runs only while the user is logged on, without stored password' ([string]$task.Principal.LogonType -eq 'Interactive' -or [string]$task.Principal.LogonType -eq '3') ([string]$task.Principal.LogonType)
Test-Setting 'Runs without elevated rights' ([string]$task.Principal.RunLevel -eq 'Limited' -or [string]$task.Principal.RunLevel -eq '0') ([string]$task.Principal.RunLevel)

if ($problems.Count -gt 0) {
    Stop-WithReport 'The task was registered, but not all settings were stored as requested.' (@($problems) + @(
        '',
        'The task exists and may run. Open Task Scheduler, find "' + $TaskName + '" and correct the',
        'settings listed above by hand (README.md, section "Manual task setup"), or remove the task',
        'with Unregister-MirrorTask.ps1.'))
}

# ------------------------------------------------------------------ test run ---

Write-Step 'Starting the task once and waiting for it to finish'
$started = Get-Date
Start-ScheduledTask -TaskName $TaskName
$deadline = $started.AddSeconds(180)
$result = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $info  = Get-ScheduledTaskInfo -TaskName $TaskName
    $state = [string](Get-ScheduledTask -TaskName $TaskName).State
    $ranNow = ($null -ne $info.LastRunTime -and $info.LastRunTime -ge $started.AddSeconds(-2))
    if ($ranNow -and $state -ne 'Running' -and $info.LastTaskResult -ne 267009) {
        $result = $info.LastTaskResult
        break
    }
}

if ($null -eq $result) {
    Stop-WithReport 'The task did not finish within 3 minutes.' @(
        'The task is registered. Check Task Scheduler ("' + $TaskName + '", column "Last Run Result")',
        'and the log folder configured in mirror-config.json.')
}
if ($result -ne 0) {
    Stop-WithReport ('The task ran but returned exit code ' + $result + ' (0x' + ('{0:X}' -f [int64]$result) + ').') @(
        'Exit code 1 means the mirror script reported an error or a safety guard stopped it;',
        'the reason is in the log folder configured in mirror-config.json.',
        'Any other code comes from Windows (for example a blocked script).',
        'The task stays registered and will try again at the next interval.')
}
Write-Ok 'The task ran once and returned exit code 0'

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ''
Write-Host ('Done. Next scheduled run: ' + $info.NextRunTime)
Write-Host 'Known nuisance: a console window may flash briefly at every run. See README.md, "Console window flash".'
exit 0
