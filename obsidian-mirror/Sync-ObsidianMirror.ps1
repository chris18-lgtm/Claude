<#
.SYNOPSIS
    One-way mirror of Obsidian notes (.md) to byte-identical .txt copies, so that
    Microsoft 365 Copilot can read them.

.DESCRIPTION
    For every source/target pair in mirror-config.json:
      <source>\A\B\Note.md  ->  <target>\A\B\Note.txt
    The vault is only ever read. The mirror is disposable: delete it and run the
    script once to rebuild it.

    Safety rules built into this script:
      * Nothing is written, renamed or deleted inside a vault.
      * Inside a mirror only .txt files and empty folders are ever deleted.
      * A mirror folder is only cleaned when it carries the marker file
        .obsidian-mirror-target.
      * A missing or empty vault, nested paths, or an unusually large number of
        deletions stop the pair instead of deleting anything.

    Works with Windows PowerShell 5.1. No modules, no network access.

.PARAMETER ConfigPath
    Path of the configuration file. Default: mirror-config.json next to this script.

.PARAMETER Pair
    Run only the pair with this name.

.PARAMETER WhatIf
    Dry run. Logs every copy and delete that would happen and changes nothing.

.PARAMETER InitializeTarget
    Confirms that an existing, non-empty mirror folder without marker file may be
    used as a mirror. Existing .txt files in it that have no matching note will be
    deleted (the delete threshold still applies).

.PARAMETER ForceDelete
    Overrides the delete threshold for this one run.

.EXAMPLE
    powershell.exe -NoProfile -File .\Sync-ObsidianMirror.ps1 -WhatIf

.NOTES
    Exit code 0: success. Exit code 1: at least one pair had errors or a safety
    guard stopped it.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$Pair,
    [switch]$WhatIf,
    [switch]$InitializeTarget,
    [switch]$ForceDelete
)

# --- Language mode check -------------------------------------------------------
# This script needs .NET file APIs. Under AppLocker / App Control policies
# PowerShell runs untrusted scripts in ConstrainedLanguage mode, where those calls
# are blocked. Only cmdlets are used up to this point so the message can be shown.
if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    $clmMessage = 'Sync-ObsidianMirror cannot run: PowerShell is in ' + $ExecutionContext.SessionState.LanguageMode +
        ' mode on this machine (application control policy). The script needs FullLanguage mode. ' +
        'Nothing was changed. Ask IT whether this script can be trusted by policy (for example by signing it), ' +
        'or request a cmdlet-only variant of the tool.'
    Write-Host $clmMessage
    try {
        if ($env:LOCALAPPDATA) {
            $clmDir = Join-Path $env:LOCALAPPDATA 'ObsidianMirror\logs'
            if (-not (Test-Path -LiteralPath $clmDir)) { New-Item -ItemType Directory -Path $clmDir -Force | Out-Null }
            $clmFile = Join-Path $clmDir ('ObsidianMirror-' + (Get-Date -Format 'yyyy-MM-dd') + '.log')
            Add-Content -LiteralPath $clmFile -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ERROR ' + $clmMessage) -Encoding UTF8
        }
    } catch { }
    exit 1
}

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:Version     = '1.0.0'
$script:MarkerName  = '.obsidian-mirror-target'
$script:MaxDepth    = 64          # folder nesting limit; protects against junction loops
$script:MaxStrayLog = 20          # per pair and run, list at most this many stray files
$script:SlowSeconds = 60
$script:Sep         = [System.IO.Path]::DirectorySeparatorChar
$script:Cmp         = [System.StringComparison]::OrdinalIgnoreCase
$script:Invariant   = [System.Globalization.CultureInfo]::InvariantCulture
$script:Utf8NoBom   = New-Object System.Text.UTF8Encoding($false)
$script:DryRun      = [bool]$WhatIf
$script:LogWriter   = $null
$script:SourceRoots = @()         # every configured vault; no write may ever land inside one

# ------------------------------------------------------------------- logging ---

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = [DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss', $script:Invariant) + ' ' + $Level.PadRight(5) + ' ' + $Message
    if ($null -ne $script:LogWriter) {
        try { $script:LogWriter.WriteLine($line) } catch { }
    }
    Write-Host $line
}

