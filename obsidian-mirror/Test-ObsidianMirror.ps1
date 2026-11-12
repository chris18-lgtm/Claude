<#
.SYNOPSIS
    Self-contained tests for Sync-ObsidianMirror.ps1. No Pester required.

.DESCRIPTION
    Every case builds a throwaway vault and mirror under the temp folder, runs the
    real script as a child process, checks exit code, files and log, and cleans up.
    Nothing outside the temp folder is touched. Real vaults are never used.

    Prints PASS / FAIL / SKIP per case. Exit code 0 when no case failed, else 1.

.PARAMETER Perf
    Additionally generate -PerfNoteCount notes and report the duration of the
    initial run and of a no-change run (brief section 7.8).

.PARAMETER Only
    Run only the cases whose name matches this wildcard, for example -Only '12*'.

.PARAMETER KeepSandbox
    Do not delete the temp folders afterwards (for debugging a failure).

.EXAMPLE
    powershell.exe -NoProfile -File .\Test-ObsidianMirror.ps1
.EXAMPLE
    powershell.exe -NoProfile -File .\Test-ObsidianMirror.ps1 -Perf
#>
[CmdletBinding()]
param(
    [switch]$Perf,
    [int]$PerfNoteCount = 5000,
    [string]$Only,
    [switch]$KeepSandbox
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:Here       = $PSScriptRoot
$script:SyncScript = Join-Path $script:Here 'Sync-ObsidianMirror.ps1'
$script:HostExe    = (Get-Process -Id $PID).Path
# When started from the ISE or another host, fall back to the console executable.
$hostName = [System.IO.Path]::GetFileNameWithoutExtension([string]$script:HostExe)
if ($hostName -ne 'powershell' -and $hostName -ne 'pwsh') {
    foreach ($candidate in @('powershell.exe', 'pwsh.exe', 'pwsh')) {
        $candidatePath = Join-Path $PSHOME $candidate
        if ([System.IO.File]::Exists($candidatePath)) { $script:HostExe = $candidatePath; break }
    }
}
$script:Sep        = [System.IO.Path]::DirectorySeparatorChar
$script:TempRoot   = [System.IO.Path]::GetTempPath()
$script:Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)
$script:Sandboxes  = New-Object System.Collections.ArrayList
$script:Passed     = 0
$script:Failed     = 0
$script:Skipped    = 0
$script:MarkerName = '.obsidian-mirror-target'

# Non-ASCII test data is built from code points so this file stays pure ASCII
# (Windows PowerShell 5.1 reads script files without BOM as ANSI).
$script:Umlauts = -join ([char[]](0x00E4, 0x00F6, 0x00FC, 0x00DF, 0x20AC))   # a-uml o-uml u-uml sharp-s euro
$script:OUml    = [string][char]0x00F6

# ---------------------------------------------------------------- helpers ----

function New-Sandbox {
    # The folder name deliberately contains a space.
    $root = Join-Path $script:TempRoot ('ObsidianMirror Test-' + [guid]::NewGuid().ToString('N').Substring(0, 10))
    [void][System.IO.Directory]::CreateDirectory($root)
    [void]$script:Sandboxes.Add($root)
    return [pscustomobject]@{
        Root   = $root
        Source = Join-Path $root 'Vault'
        Target = Join-Path $root 'Mirror'
        Logs   = Join-Path $root 'Logs'
        Config = Join-Path $root 'mirror-config.json'
    }
}

function ConvertTo-NativePath {
    param([string]$Relative)
    return $Relative.Replace('/', $script:Sep)
}

