#!/usr/bin/env python3
"""One-way mirror of Obsidian notes (.md) to byte-identical .txt copies, so that
Microsoft 365 Copilot can read them.

For every source/target pair in mirror-config.json:

    <source>/A/B/Note.md  ->  <target>/A/B/Note.txt

The vault is only ever read. The mirror is disposable: delete it and run the
script once to rebuild it.

Safety rules built into this script:
  * Nothing is written, renamed or deleted inside a vault.
  * Inside a mirror only .txt files and empty folders are ever deleted.
  * A mirror folder is only cleaned when it carries the marker file
    .obsidian-mirror-target.
  * A missing or empty vault, nested paths, or an unusually large number of
    deletions stop the pair instead of deleting anything.

Standard library only. Needs Python 3.8 or newer. No network access.

Exit code 0: success. 1: at least one pair had errors or a safety guard stopped
it. 2: wrong command line.
"""

import sys

if sys.version_info < (3, 8):
    sys.stderr.write("sync_obsidian_mirror needs Python 3.8 or newer; this is %s\n" % sys.version.split()[0])
    sys.exit(1)

import argparse
import datetime
import json
import os
import re
import stat
import time

VERSION = "2.0.0"
MARKER_NAME = ".obsidian-mirror-target"
LOCK_NAME = "ObsidianMirror.lock"
MAX_DEPTH = 64          # folder nesting limit; protects against link loops
MAX_STRAY_LOG = 20      # per pair and run, list at most this many foreign files
SLOW_SECONDS = 60
TOLERANCE_NS = 2 * 1000 * 1000 * 1000
COPY_CHUNK = 1024 * 1024
DEFAULT_LOG_DIR = "%LOCALAPPDATA%\\ObsidianMirror\\logs"
LOG_NAME_RE = re.compile(r"^ObsidianMirror-\d{4}-\d{2}-\d{2}\.log$")


class ConfigError(Exception):
    pass


class SafetyError(Exception):
    """A write or delete was about to leave the mirror or enter a vault."""


class SourceUnavailable(Exception):
    """A note could not be read (locked, not downloaded, vanished)."""


# ------------------------------------------------------------------ logging ---