function Open-Log {
    param([string]$Directory)
    [void][System.IO.Directory]::CreateDirectory($Directory)
    $file = Join-Path $Directory ('ObsidianMirror-' + [DateTime]::Now.ToString('yyyy-MM-dd', $script:Invariant) + '.log')
    $stream = New-Object System.IO.FileStream($file, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    $writer = New-Object System.IO.StreamWriter($stream, $script:Utf8NoBom)
    $writer.AutoFlush = $true
    $script:LogWriter = $writer
}

function Close-Log {
    if ($null -ne $script:LogWriter) {
        try { $script:LogWriter.Dispose() } catch { }
        $script:LogWriter = $null
    }
}

function Remove-OldLogs {
    # Deletes only this tool's own daily log files, and only inside the log folder.
    param([string]$Directory, [int]$RetentionDays)
    if ($script:DryRun -or $RetentionDays -le 0) { return }
    $cutoff = [DateTime]::UtcNow.AddDays(-$RetentionDays)
    try {
        $info = New-Object System.IO.DirectoryInfo($Directory)
        foreach ($f in $info.EnumerateFiles('ObsidianMirror-*.log')) {
            if ($f.Name -match '^ObsidianMirror-\d{4}-\d{2}-\d{2}\.log$' -and $f.LastWriteTimeUtc -lt $cutoff) {
                try {
                    $f.Delete()
                    Write-Log 'INFO' ('Removed old log file ' + $f.Name)
                } catch {
                    Write-Log 'WARN' ('Could not remove old log file ' + $f.Name + ': ' + (Get-ErrorText $_))
                }
            }
        }
    } catch {
        Write-Log 'WARN' ('Log retention failed: ' + (Get-ErrorText $_))
    }
}

function Get-ErrorText {
    param($ErrorRecord)
    $ex = $ErrorRecord.Exception
    if ($null -eq $ex) { return [string]$ErrorRecord }
    return $ex.GetBaseException().Message
}

function Format-Seconds {
    param([double]$Seconds)
    return $Seconds.ToString('0.00', $script:Invariant) + 's'
}

# --------------------------------------------------------------------- paths ---

function Get-NormalizedPath {
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    }
    return $full
}

function Get-PathPrefix {
    # Path with exactly one trailing separator, for "is inside" comparisons.
    param([string]$NormalizedPath)
    if ($NormalizedPath.EndsWith([string]$script:Sep)) { return $NormalizedPath }
    return $NormalizedPath + $script:Sep
}

function Test-PathInside {
    # True when Child is the same folder as Parent or lies below it. Both normalized.
    param([string]$Child, [string]$Parent)
    if ([string]::Equals($Child, $Parent, $script:Cmp)) { return $true }
    return $Child.StartsWith((Get-PathPrefix $Parent), $script:Cmp)
}

function Resolve-ConfiguredPath {
    param([string]$Raw, [string]$What)
    if ([string]::IsNullOrWhiteSpace($Raw)) { throw ($What + ' is empty.') }
    if ($Raw.IndexOf('<') -ge 0 -or $Raw.IndexOf('>') -ge 0) {
        throw ($What + ' still contains a placeholder (<...>): ' + $Raw + ' -- edit mirror-config.json and enter the real path.')
    }
    $expanded = [System.Environment]::ExpandEnvironmentVariables($Raw)
    if ($expanded -match '%[^%\\/:]+%') {
        throw ($What + ' uses an environment variable that is not defined: ' + $expanded)
    }
    if (-not [System.IO.Path]::IsPathRooted($expanded)) {
        throw ($What + ' must be a full path, got: ' + $expanded)
    }
    return (Get-NormalizedPath $expanded)
}

function Get-MutexName {
    # One lock per configuration file. Test-ObsidianMirror.ps1 uses the same formula.
    param([string]$ConfigFile)
    $full = [System.IO.Path]::GetFullPath($ConfigFile).ToLowerInvariant()
    $safe = [regex]::Replace($full, '[^a-z0-9]', '_')
    if ($safe.Length -gt 150) { $safe = $safe.Substring($safe.Length - 150) }
    return 'ObsidianMirror-' + $safe
}

# -------------------------------------------------------------------- config ---

function Get-ConfigValue {
    param($Object, [string]$Name, $Default)
    if ($null -ne $Object) {
        $prop = $Object.PSObject.Properties[$Name]
        if ($null -ne $prop -and $null -ne $prop.Value) { return $prop.Value }
    }
    return $Default
}

function Get-ConfigInt {
    param($Object, [string]$Name, [int]$Default, [int]$Minimum)
    $raw = Get-ConfigValue $Object $Name $Default
    $value = 0
    if (-not [int]::TryParse([string]$raw, [ref]$value)) { throw ("Config value '" + $Name + "' must be a whole number, got: " + $raw) }
    if ($value -lt $Minimum) { throw ("Config value '" + $Name + "' must be at least " + $Minimum + ', got: ' + $value) }
    return $value
}

function Read-MirrorConfig {
    param([string]$Path)
    if (-not [System.IO.File]::Exists($Path)) { throw ('Config file not found: ' + $Path) }
    $text = [System.IO.File]::ReadAllText($Path)
    $cfg = $null
    try {
        $cfg = ConvertFrom-Json -InputObject $text
    } catch {
        throw ('Config file is not valid JSON: ' + $Path + ' (' + (Get-ErrorText $_) + ')')
    }
    if ($null -eq $cfg) { throw ('Config file is empty: ' + $Path) }
    return $cfg
}