function Write-TestFile {
    param([string]$Root, [string]$Relative, [string]$Text = 'content')
    $full = Join-Path $Root (ConvertTo-NativePath $Relative)
    $dir  = [System.IO.Path]::GetDirectoryName($full)
    if (-not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
    [System.IO.File]::WriteAllBytes($full, $script:Utf8NoBom.GetBytes($Text))
    return $full
}

function New-PairSpec {
    param($Sandbox, [string]$Name = 'main', [string[]]$Exclude = @('.obsidian', '.trash', '.git', 'Templates'),
          [string]$Source, [string]$Target)
    if (-not $Source) { $Source = $Sandbox.Source }
    if (-not $Target) { $Target = $Sandbox.Target }
    return [ordered]@{ name = $Name; source = $Source; target = $Target; excludeFolders = @($Exclude) }
}

function Write-Config {
    param($Sandbox, [object[]]$Pairs, [hashtable]$Overrides = @{})
    if (-not $Pairs) { $Pairs = @(New-PairSpec $Sandbox) }
    $cfg = [ordered]@{
        pairs               = @($Pairs)
        maxDeleteCount      = 25
        maxDeletePercent    = 20
        logDirectory        = $Sandbox.Logs
        logRetentionDays    = 14
        syncIntervalMinutes = 30
    }
    foreach ($k in $Overrides.Keys) { $cfg[$k] = $Overrides[$k] }
    $json = ConvertTo-Json -InputObject $cfg -Depth 6
    [System.IO.File]::WriteAllText($Sandbox.Config, $json, $script:Utf8NoBom)
}

function Get-LogText {
    param($Sandbox)
    if (-not [System.IO.Directory]::Exists($Sandbox.Logs)) { return '' }
    $text = ''
    foreach ($f in (Get-ChildItem -LiteralPath $Sandbox.Logs -Filter '*.log' -File | Sort-Object Name)) {
        $text += [System.IO.File]::ReadAllText($f.FullName)
    }
    return $text
}

function Invoke-Sync {
    param($Sandbox, [string[]]$Arguments = @())
    $before = Get-LogText $Sandbox
    $allArgs = @('-NoProfile', '-NonInteractive', '-File', $script:SyncScript, '-ConfigPath', $Sandbox.Config) + $Arguments
    $oldPref = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # stderr of a native command must not throw in 5.1
    try {
        $output = & $script:HostExe @allArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPref
    }
    $after = Get-LogText $Sandbox
    $new = $after
    if ($after.Length -ge $before.Length) { $new = $after.Substring($before.Length) }
    return [pscustomobject]@{ ExitCode = $code; Output = ($output | Out-String); Log = $new }
}

function Get-RelativeFiles {
    # Sorted list of all files below Root, relative, with forward slashes.
    param([string]$Root)
    if (-not [System.IO.Directory]::Exists($Root)) { return @() }
    $prefix = $Root.TrimEnd($script:Sep).Length + 1
    $list = @(Get-ChildItem -LiteralPath $Root -Recurse -Force -File | ForEach-Object { $_.FullName.Substring($prefix).Replace($script:Sep, '/') })
    return @($list | Sort-Object)
}

function Get-RelativeDirs {
    param([string]$Root)
    if (-not [System.IO.Directory]::Exists($Root)) { return @() }
    $prefix = $Root.TrimEnd($script:Sep).Length + 1
    $list = @(Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory | ForEach-Object { $_.FullName.Substring($prefix).Replace($script:Sep, '/') })
    return @($list | Sort-Object)
}

function Get-TreeSnapshot {
    # name|length|mtime ticks for every file plus every directory; used to prove "nothing changed".
    param([string]$Root)
    if (-not [System.IO.Directory]::Exists($Root)) { return '<missing>' }
    $prefix = $Root.TrimEnd($script:Sep).Length + 1
    $lines = @(Get-ChildItem -LiteralPath $Root -Recurse -Force | ForEach-Object {
        $rel = $_.FullName.Substring($prefix)
        if ($_.PSIsContainer) { 'D|' + $rel } else { 'F|' + $rel + '|' + $_.Length + '|' + $_.LastWriteTimeUtc.Ticks }
    })
    return (@($lines | Sort-Object) -join "`n")
}

function Get-Hash {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Get-SummaryValue {
    # Reads e.g. copied=3 from the last SUMMARY line of the given pair.
    param($Run, [string]$PairName, [string]$Field)
    $value = $null
    foreach ($line in ($Run.Log -split "`r?`n")) {
        if ($line -match ('SUMMARY pair=' + [regex]::Escape($PairName) + ' ') -and $line -match (' ' + $Field + '=(\S+)')) {
            $value = $Matches[1]
        }
    }
    return $value
}

function Assert-True {
    param($Condition, [string]$Message)
    if (-not $Condition) { throw ('Assertion failed: ' + $Message) }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    if ("$Expected" -cne "$Actual") { throw ('Assertion failed: ' + $Message + ' (expected [' + $Expected + '], got [' + $Actual + '])') }
}

function Assert-ListEqual {
    param([string[]]$Expected, [string[]]$Actual, [string]$Message)
    $e = (@($Expected | Sort-Object) -join ' ; ')
    $a = (@($Actual | Sort-Object) -join ' ; ')
    if ($e -cne $a) { throw ('Assertion failed: ' + $Message + "`n      expected: " + $e + "`n      actual:   " + $a) }
}

function Assert-Exit {
    param($Run, [int]$Expected, [string]$Message)
    if ($Run.ExitCode -ne $Expected) {
        throw ('Assertion failed: ' + $Message + ' (expected exit ' + $Expected + ', got ' + $Run.ExitCode + ")`n" + $Run.Output + $Run.Log)
    }
}

function Assert-LogMatch {
    param($Run, [string]$Pattern, [string]$Message)
    if ($Run.Log -notmatch $Pattern) { throw ('Assertion failed: ' + $Message + ' (log does not match /' + $Pattern + "/)`n" + $Run.Log) }
}

function Test-Case {
    param([string]$Name, [scriptblock]$Body)
    if ($Only -and $Name -notlike $Only) { return }
    try {
        & $Body
        $script:Passed++
        Write-Host ('PASS  ' + $Name)
    } catch {
        $msg = $_.Exception.Message
        if ($msg -like 'SKIP:*') {
            $script:Skipped++
            Write-Host ('SKIP  ' + $Name + '  --  ' + $msg.Substring(5).Trim())
        } else {
            $script:Failed++
            Write-Host ('FAIL  ' + $Name)
            Write-Host ('      ' + $msg)
        }
    }
}

function New-BasicVault {
    param($Sandbox)
    [void](Write-TestFile $Sandbox.Source 'Inbox.md' 'inbox')
    [void](Write-TestFile $Sandbox.Source 'Projects/Alpha/Plan.md' 'plan')
    [void](Write-TestFile $Sandbox.Source 'Projects/Alpha/Notes with spaces.md' 'spaces')
    [void](Write-TestFile $Sandbox.Source 'Projects/Beta/Readme.MD' 'upper-case extension')
    [void](Write-TestFile $Sandbox.Source 'Archive/Old.md' 'old')
}

$script:BasicExpected = @(
    $script:MarkerName,
    'Archive/Old.txt',
    'Inbox.txt',
    'Projects/Alpha/Notes with spaces.txt',
    'Projects/Alpha/Plan.txt',
    'Projects/Beta/Readme.txt'
)

function Get-MirrorMutexName {
    # Must match Get-MutexName in Sync-ObsidianMirror.ps1.
    param([string]$ConfigPath)
    $full = [System.IO.Path]::GetFullPath($ConfigPath).ToLowerInvariant()
    $safe = [regex]::Replace($full, '[^a-z0-9]', '_')
    if ($safe.Length -gt 150) { $safe = $safe.Substring($safe.Length - 150) }
    return 'ObsidianMirror-' + $safe
}

# ------------------------------------------------------------------ cases ----

Write-Host ('Testing ' + $script:SyncScript)
Write-Host ('Host:    ' + $script:HostExe + '  (PowerShell ' + $PSVersionTable.PSVersion.ToString() + ')')
Write-Host ''

if (-not [System.IO.File]::Exists($script:SyncScript)) {
    Write-Host 'FAIL  Sync-ObsidianMirror.ps1 not found next to this test script.'
    exit 1
}

try {

Test-Case '01 initial run mirrors a nested structure and creates the marker' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'initial run'
    Assert-ListEqual $script:BasicExpected (Get-RelativeFiles $sb.Target) 'mirror content'
    Assert-Equal 5 (Get-SummaryValue $run 'main' 'scanned') 'scanned'
    Assert-Equal 5 (Get-SummaryValue $run 'main' 'copied') 'copied'
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'errors') 'errors'
    $src = [System.IO.FileInfo](Join-Path $sb.Source 'Inbox.md')
    $dst = [System.IO.FileInfo](Join-Path $sb.Target 'Inbox.txt')
    Assert-True ([Math]::Abs(($src.LastWriteTimeUtc - $dst.LastWriteTimeUtc).TotalSeconds) -lt 2) 'LastWriteTimeUtc preserved on the copy'
}

Test-Case '02 second run with no changes copies nothing and leaves the mirror untouched' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    # Tamper with one mirror file: same length, same timestamp, different bytes.
    # A metadata-only comparison must not notice, so the tampered bytes must survive.
    $dst = Join-Path $sb.Target 'Inbox.txt'
    $stamp = [System.IO.File]::GetLastWriteTimeUtc($dst)
    [System.IO.File]::WriteAllBytes($dst, $script:Utf8NoBom.GetBytes('XXXXX'))
    [System.IO.File]::SetLastWriteTimeUtc($dst, $stamp)
    $snapshot = Get-TreeSnapshot $sb.Target
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'no-change run'
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'copied') 'copied'
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'deleted') 'deleted'
    Assert-Equal 5 (Get-SummaryValue $run 'main' 'unchanged') 'unchanged'
    Assert-Equal $snapshot (Get-TreeSnapshot $sb.Target) 'mirror snapshot'
    Assert-Equal 'XXXXX' ([System.IO.File]::ReadAllText($dst)) 'mirror file was not rewritten'
    Assert-True ($run.Log -cnotmatch 'COPIED') 'no per-file lines for unchanged files'
}

