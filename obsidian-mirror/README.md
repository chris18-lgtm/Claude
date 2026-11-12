# Obsidian `.md` to `.txt` mirror for Microsoft 365 Copilot

Microsoft 365 Copilot does not reliably read Markdown files in OneDrive, but it does read `.txt`.
This tool copies every note of an Obsidian vault, byte for byte, to a `.txt` file in a separate
OneDrive folder:

```
<vault>\Projects\Alpha\Plan.md   ->   <mirror>\Projects\Alpha\Plan.txt
```

- **One-way.** The vault is the only source of truth and is only ever read.
- **Disposable.** Delete the mirror folder, run the script once, and it is rebuilt.
- **Local only.** No network calls, no credentials. OneDrive does the upload.
- **Windows PowerShell 5.1**, no modules, no installs, no admin rights.

Think of it as a photocopier standing next to a filing cabinet: it makes copies and puts them on a
separate shelf, it throws away copies whose original is gone, and it never opens the cabinet for
anything but reading.

## Status: what has and has not been tested

Read this before trusting the tool.

| Part | State |
|---|---|
| `Sync-ObsidianMirror.ps1` | All 27 test cases pass under **PowerShell 7 on Linux**. Each safety guard was also switched off on purpose to confirm a test then fails. |
| Windows PowerShell 5.1 | **Not run by the author.** The scripts use only 5.1 syntax, cmdlets and .NET Framework members (checked from the scripts' syntax trees), but that is a review, not a test. Running `Test-ObsidianMirror.ps1` on the laptop is the real acceptance test. |
| `Register-MirrorTask.ps1`, `Unregister-MirrorTask.ps1` | **Never executed** (there is no Task Scheduler on Linux). They parse without errors and use documented cmdlet parameters only. The register script checks its own result, so a problem shows up as a clear message. |
| Windows specifics | File locking by Obsidian/OneDrive, the 260-character limit, OneDrive placeholders, execution policy and language mode could not be exercised. The tests cover them on Windows where possible. |
| Copilot | Whether Copilot actually finds the copies is for you to verify (see [After go-live](#after-go-live)). |

## Files

| File | Purpose |
|---|---|
| `Sync-ObsidianMirror.ps1` | The mirror script |
| `mirror-config.json` | Source/target pairs, excludes, thresholds, interval |
| `Register-MirrorTask.ps1` | Creates or updates the scheduled task |
| `Unregister-MirrorTask.ps1` | Removes the scheduled task |
| `Test-ObsidianMirror.ps1` | Self-contained tests against temp folders |
| `README.md` | This file |

Put the folder somewhere **outside** every vault and outside OneDrive, for example
`%USERPROFILE%\Scripts\obsidian-mirror\`.

## 1. Before you start

### Is it allowed?

Moving scripts from a private GitHub repository onto a managed work laptop, and running them there,
is a question for your company's rules, not a technical one. If you are unsure, ask IT first. This
tool never works around a security policy; where a policy blocks it, it stops and says what to ask for.

### Three checks

Open **Windows PowerShell** (not PowerShell 7) and run:

```powershell
$PSVersionTable.PSVersion
```

```powershell
Get-ExecutionPolicy -List
```

```powershell
$ExecutionContext.SessionState.LanguageMode
```

| Result | Meaning |
|---|---|
| Version `5.1.x` | Good. |
| `LanguageMode` = `FullLanguage` | Good. |
| `LanguageMode` = `ConstrainedLanguage` | **Stop.** An application control policy restricts untrusted scripts. This tool cannot run as it is. It would need to be trusted by policy (for example signed by IT) or rewritten with cmdlets only. |
| `MachinePolicy` or `UserPolicy` = `Restricted` or `AllSigned` | **Stop.** Group Policy forbids unsigned scripts. Ask IT either to sign the scripts or to allow `RemoteSigned` for your account. |
| `MachinePolicy` or `UserPolicy` = `RemoteSigned`, `Unrestricted` or `Bypass` | Good. Local scripts may run. |
| Both `Undefined`, and `CurrentUser`/`LocalMachine` = `RemoteSigned` or looser | Good. |
| Both `Undefined`, and the rest `Undefined` or `Restricted` | Scripts are off by default but no policy forbids them. Add `-ExecutionPolicy RemoteSigned` to the commands below, like this: `powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\Sync-ObsidianMirror.ps1`. It applies to that one process only. |
| Both `Undefined`, and `CurrentUser` or `LocalMachine` = `AllSigned` | **Stop** and ask IT. Somebody chose signed-only on purpose. |

### "Downloaded from the internet"

Files that arrive through a browser (for example a ZIP from GitHub) carry a hidden mark. Under
`RemoteSigned`, PowerShell refuses unsigned scripts with that mark ("... is not digitally signed").
Microsoft's intended way to accept a file you have read and trust is `Unblock-File` (identical to
*Properties > Unblock* in File Explorer):

```powershell
Get-ChildItem -LiteralPath "$env:USERPROFILE\Scripts\obsidian-mirror" -Filter *.ps1 | Unblock-File
```

Read the scripts first. If your company does not want you to do this, do not do it.

## 2. Install and first run

All commands are run from the script folder:

```powershell
Set-Location "$env:USERPROFILE\Scripts\obsidian-mirror"
```

**Step 1. Run the tests.** They only touch the temp folder.

```powershell
powershell.exe -NoProfile -File .\Test-ObsidianMirror.ps1
```

Every line must say `PASS` (a `SKIP` states its reason). If anything says `FAIL`, stop here and
report the output. Optional speed check with 5,000 generated notes:

```powershell
powershell.exe -NoProfile -File .\Test-ObsidianMirror.ps1 -Perf -Only 'PERF*'
```

The no-change run should take well under 10 seconds.

**Step 2. Edit `mirror-config.json`.** Replace the `<user>` / `<org>` placeholders with your real
paths, remove the pair you do not need, and adjust the excludes (see
[Configuration](#configuration)). Rules:

- every `target` is inside OneDrive and **outside every vault**;
- every pair has its **own** target folder;
- a backslash is written twice in JSON: `C:\\Users\\...`;
- save the file as UTF-8 (Notepad: *Save as > Encoding: UTF-8*), otherwise umlauts in paths break.

Think about confidentiality now: everything that is mirrored becomes findable by Copilot for your
account. Exclude folders that should not be.

**Step 3. Dry run.** Nothing is changed.

```powershell
powershell.exe -NoProfile -File .\Sync-ObsidianMirror.ps1 -WhatIf
```

Read the output. You should see one `WOULD COPY` line per note, no `ERROR` lines, and a `SUMMARY`
line per pair whose `scanned` number matches the number of notes you expect.

**Step 4. First real run.**

```powershell
powershell.exe -NoProfile -File .\Sync-ObsidianMirror.ps1
```

Check: `copied` equals `scanned`, `errors=0`.

**Step 5. Second run, immediately afterwards.**

```powershell
powershell.exe -NoProfile -File .\Sync-ObsidianMirror.ps1
```

It must report `copied=0 deleted=0`. If it copies again, something keeps changing file sizes or
timestamps in the mirror; do not schedule the task until that is understood.

**Step 6. Schedule it.**

```powershell
powershell.exe -NoProfile -File .\Register-MirrorTask.ps1
```

The script prints the execution policy, registers the task, checks every setting, starts the task
once and waits for exit code 0. It refuses to register before steps 3 to 5 have happened.

## 3. Daily use

Nothing to do. The task runs two minutes after logon and then every 30 minutes.

**Log files** are in the folder set as `logDirectory` (default `%LOCALAPPDATA%\ObsidianMirror\logs`),
one file per day. Every run and pair ends with a line like this:

```
2026-11-12 11:19:21 INFO  SUMMARY pair=main status=OK scanned=412 copied=1 unchanged=411 deleted=1 dirs_removed=0 skipped=0 warnings=0 errors=0 duration=0.31s
```

| Field | Meaning |
|---|---|
| `status` | `OK`, `DRYRUN` (with `-WhatIf`), `ABORTED` (a safety guard stopped the pair), `FAILED` (unexpected error) |
| `scanned` | Notes found in the vault after excludes |
| `copied` | New or changed notes copied (in a dry run: would be copied) |
| `unchanged` | Notes whose copy is already up to date; not touched, so OneDrive does not upload them again |
| `deleted` | Copies removed because their note is gone, renamed, moved or newly excluded |
| `dirs_removed` | Empty folders removed from the mirror |
| `skipped` | Notes that could not be copied this time (locked, path too long); retried on the next run |
| `warnings` / `errors` | Number of `WARN` / `ERROR` lines for this pair |

Individual file names are logged only for copies, deletes, warnings and errors.

**Exit code:** `0` success; `1` at least one pair had an error or a safety guard stopped it.

**Parameters of `Sync-ObsidianMirror.ps1`:**

| Parameter | Effect |
|---|---|
| `-WhatIf` | Dry run: log what would happen, change nothing |
| `-Pair <name>` | Run only this pair |
| `-ConfigPath <file>` | Use another configuration file |
| `-InitializeTarget` | Accept an existing, non-empty folder without marker file as mirror. Existing `.txt` files in it that match no note are then deleted. |
| `-ForceDelete` | Allow a large number of deletions for this one run |

**After changing the configuration** (new pair, new exclude, moved vault): run `-WhatIf` first.

## Configuration

```json
{
  "pairs": [
    {
      "name": "main",
      "source": "C:\\Users\\<user>\\OneDrive - <org>\\Obsidian\\MainVault",
      "target": "C:\\Users\\<user>\\OneDrive - <org>\\CopilotMirror\\MainVault",
      "excludeFolders": [".obsidian", ".trash", ".git", "Templates"]
    }
  ],
  "maxDeleteCount": 25,
  "maxDeletePercent": 20,
  "logDirectory": "%LOCALAPPDATA%\\ObsidianMirror\\logs",
  "logRetentionDays": 14,
  "syncIntervalMinutes": 30
}
```

| Key | Meaning |
|---|---|
| `name` | Short name of the pair: letters, digits, `-`, `_`. Must be unique. |
| `source` | The vault. Full path. Never written to. |
| `target` | The mirror folder. Full path, outside every vault, not shared with another pair. |
| `excludeFolders` | Folders not to mirror. An entry without slash is a folder **name** and matches at any depth (`Templates`). An entry with slash is a **path relative to the vault** (`Work/Private`). Not case-sensitive. Folders starting with a dot are always excluded, listed or not. |
| `maxDeleteCount`, `maxDeletePercent` | Delete threshold, see [Safety guards](#safety-guards). |
| `logDirectory` | Where the log files go. Must be outside every vault and mirror; keep it outside OneDrive. |
| `logRetentionDays` | Log files older than this are removed. |
| `syncIntervalMinutes` | Interval for the scheduled task (read by `Register-MirrorTask.ps1`). Minimum 15. |

Environment variables such as `%USERPROFILE%` are expanded in all paths. JSON does not allow comments.

Only files ending in `.md` are mirrored. Attachments, canvases, images and everything else are ignored.

## Safety guards

| Guard | What happens |
|---|---|
| **Vault is read-only** | Every create, overwrite and delete goes through five small functions that refuse any path outside the mirror folder or inside a vault. |
| **Only `.txt` and empty folders** | Inside the mirror the script deletes nothing else. Other files are left alone and reported as warnings. |
| **Marker file** | A mirror folder is only cleaned if it contains `.obsidian-mirror-target`. The script creates it in a new or empty folder. An existing non-empty folder without marker stops the pair until you confirm with `-InitializeTarget`. |
| **Missing or empty vault** | If the vault path does not exist or contains no `.md` file (OneDrive not signed in, vault moved), the pair stops and deletes nothing. |
| **Nesting** | Mirror inside a vault, vault inside a mirror, identical folders, or two pairs sharing a mirror: the pairs involved stop. |
| **Delete threshold** | If a run would delete more than `maxDeleteCount` files **and** more than `maxDeletePercent` of the mirror, deletion is skipped, an error is logged and the exit code is 1. Copies still happen. `-ForceDelete` overrides it once. |
| **Incomplete listing** | If any folder of the vault or the mirror cannot be read, deletion is skipped for that pair, because missing listings would look like deleted notes. |
| **Single instance** | A second run for the same configuration exits immediately with exit code 0. |
| **Independent pairs** | A failing pair does not stop the others. |

Unchanged detection uses file size and modification time only (2 seconds tolerance). File contents
in the mirror are never read, so OneDrive Files On-Demand placeholders are not downloaded.

## Scheduling

`Register-MirrorTask.ps1` creates the task `ObsidianCopilotMirror`:

- runs as you, only while you are logged on, without admin rights and without a stored password;
- two triggers: at your logon with a 2-minute delay (so OneDrive is up), and every N minutes without
  end date;
- starts and keeps running on battery, catches up a start missed during sleep, never wakes the
  computer;
- never starts a second instance, and stops a run after 15 minutes;
- action: `powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -File "<folder>\Sync-ObsidianMirror.ps1" -ConfigPath "<folder>\mirror-config.json"`.
  `-ExecutionPolicy RemoteSigned` is added only when no policy forbids it and scripts would
  otherwise be off.

Running it again updates the task. A different interval:

```powershell
powershell.exe -NoProfile -File .\Register-MirrorTask.ps1 -IntervalMinutes 60
```

Intervals under 15 minutes are rejected: OneDrive upload and Copilot indexing take far longer than a
run, so a shorter interval brings nothing.

If the logon trigger is refused for a standard user, the script says so and offers:

```powershell
powershell.exe -NoProfile -File .\Register-MirrorTask.ps1 -TimeTriggerOnly
```

### Console window flash

`-WindowStyle Hidden` still shows a console window for a moment at every run. That is a known
Windows limitation. The tool starts with this plain variant on purpose. If the flash bothers you,
there are two ways around it, and both can collide with company security policy, so neither is
built in:

| Option | Benefit | Cost |
|---|---|---|
| Start PowerShell through a script-host wrapper (`wscript.exe` with a small `.vbs` file) | No window at all | A script host launching PowerShell invisibly is a pattern security software watches for and may block or report. VBScript is also being phased out of Windows. |
| Run the task "whether user is logged on or not" | No window, runs without a session | Needs the "Log on as a batch job" right or a stored password. Storing a password contradicts this tool's "no credentials" rule, and OneDrive only syncs while you are logged on anyway. |

Decide together with IT if needed.

### Manual task setup

If `Register-ScheduledTask` is denied, try the Task Scheduler window (*Start > Task Scheduler >
Create Task...*):

1. **General:** Name `ObsidianCopilotMirror`. Select *Run only when user is logged on*. Leave *Run
   with highest privileges* unticked.
2. **Triggers > New:** *Begin the task: At log on*, *Specific user* (you). *Delay task for:* type
   `2 minutes`. Tick *Repeat task every:* `30 minutes`, *for a duration of:* `Indefinitely`. Tick
   *Enabled*.
3. **Actions > New:** *Start a program*. Program: `powershell.exe`. Arguments:
   `-NoProfile -NonInteractive -WindowStyle Hidden -File "C:\Users\<you>\Scripts\obsidian-mirror\Sync-ObsidianMirror.ps1"`
4. **Conditions:** untick *Start the task only if the computer is on AC power*. Leave *Wake the
   computer to run this task* unticked.
5. **Settings:** tick *Run task as soon as possible after a scheduled start is missed*. Tick *Stop
   the task if it runs longer than:* and type `15 minutes`. *If the task is already running:* `Do
   not start a new instance`.
6. Right-click the task > *Run*, then check *Last Run Result* shows `0x0` and the log has a
   `SUMMARY` line.

If the window refuses as well, ask IT for the right to create a scheduled task in your own user context.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `... cannot be loaded because running scripts is disabled on this system` | Execution policy. See [Three checks](#three-checks). |
| `... is not digitally signed` | The file is marked as downloaded, or the policy is `AllSigned`. See ["Downloaded from the internet"](#downloaded-from-the-internet). |
| `cannot run: PowerShell is in ConstrainedLanguage mode` | Application control policy. Ask IT; the tool cannot run as it is. |
| `still contains a placeholder (<...>)` | `mirror-config.json` has not been edited. |
| `Source folder not found` | OneDrive is not signed in or not started yet, or the vault was moved. Nothing was deleted. |
| `contains no .md files after excludes` | Wrong vault path, or everything is excluded. Nothing was deleted. |
| `exists, is not empty and has no marker file` | The target folder was not created by this tool. Choose an empty folder, or confirm once with `-InitializeTarget`. |
| `Unsafe folder layout` | Mirror and vault overlap, or two pairs share a mirror. Fix the paths. |
| `Deletion skipped: this run would delete ...` | The delete threshold. Run `-WhatIf` to see the list. If intended (big reorganisation, new exclude), run once with `-ForceDelete`. |
| `Skipped '...': ... being used by another process` | Obsidian or OneDrive had the file open. Harmless; the next run picks it up. |
| `path too long` | The path exceeds 260 characters. Shorten folder or note names, or choose a shorter mirror path. Other notes are not affected. |
| `Source listing incomplete` | A folder could not be read (permissions, path too long, more than 64 folder levels). Deletion is paused for that pair until it is fixed. |
| `Foreign file in mirror` | Something other than a `.txt` is in the mirror. It is left alone. Move it elsewhere to silence the warning. |
| The second run copies everything again | Something changes sizes or timestamps in the mirror. Check whether another tool touches that folder. |
| Umlauts in a path are not found | `mirror-config.json` was not saved as UTF-8. |
| Task shows *Last Run Result* `0x1` | The script reported an error. Read the log. |
| Task never runs on battery or after sleep | The task was created by hand with default conditions. Run `Register-MirrorTask.ps1` or correct steps 4 and 5 of the manual setup. |
| A test fails or a temp folder is left behind | Rerun with `-KeepSandbox -Only '<number>*'` to inspect the temp folders it names, and report the output. |

## Uninstall

```powershell
powershell.exe -NoProfile -File .\Unregister-MirrorTask.ps1
```

This removes only the scheduled task. Then delete by hand, if you want:

1. the mirror folders (the `target` paths) - Copilot keeps finding the copies until they are gone;
2. the log folder (`logDirectory`);
3. this script folder.

Your vaults are not affected.

## After go-live

These are checks for you, not for the code:

- **Does Copilot find the copies, and how quickly?** Put a made-up word into a note, wait for the
  next run and for OneDrive to sync, then ask Copilot about that word.
- **Do umlauts look right in Copilot's citations?** If not, a follow-up change could write the
  copies as UTF-8 with BOM. The copies would then no longer have the same size as the notes, so
  change detection would have to switch from size + time to time only or to hashes.
- **Confidentiality.** Review which folders belong in `excludeFolders`.

## Differences from the brief

- **Log retention.** "Never delete anything outside a mirror root" and "delete old log files"
  contradict each other literally. The script deletes only its own log files (name pattern
  `ObsidianMirror-yyyy-MM-dd.log`) inside `logDirectory`, and nothing else outside a mirror.
- **Two task triggers** (logon, and a repeating time trigger) instead of one logon trigger with
  repetition, because the `-AtLogOn` trigger of `New-ScheduledTaskTrigger` accepts no repetition
  parameters. The effect is the one asked for.
- **Extra guards:** pairs sharing or nesting a mirror, a log folder inside a vault or mirror, an
  incomplete folder listing pausing deletion, and the register script requiring a manual run first.
- **A second instance** leaves no log entry (it prints one line and exits 0), so two processes never
  write to the log file at the same time.
- **`syncIntervalMinutes`** was added to the configuration so the interval lives in one place.
- **Folder junctions** are followed like normal folders, because OneDrive folders are themselves
  reparse points and skipping those would skip the whole vault. A limit of 64 folder levels guards
  against loops.
- **PSScriptAnalyzer** could not be downloaded in the build environment (PowerShell Gallery was not
  reachable). Compatibility was checked from the syntax trees instead. If you have it, this is the
  equivalent check:
  `Invoke-ScriptAnalyzer -Path . -Settings @{ Rules = @{ PSUseCompatibleSyntax = @{ Enable = $true; TargetVersions = @('5.1') } } }`