function New-ExcludeSets {
    # An entry without a slash is a folder name that matches at any depth.
    # An entry with a slash is a path relative to the vault root.
    param([object[]]$Entries)
    $names = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $paths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $Entries) {
        if ($null -eq $entry) { continue }
        $norm = ([string]$entry).Replace('\', '/').Trim().Trim('/')
        if ($norm.Length -eq 0) { continue }
        if ($norm.Contains('/')) { [void]$paths.Add($norm) } else { [void]$names.Add($norm) }
    }
    return [pscustomobject]@{ Names = $names; Paths = $paths }
}

function Get-PairDefinitions {
    param($Config)
    $list = New-Object System.Collections.ArrayList
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $index = 0
    foreach ($raw in @(Get-ConfigValue $Config 'pairs' @())) {
        $index++
        $name = [string](Get-ConfigValue $raw 'name' ('pair' + $index))
        $def = [pscustomobject]@{
            Name = $name; Source = $null; Target = $null; Excludes = $null
            ConfigError = $null; Blocked = (New-Object System.Collections.Generic.List[string])
        }
        try {
            if ($name -notmatch '^[\w\-]+$') { throw ("Pair name '" + $name + "' may only contain letters, digits, '-' and '_'.") }
            if (-not $seen.Add($name)) { throw ("Pair name '" + $name + "' is used more than once.") }
            $def.Source   = Resolve-ConfiguredPath ([string](Get-ConfigValue $raw 'source' '')) ("Source of pair '" + $name + "'")
            $def.Target   = Resolve-ConfiguredPath ([string](Get-ConfigValue $raw 'target' '')) ("Target of pair '" + $name + "'")
            $def.Excludes = New-ExcludeSets @(Get-ConfigValue $raw 'excludeFolders' @())
        } catch {
            $def.ConfigError = Get-ErrorText $_
        }
        [void]$list.Add($def)
    }
    return ,$list
}

function Test-PairLayout {
    # Nesting check across ALL pairs. Adds the reason to every pair involved.
    param($Pairs)
    $valid = @($Pairs | Where-Object { $null -eq $_.ConfigError })
    for ($i = 0; $i -lt $valid.Count; $i++) {
        $a = $valid[$i]
        for ($j = 0; $j -lt $valid.Count; $j++) {
            $b = $valid[$j]
            $reason = $null
            if ([string]::Equals($a.Target, $b.Source, $script:Cmp)) {
                $reason = "target of '" + $a.Name + "' is the same folder as source of '" + $b.Name + "'"
            } elseif (Test-PathInside $a.Target $b.Source) {
                $reason = "target of '" + $a.Name + "' lies inside source of '" + $b.Name + "'"
            } elseif (Test-PathInside $b.Source $a.Target) {
                $reason = "source of '" + $b.Name + "' lies inside target of '" + $a.Name + "'"
            }
            if ($null -ne $reason) {
                $a.Blocked.Add($reason)
                if ($i -ne $j) { $b.Blocked.Add($reason) }
            }
            if ($i -lt $j) {
                if ((Test-PathInside $a.Target $b.Target) -or (Test-PathInside $b.Target $a.Target)) {
                    $reason = "targets of '" + $a.Name + "' and '" + $b.Name + "' are the same folder or nested; each pair needs its own mirror folder"
                    $a.Blocked.Add($reason)
                    $b.Blocked.Add($reason)
                }
            }
        }
    }
}

# ---------------------------------------------------------------- enumeration ---

function Get-ListingProblem {
    param([string]$Folder, $ErrorRecord)
    $text = "Cannot list folder '" + $Folder + "': " + (Get-ErrorText $ErrorRecord)
    if ($Folder.Length -ge 240) { $text += ' (the path is ' + $Folder.Length + ' characters long; the Windows limit of 260 characters is probably exceeded -- path too long)' }
    return $text
}