Test-Case '03 a modified note is re-copied, an unmodified one is not' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    # (a) different length
    [void](Write-TestFile $sb.Source 'Inbox.md' 'inbox with more text')
    # (b) same length, newer timestamp only
    $plan = Join-Path $sb.Source (ConvertTo-NativePath 'Projects/Alpha/Plan.md')
    [System.IO.File]::WriteAllBytes($plan, $script:Utf8NoBom.GetBytes('PLAN'))
    [System.IO.File]::SetLastWriteTimeUtc($plan, [DateTime]::UtcNow.AddSeconds(30))
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'second run'
    Assert-Equal 2 (Get-SummaryValue $run 'main' 'copied') 'copied'
    Assert-Equal 3 (Get-SummaryValue $run 'main' 'unchanged') 'unchanged'
    Assert-Equal 'inbox with more text' ([System.IO.File]::ReadAllText((Join-Path $sb.Target 'Inbox.txt'))) 'changed length'
    Assert-Equal 'PLAN' ([System.IO.File]::ReadAllText((Join-Path $sb.Target (ConvertTo-NativePath 'Projects/Alpha/Plan.txt')))) 'changed timestamp'
    Assert-True ($run.Log -cnotmatch 'COPIED.*Old\.txt') 'unmodified note not copied'
}

Test-Case '04 a deleted note removes its .txt' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    [System.IO.File]::Delete((Join-Path $sb.Source 'Inbox.md'))
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'second run'
    Assert-Equal 1 (Get-SummaryValue $run 'main' 'deleted') 'deleted'
    Assert-True (-not [System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'Inbox.txt removed'
    Assert-LogMatch $run 'DELETED\s+Inbox\.txt' 'delete is logged'
}

Test-Case '05 rename and move replace the old .txt, emptied folders are removed' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    [System.IO.File]::Move((Join-Path $sb.Source 'Inbox.md'), (Join-Path $sb.Source 'Inbox renamed.md'))
    [void][System.IO.Directory]::CreateDirectory((Join-Path $sb.Source 'Moved'))
    [System.IO.File]::Move((Join-Path $sb.Source (ConvertTo-NativePath 'Archive/Old.md')), (Join-Path $sb.Source (ConvertTo-NativePath 'Moved/Old.md')))
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'second run'
    $expected = @($script:MarkerName, 'Inbox renamed.txt', 'Moved/Old.txt',
        'Projects/Alpha/Notes with spaces.txt', 'Projects/Alpha/Plan.txt', 'Projects/Beta/Readme.txt')
    Assert-ListEqual $expected (Get-RelativeFiles $sb.Target) 'mirror content after rename/move'
    Assert-True (-not [System.IO.Directory]::Exists((Join-Path $sb.Target 'Archive'))) 'emptied folder Archive removed'
    Assert-True ([System.IO.Directory]::Exists($sb.Target)) 'mirror root itself still exists'
}

Test-Case '06 excluded folders are never mirrored; a folder excluded later is cleaned up' {
    $sb = New-Sandbox; New-BasicVault $sb
    [void](Write-TestFile $sb.Source '.obsidian/workspace.md' 'x')
    [void](Write-TestFile $sb.Source '.hidden-not-listed/secret.md' 'x')          # dot folder, not listed
    [void](Write-TestFile $sb.Source 'Templates/Daily.md' 'x')                     # name, top level
    [void](Write-TestFile $sb.Source 'Projects/templates/Nested.md' 'x')           # name, any depth, other case
    [void](Write-TestFile $sb.Source 'Work/Private/Salary.md' 'x')                 # relative path
    [void](Write-TestFile $sb.Source 'Work/Public/Agenda.md' 'x')
    [void](Write-TestFile $sb.Source 'Other/Private/Fine.md' 'x')                  # same name elsewhere: not excluded
    [void](Write-TestFile $sb.Source '.dotfile-note.md' 'x')                       # dot FILE is a normal note
    Write-Config $sb -Pairs @(New-PairSpec $sb -Exclude @('.obsidian', 'Templates', 'work\private'))
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'first run'
    $expected = @($script:BasicExpected) + @('Work/Public/Agenda.txt', 'Other/Private/Fine.txt', '.dotfile-note.txt')
    Assert-ListEqual $expected (Get-RelativeFiles $sb.Target) 'mirror content with excludes'
    # Exclude "Archive" and "Other/Private" later: their copies must disappear.
    Write-Config $sb -Pairs @(New-PairSpec $sb -Exclude @('.obsidian', 'Templates', 'Work/Private', 'Archive', 'Other/Private/'))
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'second run'
    $expected = @($script:BasicExpected | Where-Object { $_ -ne 'Archive/Old.txt' }) + @('Work/Public/Agenda.txt', '.dotfile-note.txt')
    Assert-ListEqual $expected (Get-RelativeFiles $sb.Target) 'mirror content after excluding later'
    Assert-True (-not [System.IO.Directory]::Exists((Join-Path $sb.Target 'Archive'))) 'Archive folder removed from mirror'
    Assert-True (-not [System.IO.Directory]::Exists((Join-Path $sb.Target 'Other'))) 'Other folder removed from mirror'
}

