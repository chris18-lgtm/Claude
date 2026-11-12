#!/usr/bin/env python3
"""Self-contained tests for sync_obsidian_mirror.py. Standard library only.

Every case builds a throwaway vault and mirror under the temp folder, runs the
real script as a child process, checks exit code, files and log, and cleans up.
Nothing outside the temp folder is touched. Real vaults are never used.

Prints PASS / FAIL / SKIP per case. Exit code 0 when no case failed, else 1.

    python test_obsidian_mirror.py
    python test_obsidian_mirror.py --perf          also time 5,000 generated notes
    python test_obsidian_mirror.py --only "12*"    run only matching cases
    python test_obsidian_mirror.py --keep-sandbox  keep the temp folders
"""

import sys

if sys.version_info < (3, 8):
    sys.stderr.write("These tests need Python 3.8 or newer; this is %s\n" % sys.version.split()[0])
    sys.exit(1)

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC_SCRIPT = os.path.join(HERE, "sync_obsidian_mirror.py")
MARKER = ".obsidian-mirror-target"
LOCK_NAME = "ObsidianMirror.lock"
UMLAUTS = "\u00e4\u00f6\u00fc\u00df\u20ac"       # a-uml o-uml u-uml sharp-s euro
O_UML = "\u00f6"

SANDBOXES = []
RESULTS = {"pass": 0, "fail": 0, "skip": 0}
OPTIONS = None
CASES = []


class Skip(Exception):
    pass


# ------------------------------------------------------------------ helpers ---

class Sandbox:
    def __init__(self):
        # The folder name deliberately contains a space.
        self.root = os.path.join(tempfile.gettempdir(), "ObsidianMirror Test-" + uuid.uuid4().hex[:10])
        os.makedirs(self.root)
        SANDBOXES.append(self.root)
        self.source = os.path.join(self.root, "Vault")
        self.target = os.path.join(self.root, "Mirror")
        self.logs = os.path.join(self.root, "Logs")
        self.config = os.path.join(self.root, "mirror-config.json")


class Run:
    def __init__(self, exit_code, output, log):
        self.exit_code = exit_code
        self.output = output
        self.log = log


def native(relative):
    return relative.replace("/", os.sep)


def write_file(root, relative, text="content"):
    full = os.path.join(root, native(relative))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as handle:
        handle.write(text.encode("utf-8"))
    return full