function Get-SourceListing {
    # Walks the vault exactly once and never descends into excluded folders.
    # Notes: expected relative mirror path (case-insensitive) -> FileInfo of the note.
    # The key set of this dictionary is the "expected" set used for orphan detection.
    param([string]$Root, $Excludes)
    $notes      = [System.Collections.Generic.Dictionary[string, System.IO.FileInfo]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $problems   = New-Object System.Collections.Generic.List[string]
    $collisions = New-Object System.Collections.Generic.List[string]
    $checkPaths = ($Excludes.Paths.Count -gt 0)
    $stack = New-Object System.Collections.Stack
    $stack.Push(@((New-Object System.IO.DirectoryInfo($Root)), '', 0))
    while ($stack.Count -gt 0) {
        $item = $stack.Pop()
        $dir = $item[0]; $rel = $item[1]; $depth = $item[2]
        try {
            foreach ($file in $dir.EnumerateFiles()) {
                $name = $file.Name
                if ($name.Length -ge 3 -and $name.EndsWith('.md', $script:Cmp)) {
                    $key = $rel + $name.Substring(0, $name.Length - 3) + '.txt'
                    if ($notes.ContainsKey($key)) { $collisions.Add($rel + $name) } else { $notes.Add($key, $file) }
                }
            }
            foreach ($sub in $dir.EnumerateDirectories()) {
                $subName = $sub.Name
                if ($subName[0] -eq '.') { continue }                       # dot folders are always excluded
                if ($Excludes.Names.Contains($subName)) { continue }
                $subRel = $rel + $subName
                if ($checkPaths -and $Excludes.Paths.Contains($subRel.Replace('\', '/'))) { continue }
                if (($depth + 1) -gt $script:MaxDepth) {
                    $problems.Add("Folder nesting is deeper than " + $script:MaxDepth + " levels at '" + $sub.FullName + "'; not descending further.")
                    continue
                }
                $stack.Push(@($sub, ($subRel + $script:Sep), ($depth + 1)))
            }
        } catch {
            $problems.Add((Get-ListingProblem $dir.FullName $_))
        }
    }
    return [pscustomobject]@{ Notes = $notes; Problems = $problems; Collisions = $collisions }
}

function Get-TargetListing {
    # Walks the mirror exactly once. Only directory metadata is read, never file
    # content, so OneDrive Files On-Demand placeholders are not downloaded.
    param([string]$Root)
    $txt      = [System.Collections.Generic.Dictionary[string, System.IO.FileInfo]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $strays   = New-Object System.Collections.Generic.List[string]
    $dirs     = New-Object System.Collections.Generic.List[string]
    $problems = New-Object System.Collections.Generic.List[string]
    $result = [pscustomobject]@{ Txt = $txt; Strays = $strays; Dirs = $dirs; Problems = $problems }
    if (-not [System.IO.Directory]::Exists($Root)) { return $result }
    $stack = New-Object System.Collections.Stack
    $stack.Push(@((New-Object System.IO.DirectoryInfo($Root)), '', 0))
    while ($stack.Count -gt 0) {
        $item = $stack.Pop()
        $dir = $item[0]; $rel = $item[1]; $depth = $item[2]
        try {
            foreach ($file in $dir.EnumerateFiles()) {
                $name = $file.Name
                if ($name.EndsWith('.txt', $script:Cmp)) {
                    $txt[$rel + $name] = $file
                } elseif ($depth -eq 0 -and [string]::Equals($name, $script:MarkerName, $script:Cmp)) {
                    # our own marker file
                } else {
                    $strays.Add($rel + $name)
                }
            }
            foreach ($sub in $dir.EnumerateDirectories()) {
                $subRel = $rel + $sub.Name
                $dirs.Add($subRel)
                if (($depth + 1) -gt $script:MaxDepth) {
                    $problems.Add("Folder nesting is deeper than " + $script:MaxDepth + " levels at '" + $sub.FullName + "'; not descending further.")
                    continue
                }
                $stack.Push(@($sub, ($subRel + $script:Sep), ($depth + 1)))
            }
        } catch {
            $problems.Add((Get-ListingProblem $dir.FullName $_))
        }
    }
    return $result
}

# ------------------------------------------------------------------ mutations ---
# These five functions are the ONLY places where this script creates, overwrites
# or deletes anything (apart from its own log files). Each one refuses to act
# outside the mirror root or inside any vault, and does nothing in a dry run.

function Assert-MirrorPath {
    param([string]$Path, [string]$MirrorRoot, [switch]$AllowRoot)
    $full = Get-NormalizedPath $Path
    $isRoot = [string]::Equals($full, $MirrorRoot, $script:Cmp)
    if ($isRoot -and -not $AllowRoot) { throw ("SAFETY: refusing to modify the mirror root itself: '" + $full + "'") }
    if (-not $isRoot -and -not $full.StartsWith((Get-PathPrefix $MirrorRoot), $script:Cmp)) {
        throw ("SAFETY: refusing to modify '" + $full + "' because it is outside the mirror root '" + $MirrorRoot + "'")
    }
    foreach ($vault in $script:SourceRoots) {
        if (Test-PathInside $full $vault) { throw ("SAFETY: refusing to modify '" + $full + "' because it is inside the vault '" + $vault + "'") }
    }
}

function Clear-ReadOnly {
    param([string]$Path)
    $attr = [System.IO.File]::GetAttributes($Path)
    $readOnly = [int][System.IO.FileAttributes]::ReadOnly
    if (([int]$attr -band $readOnly) -ne 0) {
        [System.IO.File]::SetAttributes($Path, [System.IO.FileAttributes]([int]$attr -band (-bnot $readOnly)))
    }
}

function New-MirrorDirectory {
    param([string]$Path, [string]$MirrorRoot)
    Assert-MirrorPath -Path $Path -MirrorRoot $MirrorRoot -AllowRoot
    if ($script:DryRun) { return }
    [void][System.IO.Directory]::CreateDirectory($Path)
}

function Set-MirrorMarker {
    param([string]$MirrorRoot)
    $marker = $MirrorRoot + $script:Sep + $script:MarkerName
    Assert-MirrorPath -Path $marker -MirrorRoot $MirrorRoot
    if ($script:DryRun) { return }
    $text = 'This folder is a one-way mirror created by Sync-ObsidianMirror.ps1.' + [Environment]::NewLine +
            'The .txt files in it are copies of Obsidian notes and are overwritten and deleted automatically.' + [Environment]::NewLine +
            'Do not store anything else here. Deleting this marker file stops the script from cleaning this folder.' + [Environment]::NewLine
    [System.IO.File]::WriteAllText($marker, $text, $script:Utf8NoBom)
}

function Copy-MirrorFile {
    param([string]$SourceFile, [string]$DestFile, [string]$MirrorRoot, [datetime]$SourceTimeUtc, [bool]$DestExists)
    Assert-MirrorPath -Path $DestFile -MirrorRoot $MirrorRoot
    if (-not $DestFile.EndsWith('.txt', $script:Cmp)) { throw ("SAFETY: refusing to write a non-.txt file: '" + $DestFile + "'") }
    if ($script:DryRun) { return }
    $destDir = [System.IO.Path]::GetDirectoryName($DestFile)
    if (-not [System.IO.Directory]::Exists($destDir)) { New-MirrorDirectory -Path $destDir -MirrorRoot $MirrorRoot }
    if ($DestExists) { Clear-ReadOnly $DestFile }
    # Byte-for-byte copy; no text decoding, so umlauts and any encoding survive.
    [System.IO.File]::Copy($SourceFile, $DestFile, $true)
    Clear-ReadOnly $DestFile                      # File.Copy carries a read-only flag over
    [System.IO.File]::SetLastWriteTimeUtc($DestFile, $SourceTimeUtc)
}

function Remove-MirrorTxt {
    param([string]$Path, [string]$MirrorRoot)
    Assert-MirrorPath -Path $Path -MirrorRoot $MirrorRoot
    if (-not $Path.EndsWith('.txt', $script:Cmp)) { throw ("SAFETY: refusing to delete a non-.txt file: '" + $Path + "'") }
    if ($script:DryRun) { return }
    Clear-ReadOnly $Path
    [System.IO.File]::Delete($Path)
}

function Remove-MirrorEmptyDirectory {
    param([string]$Path, [string]$MirrorRoot)
    Assert-MirrorPath -Path $Path -MirrorRoot $MirrorRoot
    if ($script:DryRun) { return }
    [System.IO.Directory]::Delete($Path, $false)   # non-recursive: fails if anything is left inside
}

# ------------------------------------------------------------------- one pair ---

function Write-PairLog {
    param($Stats, [string]$Level, [string]$Message)
    if ($Level -eq 'WARN')  { $Stats.Warnings++ }
    if ($Level -eq 'ERROR') { $Stats.Errors++ }
    Write-Log $Level ('[' + $Stats.Name + '] ' + $Message)
}

function Stop-Pair {
    param($Stats, [string]$Message)
    $Stats.Status = 'ABORTED'
    Write-PairLog $Stats 'ERROR' ($Message + ' Pair aborted, nothing was deleted.')
}

function Add-Ancestors {
    # Adds every parent folder of a relative file path to the set.
    param($Set, [string]$RelativePath)
    $p = $RelativePath
    while ($true) {
        $i = $p.LastIndexOf($script:Sep)
        if ($i -le 0) { break }
        $p = $p.Substring(0, $i)
        if (-not $Set.Add($p)) { break }
    }
}

function Sync-Pair {
    param($Def, $Stats, [int]$MaxDeleteCount, [int]$MaxDeletePercent)

    $sourceRoot = $Def.Source
    $targetRoot = $Def.Target
    $would = ''
    if ($script:DryRun) { $would = 'WOULD ' }

    # --- Guard: source must exist --------------------------------------------
    if (-not [System.IO.Directory]::Exists($sourceRoot)) {
        Stop-Pair $Stats ("Source folder not found: '" + $sourceRoot + "' (OneDrive not signed in, or the vault was moved?).")
        return
    }

    # --- Source listing --------------------------------------------------------
    $src = Get-SourceListing -Root $sourceRoot -Excludes $Def.Excludes
    $Stats.Scanned = $src.Notes.Count
    $deletesAllowed = $true
    foreach ($problem in $src.Problems) {
        Write-PairLog $Stats 'ERROR' ('Source listing incomplete: ' + $problem)
        $deletesAllowed = $false
    }
    foreach ($collision in $src.Collisions) {
        Write-PairLog $Stats 'WARN' ("Two notes map to the same .txt name; skipped '" + $collision + "'.")
    }
    if ($src.Notes.Count -eq 0) {
        Stop-Pair $Stats ("Source '" + $sourceRoot + "' contains no .md files after excludes.")
        return
    }

    # --- Guard: marker file ----------------------------------------------------
    if ([System.IO.File]::Exists($targetRoot)) {
        Stop-Pair $Stats ("Target '" + $targetRoot + "' is a file, not a folder.")
        return
    }
    $markerPath = $targetRoot + $script:Sep + $script:MarkerName
    if (-not [System.IO.Directory]::Exists($targetRoot)) {
        New-MirrorDirectory -Path $targetRoot -MirrorRoot $targetRoot
        Set-MirrorMarker -MirrorRoot $targetRoot
        Write-PairLog $Stats 'INFO' ($would + "CREATE mirror folder with marker file: '" + $targetRoot + "'")
    } elseif (-not [System.IO.File]::Exists($markerPath)) {
        $isEmpty = $true
        foreach ($entry in [System.IO.Directory]::EnumerateFileSystemEntries($targetRoot)) { $isEmpty = $false; break }
        if ($isEmpty) {
            Set-MirrorMarker -MirrorRoot $targetRoot
            Write-PairLog $Stats 'INFO' ($would + "CREATE marker file in empty mirror folder '" + $targetRoot + "'")
        } elseif ($InitializeTarget) {
            Set-MirrorMarker -MirrorRoot $targetRoot
            Write-PairLog $Stats 'INFO' ($would + "CREATE marker file in existing folder '" + $targetRoot + "' (confirmed with -InitializeTarget). " +
                'Existing .txt files without a matching note are treated as orphans.')
        } else {
            Stop-Pair $Stats ("Target '" + $targetRoot + "' exists, is not empty and has no marker file '" + $script:MarkerName + "'. " +
                'If this really is the folder to use as mirror, run once with -InitializeTarget (existing .txt files without a matching note will then be deleted). ' +
                'Otherwise choose an empty or new folder.')
            return
        }
    }

    # --- Target listing --------------------------------------------------------
    $tgt = Get-TargetListing -Root $targetRoot
    foreach ($problem in $tgt.Problems) {
        Write-PairLog $Stats 'ERROR' ('Mirror listing incomplete: ' + $problem)
        $deletesAllowed = $false
    }

    # --- Copy new and changed notes --------------------------------------------
    foreach ($entry in $src.Notes.GetEnumerator()) {
        $key  = $entry.Key
        $note = $entry.Value
        $existing = $null
        $exists = $tgt.Txt.TryGetValue($key, [ref]$existing)
        if ($exists) {
            # Metadata only: same size and same timestamp (2 s tolerance) means unchanged.
            $delta = ($existing.LastWriteTimeUtc - $note.LastWriteTimeUtc).TotalSeconds
            if ($existing.Length -eq $note.Length -and $delta -le 2 -and $delta -ge -2) {
                $Stats.Unchanged++
                continue
            }
        }
        $reason = 'new'
        if ($exists) { $reason = 'changed' }
        $dest = $targetRoot + $script:Sep + $key
        try {
            Copy-MirrorFile -SourceFile $note.FullName -DestFile $dest -MirrorRoot $targetRoot -SourceTimeUtc $note.LastWriteTimeUtc -DestExists $exists
            $Stats.Copied++
            if ($script:DryRun) { Write-PairLog $Stats 'INFO' ('WOULD COPY  ' + $key + ' (' + $reason + ')') }
            else                { Write-PairLog $Stats 'INFO' ('COPIED  ' + $key + ' (' + $reason + ')') }
        } catch {
            $Stats.Skipped++
            $base = $_.Exception.GetBaseException()
            $text = "Skipped '" + $key + "': " + $base.Message
            if ($dest.Length -ge 260 -or $note.FullName.Length -ge 260) {
                $text += ' (path too long: source ' + $note.FullName.Length + ', mirror ' + $dest.Length + ' characters; the Windows limit is 260)'
            }
            if ($base.Message -like 'SAFETY:*') {
                Write-PairLog $Stats 'ERROR' $text
            } elseif ($base -is [System.IO.IOException]) {
                # Locked by Obsidian or OneDrive, not downloaded yet, path too long, vanished meanwhile.
                Write-PairLog $Stats 'WARN' ($text + ' It will be picked up on a later run.')
            } else {
                Write-PairLog $Stats 'ERROR' $text
            }
        }
    }

    # --- Stray files: never touched, only reported ------------------------------
    $shown = 0
    foreach ($stray in $tgt.Strays) {
        if ($shown -lt $script:MaxStrayLog) {
            Write-PairLog $Stats 'WARN' ("Foreign file in mirror (not a .txt, left alone): '" + $stray + "'")
        } else {
            $Stats.Warnings++
        }
        $shown++
    }
    if ($shown -gt $script:MaxStrayLog) {
        Write-Log 'WARN' ('[' + $Stats.Name + '] ... and ' + ($shown - $script:MaxStrayLog) + ' more foreign files in the mirror.')
    }

    # --- Orphans: .txt files without a note --------------------------------------
    $orphans = New-Object System.Collections.Generic.List[string]
    foreach ($key in $tgt.Txt.Keys) {
        if (-not $src.Notes.ContainsKey($key)) { $orphans.Add($key) }
    }

    if ($orphans.Count -gt 0 -and -not $deletesAllowed) {
        Write-PairLog $Stats 'ERROR' ('Deletion skipped because a folder listing was incomplete; ' + $orphans.Count + ' file(s) look orphaned but may not be.')
    }
    if ($orphans.Count -gt 0 -and $deletesAllowed) {
        $overCount   = ($orphans.Count -gt $MaxDeleteCount)
        $overPercent = (($orphans.Count * 100) -gt ($MaxDeletePercent * $tgt.Txt.Count))
        if ($overCount -and $overPercent) {
            if ($ForceDelete) {
                Write-PairLog $Stats 'INFO' ('Delete threshold exceeded (' + $orphans.Count + ' of ' + $tgt.Txt.Count + ' mirror files) but -ForceDelete was given.')
            } else {
                $deletesAllowed = $false
                Write-PairLog $Stats 'ERROR' ('Deletion skipped: this run would delete ' + $orphans.Count + ' of ' + $tgt.Txt.Count +
                    ' mirror files, more than maxDeleteCount=' + $MaxDeleteCount + ' and maxDeletePercent=' + $MaxDeletePercent +
                    '. If that is intended (large reorganisation, new exclude), run once with -ForceDelete. Use -WhatIf first to see the list.')
            }
        }
    }

    $deleteFailed = New-Object System.Collections.Generic.List[string]
    if ($deletesAllowed) {
        foreach ($key in $orphans) {
            $path = $targetRoot + $script:Sep + $key
            try {
                Remove-MirrorTxt -Path $path -MirrorRoot $targetRoot
                $Stats.Deleted++
                if ($script:DryRun) { Write-PairLog $Stats 'INFO' ('WOULD DELETE  ' + $key) }
                else                { Write-PairLog $Stats 'INFO' ('DELETED  ' + $key) }
            } catch {
                $deleteFailed.Add($key)
                $base = $_.Exception.GetBaseException()
                if ($base -is [System.IO.IOException] -and $base.Message -notlike 'SAFETY:*') {
                    Write-PairLog $Stats 'WARN' ("Could not delete '" + $key + "': " + $base.Message + ' It will be retried on a later run.')
                } else {
                    Write-PairLog $Stats 'ERROR' ("Could not delete '" + $key + "': " + $base.Message)
                }
            }
        }

        # --- Empty folders, deepest first ---------------------------------------
        # A folder stays when it (or anything below it) holds an expected copy, a
        # foreign file, or an orphan that could not be deleted.
        $keep = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        foreach ($key in $src.Notes.Keys) { Add-Ancestors $keep $key }
        foreach ($stray in $tgt.Strays)  { Add-Ancestors $keep $stray }
        foreach ($key in $deleteFailed)  { Add-Ancestors $keep $key }
        $candidates = @($tgt.Dirs | Where-Object { -not $keep.Contains($_) } |
            Sort-Object -Property @{ Expression = { $_.Split($script:Sep).Length }; Descending = $true }, @{ Expression = { $_ }; Descending = $true })
        foreach ($relDir in $candidates) {
            try {
                Remove-MirrorEmptyDirectory -Path ($targetRoot + $script:Sep + $relDir) -MirrorRoot $targetRoot
                $Stats.DirsRemoved++
                if ($script:DryRun) { Write-PairLog $Stats 'INFO' ('WOULD REMOVE DIR  ' + $relDir) }
                else                { Write-PairLog $Stats 'INFO' ('REMOVED DIR  ' + $relDir) }
            } catch {
                Write-PairLog $Stats 'WARN' ("Could not remove empty folder '" + $relDir + "': " + (Get-ErrorText $_))
            }
        }
    }
}

function Invoke-Pair {
    param($Def, [int]$MaxDeleteCount, [int]$MaxDeletePercent)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $stats = @{
        Name = $Def.Name; Status = 'OK'
        Scanned = 0; Copied = 0; Unchanged = 0; Deleted = 0; DirsRemoved = 0; Skipped = 0; Warnings = 0; Errors = 0
    }
    try {
        if ($null -ne $Def.ConfigError) {
            Stop-Pair $stats ('Configuration problem: ' + $Def.ConfigError)
        } elseif ($Def.Blocked.Count -gt 0) {
            Stop-Pair $stats ('Unsafe folder layout: ' + (@($Def.Blocked | Select-Object -Unique) -join '; ') + '.')
        } else {
            Sync-Pair -Def $Def -Stats $stats -MaxDeleteCount $MaxDeleteCount -MaxDeletePercent $MaxDeletePercent
        }
    } catch {
        $stats.Status = 'FAILED'
        Write-PairLog $stats 'ERROR' ('Unexpected failure: ' + (Get-ErrorText $_))
    }
    $watch.Stop()
    $seconds = $watch.Elapsed.TotalSeconds
    if ($seconds -gt $script:SlowSeconds) {
        Write-PairLog $stats 'WARN' ('This pair took ' + (Format-Seconds $seconds) + ', longer than ' + $script:SlowSeconds + ' s. The scheduled task stops a run after 15 minutes.')
    }
    $status = $stats.Status
    if ($status -eq 'OK' -and $script:DryRun) { $status = 'DRYRUN' }
    Write-Log 'INFO' ('SUMMARY pair=' + $stats.Name + ' status=' + $status +
        ' scanned=' + $stats.Scanned + ' copied=' + $stats.Copied + ' unchanged=' + $stats.Unchanged +
        ' deleted=' + $stats.Deleted + ' dirs_removed=' + $stats.DirsRemoved + ' skipped=' + $stats.Skipped +
        ' warnings=' + $stats.Warnings + ' errors=' + $stats.Errors + ' duration=' + (Format-Seconds $seconds))
    return $stats.Errors
}

# ----------------------------------------------------------------------- main ---

$exitCode = 0
$mutex = $null
$mutexHeld = $false
$runWatch = [System.Diagnostics.Stopwatch]::StartNew()

try {
    if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'mirror-config.json' }
    # Resolve relative to the PowerShell location (the process directory can differ).
    $ConfigPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigPath)

    # --- Single instance -------------------------------------------------------
    $mutex = New-Object System.Threading.Mutex($false, (Get-MutexName $ConfigPath))
    try {
        $mutexHeld = $mutex.WaitOne(0)
    } catch {
        if ($_.Exception.GetBaseException() -is [System.Threading.AbandonedMutexException]) {
            $mutexHeld = $true      # the previous run died; the lock is ours now
        } else {
            throw
        }
    }

    if (-not $mutexHeld) {
        # Quiet exit: no log entry, exit code 0.
        Write-Host 'Another instance of Sync-ObsidianMirror is already running for this configuration. Exiting.'
    } else {
        # --- Configuration -----------------------------------------------------
        $config = Read-MirrorConfig $ConfigPath
        $logDirectory = Resolve-ConfiguredPath ([string](Get-ConfigValue $config 'logDirectory' '%LOCALAPPDATA%\ObsidianMirror\logs')) 'logDirectory'
        $maxDeleteCount   = Get-ConfigInt $config 'maxDeleteCount' 25 0
        $maxDeletePercent = Get-ConfigInt $config 'maxDeletePercent' 20 0
        $retentionDays    = Get-ConfigInt $config 'logRetentionDays' 14 0

        $pairs = Get-PairDefinitions $config
        if ($pairs.Count -eq 0) { throw "The configuration contains no entries under 'pairs'." }
        Test-PairLayout $pairs
        $script:SourceRoots = @($pairs | Where-Object { $null -eq $_.ConfigError } | ForEach-Object { $_.Source })

        # The log folder must never be inside a vault (that would be a vault write) or a mirror.
        foreach ($def in $pairs) {
            if ($null -ne $def.ConfigError) { continue }
            if (Test-PathInside $logDirectory $def.Source) { throw ("logDirectory '" + $logDirectory + "' lies inside the vault of pair '" + $def.Name + "'. Choose a folder outside all vaults and outside OneDrive.") }
            if (Test-PathInside $logDirectory $def.Target) { throw ("logDirectory '" + $logDirectory + "' lies inside the mirror of pair '" + $def.Name + "'. Choose a folder outside all mirrors and outside OneDrive.") }
        }

        Open-Log $logDirectory
        $mode = 'LIVE'
        if ($script:DryRun) { $mode = 'DRYRUN (-WhatIf: nothing will be changed)' }
        $flags = ''
        if ($InitializeTarget) { $flags += ' -InitializeTarget' }
        if ($ForceDelete)      { $flags += ' -ForceDelete' }
        if ($Pair)             { $flags += ' -Pair ' + $Pair }
        Write-Log 'INFO' ('Run started: Sync-ObsidianMirror ' + $script:Version + ', PowerShell ' + $PSVersionTable.PSVersion.ToString() +
            ', mode=' + $mode + ', config=' + $ConfigPath + $flags)

        # --- Pairs -------------------------------------------------------------
        $selected = @($pairs)
        if (-not [string]::IsNullOrWhiteSpace($Pair)) {
            $selected = @($pairs | Where-Object { [string]::Equals($_.Name, $Pair, $script:Cmp) })
            if ($selected.Count -eq 0) {
                throw ("No pair named '" + $Pair + "' in the configuration. Available: " + (@($pairs | ForEach-Object { $_.Name }) -join ', '))
            }
        }
        foreach ($def in $selected) {
            $errors = Invoke-Pair -Def $def -MaxDeleteCount $maxDeleteCount -MaxDeletePercent $maxDeletePercent
            if ($errors -gt 0) { $exitCode = 1 }
        }

        Remove-OldLogs -Directory $logDirectory -RetentionDays $retentionDays
    }
} catch {
    $exitCode = 1
    Write-Log 'ERROR' ('Run failed: ' + (Get-ErrorText $_))
} finally {
    $runWatch.Stop()
    if ($null -ne $script:LogWriter) {
        Write-Log 'INFO' ('Run finished: exit=' + $exitCode + ' duration=' + (Format-Seconds $runWatch.Elapsed.TotalSeconds))
    }
    Close-Log
    if ($null -ne $mutex) {
        if ($mutexHeld) { try { $mutex.ReleaseMutex() } catch { } }
        $mutex.Dispose()
    }
}

exit $exitCode