Test-Case '07 non-.md files in the source are ignored' {
    $sb = New-Sandbox; New-BasicVault $sb
    [void](Write-TestFile $sb.Source 'Picture.png' 'x')
    [void](Write-TestFile $sb.Source 'Board.canvas' 'x')
    [void](Write-TestFile $sb.Source 'Projects/Alpha/Sheet.xlsx' 'x')
    [void](Write-TestFile $sb.Source 'Projects/Alpha/Already.txt' 'x')
    [void](Write-TestFile $sb.Source 'Projects/Alpha/Fake.md.bak' 'x')
    [void](Write-TestFile $sb.Source 'Projects/Alpha/NoExtension' 'x')
    [void](Write-TestFile $sb.Source 'Attachments/only.pdf' 'x')
    Write-Config $sb
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'run'
    Assert-ListEqual $script:BasicExpected (Get-RelativeFiles $sb.Target) 'only notes mirrored'
    Assert-True (-not [System.IO.Directory]::Exists((Join-Path $sb.Target 'Attachments'))) 'no folder for attachment-only directory'
}

Test-Case '08 umlauts, euro sign and brackets arrive byte-identical' {
    $sb = New-Sandbox
    $name1 = 'N' + $script:OUml + 'te [draft] #1 & more.md'
    $dir   = 'Gr' + $script:OUml + [string][char]0x00DF + 'e [2026]'
    $a = Write-TestFile $sb.Source 'Umlaut content.md' ('Text ' + $script:Umlauts + " end`r`n")
    $b = Write-TestFile $sb.Source ($dir + '/' + $name1) ('# ' + $script:Umlauts)
    # UTF-16 with BOM and raw bytes that are not valid UTF-8 must survive as well.
    $c = Join-Path $sb.Source 'Utf16.md'
    [System.IO.File]::WriteAllText($c, ('Wide ' + $script:Umlauts), [System.Text.Encoding]::Unicode)
    $d = Join-Path $sb.Source 'Ansi.md'
    [System.IO.File]::WriteAllBytes($d, [byte[]](0x47, 0x72, 0xFC, 0xDF, 0x65, 0x00, 0xFF, 0x0A))
    Write-Config $sb
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'run'
    $ta = Join-Path $sb.Target 'Umlaut content.txt'
    $tb = Join-Path (Join-Path $sb.Target $dir) ($name1.Substring(0, $name1.Length - 3) + '.txt')
    $tc = Join-Path $sb.Target 'Utf16.txt'
    $td = Join-Path $sb.Target 'Ansi.txt'
    foreach ($p in @($ta, $tb, $tc, $td)) { Assert-True ([System.IO.File]::Exists($p)) ('exists: ' + $p) }
    Assert-Equal (Get-Hash $a) (Get-Hash $ta) 'hash of umlaut content'
    Assert-Equal (Get-Hash $b) (Get-Hash $tb) 'hash of file with umlaut/bracket name'
    Assert-Equal (Get-Hash $c) (Get-Hash $tc) 'hash of UTF-16 file'
    Assert-Equal (Get-Hash $d) (Get-Hash $td) 'hash of non-UTF-8 bytes'
    $run = Invoke-Sync $sb
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'copied') 'second run copies nothing'
}

Test-Case '09 a missing source aborts with no deletions' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    $before = Get-TreeSnapshot $sb.Target
    [System.IO.Directory]::Move($sb.Source, ($sb.Source + '-gone'))
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'missing source must exit 1'
    Assert-Equal $before (Get-TreeSnapshot $sb.Target) 'mirror untouched'
    Assert-LogMatch $run 'ERROR.*[Ss]ource' 'error explains the missing source'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Source)) 'missing source is not created'
}

Test-Case '10 a source with zero .md files aborts with no deletions' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    $before = Get-TreeSnapshot $sb.Target
    foreach ($f in (Get-ChildItem -LiteralPath $sb.Source -Recurse -File)) { [System.IO.File]::Delete($f.FullName) }
    [void](Write-TestFile $sb.Source 'leftover.png' 'x')
    [void](Write-TestFile $sb.Source 'Templates/OnlyExcluded.md' 'x')    # only notes left are excluded
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'empty source must exit 1'
    Assert-Equal $before (Get-TreeSnapshot $sb.Target) 'mirror untouched'
}

Test-Case '11 non-empty target without marker aborts; -InitializeTarget resolves it' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    [void](Write-TestFile $sb.Target 'Somebody elses.txt' 'not ours')
    [void](Write-TestFile $sb.Target 'Report.pdf' 'not ours')
    $before = Get-TreeSnapshot $sb.Target
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'unmarked non-empty target must exit 1'
    Assert-Equal $before (Get-TreeSnapshot $sb.Target) 'target untouched without marker'
    Assert-LogMatch $run 'InitializeTarget' 'message tells the user how to confirm'
    $run = Invoke-Sync $sb @('-InitializeTarget')
    Assert-Exit $run 0 'run with -InitializeTarget'
    $files = Get-RelativeFiles $sb.Target
    Assert-True ($files -contains $script:MarkerName) 'marker created'
    Assert-True ($files -contains 'Inbox.txt') 'notes mirrored'
    Assert-True ($files -contains 'Report.pdf') 'foreign non-.txt file kept'
    Assert-True ($files -notcontains 'Somebody elses.txt') 'orphan .txt cleaned after explicit confirmation'
    # An empty existing folder needs no confirmation.
    $sb2 = New-Sandbox; New-BasicVault $sb2; Write-Config $sb2
    [void][System.IO.Directory]::CreateDirectory($sb2.Target)
    Assert-Exit (Invoke-Sync $sb2) 0 'empty existing target'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb2.Target $script:MarkerName))) 'marker created in empty target'
}