def read_text(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


def pair_spec(sb, name="main", exclude=(".obsidian", ".trash", ".git", "Templates"), source=None, target=None):
    return {"name": name, "source": source or sb.source, "target": target or sb.target,
            "excludeFolders": list(exclude)}


def write_config(sb, pairs=None, **overrides):
    config = {"pairs": pairs if pairs is not None else [pair_spec(sb)],
              "maxDeleteCount": 25, "maxDeletePercent": 20,
              "logDirectory": sb.logs, "logRetentionDays": 14}
    config.update(overrides)
    with open(sb.config, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)


def log_text(sb):
    if not os.path.isdir(sb.logs):
        return ""
    text = ""
    for name in sorted(os.listdir(sb.logs)):
        if name.endswith(".log"):
            with open(os.path.join(sb.logs, name), "r", encoding="utf-8") as handle:
                text += handle.read()
    return text


def sync(sb, *arguments):
    before = log_text(sb)
    result = subprocess.run([sys.executable, SYNC_SCRIPT, "--config", sb.config] + list(arguments),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    after = log_text(sb)
    new = after[len(before):] if len(after) >= len(before) else after
    return Run(result.returncode, result.stdout.decode("utf-8", "replace"), new)


def relative_files(root):
    """Sorted list of all files below root, relative, with forward slashes."""
    found = []
    if not os.path.isdir(root):
        return found
    for folder, _dirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/"))
    return sorted(found)


def snapshot(root):
    """name|size|mtime for every file plus every folder; proves 'nothing changed'."""
    if not os.path.isdir(root):
        return "<missing>"
    lines = []
    for folder, dirs, files in os.walk(root):
        for name in dirs:
            lines.append("D|" + os.path.relpath(os.path.join(folder, name), root))
        for name in files:
            full = os.path.join(folder, name)
            info = os.stat(full)
            lines.append("F|%s|%d|%d" % (os.path.relpath(full, root), info.st_size, info.st_mtime_ns))
    return "\n".join(sorted(lines))


def file_hash(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def summary(run, pair, field):
    """Reads e.g. copied=3 from the last SUMMARY line of the given pair."""
    value = None
    for line in run.log.splitlines():
        if ("SUMMARY pair=%s " % pair) in line:
            match = re.search(r" %s=(\S+)" % re.escape(field), line)
            if match:
                value = match.group(1)
    return value


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def equal(expected, actual, message):
    if str(expected) != str(actual):
        raise AssertionError("%s (expected [%s], got [%s])" % (message, expected, actual))


def same_list(expected, actual, message):
    if sorted(expected) != sorted(actual):
        raise AssertionError("%s\n      expected: %s\n      actual:   %s"
                             % (message, " ; ".join(sorted(expected)), " ; ".join(sorted(actual))))


def exit_is(run, expected, message):
    if run.exit_code != expected:
        raise AssertionError("%s (expected exit %d, got %d)\n%s%s"
                             % (message, expected, run.exit_code, run.output, run.log))


def log_has(run, pattern, message):
    if not re.search(pattern, run.log):
        raise AssertionError("%s (log does not match /%s/)\n%s" % (message, pattern, run.log))


def basic_vault(sb):
    write_file(sb.source, "Inbox.md", "inbox")
    write_file(sb.source, "Projects/Alpha/Plan.md", "plan")
    write_file(sb.source, "Projects/Alpha/Notes with spaces.md", "spaces")
    write_file(sb.source, "Projects/Beta/Readme.MD", "upper-case extension")
    write_file(sb.source, "Archive/Old.md", "old")
    # Give every note an old, distinct timestamp, so that a copy which did not get
    # the note's modification time is detected.
    stamp = 1614850000                                           # March 2021
    for folder, _dirs, files in os.walk(sb.source):
        for name in files:
            stamp += 3600
            os.utime(os.path.join(folder, name), (stamp, stamp))


BASIC_EXPECTED = [MARKER, "Archive/Old.txt", "Inbox.txt", "Projects/Alpha/Notes with spaces.txt",
                  "Projects/Alpha/Plan.txt", "Projects/Beta/Readme.txt"]


def new_basic():
    sb = Sandbox()
    basic_vault(sb)
    write_config(sb)
    return sb


class Unreadable:
    """Makes a note impossible to read for the duration of a 'with' block.

    Windows: a mandatory byte-range lock, the same kind of obstacle Obsidian or
    OneDrive can cause. Linux: the note becomes a link to /proc/self/mem, which
    opens fine and then fails on read. Elsewhere: the case is skipped."""

    def __init__(self, path):
        self.path = path
        self.handle = None
        self.size = 0
        self.backup = None

    def __enter__(self):
        if os.name == "nt":
            import msvcrt
            self.size = max(1, os.path.getsize(self.path))
            self.handle = open(self.path, "r+b")
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, self.size)
        elif os.path.exists("/proc/self/mem"):
            self.backup = self.path + ".real"
            os.rename(self.path, self.backup)
            os.symlink("/proc/self/mem", self.path)
        else:
            raise Skip("no way to make a file unreadable on this platform")
        return self

    def __exit__(self, *_exc):
        if self.handle is not None:
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, self.size)
            self.handle.close()
        if self.backup is not None:
            os.remove(self.path)
            os.rename(self.backup, self.path)
        return False


def hold_lock(path):
    """Takes the same lock the script uses for 'single instance'."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def case(name):
    def register(function):
        CASES.append((name, function))
        return function
    return register


# -------------------------------------------------------------------- cases ---

@case("01 initial run mirrors a nested structure and creates the marker")
def _():
    sb = new_basic()
    run = sync(sb)
    exit_is(run, 0, "initial run")
    same_list(BASIC_EXPECTED, relative_files(sb.target), "mirror content")
    equal(5, summary(run, "main", "scanned"), "scanned")
    equal(5, summary(run, "main", "copied"), "copied")
    equal(0, summary(run, "main", "errors"), "errors")
    for note, copy in (("Inbox.md", "Inbox.txt"), ("Projects/Beta/Readme.MD", "Projects/Beta/Readme.txt")):
        delta = abs(os.stat(os.path.join(sb.source, native(note))).st_mtime - os.stat(os.path.join(sb.target, native(copy))).st_mtime)
        check(delta < 2, "modification time of %s preserved on the copy (difference %.0f s)" % (note, delta))


@case("02 second run with no changes copies nothing and leaves the mirror untouched")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    # Tamper with one mirror file: same length, same timestamp, different bytes.
    # A metadata-only comparison must not notice, so the tampered bytes must survive.
    dest = os.path.join(sb.target, "Inbox.txt")
    info = os.stat(dest)
    with open(dest, "wb") as handle:
        handle.write(b"XXXXX")
    os.utime(dest, ns=(info.st_atime_ns, info.st_mtime_ns))
    before = snapshot(sb.target)
    run = sync(sb)
    exit_is(run, 0, "no-change run")
    equal(0, summary(run, "main", "copied"), "copied")
    equal(0, summary(run, "main", "deleted"), "deleted")
    equal(5, summary(run, "main", "unchanged"), "unchanged")
    equal(before, snapshot(sb.target), "mirror snapshot")
    equal("XXXXX", read_text(dest), "mirror file was not rewritten")
    check("COPIED" not in run.log, "no per-file lines for unchanged files")


@case("03 a modified note is re-copied, an unmodified one is not")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    write_file(sb.source, "Inbox.md", "inbox with more text")              # (a) different length
    plan = write_file(sb.source, "Projects/Alpha/Plan.md", "PLAN")         # (b) same length ...
    future = time.time_ns() + 30 * 10**9
    os.utime(plan, ns=(future, future))                                    # ... newer timestamp only
    run = sync(sb)
    exit_is(run, 0, "second run")
    equal(2, summary(run, "main", "copied"), "copied")
    equal(3, summary(run, "main", "unchanged"), "unchanged")
    equal("inbox with more text", read_text(os.path.join(sb.target, "Inbox.txt")), "changed length")
    equal("PLAN", read_text(os.path.join(sb.target, native("Projects/Alpha/Plan.txt"))), "changed timestamp")
    check(not re.search(r"COPIED.*Old\.txt", run.log), "unmodified note not copied")


@case("04 a deleted note removes its .txt")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    os.remove(os.path.join(sb.source, "Inbox.md"))
    run = sync(sb)
    exit_is(run, 0, "second run")
    equal(1, summary(run, "main", "deleted"), "deleted")
    check(not os.path.exists(os.path.join(sb.target, "Inbox.txt")), "Inbox.txt removed")
    log_has(run, r"DELETED\s+Inbox\.txt", "delete is logged")


@case("05 rename and move replace the old .txt, emptied folders are removed")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    os.rename(os.path.join(sb.source, "Inbox.md"), os.path.join(sb.source, "Inbox renamed.md"))
    os.makedirs(os.path.join(sb.source, "Moved"))
    os.rename(os.path.join(sb.source, native("Archive/Old.md")), os.path.join(sb.source, native("Moved/Old.md")))
    run = sync(sb)
    exit_is(run, 0, "second run")
    same_list([MARKER, "Inbox renamed.txt", "Moved/Old.txt", "Projects/Alpha/Notes with spaces.txt",
               "Projects/Alpha/Plan.txt", "Projects/Beta/Readme.txt"],
              relative_files(sb.target), "mirror content after rename/move")
    check(not os.path.isdir(os.path.join(sb.target, "Archive")), "emptied folder Archive removed")
    check(os.path.isdir(sb.target), "mirror root itself still exists")


@case("06 excluded folders are never mirrored; a folder excluded later is cleaned up")
def _():
    sb = Sandbox()
    basic_vault(sb)
    write_file(sb.source, ".obsidian/workspace.md", "x")
    write_file(sb.source, ".hidden-not-listed/secret.md", "x")       # dot folder, not listed
    write_file(sb.source, "Templates/Daily.md", "x")                 # name, top level
    write_file(sb.source, "Projects/templates/Nested.md", "x")       # name, any depth, other case
    write_file(sb.source, "Work/Private/Salary.md", "x")             # relative path
    write_file(sb.source, "Work/Public/Agenda.md", "x")
    write_file(sb.source, "Other/Private/Fine.md", "x")              # same name elsewhere: not excluded
    write_file(sb.source, ".dotfile-note.md", "x")                   # a dot FILE is a normal note
    write_config(sb, [pair_spec(sb, exclude=[".obsidian", "Templates", "work\\private"])])
    run = sync(sb)
    exit_is(run, 0, "first run")
    same_list(BASIC_EXPECTED + ["Work/Public/Agenda.txt", "Other/Private/Fine.txt", ".dotfile-note.txt"],
              relative_files(sb.target), "mirror content with excludes")
    # Exclude "Archive" and "Other/Private" later: their copies must disappear.
    write_config(sb, [pair_spec(sb, exclude=[".obsidian", "Templates", "Work/Private", "Archive", "Other/Private/"])])
    run = sync(sb)
    exit_is(run, 0, "second run")
    same_list([f for f in BASIC_EXPECTED if f != "Archive/Old.txt"] + ["Work/Public/Agenda.txt", ".dotfile-note.txt"],
              relative_files(sb.target), "mirror content after excluding later")
    check(not os.path.isdir(os.path.join(sb.target, "Archive")), "Archive folder removed from mirror")
    check(not os.path.isdir(os.path.join(sb.target, "Other")), "Other folder removed from mirror")


@case("07 non-.md files in the source are ignored")
def _():
    sb = Sandbox()
    basic_vault(sb)
    for name in ("Picture.png", "Board.canvas", "Projects/Alpha/Sheet.xlsx", "Projects/Alpha/Already.txt",
                 "Projects/Alpha/Fake.md.bak", "Projects/Alpha/NoExtension", "Attachments/only.pdf"):
        write_file(sb.source, name, "x")
    write_config(sb)
    run = sync(sb)
    exit_is(run, 0, "run")
    same_list(BASIC_EXPECTED, relative_files(sb.target), "only notes mirrored")
    check(not os.path.isdir(os.path.join(sb.target, "Attachments")), "no folder for attachment-only directory")


@case("08 umlauts, euro sign and brackets arrive byte-identical")
def _():
    sb = Sandbox()
    name = "N" + O_UML + "te [draft] #1 & more.md"
    folder = "Gr" + O_UML + "\u00dfe [2026]"
    a = write_file(sb.source, "Umlaut content.md", "Text " + UMLAUTS + " end\r\n")
    b = write_file(sb.source, folder + "/" + name, "# " + UMLAUTS)
    # UTF-16 with BOM and raw bytes that are not valid UTF-8 must survive as well.
    c = os.path.join(sb.source, "Utf16.md")
    with open(c, "wb") as handle:
        handle.write(("Wide " + UMLAUTS).encode("utf-16"))
    d = os.path.join(sb.source, "Ansi.md")
    with open(d, "wb") as handle:
        handle.write(bytes([0x47, 0x72, 0xFC, 0xDF, 0x65, 0x00, 0xFF, 0x0A]))
    write_config(sb)
    run = sync(sb)
    exit_is(run, 0, "run")
    copies = [os.path.join(sb.target, "Umlaut content.txt"),
              os.path.join(sb.target, folder, name[:-3] + ".txt"),
              os.path.join(sb.target, "Utf16.txt"),
              os.path.join(sb.target, "Ansi.txt")]
    for original, copy in zip([a, b, c, d], copies):
        check(os.path.isfile(copy), "exists: " + copy)
        equal(file_hash(original), file_hash(copy), "hash of " + os.path.basename(original))
    run = sync(sb)
    equal(0, summary(run, "main", "copied"), "second run copies nothing")


@case("09 a missing source aborts with no deletions")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    before = snapshot(sb.target)
    os.rename(sb.source, sb.source + "-gone")
    run = sync(sb)
    exit_is(run, 1, "missing source must exit 1")
    equal(before, snapshot(sb.target), "mirror untouched")
    log_has(run, r"ERROR.*[Ss]ource", "error explains the missing source")
    check(not os.path.exists(sb.source), "missing source is not created")


@case("10 a source with zero .md files aborts with no deletions")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    before = snapshot(sb.target)
    for folder, _dirs, files in os.walk(sb.source):
        for name in files:
            os.remove(os.path.join(folder, name))
    write_file(sb.source, "leftover.png", "x")
    write_file(sb.source, "Templates/OnlyExcluded.md", "x")      # the only notes left are excluded
    run = sync(sb)
    exit_is(run, 1, "empty source must exit 1")
    equal(before, snapshot(sb.target), "mirror untouched")


@case("11 non-empty target without marker aborts; --initialize-target resolves it")
def _():
    sb = new_basic()
    write_file(sb.target, "Somebody elses.txt", "not ours")
    write_file(sb.target, "Report.pdf", "not ours")
    before = snapshot(sb.target)
    run = sync(sb)
    exit_is(run, 1, "unmarked non-empty target must exit 1")
    equal(before, snapshot(sb.target), "target untouched without marker")
    log_has(run, r"initialize-target", "message tells the user how to confirm")
    run = sync(sb, "--initialize-target")
    exit_is(run, 0, "run with --initialize-target")
    files = relative_files(sb.target)
    check(MARKER in files, "marker created")
    check("Inbox.txt" in files, "notes mirrored")
    check("Report.pdf" in files, "foreign non-.txt file kept")
    check("Somebody elses.txt" not in files, "orphan .txt cleaned after explicit confirmation")
    # An empty existing folder needs no confirmation.
    sb2 = new_basic()
    os.makedirs(sb2.target)
    exit_is(sync(sb2), 0, "empty existing target")
    check(os.path.isfile(os.path.join(sb2.target, MARKER)), "marker created in empty target")


@case("12 delete threshold blocks a mass delete; --force-delete allows it")
def _():
    sb = Sandbox()
    for i in range(1, 31):
        write_file(sb.source, "Bulk/Note%02d.md" % i, "n%d" % i)
    write_file(sb.source, "Keep.md", "keep")
    write_config(sb)
    exit_is(sync(sb), 0, "initial run")
    shutil.rmtree(os.path.join(sb.source, "Bulk"))               # 30 of 31 would go
    write_file(sb.source, "New.md", "new")
    run = sync(sb)
    exit_is(run, 1, "threshold must exit 1")
    equal(0, summary(run, "main", "deleted"), "nothing deleted")
    equal(33, len(relative_files(sb.target)), "all 31 old copies + marker + the new copy remain")
    check(os.path.isfile(os.path.join(sb.target, "New.txt")), "copies still happen when deletes are blocked")
    log_has(run, r"ERROR.*force-delete", "error mentions --force-delete")
    run = sync(sb, "--force-delete")
    exit_is(run, 0, "run with --force-delete")
    equal(30, summary(run, "main", "deleted"), "deleted with --force-delete")
    same_list([MARKER, "Keep.txt", "New.txt"], relative_files(sb.target), "mirror after forced delete")

    # Both limits must be exceeded. Many files but small share: allowed.
    sb = Sandbox()
    for i in range(1, 41):
        write_file(sb.source, "N%02d.md" % i, "n%d" % i)
    write_config(sb, maxDeleteCount=2, maxDeletePercent=20)
    exit_is(sync(sb), 0, "initial run (b)")
    for i in range(1, 6):                                        # 5 > 2 but 12.5 % < 20 %
        os.remove(os.path.join(sb.source, "N%02d.md" % i))
    run = sync(sb)
    exit_is(run, 0, "count above, percent below")
    equal(5, summary(run, "main", "deleted"), "deleted (b)")
    # Large share but few files: allowed.
    sb = Sandbox()
    for i in range(1, 11):
        write_file(sb.source, "N%02d.md" % i, "n%d" % i)
    write_config(sb)
    exit_is(sync(sb), 0, "initial run (c)")
    for i in range(1, 7):                                        # 60 % but 6 <= 25
        os.remove(os.path.join(sb.source, "N%02d.md" % i))
    run = sync(sb)
    exit_is(run, 0, "percent above, count below")
    equal(6, summary(run, "main", "deleted"), "deleted (c)")


@case("13 nested or identical source and target are rejected")
def _():
    # target inside source
    sb = Sandbox()
    basic_vault(sb)
    write_config(sb, [pair_spec(sb, target=os.path.join(sb.source, "Mirror"))])
    before = snapshot(sb.source)
    run = sync(sb)
    exit_is(run, 1, "target inside source")
    log_has(run, "Unsafe folder layout", "layout check names the problem (target inside source)")
    equal(before, snapshot(sb.source), "vault untouched (target inside source)")
    # source inside target -- the dangerous variant: the mirror even carries a valid
    # marker, and the vault holds a .txt that orphan cleanup would otherwise delete.
    sb = Sandbox()
    inner = os.path.join(sb.target, "Vault")
    write_file(inner, "A.md", "a")
    precious = write_file(inner, "precious.txt", "a real file inside the vault")
    write_file(sb.target, MARKER, "marker")
    write_config(sb, [pair_spec(sb, source=inner)])
    before = snapshot(sb.target)
    run = sync(sb)
    exit_is(run, 1, "source inside target")
    log_has(run, "Unsafe folder layout", "layout check names the problem (source inside target)")
    check(os.path.isfile(precious), ".txt inside the vault survives")
    equal(before, snapshot(sb.target), "nothing touched (source inside target)")
    # identical, written differently
    sb = Sandbox()
    basic_vault(sb)
    write_config(sb, [pair_spec(sb, target=os.path.join(sb.source, "..", "Vault"))])
    before = snapshot(sb.source)
    run = sync(sb)
    exit_is(run, 1, "source equals target")
    log_has(run, "Unsafe folder layout", "layout check names the problem (identical)")
    equal(before, snapshot(sb.source), "vault untouched (identical paths)")
    # nesting across two different pairs: both involved pairs stop
    sb = Sandbox()
    basic_vault(sb)
    vault2 = os.path.join(sb.root, "Vault2")
    write_file(vault2, "B.md", "b")
    write_config(sb, [pair_spec(sb, name="one", target=os.path.join(vault2, "MirrorOfOne")),
                      pair_spec(sb, name="two", source=vault2, target=os.path.join(sb.root, "MirrorTwo"))])
    before = snapshot(vault2)
    run = sync(sb)
    exit_is(run, 1, "target of one pair inside source of another")
    log_has(run, r"SUMMARY pair=one .*status=ABORTED", "pair one aborted")
    log_has(run, r"SUMMARY pair=two .*status=ABORTED", "pair two aborted")
    equal(before, snapshot(vault2), "second vault untouched")
    check(not os.path.exists(os.path.join(sb.root, "MirrorTwo")), "no mirror created for the involved pair")
    # a sibling folder whose name merely starts with the vault name is NOT nested
    sb = Sandbox()
    basic_vault(sb)
    write_config(sb, [pair_spec(sb, target=sb.source + "-Mirror")])
    exit_is(sync(sb), 0, "sibling folder with a common name prefix is allowed")


@case("14 --what-if changes nothing on disk")
def _():
    # (a) fresh target: nothing is created
    sb = new_basic()
    source_before = snapshot(sb.source)
    run = sync(sb, "--what-if")
    exit_is(run, 0, "dry run on fresh target")
    check(not os.path.exists(sb.target), "target not created by dry run")
    equal(source_before, snapshot(sb.source), "vault untouched")
    log_has(run, r"WOULD COPY\s+Inbox\.txt", "planned copy is logged")
    equal(5, summary(run, "main", "copied"), "planned copies counted")
    log_has(run, r"SUMMARY pair=main status=DRYRUN", "summary marks the dry run")
    # (b) existing mirror with pending copy, delete and empty folder
    exit_is(sync(sb), 0, "real run")
    write_file(sb.source, "Added.md", "added")
    write_file(sb.source, "Inbox.md", "inbox changed")
    os.remove(os.path.join(sb.source, native("Archive/Old.md")))
    source_before = snapshot(sb.source)
    target_before = snapshot(sb.target)
    run = sync(sb, "--what-if")
    exit_is(run, 0, "dry run on existing mirror")
    equal(source_before, snapshot(sb.source), "vault untouched (b)")
    equal(target_before, snapshot(sb.target), "mirror untouched (b)")
    log_has(run, r"WOULD COPY\s+Added\.txt", "planned new copy")
    log_has(run, r"WOULD COPY\s+Inbox\.txt", "planned changed copy")
    log_has(run, r"WOULD DELETE\s+Archive.Old\.txt", "planned delete")
    log_has(run, r"WOULD REMOVE DIR\s+Archive", "planned folder removal")


@case("15 a stray non-.txt file in the target is left alone and logged")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    stray1 = write_file(sb.target, "Projects/Alpha/stray.docx", "keep me")
    stray2 = write_file(sb.target, "OnlyStray/picture.png", "keep me too")
    run = sync(sb)
    exit_is(run, 0, "stray files are only a warning")
    check(os.path.isfile(stray1), "stray.docx kept")
    check(os.path.isfile(stray2), "picture.png kept (and its folder)")
    log_has(run, r"WARN.*stray\.docx", "stray file logged as warning")
    check(not re.search("WARN.*" + re.escape(MARKER), run.log), "marker is not reported as stray")
    equal(2, summary(run, "main", "warnings"), "warning count")


@case("16 an unreadable (locked) note is skipped with a warning and the run continues")
def _():
    sb = new_basic()
    with Unreadable(os.path.join(sb.source, "Inbox.md")):
        run = sync(sb)
    exit_is(run, 0, "a locked note must not fail the run")
    log_has(run, r"WARN.*Inbox", "locked note logged as warning")
    equal(4, summary(run, "main", "copied"), "other notes copied")
    equal(1, summary(run, "main", "skipped"), "skipped count")
    check(not os.path.exists(os.path.join(sb.target, "Inbox.txt")), "no half-written copy left behind")
    run = sync(sb)
    exit_is(run, 0, "next run")
    equal(1, summary(run, "main", "copied"), "picked up on the next run")
    equal("inbox", read_text(os.path.join(sb.target, "Inbox.txt")), "Inbox.txt now present")


@case("17 a locked note keeps its existing .txt (not treated as orphan, not damaged)")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    write_file(sb.source, "Inbox.md", "changed while locked, longer")
    with Unreadable(os.path.join(sb.source, "Inbox.md")):
        run = sync(sb)
    exit_is(run, 0, "run with locked changed note")
    equal("inbox", read_text(os.path.join(sb.target, "Inbox.txt")), "existing copy kept intact")
    equal(0, summary(run, "main", "deleted"), "nothing deleted")
    equal(1, summary(run, "main", "skipped"), "skipped count")


@case("18 a second instance exits quietly")
def _():
    sb = new_basic()
    lock = hold_lock(os.path.join(sb.logs, LOCK_NAME))
    try:
        run = sync(sb)
    finally:
        lock.close()
    exit_is(run, 0, "second instance exits 0")
    check(not os.path.exists(sb.target), "second instance did no work")
    check("Another instance" in run.output, "second instance says why it stopped")
    equal("", run.log, "second instance writes nothing to the log")
    exit_is(sync(sb), 0, "runs normally once the lock is free")
    check(os.path.isfile(os.path.join(sb.target, "Inbox.txt")), "mirror built afterwards")


@case("19 one failing pair does not stop the others; --pair runs a single pair")
def _():
    sb = Sandbox()
    basic_vault(sb)
    bad = pair_spec(sb, name="bad", source=os.path.join(sb.root, "DoesNotExist"), target=os.path.join(sb.root, "MirrorBad"))
    write_config(sb, [bad, pair_spec(sb, name="good")])
    run = sync(sb)
    exit_is(run, 1, "overall exit code reflects the failing pair")
    same_list(BASIC_EXPECTED, relative_files(sb.target), "good pair mirrored despite failing pair")
    log_has(run, r"SUMMARY pair=bad .*status=ABORTED", "summary for failing pair")
    log_has(run, r"SUMMARY pair=good .*status=OK", "summary for good pair")
    run = sync(sb, "--pair", "good")
    exit_is(run, 0, "--pair good")
    check("SUMMARY pair=bad" not in run.log, "bad pair not run with --pair good")
    exit_is(sync(sb, "--pair", "nope"), 1, "unknown pair name")


@case("20 pairs sharing or nesting a target are rejected")
def _():
    sb = Sandbox()
    basic_vault(sb)
    vault2 = os.path.join(sb.root, "Vault2")
    write_file(vault2, "B.md", "b")
    write_config(sb, [pair_spec(sb, name="one"), pair_spec(sb, name="two", source=vault2)])
    exit_is(sync(sb), 1, "shared target")
    check(not os.path.exists(sb.target), "shared target not created")
    write_config(sb, [pair_spec(sb, name="one"),
                      pair_spec(sb, name="two", source=vault2, target=os.path.join(sb.target, "Sub"))])
    exit_is(sync(sb), 1, "nested targets")
    check(not os.path.exists(sb.target), "nested target not created")


@case("21 placeholder, relative and broken configs are rejected")
def _():
    sb = Sandbox()
    basic_vault(sb)
    write_config(sb, [pair_spec(sb, target="C:\\Users\\<user>\\OneDrive - <org>\\CopilotMirror\\MainVault")])
    run = sync(sb)
    exit_is(run, 1, "placeholder path")
    log_has(run, "placeholder", "message names the placeholder problem")
    write_config(sb, [pair_spec(sb, target="relative-mirror")])
    exit_is(sync(sb), 1, "relative path")
    with open(sb.config, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    exit_is(sync(sb), 1, "broken json")
    os.remove(sb.config)
    exit_is(sync(sb), 1, "missing config")
    check(not os.path.exists(sb.target), "nothing created")
    # A config saved with a UTF-8 byte order mark (Windows Notepad) is accepted.
    write_config(sb)
    with open(sb.config, "rb") as handle:
        raw = handle.read()
    with open(sb.config, "wb") as handle:
        handle.write(b"\xef\xbb\xbf" + raw)
    exit_is(sync(sb), 0, "config with byte order mark")
    # The shipped config must be refused as long as it holds placeholders.
    shipped = os.path.join(HERE, "mirror-config.json")
    check(os.path.isfile(shipped), "shipped mirror-config.json exists")
    with open(shipped, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    check(len(config["pairs"]) >= 1, "shipped config has pairs")
    for pair in config["pairs"]:
        check("<" in pair["source"] and "<" in pair["target"], "shipped config holds placeholders only")


@case("22 log directory inside a vault or mirror is refused")
def _():
    sb = Sandbox()
    basic_vault(sb)
    write_config(sb, logDirectory=os.path.join(sb.source, "logs"))
    before = snapshot(sb.source)
    exit_is(sync(sb), 1, "log directory inside vault")
    equal(before, snapshot(sb.source), "vault untouched")
    check(not os.path.exists(sb.target), "no mirroring happened")
    write_config(sb, logDirectory=os.path.join(sb.target, "logs"))
    exit_is(sync(sb), 1, "log directory inside mirror")
    check(not os.path.exists(sb.target), "mirror not created for the log folder")


@case("23 old log files are removed, other files in the log folder are kept")
def _():
    sb = new_basic()
    old = write_file(sb.logs, "ObsidianMirror-2020-01-01.log", "old")
    fresh = write_file(sb.logs, "ObsidianMirror-2020-01-02.log", "recent by timestamp")
    other = write_file(sb.logs, "something-else.log", "not ours")
    now = time.time()
    os.utime(old, (now - 30 * 86400, now - 30 * 86400))
    os.utime(fresh, (now - 3 * 86400, now - 3 * 86400))
    os.utime(other, (now - 300 * 86400, now - 300 * 86400))
    exit_is(sync(sb), 0, "run")
    check(not os.path.exists(old), "log older than retention removed")
    check(os.path.isfile(fresh), "recent log kept")
    check(os.path.isfile(other), "foreign file in log folder kept")
    today = os.path.join(sb.logs, "ObsidianMirror-%s.log" % datetime.datetime.now().strftime("%Y-%m-%d"))
    check(os.path.isfile(today), "one log file per day with the expected name")
    check(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", read_text(today)), "log lines are timestamped, UTF-8")


@case("24 incomplete source listing blocks deletes (folder nesting beyond the depth limit)")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    os.remove(os.path.join(sb.source, "Inbox.md"))               # would normally be deleted from the mirror
    deep = os.path.join(sb.source, *(["d"] * 70))
    if len(deep) > 240:
        raise Skip("temp path too long to build a 70-level folder chain here")
    os.makedirs(deep)
    run = sync(sb)
    exit_is(run, 1, "incomplete listing must exit 1")
    check(os.path.isfile(os.path.join(sb.target, "Inbox.txt")), "no delete while the source listing is incomplete")
    equal(0, summary(run, "main", "deleted"), "deleted")


@case("25 a read-only note and a read-only copy can be updated and removed")
def _():
    sb = new_basic()
    note = os.path.join(sb.source, "Inbox.md")
    copy = os.path.join(sb.target, "Inbox.txt")
    os.chmod(note, stat.S_IREAD)
    try:
        exit_is(sync(sb), 0, "initial run with read-only note")
        os.chmod(note, stat.S_IREAD | stat.S_IWRITE)
        write_file(sb.source, "Inbox.md", "changed read-only note")
        os.chmod(note, stat.S_IREAD)
        os.chmod(copy, stat.S_IREAD)                             # somebody made the copy read-only
        run = sync(sb)
        exit_is(run, 0, "update of read-only note onto read-only copy")
        equal("changed read-only note", read_text(copy), "updated content")
        os.chmod(copy, stat.S_IREAD)
    finally:
        os.chmod(note, stat.S_IREAD | stat.S_IWRITE)
    os.remove(note)
    run = sync(sb)
    exit_is(run, 0, "delete of a read-only copy")
    check(not os.path.exists(copy), "copy removed")


@case("26 a very long path does not crash the run")
def _():
    sb = new_basic()
    rel = "".join("LongFolderName-" + "x" * 35 + "/" for _ in range(6))
    try:
        write_file(sb.source, rel + "Deep note.md", "deep")
    except OSError as ex:
        raise Skip("cannot create a 300-character path here: %s" % ex)
    run = sync(sb)
    check(run.exit_code in (0, 1), "script ended with a regular exit code")
    log_has(run, "SUMMARY pair=main", "summary line written")
    check("Traceback" not in run.output, "no unhandled Python error")
    check(os.path.isfile(os.path.join(sb.target, "Inbox.txt")), "normal notes still mirrored")
    mirrored = os.path.isfile(os.path.join(sb.target, native(rel + "Deep note.txt")))
    check(mirrored or re.search(r"(WARN|ERROR).*(too long|260)", run.log), "long path either mirrored or reported clearly")


@case("27 a copy that cannot be written is reported and does not stop the run")
def _():
    sb = new_basic()
    os.makedirs(os.path.join(sb.target, "Inbox.txt"))            # a folder sits where the copy should go
    write_file(sb.target, MARKER, "marker")
    run = sync(sb)
    exit_is(run, 1, "a problem on the mirror side is an error")
    log_has(run, r"ERROR.*Inbox\.txt", "failed copy logged as error")
    equal(4, summary(run, "main", "copied"), "other notes copied")
    equal(1, summary(run, "main", "skipped"), "skipped count")


@case("28 links inside the mirror are never followed or deleted")
def _():
    sb = new_basic()
    exit_is(sync(sb), 0, "initial run")
    outside = os.path.join(sb.root, "Outside")
    precious = write_file(outside, "precious.txt", "must survive")
    link = os.path.join(sb.target, "LinkToOutside")
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as ex:
        raise Skip("cannot create a folder link here (on Windows this needs Developer Mode): %s" % ex)
    run = sync(sb)
    exit_is(run, 0, "run with a link in the mirror")
    check(os.path.isfile(precious), "file behind the link survives")
    check(os.path.lexists(link), "the link itself is left alone")
    log_has(run, r"WARN.*LinkToOutside", "link reported as foreign")
    equal(0, summary(run, "main", "deleted"), "nothing deleted")


@case("29 the script is plain ASCII, compiles, and imports only the standard library")
def _():
    allowed = {"argparse", "datetime", "json", "os", "re", "stat", "sys", "time", "msvcrt", "fcntl"}
    for name in ("sync_obsidian_mirror.py", "test_obsidian_mirror.py"):
        path = os.path.join(HERE, name)
        with open(path, "rb") as handle:
            raw = handle.read()
        equal(0, sum(1 for b in raw if b > 127), name + " contains non-ASCII bytes")
        compile(raw, path, "exec")
    with open(SYNC_SCRIPT, "r", encoding="ascii") as handle:
        text = handle.read()
    imported = set(re.findall(r"^\s*import\s+(\w+)", text, re.M)) | set(re.findall(r"^\s*from\s+(\w+)", text, re.M))
    same_list(sorted(imported & allowed), sorted(imported), "imports of sync_obsidian_mirror.py")
    result = subprocess.run([sys.executable, SYNC_SCRIPT, "--show-task-settings"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    equal(0, result.returncode, "--show-task-settings exit code")
    check(b"Program/script" in result.stdout, "--show-task-settings prints the task values")


def perf_case():
    count = OPTIONS.perf_notes
    sb = Sandbox()
    print("      generating %d notes ..." % count)
    body = ("# Note\n" + "Lorem ipsum dolor sit amet. " * 8).encode("utf-8")
    for i in range(count):
        folder = os.path.join(sb.source, "Area%02d" % (i % 10), "Topic%03d" % (i // 100))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "Note%05d.md" % i), "wb") as handle:
            handle.write(body)
    for i in range(500):                                          # something to prune
        write_file(sb.source, ".obsidian/cache/c%04d.md" % i, "x")
    write_config(sb)
    started = time.monotonic()
    first = sync(sb)
    t1 = time.monotonic() - started
    exit_is(first, 0, "initial perf run")
    equal(count, summary(first, "main", "copied"), "copied on initial run")
    started = time.monotonic()
    second = sync(sb)
    t2 = time.monotonic() - started
    exit_is(second, 0, "no-change perf run")
    equal(0, summary(second, "main", "copied"), "copied on no-change run")
    equal(count, summary(second, "main", "unchanged"), "unchanged on no-change run")
    print("      initial run   : %7.2f s  (script-internal: %s)" % (t1, summary(first, "main", "duration")))
    print("      no-change run : %7.2f s  (script-internal: %s)" % (t2, summary(second, "main", "duration")))
    print("      (wall time includes Python start-up; target for the no-change run: under 10 s)")
    check(t2 < 10, "no-change run took %.2f s, target is under 10 s" % t2)


# ------------------------------------------------------------------- runner ---

def run_case(name, function):
    if OPTIONS.only and not fnmatch.fnmatch(name, OPTIONS.only):
        return
    try:
        function()
        RESULTS["pass"] += 1
        print("PASS  " + name)
    except Skip as ex:
        RESULTS["skip"] += 1
        print("SKIP  %s  --  %s" % (name, ex))
    except AssertionError as ex:
        RESULTS["fail"] += 1
        print("FAIL  " + name)
        print("      " + str(ex).encode("ascii", "replace").decode("ascii"))
    except Exception as ex:
        RESULTS["fail"] += 1
        print("FAIL  " + name)
        print("      unexpected %s: %s" % (type(ex).__name__, str(ex).encode("ascii", "replace").decode("ascii")))
    sys.stdout.flush()


def remove_tree(root):
    def retry_writable(function, path, _info):
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            function(path)
        except OSError:
            pass
    shutil.rmtree(root, onerror=retry_writable)


def main():
    global OPTIONS
    parser = argparse.ArgumentParser(description="Tests for sync_obsidian_mirror.py (temp folders only).")
    parser.add_argument("--perf", action="store_true", help="also time a run over --perf-notes generated notes")
    parser.add_argument("--perf-notes", type=int, default=5000, metavar="N")
    parser.add_argument("--only", metavar="PATTERN", help="run only cases whose name matches, e.g. \"12*\"")
    parser.add_argument("--keep-sandbox", action="store_true", help="do not delete the temp folders")
    OPTIONS = parser.parse_args()

    print("Testing " + SYNC_SCRIPT)
    print("Python  %s  (%s)" % (sys.version.split()[0], sys.executable))
    print("")
    if not os.path.isfile(SYNC_SCRIPT):
        print("FAIL  sync_obsidian_mirror.py not found next to this test script.")
        return 1
    try:
        for name, function in CASES:
            run_case(name, function)
        if OPTIONS.perf:
            run_case("PERF %d notes: initial run and no-change run" % OPTIONS.perf_notes, perf_case)
    finally:
        if OPTIONS.keep_sandbox:
            print("\nSandboxes kept:")
            for root in SANDBOXES:
                print("  " + root)
        else:
            for root in SANDBOXES:
                try:
                    remove_tree(root)
                except OSError as ex:
                    print("      note: could not remove temp folder %s: %s" % (root, ex))
    print("")
    print("Result: %d passed, %d failed, %d skipped" % (RESULTS["pass"], RESULTS["fail"], RESULTS["skip"]))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
