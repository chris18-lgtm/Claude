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
- **Python 3.8 or newer, standard library only.** Nothing to `pip install`, no admin rights.

Think of it as a photocopier standing next to a filing cabinet: it makes copies and puts them on a
separate shelf, it throws away copies whose original is gone, and it never opens the cabinet for
anything but reading.

## Status: what has and has not been tested

Read this before trusting the tool.

| Part | State |
|---|---|
| `sync_obsidian_mirror.py` | All 29 test cases pass on **Linux** with Python 3.8, 3.9, 3.10, 3.11, 3.12 and 3.13. Safety guards were also switched off one at a time to confirm that a test then fails. |
| Windows | **Not run by the author.** Running `test_obsidian_mirror.py` on the laptop is the real acceptance test. Windows-only behaviour (file locking by Obsidian or OneDrive, read-only files, the 260-character limit, OneDrive placeholders) is covered by the tests there, not here. |
| Scheduled task | Set up by hand (see [Schedule it](#step-6-schedule-it)). There is no script for it, so nothing about it was tested. |
| Copilot | Whether Copilot actually finds the copies is for you to verify (see [After go-live](#after-go-live)). |

An earlier PowerShell version of this tool was replaced by this Python version. It is still in the
git history of this repository.

## Files

| File | Purpose |
|---|---|
| `sync_obsidian_mirror.py` | The mirror script |
| `mirror-config.json` | Source/target pairs, excludes, thresholds |
| `test_obsidian_mirror.py` | Self-contained tests against temp folders |
| `README.md` | This file |

Put the folder somewhere **outside** every vault and outside OneDrive, for example
`C:\Users\<you>\Scripts\obsidian-mirror\`.

## Before you start

**Is it allowed?** Running your own scripts on a managed work laptop, with a Python you installed
yourself, is a question for your company's rules, not a technical one. If you are unsure, ask IT.

**Which command starts Python?** Open *Windows PowerShell* or *Command Prompt* and try:

```
python --version
```

If that prints `Python 3.8` or higher, use `python` in all commands below. If it reports an error
or opens the Microsoft Store, try `py --version` and use `py` instead of `python` everywhere.

## Install and first run

All commands are typed into a PowerShell or Command Prompt window. Paste a command and press Enter.

### Step 1. Go into the script folder

```
cd "$env:USERPROFILE\Scripts\obsidian-mirror"
```

(In Command Prompt instead of PowerShell: `cd "%USERPROFILE%\Scripts\obsidian-mirror"`.)

### Step 2. Run the tests

They only touch the temp folder, never your vault or OneDrive, and take a few seconds.

```
python test_obsidian_mirror.py
```

Every line must say `PASS`. A `SKIP` is fine and states its reason (case 28 is skipped on most
Windows machines because creating a folder link needs special rights). If anything says `FAIL`,
stop here and report the output.

Optional speed check with 5,000 generated notes:

```
python test_obsidian_mirror.py --perf --only "PERF*"
```

The no-change run should take well under 10 seconds.

### Step 3. Edit `mirror-config.json`

Open it in Notepad. Replace the `<user>` / `<org>` placeholders with your real paths, remove the
pair you do not need, and adjust the excludes (see [Configuration](#configuration)). Rules:

- every `target` is inside OneDrive and **outside every vault**;
- every pair has its **own** target folder;
- a backslash is written twice in JSON: `C:\\Users\\...`;
- save the file as UTF-8 (Notepad: *Save as > Encoding: UTF-8*), otherwise umlauts in paths break.

Think about confidentiality now: everything that is mirrored becomes findable by Copilot for your
account. Exclude folders that should not be.

### Step 4. Dry run

Nothing is changed.

```
python sync_obsidian_mirror.py --what-if
```

Read the output. You should see one `WOULD COPY` line per note, no `ERROR` lines, and a `SUMMARY`
line per pair whose `scanned` number matches the number of notes you expect.

### Step 5. First real run, then a second one

```
python sync_obsidian_mirror.py
```

Check: `copied` equals `scanned`, `errors=0`. Then run the same command again. It must report
`copied=0 deleted=0`. If it copies again, something keeps changing file sizes or timestamps in the
mirror; do not schedule the task until that is understood.

### Step 6. Schedule it

You set the task up by hand in the Windows Task Scheduler. First let the script print the exact
values for your machine:

```
python sync_obsidian_mirror.py --show-task-settings
```

It prints three lines: **Program/script**, **Add arguments** and **Start in**. The program is
`pythonw.exe`, the variant of Python without a console window, so nothing flashes on the screen
when the task runs.

Then open *Start > Task Scheduler > Create Task...* (not "Create Basic Task") and fill in the tabs:

1. **General**
   - Name: `ObsidianCopilotMirror`
   - Select *Run only when user is logged on*.
   - Leave *Run with highest privileges* unticked.
2. **Triggers > New...**
   - *Begin the task:* `At log on`, *Specific user* (you).
   - Tick *Delay task for:* and type `2 minutes` (gives OneDrive time to start).
   - Tick *Repeat task every:* `30 minutes`, *for a duration of:* `Indefinitely`.
   - Tick *Enabled*, then OK.
3. **Actions > New...**
   - *Action:* `Start a program`.
   - Copy the three printed values into *Program/script*, *Add arguments* and *Start in*.
4. **Conditions**
   - Untick *Start the task only if the computer is on AC power*. Otherwise the mirror silently
     stops whenever the laptop is unplugged.
   - Leave *Wake the computer to run this task* unticked.
5. **Settings**
   - Tick *Run task as soon as possible after a scheduled start is missed*.
   - Tick *Stop the task if it runs longer than:* and type `15 minutes`.
   - *If the task is already running:* `Do not start a new instance`.
6. **Check it.** Click OK, right-click the new task > *Run*. After a few seconds press F5. The
   column *Last Run Result* must show `The operation completed successfully. (0x0)`, and today's
   log file must have a new `SUMMARY` line (see [Daily use](#daily-use)).

Do not choose an interval under 15 minutes. OneDrive upload and Copilot indexing take far longer
than a run of the script, so a shorter interval brings nothing.

If the Task Scheduler refuses to save the task, creating scheduled tasks is restricted for your
account. Ask IT for the right to create a task in your own user context, or run the script by hand
when you want the mirror updated.

## Daily use

Nothing to do. The task runs two minutes after logon and then every 30 minutes.

**Log files** are in the folder set as `logDirectory` (default `%LOCALAPPDATA%\ObsidianMirror\logs`;
paste that into the File Explorer address bar). There is one file per day. Every run and pair ends
with a line like this:

```
2026-11-12 11:19:21 INFO  SUMMARY pair=main status=OK scanned=412 copied=1 unchanged=411 deleted=1 dirs_removed=0 skipped=0 warnings=0 errors=0 duration=0.31s
```

| Field | Meaning |
|---|---|
| `status` | `OK`, `DRYRUN` (with `--what-if`), `ABORTED` (a safety guard stopped the pair), `FAILED` (unexpected error) |
| `scanned` | Notes found in the vault after excludes |
| `copied` | New or changed notes copied (in a dry run: would be copied) |
| `unchanged` | Notes whose copy is already up to date; not touched, so OneDrive does not upload them again |
| `deleted` | Copies removed because their note is gone, renamed, moved or newly excluded |
| `dirs_removed` | Empty folders removed from the mirror |
| `skipped` | Notes that could not be copied this time (locked, path too long); retried on the next run |
| `warnings` / `errors` | Number of `WARN` / `ERROR` lines for this pair |

Individual file names are logged only for copies, deletes, warnings and errors.

**Exit code:** `0` success; `1` at least one pair had an error or a safety guard stopped it; `2`
wrong command line.

**Options of `sync_obsidian_mirror.py`:**

| Option | Effect |
|---|---|
| `--what-if` | Dry run: log what would happen, change nothing |
| `--pair NAME` | Run only this pair |
| `--config FILE` | Use another configuration file |
| `--initialize-target` | Accept an existing, non-empty folder without marker file as mirror. Existing `.txt` files in it that match no note are then deleted. |
| `--force-delete` | Allow a large number of deletions for this one run |
| `--show-task-settings` | Print the values for the Task Scheduler and exit |

**After changing the configuration** (new pair, new exclude, moved vault): run `--what-if` first.

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
  "logRetentionDays": 14
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

Environment variables written as `%NAME%` are expanded in all paths. JSON does not allow comments.

Only files ending in `.md` are mirrored. Attachments, canvases, images and everything else are ignored.

## Safety guards

| Guard | What happens |
|---|---|
| **Vault is read-only** | Every create, overwrite and delete goes through one small class that refuses any path outside the mirror folder or inside a vault. |
| **Only `.txt` and empty folders** | Inside the mirror the script deletes nothing else. Other files are left alone and reported as warnings. |
| **Marker file** | A mirror folder is only cleaned if it contains `.obsidian-mirror-target`. The script creates it in a new or empty folder. An existing non-empty folder without marker stops the pair until you confirm with `--initialize-target`. |
| **Missing or empty vault** | If the vault path does not exist or contains no `.md` file (OneDrive not signed in, vault moved), the pair stops and deletes nothing. |
| **Nesting** | Mirror inside a vault, vault inside a mirror, identical folders, or two pairs sharing a mirror: the pairs involved stop. |
| **Delete threshold** | If a run would delete more than `maxDeleteCount` files **and** more than `maxDeletePercent` of the mirror, deletion is skipped, an error is logged and the exit code is 1. Copies still happen. `--force-delete` overrides it once. |
| **Incomplete listing** | If any folder of the vault or the mirror cannot be read, deletion is skipped for that pair, because missing listings would look like deleted notes. |
| **Links in the mirror** | A link or junction inside the mirror is never followed and never removed, so nothing behind it can be deleted. |
| **Locked notes** | A note that cannot be read is skipped with a warning. An existing copy of it is left intact and picked up on a later run. |
| **Single instance** | A second run using the same log folder exits immediately with exit code 0. |
| **Independent pairs** | A failing pair does not stop the others. |

Unchanged detection uses file size and modification time only (2 seconds tolerance). File contents
in the mirror are never read, so OneDrive Files On-Demand placeholders are not downloaded.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python` is not recognized, or the Microsoft Store opens | Use `py` instead of `python`, or Python is not installed. |
| `needs Python 3.8 or newer` | The installed Python is too old. |
| `still contains a placeholder (<...>)` | `mirror-config.json` has not been edited. |
| `Config file is not valid JSON` | A typo in `mirror-config.json`. Most often a single backslash (must be `\\`), a missing comma, or a comment. |
| `Source folder not found` | OneDrive is not signed in or not started yet, or the vault was moved. Nothing was deleted. |
| `contains no .md files after excludes` | Wrong vault path, or everything is excluded. Nothing was deleted. |
| `exists, is not empty and has no marker file` | The target folder was not created by this tool. Choose an empty folder, or confirm once with `--initialize-target`. |
| `Unsafe folder layout` | Mirror and vault overlap, or two pairs share a mirror. Fix the paths. |
| `Deletion skipped: this run would delete ...` | The delete threshold. Run `--what-if` to see the list. If intended (big reorganisation, new exclude), run once with `--force-delete`. |
| `Skipped '...': the note could not be read` | Obsidian or OneDrive had the file open, or it is not downloaded. Harmless; the next run picks it up. |
| `path too long` | The path exceeds 260 characters. Shorten folder or note names, or choose a shorter mirror path. Other notes are not affected. |
| `Source listing incomplete` | A folder could not be read (permissions, path too long, more than 64 folder levels). Deletion is paused for that pair until it is fixed. |
| `Foreign file in mirror` | Something other than a `.txt` is in the mirror. It is left alone. Move it elsewhere to silence the warning. |
| The second run copies everything again | Something changes sizes or timestamps in the mirror. Check whether another tool touches that folder. |
| Umlauts in a path are not found | `mirror-config.json` was not saved as UTF-8. |
| Task shows *Last Run Result* `0x1` | The script reported an error. Read the log. |
| Task shows `0x2` or "file not found" | *Program/script* or *Add arguments* is wrong. Run `--show-task-settings` again and copy the values exactly, including the quotation marks. |
| Task runs but no new log line appears | With a Python from the Microsoft Store the full path to `pythonw.exe` sometimes does not start from the Task Scheduler. Put just `pythonw.exe` into *Program/script*. |
| Task never runs on battery or after sleep | Steps 4 and 5 of the task setup were skipped. Open the task and correct the *Conditions* and *Settings* tabs. |
| A test fails or a temp folder is left behind | Rerun with `--keep-sandbox --only "<number>*"` to inspect the temp folders it names, and report the output. |

## Uninstall

1. *Task Scheduler > Task Scheduler Library >* right-click `ObsidianCopilotMirror` *> Delete*.
2. Delete the mirror folders (the `target` paths). Copilot keeps finding the copies until they are gone.
3. Delete the log folder (`logDirectory`).
4. Delete this script folder.

Your vaults are not affected.

## After go-live

These are checks for you, not for the code:

- **Does Copilot find the copies, and how quickly?** Put a made-up word into a note, wait for the
  next run and for OneDrive to sync, then ask Copilot about that word.
- **Do umlauts look right in Copilot's citations?** If not, a follow-up change could write the
  copies as UTF-8 with BOM. The copies would then no longer have the same size as the notes, so
  change detection would have to switch from size + time to time only or to hashes.
- **Confidentiality.** Review which folders belong in `excludeFolders`.

## Differences from the original brief

- **Python instead of Windows PowerShell 5.1**, because PowerShell scripts may not be run on the
  target laptop. The brief had ruled Python out on the assumption that nothing could be installed.
- **No scripts for the scheduled task.** The task is created by hand; `--show-task-settings` prints
  the values. Started through `pythonw.exe`, there is no console window flash.
- **Log retention.** "Never delete anything outside a mirror root" and "delete old log files"
  contradict each other literally. The script deletes only its own log files (name pattern
  `ObsidianMirror-yyyy-MM-dd.log`) inside `logDirectory`, and nothing else outside a mirror.
- **Extra guards:** pairs sharing or nesting a mirror, a log folder inside a vault or mirror, an
  incomplete folder listing pausing deletion, links in the mirror never being followed, and an
  unreadable note never damaging its existing copy.
- **A second instance** leaves no log entry (it prints one line and exits 0). The lock is a file
  `ObsidianMirror.lock` in the log folder, released by Windows when the process ends.
- **Option names** follow Python conventions: `--what-if`, `--pair`, `--config`,
  `--initialize-target`, `--force-delete`.