Test-Case '12 delete threshold blocks a mass delete; -ForceDelete allows it' {
    $sb = New-Sandbox
    for ($i = 1; $i -le 30; $i++) { [void](Write-TestFile $sb.Source ('Bulk/Note{0:D2}.md' -f $i) "n$i") }
    [void](Write-TestFile $sb.Source 'Keep.md' 'keep')
    Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    [System.IO.Directory]::Delete((Join-Path $sb.Source 'Bulk'), $true)     # 30 of 31 would go
    [void](Write-TestFile $sb.Source 'New.md' 'new')
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'threshold must exit 1'
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'deleted') 'nothing deleted'
    Assert-Equal 33 (@(Get-RelativeFiles $sb.Target)).Count 'all 31 old copies + marker + the new copy remain'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb.Target 'New.txt'))) 'copies still happen when deletes are blocked'
    Assert-LogMatch $run 'ERROR.*ForceDelete' 'error mentions -ForceDelete'
    $run = Invoke-Sync $sb @('-ForceDelete')
    Assert-Exit $run 0 'run with -ForceDelete'
    Assert-Equal 30 (Get-SummaryValue $run 'main' 'deleted') 'deleted with -ForceDelete'
    Assert-ListEqual @($script:MarkerName, 'Keep.txt', 'New.txt') (Get-RelativeFiles $sb.Target) 'mirror after forced delete'

    # Both limits must be exceeded. Many files but small share: allowed.
    $sb = New-Sandbox
    for ($i = 1; $i -le 40; $i++) { [void](Write-TestFile $sb.Source ('N{0:D2}.md' -f $i) "n$i") }
    Write-Config $sb -Overrides @{ maxDeleteCount = 2; maxDeletePercent = 20 }
    Assert-Exit (Invoke-Sync $sb) 0 'initial run (b)'
    for ($i = 1; $i -le 5; $i++) { [System.IO.File]::Delete((Join-Path $sb.Source ('N{0:D2}.md' -f $i))) }   # 5 > 2 but 12.5 % < 20 %
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'count above, percent below'
    Assert-Equal 5 (Get-SummaryValue $run 'main' 'deleted') 'deleted (b)'
    # Large share but few files: allowed.
    $sb = New-Sandbox
    for ($i = 1; $i -le 10; $i++) { [void](Write-TestFile $sb.Source ('N{0:D2}.md' -f $i) "n$i") }
    Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run (c)'
    for ($i = 1; $i -le 6; $i++) { [System.IO.File]::Delete((Join-Path $sb.Source ('N{0:D2}.md' -f $i))) }   # 60 % but 6 <= 25
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'percent above, count below'
    Assert-Equal 6 (Get-SummaryValue $run 'main' 'deleted') 'deleted (c)'
}

Test-Case '13 nested or identical source and target are rejected' {
    # target inside source
    $sb = New-Sandbox; New-BasicVault $sb
    Write-Config $sb -Pairs @(New-PairSpec $sb -Target (Join-Path $sb.Source 'Mirror'))
    $before = Get-TreeSnapshot $sb.Source
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'target inside source'
    Assert-LogMatch $run 'Unsafe folder layout' 'layout check names the problem (target inside source)'
    Assert-Equal $before (Get-TreeSnapshot $sb.Source) 'vault untouched (target inside source)'
    # source inside target -- the dangerous variant: the mirror even carries a valid
    # marker, and the vault holds a .txt that orphan cleanup would otherwise delete.
    $sb = New-Sandbox
    $inner = Join-Path $sb.Target 'Vault'
    [void](Write-TestFile $inner 'A.md' 'a')
    $precious = Write-TestFile $inner 'precious.txt' 'a real file inside the vault'
    [void](Write-TestFile $sb.Target $script:MarkerName 'marker')
    Write-Config $sb -Pairs @(New-PairSpec $sb -Source $inner)
    $before = Get-TreeSnapshot $sb.Target
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'source inside target'
    Assert-LogMatch $run 'Unsafe folder layout' 'layout check names the problem (source inside target)'
    Assert-True ([System.IO.File]::Exists($precious)) '.txt inside the vault survives'
    Assert-Equal $before (Get-TreeSnapshot $sb.Target) 'nothing touched (source inside target)'
    # identical, written differently
    $sb = New-Sandbox; New-BasicVault $sb
    Write-Config $sb -Pairs @(New-PairSpec $sb -Target (Join-Path (Join-Path $sb.Source '..') 'Vault'))
    $before = Get-TreeSnapshot $sb.Source
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'source equals target'
    Assert-LogMatch $run 'Unsafe folder layout' 'layout check names the problem (identical)'
    Assert-Equal $before (Get-TreeSnapshot $sb.Source) 'vault untouched (identical paths)'
    # nesting across two different pairs: both involved pairs stop
    $sb = New-Sandbox; New-BasicVault $sb
    $vault2 = Join-Path $sb.Root 'Vault2'
    [void](Write-TestFile $vault2 'B.md' 'b')
    $p1 = New-PairSpec $sb -Name 'one' -Target (Join-Path $vault2 'MirrorOfOne')
    $p2 = New-PairSpec $sb -Name 'two' -Source $vault2 -Target (Join-Path $sb.Root 'MirrorTwo')
    Write-Config $sb -Pairs @($p1, $p2)
    $before = Get-TreeSnapshot $vault2
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'target of one pair inside source of another'
    Assert-LogMatch $run 'SUMMARY pair=one .*status=ABORTED' 'pair one aborted'
    Assert-LogMatch $run 'SUMMARY pair=two .*status=ABORTED' 'pair two aborted'
    Assert-Equal $before (Get-TreeSnapshot $vault2) 'second vault untouched'
    Assert-True (-not [System.IO.Directory]::Exists((Join-Path $sb.Root 'MirrorTwo'))) 'no mirror created for the involved pair'
    # a sibling folder whose name merely starts with the vault name is NOT nested
    $sb = New-Sandbox; New-BasicVault $sb
    Write-Config $sb -Pairs @(New-PairSpec $sb -Target ($sb.Source + '-Mirror'))
    Assert-Exit (Invoke-Sync $sb) 0 'sibling folder with a common name prefix is allowed'
}