class Log:
    def __init__(self):
        self.handle = None

    def open(self, directory):
        os.makedirs(directory, exist_ok=True)
        name = "ObsidianMirror-%s.log" % datetime.datetime.now().strftime("%Y-%m-%d")
        self.handle = open(os.path.join(directory, name), "a", encoding="utf-8", buffering=1)

    def close(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None

    def write(self, level, message):
        line = "%s %-5s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, message)
        if self.handle is not None:
            try:
                self.handle.write(line + "\n")
            except OSError:
                pass
        console(line)


def console(line):
    """Print to the console if there is one. pythonw.exe has none, and a legacy
    console code page may not know every character of a file name."""
    out = sys.stdout
    if out is None:
        return
    try:
        out.write(line + "\n")
    except UnicodeEncodeError:
        try:
            out.write(line.encode("ascii", "replace").decode("ascii") + "\n")
        except Exception:
            pass
    except Exception:
        pass


LOG = Log()


def format_seconds(seconds):
    return "%.2fs" % seconds


def remove_old_logs(directory, retention_days, dry_run):
    """Deletes only this tool's own daily log files, and only in the log folder."""
    if dry_run or retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not LOG_NAME_RE.match(entry.name) or not entry.is_file(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    try:
                        os.remove(entry.path)
                        LOG.write("INFO", "Removed old log file " + entry.name)
                    except OSError as ex:
                        LOG.write("WARN", "Could not remove old log file %s: %s" % (entry.name, ex))
    except OSError as ex:
        LOG.write("WARN", "Log retention failed: %s" % ex)


# -------------------------------------------------------------------- paths ---

def normalize(path):
    return os.path.normpath(os.path.abspath(path))


def resolved(path):
    """Path with links resolved; falls back to the normalized path."""
    try:
        return os.path.normpath(os.path.realpath(path))
    except (OSError, ValueError):
        return normalize(path)


def path_key(path):
    # Always compare without regard to case: the stricter choice on any system.
    return os.path.normcase(path).lower()


def is_inside(child, parent):
    """True when child is the same folder as parent or lies below it."""
    c = path_key(child)
    p = path_key(parent)
    if c == p:
        return True
    if not p.endswith(os.sep):
        p += os.sep
    return c.startswith(p)


def overlaps(child_forms, parent_forms):
    """Compares the written form with the written form and the resolved form
    with the resolved form."""
    return is_inside(child_forms[0], parent_forms[0]) or is_inside(child_forms[1], parent_forms[1])


_ENV_RE = re.compile(r"%([^%\\/:]+)%")


def expand_env(text):
    def replace(match):
        value = os.environ.get(match.group(1))
        if value is None:
            raise ConfigError("uses an environment variable that is not defined: %" + match.group(1) + "%")
        return value
    return _ENV_RE.sub(replace, text)


def resolve_configured_path(raw, what):
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(what + " is empty.")
    if "<" in raw or ">" in raw:
        raise ConfigError(what + " still contains a placeholder (<...>): " + raw +
                          " -- edit mirror-config.json and enter the real path.")
    try:
        expanded = expand_env(raw)
    except ConfigError as ex:
        raise ConfigError(what + " " + str(ex))
    if not os.path.isabs(expanded):
        raise ConfigError(what + " must be a full path, got: " + expanded)
    return normalize(expanded)


# ------------------------------------------------------------------- config ---

def read_config(path):
    if not os.path.isfile(path):
        raise ConfigError("Config file not found: " + path)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except (ValueError, UnicodeDecodeError) as ex:
        raise ConfigError("Config file is not valid JSON (saved as UTF-8?): %s (%s)" % (path, ex))
    if not isinstance(config, dict):
        raise ConfigError("Config file must contain a JSON object: " + path)
    return config


def config_int(config, name, default, minimum):
    raw = config.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError("Config value '%s' must be a whole number, got: %r" % (name, raw))
    if raw < minimum:
        raise ConfigError("Config value '%s' must be at least %d, got: %d" % (name, minimum, raw))
    return raw


class PairDef:
    def __init__(self, name):
        self.name = name
        self.source = None
        self.target = None
        self.source_forms = None     # (written, resolved)
        self.target_forms = None
        self.exclude_names = set()   # folder names, any depth, lower case
        self.exclude_paths = set()   # vault-relative paths with '/', lower case
        self.config_error = None
        self.blocked = []


def read_pairs(config):
    pairs = []
    seen = set()
    raw_pairs = config.get("pairs") or []
    if not isinstance(raw_pairs, list):
        raise ConfigError("'pairs' must be a list.")
    for index, raw in enumerate(raw_pairs, 1):
        if not isinstance(raw, dict):
            raw = {}
        name = str(raw.get("name") or "pair%d" % index)
        pair = PairDef(name)
        try:
            if not re.match(r"^[\w\-]+$", name):
                raise ConfigError("Pair name '%s' may only contain letters, digits, '-' and '_'." % name)
            if name.lower() in seen:
                raise ConfigError("Pair name '%s' is used more than once." % name)
            seen.add(name.lower())
            pair.source = resolve_configured_path(raw.get("source"), "Source of pair '%s'" % name)
            pair.target = resolve_configured_path(raw.get("target"), "Target of pair '%s'" % name)
            pair.source_forms = (pair.source, resolved(pair.source))
            pair.target_forms = (pair.target, resolved(pair.target))
            excludes = raw.get("excludeFolders") or []
            if not isinstance(excludes, list):
                raise ConfigError("excludeFolders of pair '%s' must be a list." % name)
            for entry in excludes:
                # Without a slash: a folder name that matches at any depth.
                # With a slash: a path relative to the vault root.
                norm = str(entry).replace("\\", "/").strip().strip("/").lower()
                if not norm:
                    continue
                if "/" in norm:
                    pair.exclude_paths.add(norm)
                else:
                    pair.exclude_names.add(norm)
        except ConfigError as ex:
            pair.config_error = str(ex)
        pairs.append(pair)
    return pairs


def check_layout(pairs):
    """Nesting check across ALL pairs. Adds the reason to every pair involved."""
    valid = [p for p in pairs if p.config_error is None]
    for i, a in enumerate(valid):
        for j, b in enumerate(valid):
            reason = None
            if path_key(a.target) == path_key(b.source) or path_key(a.target_forms[1]) == path_key(b.source_forms[1]):
                reason = "target of '%s' is the same folder as source of '%s'" % (a.name, b.name)
            elif overlaps(a.target_forms, b.source_forms):
                reason = "target of '%s' lies inside source of '%s'" % (a.name, b.name)
            elif overlaps(b.source_forms, a.target_forms):
                reason = "source of '%s' lies inside target of '%s'" % (b.name, a.name)
            if reason:
                for pair in (a, b):
                    if reason not in pair.blocked:
                        pair.blocked.append(reason)
            if i < j and (overlaps(a.target_forms, b.target_forms) or overlaps(b.target_forms, a.target_forms)):
                reason = ("targets of '%s' and '%s' are the same folder or nested; "
                          "each pair needs its own mirror folder" % (a.name, b.name))
                a.blocked.append(reason)
                b.blocked.append(reason)


# -------------------------------------------------------------- enumeration ---

def listing_problem(folder, ex):
    text = "Cannot list folder '%s': %s" % (folder, ex)
    if len(folder) >= 240:
        text += (" (the path is %d characters long; the Windows limit of 260 characters is "
                 "probably exceeded -- path too long)" % len(folder))
    return text


def is_link(entry):
    """Symbolic link or Windows junction. OneDrive folders are reparse points of
    another kind and are NOT links."""
    try:
        if entry.is_symlink():
            return True
        junction = getattr(entry, "is_junction", None)       # Python 3.12+
        if junction is not None:
            return bool(junction())
        if os.name == "nt":
            tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
            return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    except OSError:
        return True      # unknown: treat as a link, i.e. leave it alone
    return False


def list_source(root, exclude_names, exclude_paths):
    """Walks the vault exactly once and never descends into excluded folders.

    Returns (notes, problems, collisions). notes maps the expected relative mirror
    path in lower case to (relative mirror path, note path, size, mtime_ns). Its key
    set is the 'expected' set used for orphan detection.
    """
    notes = {}
    problems = []
    collisions = []
    stack = [(root, "", 0)]
    while stack:
        folder, rel, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    try:
                        if entry.is_dir():
                            if name.startswith("."):
                                continue                       # dot folders are always excluded
                            if name.lower() in exclude_names:
                                continue
                            sub = rel + name
                            if exclude_paths and sub.replace("\\", "/").lower() in exclude_paths:
                                continue
                            if depth + 1 > MAX_DEPTH:
                                problems.append("Folder nesting is deeper than %d levels at '%s'; "
                                                "not descending further." % (MAX_DEPTH, entry.path))
                                continue
                            stack.append((entry.path, sub + os.sep, depth + 1))
                        elif name[-3:].lower() == ".md" and entry.is_file():
                            info = entry.stat()                # from the directory listing
                            rel_txt = rel + name[:-3] + ".txt"
                            key = rel_txt.lower()
                            if key in notes:
                                collisions.append(rel + name)
                            else:
                                notes[key] = (rel_txt, entry.path, info.st_size, info.st_mtime_ns)
                    except OSError as ex:
                        problems.append("Cannot read '%s': %s" % (entry.path, ex))
        except OSError as ex:
            problems.append(listing_problem(folder, ex))
    return notes, problems, collisions


def list_target(root):
    """Walks the mirror exactly once. Only directory metadata is read, never file
    content, so OneDrive Files On-Demand placeholders are not downloaded.

    Links and junctions are never followed and never touched: deleting through one
    could reach files outside the mirror.

    Returns (txt, strays, dirs, problems). txt maps the relative path in lower case
    to (relative path, size, mtime_ns).
    """
    txt = {}
    strays = []
    dirs = []
    problems = []
    if not os.path.isdir(root):
        return txt, strays, dirs, problems
    stack = [(root, "", 0)]
    while stack:
        folder, rel, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    try:
                        if is_link(entry):
                            strays.append(rel + name)
                        elif entry.is_dir(follow_symlinks=False):
                            sub = rel + name
                            dirs.append(sub)
                            if depth + 1 > MAX_DEPTH:
                                problems.append("Folder nesting is deeper than %d levels at '%s'; "
                                                "not descending further." % (MAX_DEPTH, entry.path))
                                continue
                            stack.append((entry.path, sub + os.sep, depth + 1))
                        elif name[-4:].lower() == ".txt" and entry.is_file(follow_symlinks=False):
                            info = entry.stat(follow_symlinks=False)
                            txt[(rel + name).lower()] = (rel + name, info.st_size, info.st_mtime_ns)
                        elif depth == 0 and name.lower() == MARKER_NAME:
                            pass                               # our own marker file
                        else:
                            strays.append(rel + name)
                    except OSError as ex:
                        problems.append("Cannot read '%s': %s" % (entry.path, ex))
        except OSError as ex:
            problems.append(listing_problem(folder, ex))
    return txt, strays, dirs, problems


# ---------------------------------------------------------------- mutations ---

class MirrorWriter:
    """The ONLY place where this script creates, overwrites or deletes anything
    (apart from its own log and lock file). Every method refuses to act outside the
    mirror root or inside any vault, and does nothing in a dry run."""

    def __init__(self, root, vault_forms, dry_run):
        self.root = root
        self.vault_forms = vault_forms      # list of (written, resolved) of every vault
        self.dry_run = dry_run

    def _check(self, path, allow_root=False):
        full = normalize(path)
        if path_key(full) == path_key(self.root):
            if not allow_root:
                raise SafetyError("SAFETY: refusing to modify the mirror root itself: '%s'" % full)
        elif not is_inside(full, self.root):
            raise SafetyError("SAFETY: refusing to modify '%s' because it is outside the mirror root '%s'"
                              % (full, self.root))
        for forms in self.vault_forms:
            if is_inside(full, forms[0]) or is_inside(full, forms[1]):
                raise SafetyError("SAFETY: refusing to modify '%s' because it is inside the vault '%s'"
                                  % (full, forms[0]))
        return full

    @staticmethod
    def _require_txt(path, action):
        if path[-4:].lower() != ".txt":
            raise SafetyError("SAFETY: refusing to %s a non-.txt file: '%s'" % (action, path))

    @staticmethod
    def _make_writable(path):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass

    def make_dir(self, path):
        self._check(path, allow_root=True)
        if self.dry_run:
            return
        os.makedirs(path, exist_ok=True)

    def write_marker(self):
        marker = os.path.join(self.root, MARKER_NAME)
        self._check(marker)
        if self.dry_run:
            return
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(
                "This folder is a one-way mirror created by sync_obsidian_mirror.py.\n"
                "The .txt files in it are copies of Obsidian notes and are overwritten and deleted automatically.\n"
                "Do not store anything else here. Deleting this marker file stops the script from cleaning this folder.\n")

    def copy(self, source, dest, mtime_ns):
        """Byte-for-byte copy; no text decoding, so umlauts and any encoding survive.

        Problems on the note's side raise SourceUnavailable. Problems on the mirror's
        side raise OSError. The first chunk is read before the destination is opened,
        so an unreadable note does not damage an existing copy."""
        self._check(dest)
        self._require_txt(dest, "write")
        if self.dry_run:
            return
        try:
            reader = open(source, "rb")
        except OSError as ex:
            raise SourceUnavailable(str(ex))
        with reader:
            try:
                chunk = reader.read(COPY_CHUNK)
            except OSError as ex:
                raise SourceUnavailable(str(ex))
            dest_dir = os.path.dirname(dest)
            if not os.path.isdir(dest_dir):
                self.make_dir(dest_dir)
            try:
                writer = open(dest, "wb")
            except PermissionError:
                if not os.path.isfile(dest) or os.access(dest, os.W_OK):
                    raise
                self._make_writable(dest)             # a read-only copy: clear the flag and retry
                writer = open(dest, "wb")
            with writer:
                while chunk:
                    writer.write(chunk)
                    try:
                        chunk = reader.read(COPY_CHUNK)
                    except OSError as ex:
                        raise SourceUnavailable(str(ex))
        os.utime(dest, ns=(mtime_ns, mtime_ns))        # keep the note's modification time

    def delete_txt(self, path):
        self._check(path)
        self._require_txt(path, "delete")
        if self.dry_run:
            return
        try:
            os.remove(path)
        except PermissionError:
            if not os.path.isfile(path) or os.access(path, os.W_OK):
                raise
            self._make_writable(path)
            os.remove(path)

    def remove_empty_dir(self, path):
        self._check(path)
        if self.dry_run:
            return
        os.rmdir(path)                                 # fails if anything is left inside


# ----------------------------------------------------------------- one pair ---

class Stats:
    def __init__(self, name):
        self.name = name
        self.status = "OK"
        self.scanned = self.copied = self.unchanged = self.deleted = 0
        self.dirs_removed = self.skipped = self.warnings = self.errors = 0

    def log(self, level, message):
        if level == "WARN":
            self.warnings += 1
        elif level == "ERROR":
            self.errors += 1
        LOG.write(level, "[%s] %s" % (self.name, message))

    def abort(self, message):
        self.status = "ABORTED"
        self.log("ERROR", message + " Pair aborted, nothing was deleted.")


def add_ancestors(keep, relative_path):
    path = relative_path.lower()
    while True:
        index = path.rfind(os.sep)
        if index <= 0:
            break
        path = path[:index]
        if path in keep:
            break
        keep.add(path)


def path_length_hint(source, dest):
    if len(source) >= 260 or len(dest) >= 260:
        return (" (path too long: source %d, mirror %d characters; the Windows limit is 260)"
                % (len(source), len(dest)))
    return ""


def sync_pair(pair, stats, options, vault_forms, max_delete_count, max_delete_percent):
    dry_run = options.what_if
    would = "WOULD " if dry_run else ""
    writer = MirrorWriter(pair.target, vault_forms, dry_run)

    # --- Guard: source must exist ------------------------------------------
    if not os.path.isdir(pair.source):
        stats.abort("Source folder not found: '%s' (OneDrive not signed in, or the vault was moved?)." % pair.source)
        return

    # --- Source listing -----------------------------------------------------
    notes, problems, collisions = list_source(pair.source, pair.exclude_names, pair.exclude_paths)
    stats.scanned = len(notes)
    deletes_allowed = True
    for problem in problems:
        stats.log("ERROR", "Source listing incomplete: " + problem)
        deletes_allowed = False
    for collision in collisions:
        stats.log("WARN", "Two notes map to the same .txt name; skipped '%s'." % collision)
    if not notes:
        stats.abort("Source '%s' contains no .md files after excludes." % pair.source)
        return

    # --- Guard: marker file ---------------------------------------------------
    marker = os.path.join(pair.target, MARKER_NAME)
    if os.path.lexists(pair.target) and not os.path.isdir(pair.target):
        stats.abort("Target '%s' is not a folder." % pair.target)
        return
    if not os.path.isdir(pair.target):
        writer.make_dir(pair.target)
        writer.write_marker()
        stats.log("INFO", would + "CREATE mirror folder with marker file: '%s'" % pair.target)
    elif not os.path.isfile(marker):
        with os.scandir(pair.target) as entries:
            is_empty = next(entries, None) is None
        if is_empty:
            writer.write_marker()
            stats.log("INFO", would + "CREATE marker file in empty mirror folder '%s'" % pair.target)
        elif options.initialize_target:
            writer.write_marker()
            stats.log("INFO", would + "CREATE marker file in existing folder '%s' (confirmed with "
                      "--initialize-target). Existing .txt files without a matching note are treated "
                      "as orphans." % pair.target)
        else:
            stats.abort("Target '%s' exists, is not empty and has no marker file '%s'. If this really is "
                        "the folder to use as mirror, run once with --initialize-target (existing .txt "
                        "files without a matching note will then be deleted). Otherwise choose an empty "
                        "or new folder." % (pair.target, MARKER_NAME))
            return

    # --- Target listing -------------------------------------------------------
    txt, strays, dirs, problems = list_target(pair.target)
    for problem in problems:
        stats.log("ERROR", "Mirror listing incomplete: " + problem)
        deletes_allowed = False

    # --- Copy new and changed notes -------------------------------------------
    for key, (rel_txt, note_path, size, mtime_ns) in notes.items():
        existing = txt.get(key)
        if existing is not None:
            # Metadata only: same size and same timestamp (2 s tolerance) means unchanged.
            if existing[1] == size and abs(existing[2] - mtime_ns) <= TOLERANCE_NS:
                stats.unchanged += 1
                continue
        reason = "new" if existing is None else "changed"
        dest = os.path.join(pair.target, existing[0] if existing is not None else rel_txt)
        try:
            writer.copy(note_path, dest, mtime_ns)
            stats.copied += 1
            stats.log("INFO", "%s  %s (%s)" % ("WOULD COPY" if dry_run else "COPIED", rel_txt, reason))
        except SourceUnavailable as ex:
            # Locked by Obsidian or OneDrive, not downloaded yet, vanished meanwhile.
            stats.skipped += 1
            stats.log("WARN", "Skipped '%s': the note could not be read: %s%s It will be picked up on a later run."
                      % (rel_txt, ex, path_length_hint(note_path, dest)))
        except (OSError, SafetyError) as ex:
            stats.skipped += 1
            hint = path_length_hint(note_path, dest)
            # A path beyond the Windows limit is a known, per-note limitation: warn.
            # Anything else on the mirror's side is an error.
            level = "WARN" if hint and not isinstance(ex, SafetyError) else "ERROR"
            stats.log(level, "Skipped '%s': could not write the copy: %s%s" % (rel_txt, ex, hint))

    # --- Foreign files: never touched, only reported ---------------------------
    for index, stray in enumerate(strays):
        if index < MAX_STRAY_LOG:
            stats.log("WARN", "Foreign file in mirror (not a .txt, left alone): '%s'" % stray)
        else:
            stats.warnings += 1
    if len(strays) > MAX_STRAY_LOG:
        LOG.write("WARN", "[%s] ... and %d more foreign files in the mirror." % (stats.name, len(strays) - MAX_STRAY_LOG))

    # --- Orphans: .txt files without a note --------------------------------------
    orphans = [value[0] for key, value in txt.items() if key not in notes]
    if orphans and not deletes_allowed:
        stats.log("ERROR", "Deletion skipped because a folder listing was incomplete; %d file(s) look "
                  "orphaned but may not be." % len(orphans))
    if orphans and deletes_allowed:
        over_count = len(orphans) > max_delete_count
        over_percent = len(orphans) * 100 > max_delete_percent * len(txt)
        if over_count and over_percent:
            if options.force_delete:
                stats.log("INFO", "Delete threshold exceeded (%d of %d mirror files) but --force-delete was given."
                          % (len(orphans), len(txt)))
            else:
                deletes_allowed = False
                stats.log("ERROR", "Deletion skipped: this run would delete %d of %d mirror files, more than "
                          "maxDeleteCount=%d and maxDeletePercent=%d. If that is intended (large "
                          "reorganisation, new exclude), run once with --force-delete. Use --what-if first "
                          "to see the list." % (len(orphans), len(txt), max_delete_count, max_delete_percent))
    if not deletes_allowed:
        return

    delete_failed = []
    for rel in orphans:
        try:
            writer.delete_txt(os.path.join(pair.target, rel))
            stats.deleted += 1
            stats.log("INFO", "%s  %s" % ("WOULD DELETE" if dry_run else "DELETED", rel))
        except SafetyError as ex:
            delete_failed.append(rel)
            stats.log("ERROR", "Could not delete '%s': %s" % (rel, ex))
        except OSError as ex:
            delete_failed.append(rel)
            stats.log("WARN", "Could not delete '%s': %s It will be retried on a later run." % (rel, ex))

    # --- Empty folders, deepest first --------------------------------------------
    # A folder stays when it (or anything below it) holds an expected copy, a foreign
    # file, or an orphan that could not be deleted.
    keep = set()
    for value in notes.values():
        add_ancestors(keep, value[0])
    for rel in strays:
        add_ancestors(keep, rel)
    for rel in delete_failed:
        add_ancestors(keep, rel)
    candidates = [d for d in dirs if d.lower() not in keep]
    candidates.sort(key=lambda d: (d.count(os.sep), d), reverse=True)
    for rel_dir in candidates:
        try:
            writer.remove_empty_dir(os.path.join(pair.target, rel_dir))
            stats.dirs_removed += 1
            stats.log("INFO", "%s  %s" % ("WOULD REMOVE DIR" if dry_run else "REMOVED DIR", rel_dir))
        except (OSError, SafetyError) as ex:
            stats.log("WARN", "Could not remove empty folder '%s': %s" % (rel_dir, ex))


def run_pair(pair, options, vault_forms, max_delete_count, max_delete_percent):
    started = time.monotonic()
    stats = Stats(pair.name)
    try:
        if pair.config_error is not None:
            stats.abort("Configuration problem: " + pair.config_error)
        elif pair.blocked:
            stats.abort("Unsafe folder layout: " + "; ".join(pair.blocked) + ".")
        else:
            sync_pair(pair, stats, options, vault_forms, max_delete_count, max_delete_percent)
    except Exception as ex:      # one failing pair must not stop the others
        stats.status = "FAILED"
        stats.log("ERROR", "Unexpected failure: %s: %s" % (type(ex).__name__, ex))
    seconds = time.monotonic() - started
    if seconds > SLOW_SECONDS:
        stats.log("WARN", "This pair took %s, longer than %d s. The scheduled task stops a run after 15 minutes."
                  % (format_seconds(seconds), SLOW_SECONDS))
    status = stats.status
    if status == "OK" and options.what_if:
        status = "DRYRUN"
    LOG.write("INFO", "SUMMARY pair=%s status=%s scanned=%d copied=%d unchanged=%d deleted=%d dirs_removed=%d "
              "skipped=%d warnings=%d errors=%d duration=%s"
              % (stats.name, status, stats.scanned, stats.copied, stats.unchanged, stats.deleted,
                 stats.dirs_removed, stats.skipped, stats.warnings, stats.errors, format_seconds(seconds)))
    return stats.errors


# -------------------------------------------------------------- single instance ---

def acquire_lock(path):
    """Returns an open file holding an exclusive lock, or None when another process
    holds it. The operating system releases the lock when the process ends, even
    after a crash."""
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# ----------------------------------------------------------------------- main ---

def show_task_settings(config_path):
    """Prints the values to type into the Task Scheduler dialogs."""
    folder = os.path.dirname(sys.executable)
    windowless = os.path.join(folder, "pythonw.exe")
    if os.name == "nt" and not os.path.isfile(windowless):
        windowless = sys.executable
    script = os.path.abspath(__file__)
    lines = [
        "Values for the Task Scheduler (see README.md, 'Schedule it'):",
        "",
        "  Program/script:  " + windowless,
        '  Add arguments:   "%s" --config "%s"' % (script, config_path),
        "  Start in:        " + os.path.dirname(script),
        "",
        "pythonw.exe runs without a console window, so nothing flashes on the screen.",
    ]
    if "WindowsApps" in sys.executable:
        lines += ["",
                  "Note: this Python comes from the Microsoft Store. If the task does not start with the",
                  "path above, use plain  pythonw.exe  as Program/script instead."]
    for line in lines:
        console(line)


def build_parser():
    parser = argparse.ArgumentParser(
        description="One-way mirror of Obsidian notes (.md) to .txt copies for Microsoft 365 Copilot.")
    parser.add_argument("--config", metavar="FILE",
                        help="configuration file (default: mirror-config.json next to this script)")
    parser.add_argument("--pair", metavar="NAME", help="run only the pair with this name")
    parser.add_argument("--what-if", "--dry-run", dest="what_if", action="store_true",
                        help="dry run: log every copy and delete that would happen, change nothing")
    parser.add_argument("--initialize-target", action="store_true",
                        help="accept an existing, non-empty folder without marker file as mirror")
    parser.add_argument("--force-delete", action="store_true",
                        help="override the delete threshold for this one run")
    parser.add_argument("--show-task-settings", action="store_true",
                        help="print the values for the Task Scheduler dialogs and exit")
    parser.add_argument("--version", action="version", version="sync_obsidian_mirror " + VERSION)
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    config_path = options.config or os.path.join(os.path.dirname(os.path.abspath(__file__)), "mirror-config.json")
    config_path = os.path.abspath(config_path)

    if options.show_task_settings:
        show_task_settings(config_path)
        return 0

    exit_code = 0
    lock = None
    started = time.monotonic()
    try:
        config = read_config(config_path)
        log_directory = resolve_configured_path(config.get("logDirectory", DEFAULT_LOG_DIR), "logDirectory")
        max_delete_count = config_int(config, "maxDeleteCount", 25, 0)
        max_delete_percent = config_int(config, "maxDeletePercent", 20, 0)
        retention_days = config_int(config, "logRetentionDays", 14, 0)

        pairs = read_pairs(config)
        if not pairs:
            raise ConfigError("The configuration contains no entries under 'pairs'.")
        check_layout(pairs)
        vault_forms = [p.source_forms for p in pairs if p.config_error is None]

        # The log folder must never be inside a vault (that would be a vault write) or a mirror.
        log_forms = (log_directory, resolved(log_directory))
        for pair in pairs:
            if pair.config_error is not None:
                continue
            if overlaps(log_forms, pair.source_forms):
                raise ConfigError("logDirectory '%s' lies inside the vault of pair '%s'. Choose a folder "
                                  "outside all vaults and outside OneDrive." % (log_directory, pair.name))
            if overlaps(log_forms, pair.target_forms):
                raise ConfigError("logDirectory '%s' lies inside the mirror of pair '%s'. Choose a folder "
                                  "outside all mirrors and outside OneDrive." % (log_directory, pair.name))

        # --- Single instance ---------------------------------------------------
        os.makedirs(log_directory, exist_ok=True)
        lock = acquire_lock(os.path.join(log_directory, LOCK_NAME))
        if lock is None:
            # Quiet exit: no log entry, exit code 0.
            console("Another instance of sync_obsidian_mirror is already running. Exiting.")
            return 0

        LOG.open(log_directory)
        flags = ""
        if options.initialize_target:
            flags += " --initialize-target"
        if options.force_delete:
            flags += " --force-delete"
        if options.pair:
            flags += " --pair " + options.pair
        LOG.write("INFO", "Run started: sync_obsidian_mirror %s, Python %s, mode=%s, config=%s%s"
                  % (VERSION, sys.version.split()[0],
                     "DRYRUN (--what-if: nothing will be changed)" if options.what_if else "LIVE",
                     config_path, flags))

        selected = pairs
        if options.pair:
            selected = [p for p in pairs if p.name.lower() == options.pair.lower()]
            if not selected:
                raise ConfigError("No pair named '%s' in the configuration. Available: %s"
                                  % (options.pair, ", ".join(p.name for p in pairs)))
        for pair in selected:
            if run_pair(pair, options, vault_forms, max_delete_count, max_delete_percent) > 0:
                exit_code = 1

        remove_old_logs(log_directory, retention_days, options.what_if)
    except ConfigError as ex:
        exit_code = 1
        LOG.write("ERROR", "Run failed: %s" % ex)
    except Exception as ex:
        exit_code = 1
        LOG.write("ERROR", "Run failed: %s: %s" % (type(ex).__name__, ex))
    finally:
        if LOG.handle is not None:
            LOG.write("INFO", "Run finished: exit=%d duration=%s" % (exit_code, format_seconds(time.monotonic() - started)))
        LOG.close()
        if lock is not None:
            lock.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