Test-Case '14 -WhatIf changes nothing on disk' {
    # (a) fresh target: nothing is created
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    $srcBefore = Get-TreeSnapshot $sb.Source
    $run = Invoke-Sync $sb @('-WhatIf')
    Assert-Exit $run 0 'dry run on fresh target'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Target)) 'target not created by dry run'
    Assert-Equal $srcBefore (Get-TreeSnapshot $sb.Source) 'vault untouched'
    Assert-LogMatch $run 'WOULD COPY\s+Inbox\.txt' 'planned copy is logged'
    Assert-Equal 5 (Get-SummaryValue $run 'main' 'copied') 'planned copies counted'
    # (b) existing mirror with pending copy, delete and empty folder
    Assert-Exit (Invoke-Sync $sb) 0 'real run'
    [void](Write-TestFile $sb.Source 'Added.md' 'added')
    [void](Write-TestFile $sb.Source 'Inbox.md' 'inbox changed')
    [System.IO.File]::Delete((Join-Path $sb.Source (ConvertTo-NativePath 'Archive/Old.md')))
    $srcBefore = Get-TreeSnapshot $sb.Source
    $dstBefore = Get-TreeSnapshot $sb.Target
    $run = Invoke-Sync $sb @('-WhatIf')
    Assert-Exit $run 0 'dry run on existing mirror'
    Assert-Equal $srcBefore (Get-TreeSnapshot $sb.Source) 'vault untouched (b)'
    Assert-Equal $dstBefore (Get-TreeSnapshot $sb.Target) 'mirror untouched (b)'
    Assert-LogMatch $run 'WOULD COPY\s+Added\.txt' 'planned new copy'
    Assert-LogMatch $run 'WOULD COPY\s+Inbox\.txt' 'planned changed copy'
    Assert-LogMatch $run 'WOULD DELETE\s+Archive.Old\.txt' 'planned delete'
    Assert-LogMatch $run 'WOULD REMOVE DIR\s+Archive' 'planned folder removal'
}

Test-Case '15 a stray non-.txt file in the target is left alone and logged' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    $stray1 = Write-TestFile $sb.Target 'Projects/Alpha/stray.docx' 'keep me'
    $stray2 = Write-TestFile $sb.Target 'OnlyStray/picture.png' 'keep me too'
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'stray files are only a warning'
    Assert-True ([System.IO.File]::Exists($stray1)) 'stray.docx kept'
    Assert-True ([System.IO.File]::Exists($stray2)) 'picture.png kept (and its folder)'
    Assert-LogMatch $run 'WARN.*stray\.docx' 'stray file logged as warning'
    Assert-True ($run.Log -notmatch ('WARN.*' + [regex]::Escape($script:MarkerName))) 'marker is not reported as stray'
    Assert-Equal 2 (Get-SummaryValue $run 'main' 'warnings') 'warning count'
}

Test-Case '16 a locked source file is skipped with a warning and the run continues' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    $locked = Join-Path $sb.Source 'Inbox.md'
    $stream = [System.IO.File]::Open($locked, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try { $run = Invoke-Sync $sb } finally { $stream.Dispose() }
    if ([System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) {
        throw 'SKIP: this platform does not enforce exclusive file locks between processes'
    }
    Assert-Exit $run 0 'a locked file must not fail the run'
    Assert-LogMatch $run 'WARN.*Inbox' 'locked file logged as warning'
    Assert-Equal 4 (Get-SummaryValue $run 'main' 'copied') 'other files copied'
    Assert-Equal 1 (Get-SummaryValue $run 'main' 'skipped') 'skipped count'
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'next run'
    Assert-Equal 1 (Get-SummaryValue $run 'main' 'copied') 'picked up on the next run'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'Inbox.txt now present'
}

Test-Case '17 a locked note keeps its existing .txt (not treated as orphan)' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    $locked = Join-Path $sb.Source 'Inbox.md'
    [void](Write-TestFile $sb.Source 'Inbox.md' 'changed while locked, longer')
    $stream = [System.IO.File]::Open($locked, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try { $run = Invoke-Sync $sb } finally { $stream.Dispose() }
    Assert-Exit $run 0 'run with locked changed note'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'existing copy kept'
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'deleted') 'nothing deleted'
}

Test-Case '18 a second instance exits quietly' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    $mutex = New-Object System.Threading.Mutex($false, (Get-MirrorMutexName $sb.Config))
    $held = $false
    try {
        $held = $mutex.WaitOne(0)
        Assert-True $held 'test could take the mutex'
        $run = Invoke-Sync $sb
    } finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    Assert-Exit $run 0 'second instance exits 0'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Target)) 'second instance did no work'
    Assert-True ($run.Output -match 'Another instance') 'second instance says why it stopped'
    Assert-Exit (Invoke-Sync $sb) 0 'runs normally once the lock is free'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'mirror built afterwards'
}

Test-Case '19 one failing pair does not stop the others; -Pair runs a single pair' {
    $sb = New-Sandbox; New-BasicVault $sb
    $good = New-PairSpec $sb -Name 'good'
    $bad  = New-PairSpec $sb -Name 'bad' -Source (Join-Path $sb.Root 'DoesNotExist') -Target (Join-Path $sb.Root 'MirrorBad')
    Write-Config $sb -Pairs @($bad, $good)
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'overall exit code reflects the failing pair'
    Assert-ListEqual $script:BasicExpected (Get-RelativeFiles $sb.Target) 'good pair mirrored despite failing pair'
    Assert-LogMatch $run 'SUMMARY pair=bad .*status=ABORTED' 'summary for failing pair'
    Assert-LogMatch $run 'SUMMARY pair=good .*status=OK' 'summary for good pair'
    $run = Invoke-Sync $sb @('-Pair', 'good')
    Assert-Exit $run 0 '-Pair good'
    Assert-True ($run.Log -notmatch 'SUMMARY pair=bad') 'bad pair not run with -Pair good'
    $run = Invoke-Sync $sb @('-Pair', 'nope')
    Assert-Exit $run 1 'unknown pair name'
}

Test-Case '20 pairs sharing or nesting a target are rejected' {
    $sb = New-Sandbox; New-BasicVault $sb
    $vault2 = Join-Path $sb.Root 'Vault2'
    [void](Write-TestFile $vault2 'B.md' 'b')
    Write-Config $sb -Pairs @((New-PairSpec $sb -Name 'one'), (New-PairSpec $sb -Name 'two' -Source $vault2))
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'shared target'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Target)) 'shared target not created'
    Write-Config $sb -Pairs @((New-PairSpec $sb -Name 'one'), (New-PairSpec $sb -Name 'two' -Source $vault2 -Target (Join-Path $sb.Target 'Sub')))
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'nested targets'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Target)) 'nested target not created'
}

Test-Case '21 placeholder, relative and broken configs are rejected' {
    $sb = New-Sandbox; New-BasicVault $sb
    Write-Config $sb -Pairs @(New-PairSpec $sb -Target 'C:\Users\<user>\OneDrive - <org>\CopilotMirror\MainVault')
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'placeholder path'
    Assert-LogMatch $run 'placeholder' 'message names the placeholder problem'
    Write-Config $sb -Pairs @(New-PairSpec $sb -Target 'relative-mirror')
    Assert-Exit (Invoke-Sync $sb) 1 'relative path'
    [System.IO.File]::WriteAllText($sb.Config, '{ this is not json', $script:Utf8NoBom)
    Assert-Exit (Invoke-Sync $sb) 1 'broken json'
    [System.IO.File]::Delete($sb.Config)
    Assert-Exit (Invoke-Sync $sb) 1 'missing config'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Target)) 'nothing created'
    # The shipped config must be refused as long as it holds placeholders.
    $shipped = Join-Path $script:Here 'mirror-config.json'
    Assert-True ([System.IO.File]::Exists($shipped)) 'shipped mirror-config.json exists'
    $cfg = ConvertFrom-Json ([System.IO.File]::ReadAllText($shipped))
    Assert-True (@($cfg.pairs).Count -ge 1) 'shipped config has pairs'
    foreach ($p in @($cfg.pairs)) { Assert-True ($p.source -match '<' -and $p.target -match '<') 'shipped config holds placeholders only' }
}

Test-Case '22 log directory inside a vault is refused' {
    $sb = New-Sandbox; New-BasicVault $sb
    Write-Config $sb -Overrides @{ logDirectory = (Join-Path $sb.Source 'logs') }
    $before = Get-TreeSnapshot $sb.Source
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'log directory inside vault'
    Assert-Equal $before (Get-TreeSnapshot $sb.Source) 'vault untouched'
    Assert-True (-not [System.IO.Directory]::Exists($sb.Target)) 'no mirroring happened'
}

Test-Case '23 old log files are removed, other files in the log folder are kept' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb -Overrides @{ logRetentionDays = 14 }
    $old   = Write-TestFile $sb.Logs 'ObsidianMirror-2020-01-01.log' 'old'
    $fresh = Write-TestFile $sb.Logs 'ObsidianMirror-2020-01-02.log' 'recent by timestamp'
    $other = Write-TestFile $sb.Logs 'something-else.log' 'not ours'
    [System.IO.File]::SetLastWriteTimeUtc($old, [DateTime]::UtcNow.AddDays(-30))
    [System.IO.File]::SetLastWriteTimeUtc($fresh, [DateTime]::UtcNow.AddDays(-3))
    [System.IO.File]::SetLastWriteTimeUtc($other, [DateTime]::UtcNow.AddDays(-300))
    Assert-Exit (Invoke-Sync $sb) 0 'run'
    Assert-True (-not [System.IO.File]::Exists($old)) 'log older than retention removed'
    Assert-True ([System.IO.File]::Exists($fresh)) 'recent log kept'
    Assert-True ([System.IO.File]::Exists($other)) 'foreign file in log folder kept'
    $today = Join-Path $sb.Logs ('ObsidianMirror-' + (Get-Date).ToString('yyyy-MM-dd') + '.log')
    Assert-True ([System.IO.File]::Exists($today)) 'one log file per day with the expected name'
    $bytes = [System.IO.File]::ReadAllBytes($today)
    Assert-True (-not ($bytes[0] -eq 0xFF -or $bytes[0] -eq 0xFE)) 'log is not UTF-16'
    Assert-True ([System.IO.File]::ReadAllText($today) -match '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ') 'log lines are timestamped'
}

Test-Case '24 incomplete source listing blocks deletes (folder nesting beyond the depth limit)' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    Assert-Exit (Invoke-Sync $sb) 0 'initial run'
    [System.IO.File]::Delete((Join-Path $sb.Source 'Inbox.md'))      # would normally be deleted from the mirror
    $deep = $sb.Source
    for ($i = 0; $i -lt 70; $i++) { $deep = Join-Path $deep 'd' }
    if ($deep.Length -gt 240) { throw 'SKIP: temp path too long to build a 70-level folder chain here' }
    [void][System.IO.Directory]::CreateDirectory($deep)
    $run = Invoke-Sync $sb
    Assert-Exit $run 1 'incomplete listing must exit 1'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'no delete while the source listing is incomplete'
    Assert-Equal 0 (Get-SummaryValue $run 'main' 'deleted') 'deleted'
}

Test-Case '25 a read-only note can be mirrored, updated and removed' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    $ro = Join-Path $sb.Source 'Inbox.md'
    [System.IO.File]::SetAttributes($ro, [System.IO.FileAttributes]::ReadOnly)
    try {
        Assert-Exit (Invoke-Sync $sb) 0 'initial run with read-only note'
        [System.IO.File]::SetAttributes($ro, [System.IO.FileAttributes]::Normal)
        [void](Write-TestFile $sb.Source 'Inbox.md' 'changed read-only note')
        [System.IO.File]::SetAttributes($ro, [System.IO.FileAttributes]::ReadOnly)
        $run = Invoke-Sync $sb
        Assert-Exit $run 0 'update of read-only note'
        Assert-Equal 'changed read-only note' ([System.IO.File]::ReadAllText((Join-Path $sb.Target 'Inbox.txt'))) 'updated content'
    } finally {
        [System.IO.File]::SetAttributes($ro, [System.IO.FileAttributes]::Normal)
    }
    [System.IO.File]::Delete($ro)
    $run = Invoke-Sync $sb
    Assert-Exit $run 0 'delete after read-only'
    Assert-True (-not [System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'copy removed'
}

Test-Case '26 a very long path does not crash the run' {
    $sb = New-Sandbox; New-BasicVault $sb; Write-Config $sb
    $rel = ''
    for ($i = 0; $i -lt 6; $i++) { $rel += ('LongFolderName-' + ('x' * 35) + '/') }
    try {
        [void](Write-TestFile $sb.Source ($rel + 'Deep note.md') 'deep')
    } catch {
        throw ('SKIP: cannot create a 300-character path here: ' + $_.Exception.Message)
    }
    $run = Invoke-Sync $sb
    Assert-True ($run.ExitCode -eq 0 -or $run.ExitCode -eq 1) 'script ended with a regular exit code'
    Assert-LogMatch $run 'SUMMARY pair=main' 'summary line written'
    Assert-True ($run.Output -notmatch 'At line:|CategoryInfo|FullyQualifiedErrorId') 'no unhandled PowerShell error'
    Assert-True ([System.IO.File]::Exists((Join-Path $sb.Target 'Inbox.txt'))) 'normal notes still mirrored'
    $mirrored = [System.IO.File]::Exists((Join-Path $sb.Target (ConvertTo-NativePath ($rel + 'Deep note.txt'))))
    Assert-True ($mirrored -or $run.Log -match '(WARN|ERROR).*(too long|260)') 'long path either mirrored or reported clearly'
}

Test-Case '27 all scripts are pure ASCII and parse without errors' {
    foreach ($f in (Get-ChildItem -LiteralPath $script:Here -Filter '*.ps1' -File)) {
        $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
        $bad = @($bytes | Where-Object { $_ -gt 127 }).Count
        Assert-Equal 0 $bad ($f.Name + ' contains non-ASCII bytes')
        $tokens = $null; $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errors)
        Assert-Equal 0 (@($errors).Count) ($f.Name + ' has parse errors: ' + (@($errors | ForEach-Object { $_.Message }) -join '; '))
    }
}

if ($Perf) {
    Test-Case ('PERF ' + $PerfNoteCount + ' notes: initial run and no-change run') {
        $sb = New-Sandbox
        Write-Host ('      generating ' + $PerfNoteCount + ' notes ...')
        $body = $script:Utf8NoBom.GetBytes(('# Note' + "`n" + ('Lorem ipsum dolor sit amet. ' * 8)))
        for ($i = 0; $i -lt $PerfNoteCount; $i++) {
            $dir = Join-Path (Join-Path $sb.Source ('Area{0:D2}' -f ($i % 10))) ('Topic{0:D3}' -f [int][Math]::Floor($i / 100))
            if (($i % 100) -eq 0 -or -not [System.IO.Directory]::Exists($dir)) { [void][System.IO.Directory]::CreateDirectory($dir) }
            [System.IO.File]::WriteAllBytes((Join-Path $dir ('Note{0:D5}.md' -f $i)), $body)
        }
        # Something to prune: a large excluded folder.
        for ($i = 0; $i -lt 500; $i++) { [void](Write-TestFile $sb.Source ('.obsidian/cache/c{0:D4}.md' -f $i) 'x') }
        Write-Config $sb
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $run1 = Invoke-Sync $sb
        $t1 = $sw.Elapsed.TotalSeconds
        Assert-Exit $run1 0 'initial perf run'
        Assert-Equal $PerfNoteCount (Get-SummaryValue $run1 'main' 'copied') 'copied on initial run'
        $sw.Restart()
        $run2 = Invoke-Sync $sb
        $t2 = $sw.Elapsed.TotalSeconds
        Assert-Exit $run2 0 'no-change perf run'
        Assert-Equal 0 (Get-SummaryValue $run2 'main' 'copied') 'copied on no-change run'
        Assert-Equal $PerfNoteCount (Get-SummaryValue $run2 'main' 'unchanged') 'unchanged on no-change run'
        Write-Host ('      initial run   : {0,7:N2} s  (script-internal: {1})' -f $t1, (Get-SummaryValue $run1 'main' 'duration'))
        Write-Host ('      no-change run : {0,7:N2} s  (script-internal: {1})' -f $t2, (Get-SummaryValue $run2 'main' 'duration'))
        Write-Host  '      (wall time includes PowerShell start-up; target for the no-change run: under 10 s)'
        Assert-True ($t2 -lt 10) ('no-change run took ' + [Math]::Round($t2, 2) + ' s, target is under 10 s')
    }
}

} finally {
    if (-not $KeepSandbox) {
        foreach ($root in $script:Sandboxes) {
            try {
                if ([System.IO.Directory]::Exists($root)) {
                    try {
                        foreach ($f in (Get-ChildItem -LiteralPath $root -Recurse -Force -File)) {
                            if ($f.IsReadOnly) { $f.IsReadOnly = $false }
                        }
                    } catch { }
                    [System.IO.Directory]::Delete($root, $true)
                }
            } catch {
                Write-Host ('      note: could not remove temp folder ' + $root + ': ' + $_.Exception.Message)
            }
        }
    } else {
        Write-Host ''
        Write-Host 'Sandboxes kept:'
        foreach ($root in $script:Sandboxes) { Write-Host ('  ' + $root) }
    }
}

Write-Host ''
Write-Host ('Result: {0} passed, {1} failed, {2} skipped' -f $script:Passed, $script:Failed, $script:Skipped)
if ($script:Failed -gt 0) { exit 1 }
exit 0
