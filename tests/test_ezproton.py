#!/usr/bin/env python3
"""Tests for ezproton.

Run from the repository root:  python3 -m unittest discover tests

Every test runs the real `ezproton` program against a fake `proton-drive`. The fake lives at the
bottom of this file: when this file is run as `test_ezproton.py --fake-proton-drive ...` it acts
like Proton's CLI and keeps its "Drive" in a temporary folder. No test touches a real account.
"""

import fcntl
import fnmatch
import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EZPROTON = HERE.parent / "ezproton"
ROOT_NAME = "my-files"
BACKUP_ROOT = "/my-files/Backups/testhost"


# --------------------------------------------------------------------------------------------
# Test helpers
# --------------------------------------------------------------------------------------------


class EzTestCase(unittest.TestCase):
    """A fresh home folder, config, state, desktop trash and fake Drive for every test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ezproton-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config = self.home / ".config" / "ezproton"
        self.state = self.home / ".local" / "state" / "ezproton"
        self.trash = self.home / ".local" / "share" / "Trash"
        self.drive_root = self.tmp / "drive"
        self.drive_root.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "proton-drive"
        fake.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve()}" --fake-proton-drive "$@"\n'
        )
        fake.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "FAKE_PD_ROOT": str(self.drive_root),
            "NO_COLOR": "1",
            "LANG": "C.UTF-8",
        }
        self.settings(f"backup-root = {BACKUP_ROOT}\n")

    def tearDown(self):
        for path in self.tmp.rglob("*"):
            try:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o755)
                elif not path.is_symlink():
                    path.chmod(0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Running ezproton ------------------------------------------------------------------------

    def run_ez(self, *args, env=None, input=None, cwd=None):
        full_env = dict(self.env, **(env or {}))
        return subprocess.run(
            [sys.executable, str(EZPROTON), *args],
            env=full_env,
            input=input,
            capture_output=True,
            text=True,
            cwd=cwd or str(self.home),
            timeout=120,
        )

    def ok(self, *args, **kwargs):
        result = self.run_ez(*args, **kwargs)
        self.assertEqual(result.returncode, 0, f"ezproton {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def run_tty(self, *args, answers=(), env=None, timeout=60):
        """Run ezproton in a pseudo-terminal, sending each answer when output goes quiet."""
        full_env = dict(self.env, **(env or {}))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.home)
            os.execve(sys.executable, [sys.executable, str(EZPROTON), *args], full_env)
        output = b""
        pending = list(answers)
        deadline = time.time() + timeout
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            elif pending:
                os.write(fd, (pending.pop(0) + "\n").encode())
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")

    # Config ----------------------------------------------------------------------------------

    def write_config(self, name, text):
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / name).write_text(text)

    def settings(self, text):
        self.write_config("settings", text)

    def backup(self, *lines):
        self.write_config("backup", "\n".join(lines) + "\n")

    def sync(self, *lines):
        self.write_config("sync", "\n".join(lines) + "\n")

    def exclude(self, *lines):
        self.write_config("exclude", "\n".join(lines) + "\n")

    # Local files -----------------------------------------------------------------------------

    def mkfile(self, relative, content="x", mtime=None):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def read(self, relative):
        path = self.home / relative
        return path.read_text() if path.exists() else None

    def desktop_trash(self):
        """Names in the desktop trash, with their original paths."""
        info = self.trash / "info"
        items = {}
        if info.is_dir():
            for entry in info.glob("*.trashinfo"):
                text = entry.read_text()
                match = re.search(r"^Path=(.*)$", text, re.M)
                items[entry.name[: -len(".trashinfo")]] = match.group(1) if match else None
        return items

    # Fake Drive ------------------------------------------------------------------------------

    def drive(self):
        return FakeDrive(self.drive_root)

    def drive_put(self, path, content, mtime=None):
        """Create or change a file on the fake Drive, as another device would."""
        with self.drive() as d:
            d.put_file(path, content.encode(), mtime)

    def drive_read(self, path):
        with self.drive() as d:
            node = d.resolve_live(path)
            return None if node is None or node["type"] != "file" else d.content(node).decode()

    def drive_exists(self, path):
        with self.drive() as d:
            return d.resolve_live(path) is not None

    def drive_trash_names(self):
        with self.drive() as d:
            return [n["name"] for n in d.trashed_top()]

    def drive_revisions(self, path):
        with self.drive() as d:
            node = d.resolve_live(path)
            return len(node["revisions"]) if node else 0

    def drive_trash(self, path):
        with self.drive() as d:
            d.trash(d.resolve_live(path))

    def calls(self):
        log = self.drive_root / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def calls_of(self, command):
        return [c for c in self.calls() if c[1:2] == [command] or c[:2] == ["filesystem", command]]

    def clear_calls(self):
        log = self.drive_root / "calls.log"
        if log.exists():
            log.unlink()


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


class CommandLineTests(EzTestCase):
    def test_no_arguments_shows_help_and_does_nothing(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        result = self.ok()
        self.assertIn("Usage", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_help_and_version(self):
        self.assertIn("ezproton backup", self.ok("--help").stdout)
        self.assertRegex(self.ok("--version").stdout, r"ezproton \d+\.\d+")

    def test_unknown_command_is_an_error(self):
        result = self.run_ez("bakup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bakup", result.stderr)

    def test_unknown_option_is_an_error(self):
        self.assertEqual(self.run_ez("backup", "--dry").returncode, 1)
        self.assertEqual(self.run_ez("last", "--files").returncode, 1)

    def test_path_argument_must_match_a_configured_entry(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        (self.home / "Other").mkdir()
        result = self.run_ez("backup", str(self.home / "Other"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Documents", result.stderr)
        self.assertEqual(self.calls_of("upload"), [])

    def test_path_from_the_wrong_list_says_so(self):
        self.backup("~/Documents")
        self.sync("~/Music : /my-files/Music")
        self.mkfile("Documents/a.txt")
        self.mkfile("Music/song.mp3")
        result = self.run_ez("backup", str(self.home / "Music"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("sync", result.stderr.lower())

    def test_relative_path_argument_is_resolved_from_the_current_folder(self):
        self.backup("~/Documents", "~/Work")
        self.mkfile("Documents/a.txt")
        self.mkfile("Work/b.txt")
        self.ok("backup", ".", cwd=str(self.home / "Documents"))
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))
        self.assertFalse(self.drive_exists(f"{BACKUP_ROOT}/Work"))

    def test_double_dash_allows_paths_starting_with_a_dash(self):
        self.backup("~/-notes")
        self.mkfile("-notes/a.txt")
        self.ok("backup", "--", "-notes", cwd=str(self.home))
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/-notes/a.txt"))


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


class ConfigTests(EzTestCase):
    def test_comments_spaces_windows_line_endings_and_duplicates_are_ignored(self):
        self.write_config("backup", "  # comment\r\n\r\n   ~/Documents/   \r\n~/Documents\r\n")
        self.write_config("exclude", " node_modules \r\n#x\r\n")
        self.mkfile("Documents/a.txt")
        self.mkfile("Documents/node_modules/dep.js")
        self.ok("backup")
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))
        self.assertFalse(self.drive_exists(f"{BACKUP_ROOT}/Documents/node_modules"))

    def test_relative_paths_in_config_are_refused(self):
        self.backup("Documents")
        self.mkfile("Documents/a.txt")
        result = self.run_ez("backup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Documents", result.stderr)
        self.assertEqual(self.calls_of("upload"), [])

    def test_drive_path_must_be_inside_my_files(self):
        self.sync("~/Music : /Music")
        self.mkfile("Music/a.mp3")
        self.assertEqual(self.run_ez("sync").returncode, 1)

    def test_backup_root_must_be_inside_my_files(self):
        self.settings("backup-root = /Backups\n")
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        self.assertEqual(self.run_ez("backup").returncode, 1)

    def test_custom_separator(self):
        self.settings(f"backup-root = {BACKUP_ROOT}\nseparator = |\n")
        self.sync("~/Music | /my-files/Music")
        self.mkfile("Music/a.mp3", "song")
        self.ok("sync")
        self.assertEqual(self.drive_read("/my-files/Music/a.mp3"), "song")

    def test_escaped_separator_in_a_path(self):
        self.sync(r"~/Times 12\:30 : /my-files/Times")
        self.mkfile("Times 12:30/a.txt", "t")
        self.ok("sync")
        self.assertEqual(self.drive_read("/my-files/Times/a.txt"), "t")

    def test_invalid_separator_is_refused(self):
        self.settings("separator = /\n")
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        self.assertEqual(self.run_ez("backup").returncode, 1)

    def test_same_folder_in_backup_and_sync_is_refused(self):
        self.backup("~/Music")
        self.sync("~/Music : /my-files/Music")
        self.mkfile("Music/a.mp3")
        result = self.run_ez("backup")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls_of("upload"), [])

    def test_two_backup_folders_with_the_same_name_are_refused(self):
        self.backup("~/Work/notes", "~/School/notes")
        self.mkfile("Work/notes/a.txt")
        self.mkfile("School/notes/b.txt")
        result = self.run_ez("backup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("notes", result.stderr)

    def test_overlapping_folders_are_refused(self):
        self.backup("~/Documents")
        self.sync("~/Documents/School : /my-files/School")
        self.mkfile("Documents/School/a.txt")
        self.assertEqual(self.run_ez("sync").returncode, 1)

    def test_backup_root_inside_a_synced_drive_folder_is_refused(self):
        self.backup("~/Documents")
        self.sync("~/Stuff : /my-files/Backups")
        self.mkfile("Documents/a.txt")
        self.mkfile("Stuff/b.txt")
        self.assertEqual(self.run_ez("backup").returncode, 1)

    def test_overlapping_sync_drive_folders_are_refused(self):
        self.sync("~/Music : /my-files/Media", "~/Videos : /my-files/Media/Videos")
        self.mkfile("Music/a.mp3")
        self.mkfile("Videos/b.mp4")
        self.assertEqual(self.run_ez("sync").returncode, 1)

    def test_missing_local_folder_stops_before_changing_anything(self):
        self.backup("~/Documents", "~/Gone")
        self.mkfile("Documents/a.txt")
        result = self.run_ez("backup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Gone", result.stderr)
        self.assertEqual(self.calls_of("upload"), [])

    def test_exclude_line_with_a_slash_is_ignored_with_a_warning(self):
        self.backup("~/Documents")
        self.exclude("sub/a.txt")
        self.mkfile("Documents/sub/a.txt")
        result = self.ok("backup")
        self.assertIn("sub/a.txt", result.stderr)
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/Documents/sub/a.txt"))

    def test_hostname_default_for_backup_root(self):
        self.settings("")
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        self.ok("backup")
        host = os.uname().nodename
        self.assertTrue(self.drive_exists(f"/my-files/Backups/{host}/Documents/a.txt"))


# --------------------------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------------------------


class BackupTests(EzTestCase):
    def setUp(self):
        super().setUp()
        self.backup("~/Documents")
        self.root = f"{BACKUP_ROOT}/Documents"

    def test_first_backup_uploads_files_but_not_excluded_names_or_symlinks(self):
        self.exclude("node_modules", "*.iso")
        self.mkfile("Documents/a.txt", "A")
        self.mkfile("Documents/sub/b.txt", "B")
        self.mkfile("Documents/proj/node_modules/dep.js")
        self.mkfile("Documents/big.iso")
        os.symlink("/etc/hostname", self.home / "Documents/link")
        self.ok("backup")
        self.assertEqual(self.drive_read(f"{self.root}/a.txt"), "A")
        self.assertEqual(self.drive_read(f"{self.root}/sub/b.txt"), "B")
        self.assertTrue(self.drive_exists(f"{self.root}/proj"))
        self.assertFalse(self.drive_exists(f"{self.root}/proj/node_modules"))
        self.assertFalse(self.drive_exists(f"{self.root}/big.iso"))
        self.assertFalse(self.drive_exists(f"{self.root}/link"))

    def test_a_file_can_be_listed_instead_of_a_folder(self):
        self.backup("~/notes.md")
        self.mkfile("notes.md", "n")
        self.ok("backup")
        self.assertEqual(self.drive_read(f"{BACKUP_ROOT}/notes.md"), "n")

    def test_unchanged_files_are_not_uploaded_again(self):
        self.mkfile("Documents/a.txt", "A")
        self.ok("backup")
        self.clear_calls()
        self.ok("backup")
        self.assertEqual(self.calls_of("upload"), [])

    def test_changed_file_becomes_a_new_version(self):
        self.mkfile("Documents/a.txt", "v1")
        self.ok("backup")
        self.mkfile("Documents/a.txt", "version 2", mtime=time.time() + 5)
        self.ok("backup")
        self.assertEqual(self.drive_read(f"{self.root}/a.txt"), "version 2")
        self.assertEqual(self.drive_revisions(f"{self.root}/a.txt"), 2)

    def test_deleted_file_goes_to_drive_trash(self):
        self.mkfile("Documents/a.txt")
        self.mkfile("Documents/b.txt")
        self.ok("backup")
        (self.home / "Documents/a.txt").unlink()
        self.ok("backup")
        self.assertFalse(self.drive_exists(f"{self.root}/a.txt"))
        self.assertTrue(self.drive_exists(f"{self.root}/b.txt"))
        self.assertIn("a.txt", self.drive_trash_names())

    def test_deleted_folder_is_trashed_as_one_item(self):
        self.mkfile("Documents/old/x.txt")
        self.mkfile("Documents/old/y.txt")
        self.mkfile("Documents/keep.txt")
        self.ok("backup")
        shutil.rmtree(self.home / "Documents/old")
        self.ok("backup")
        self.assertEqual(self.drive_trash_names(), ["old"])
        self.assertTrue(self.drive_exists(f"{self.root}/keep.txt"))

    def test_own_mode_leaves_files_added_on_drive_alone(self):
        self.mkfile("Documents/a.txt")
        self.ok("backup")
        self.drive_put(f"{self.root}/added-by-hand.txt", "manual")
        self.ok("backup")
        self.assertEqual(self.drive_read(f"{self.root}/added-by-hand.txt"), "manual")

    def test_mirror_mode_trashes_files_added_on_drive(self):
        self.backup("~/Documents : mirror")
        self.mkfile("Documents/a.txt")
        self.ok("backup")
        self.drive_put(f"{self.root}/added-by-hand.txt", "manual")
        self.ok("backup")
        self.assertFalse(self.drive_exists(f"{self.root}/added-by-hand.txt"))
        self.assertIn("added-by-hand.txt", self.drive_trash_names())

    def test_mirror_mode_replaces_drive_content_that_differs(self):
        self.backup("~/Documents : mirror")
        self.mkfile("Documents/a.txt", "laptop")
        self.ok("backup")
        self.drive_put(f"{self.root}/a.txt", "changed on drive")
        self.ok("backup")
        self.assertEqual(self.drive_read(f"{self.root}/a.txt"), "laptop")

    def test_newly_excluded_file_is_trashed_on_drive(self):
        self.mkfile("Documents/a.txt")
        self.mkfile("Documents/b.log")
        self.ok("backup")
        self.exclude("*.log")
        self.ok("backup")
        self.assertFalse(self.drive_exists(f"{self.root}/b.log"))
        self.assertTrue(self.drive_exists(f"{self.root}/a.txt"))

    def test_unreadable_file_is_skipped_and_its_drive_copy_kept(self):
        self.mkfile("Documents/a.txt", "A")
        secret = self.mkfile("Documents/s.txt", "S")
        self.ok("backup")
        self.mkfile("Documents/s.txt", "S2", mtime=time.time() + 5)
        secret.chmod(0)
        result = self.run_ez("backup")
        self.assertEqual(result.returncode, 2)
        self.assertIn("s.txt", result.stdout + result.stderr)
        self.assertEqual(self.drive_read(f"{self.root}/s.txt"), "S")

    def test_upload_failure_is_partial_and_retried_next_run(self):
        self.mkfile("Documents/a.txt", "A")
        self.mkfile("Documents/bad.txt", "B")
        result = self.run_ez("backup", env={"FAKE_PD_FAIL": "bad.txt"})
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.drive_exists(f"{self.root}/bad.txt"))
        self.ok("backup")
        self.assertEqual(self.drive_read(f"{self.root}/bad.txt"), "B")

    def test_lost_state_reuploads_and_deletes_nothing(self):
        self.mkfile("Documents/a.txt")
        self.mkfile("Documents/b.txt")
        self.ok("backup")
        shutil.rmtree(self.state)
        (self.home / "Documents/b.txt").unlink()
        self.ok("backup")
        self.assertTrue(self.drive_exists(f"{self.root}/b.txt"))
        (self.home / "Documents/a.txt").unlink()
        self.ok("backup")
        self.assertFalse(self.drive_exists(f"{self.root}/a.txt"))

    def test_deletion_limit_stops_before_changing_anything(self):
        self.settings(f"backup-root = {BACKUP_ROOT}\ndelete-limit = 2\n")
        for n in range(4):
            self.mkfile(f"Documents/f{n}.txt")
        self.ok("backup")
        for n in range(4):
            (self.home / f"Documents/f{n}.txt").unlink()
        self.mkfile("Documents/new.txt")
        result = self.run_ez("backup")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--yes", result.stdout + result.stderr)
        self.assertTrue(self.drive_exists(f"{self.root}/f0.txt"))
        self.assertFalse(self.drive_exists(f"{self.root}/new.txt"))
        self.ok("backup", "--yes")
        self.assertFalse(self.drive_exists(f"{self.root}/f0.txt"))
        self.assertTrue(self.drive_exists(f"{self.root}/new.txt"))

    def test_offline_fails_before_doing_anything(self):
        self.mkfile("Documents/a.txt")
        result = self.run_ez("backup", env={"FAKE_PD_OFFLINE": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.drive_exists(f"{self.root}/a.txt"))

    def test_logged_out_mentions_how_to_log_in(self):
        self.mkfile("Documents/a.txt")
        result = self.run_ez("backup", env={"FAKE_PD_LOGGED_OUT": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("proton-drive auth login", result.stderr)

    def test_only_one_run_at_a_time(self):
        self.mkfile("Documents/a.txt")
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_ez("backup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already running", result.stderr)
        self.assertFalse(self.drive_exists(f"{self.root}/a.txt"))


# --------------------------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------------------------


class SyncTests(EzTestCase):
    def setUp(self):
        super().setUp()
        self.sync("~/Music : /my-files/Music")
        (self.home / "Music").mkdir()
        self.remote = "/my-files/Music"

    def first_sync(self):
        self.ok("sync")

    def test_first_sync_merges_both_ways_and_deletes_nothing(self):
        self.mkfile("Music/local.mp3", "L")
        self.drive_put(f"{self.remote}/remote.mp3", "R")
        self.ok("sync")
        self.assertEqual(self.drive_read(f"{self.remote}/local.mp3"), "L")
        self.assertEqual(self.read("Music/remote.mp3"), "R")
        self.assertEqual(self.drive_trash_names(), [])
        self.assertEqual(self.desktop_trash(), {})

    def test_first_sync_with_identical_files_transfers_nothing(self):
        self.mkfile("Music/same.mp3", "S")
        self.drive_put(f"{self.remote}/same.mp3", "S")
        self.clear_calls()
        self.ok("sync")
        self.assertEqual(self.calls_of("upload"), [])
        self.assertEqual(self.calls_of("download"), [])

    def test_first_sync_with_different_files_keeps_both(self):
        self.mkfile("Music/song.mp3", "laptop")
        self.drive_put(f"{self.remote}/song.mp3", "drive")
        self.ok("sync")
        self.assertEqual(self.read("Music/song.mp3"), "drive")
        conflicts = [p.name for p in (self.home / "Music").iterdir() if "conflict" in p.name]
        self.assertEqual(len(conflicts), 1)
        self.assertRegex(conflicts[0], r"^song \(conflict .+\)\.mp3$")
        self.assertEqual(self.read(f"Music/{conflicts[0]}"), "laptop")
        self.assertEqual(self.drive_read(f"{self.remote}/{conflicts[0]}"), "laptop")

    def test_local_change_is_uploaded(self):
        self.mkfile("Music/a.mp3", "v1")
        self.first_sync()
        self.mkfile("Music/a.mp3", "v2", mtime=time.time() + 5)
        self.ok("sync")
        self.assertEqual(self.drive_read(f"{self.remote}/a.mp3"), "v2")

    def test_drive_change_is_downloaded_with_its_modification_time(self):
        self.mkfile("Music/a.mp3", "v1")
        self.first_sync()
        stamp = 1700000000
        self.drive_put(f"{self.remote}/a.mp3", "v2 from phone", mtime=stamp)
        self.ok("sync")
        self.assertEqual(self.read("Music/a.mp3"), "v2 from phone")
        self.assertEqual(int((self.home / "Music/a.mp3").stat().st_mtime), stamp)
        self.assertIn("a.mp3", self.desktop_trash())

    def test_new_file_on_drive_is_downloaded(self):
        self.first_sync()
        self.drive_put(f"{self.remote}/sub/new.mp3", "N")
        self.ok("sync")
        self.assertEqual(self.read("Music/sub/new.mp3"), "N")

    def test_both_changed_differently_is_a_conflict(self):
        self.mkfile("Music/a.mp3", "v1")
        self.first_sync()
        self.mkfile("Music/a.mp3", "laptop edit", mtime=time.time() + 5)
        self.drive_put(f"{self.remote}/a.mp3", "drive edit")
        self.ok("sync")
        self.assertEqual(self.read("Music/a.mp3"), "drive edit")
        conflict = [p.name for p in (self.home / "Music").iterdir() if "conflict" in p.name]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(self.read(f"Music/{conflict[0]}"), "laptop edit")
        self.assertEqual(self.drive_read(f"{self.remote}/{conflict[0]}"), "laptop edit")

    def test_both_changed_to_the_same_content_transfers_nothing(self):
        self.mkfile("Music/a.mp3", "v1")
        self.first_sync()
        self.mkfile("Music/a.mp3", "same", mtime=time.time() + 5)
        self.drive_put(f"{self.remote}/a.mp3", "same")
        self.clear_calls()
        self.ok("sync")
        self.assertEqual(self.calls_of("upload"), [])
        self.assertEqual(self.calls_of("download"), [])

    def test_touched_but_unchanged_file_is_not_transferred(self):
        self.mkfile("Music/a.mp3", "v1")
        self.first_sync()
        os.utime(self.home / "Music/a.mp3", (time.time() + 9, time.time() + 9))
        self.clear_calls()
        self.ok("sync")
        self.assertEqual(self.calls_of("upload"), [])

    def test_local_deletion_goes_to_drive_trash(self):
        self.mkfile("Music/a.mp3")
        self.mkfile("Music/b.mp3")
        self.first_sync()
        (self.home / "Music/a.mp3").unlink()
        self.ok("sync")
        self.assertFalse(self.drive_exists(f"{self.remote}/a.mp3"))
        self.assertIn("a.mp3", self.drive_trash_names())
        self.assertTrue(self.drive_exists(f"{self.remote}/b.mp3"))

    def test_drive_deletion_goes_to_desktop_trash(self):
        self.mkfile("Music/a.mp3", "A")
        self.first_sync()
        self.drive_trash(f"{self.remote}/a.mp3")
        self.ok("sync")
        self.assertFalse((self.home / "Music/a.mp3").exists())
        trash = self.desktop_trash()
        self.assertIn("a.mp3", trash)
        self.assertTrue(trash["a.mp3"].endswith("/Music/a.mp3"))
        self.assertEqual((self.trash / "files" / "a.mp3").read_text(), "A")

    def test_deleted_on_one_side_changed_on_the_other_keeps_the_change(self):
        self.mkfile("Music/a.mp3", "v1")
        self.mkfile("Music/b.mp3", "v1")
        self.first_sync()
        (self.home / "Music/a.mp3").unlink()
        self.drive_put(f"{self.remote}/a.mp3", "changed on drive")
        self.drive_trash(f"{self.remote}/b.mp3")
        self.mkfile("Music/b.mp3", "changed on laptop", mtime=time.time() + 5)
        self.ok("sync")
        self.assertEqual(self.read("Music/a.mp3"), "changed on drive")
        self.assertEqual(self.drive_read(f"{self.remote}/b.mp3"), "changed on laptop")

    def test_deleted_folders_are_mirrored(self):
        self.mkfile("Music/album/1.mp3")
        self.mkfile("Music/album/2.mp3")
        self.drive_put(f"{self.remote}/other/3.mp3", "3")
        self.first_sync()
        shutil.rmtree(self.home / "Music/album")
        self.drive_trash(f"{self.remote}/other")
        self.ok("sync")
        self.assertEqual(self.drive_trash_names().count("album"), 1)
        self.assertFalse((self.home / "Music/other").exists())
        self.assertIn("other", self.desktop_trash())

    def test_new_folders_are_created_on_both_sides(self):
        self.first_sync()
        (self.home / "Music/empty-local").mkdir()
        with self.drive() as d:
            d.mkdir_path(f"{self.remote}/empty-drive")
        self.ok("sync")
        self.assertTrue(self.drive_exists(f"{self.remote}/empty-local"))
        self.assertTrue((self.home / "Music/empty-drive").is_dir())

    def test_excluded_names_are_ignored_on_both_sides(self):
        self.exclude("*.tmp")
        self.mkfile("Music/a.tmp")
        self.drive_put(f"{self.remote}/b.tmp", "b")
        self.ok("sync")
        self.assertFalse(self.drive_exists(f"{self.remote}/a.tmp"))
        self.assertFalse((self.home / "Music/b.tmp").exists())
        (self.home / "Music/a.tmp").unlink()
        self.ok("sync")
        self.assertTrue(self.drive_exists(f"{self.remote}/b.tmp"))

    def test_missing_drive_folder_after_first_sync_is_not_treated_as_deleted(self):
        self.mkfile("Music/a.mp3")
        self.first_sync()
        self.drive_trash(self.remote)
        result = self.run_ez("sync")
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.home / "Music/a.mp3").exists())

    def test_drive_folder_is_created_on_first_sync(self):
        self.mkfile("Music/a.mp3", "A")
        self.sync("~/Music : /my-files/Media/Laptop Music")
        self.ok("sync")
        self.assertEqual(self.drive_read("/my-files/Media/Laptop Music/a.mp3"), "A")

    def test_deletion_limit_counts_both_sides(self):
        self.settings(f"backup-root = {BACKUP_ROOT}\ndelete-limit = 2\n")
        for n in range(2):
            self.mkfile(f"Music/l{n}.mp3")
            self.mkfile(f"Music/r{n}.mp3")
        self.first_sync()
        for n in range(2):
            (self.home / f"Music/l{n}.mp3").unlink()
            self.drive_trash(f"{self.remote}/r{n}.mp3")
        result = self.run_ez("sync")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(self.drive_exists(f"{self.remote}/l0.mp3"))
        self.assertTrue((self.home / "Music/r0.mp3").exists())
        self.ok("sync", "--yes")
        self.assertFalse(self.drive_exists(f"{self.remote}/l0.mp3"))
        self.assertFalse((self.home / "Music/r0.mp3").exists())

    def test_sync_one_pair_by_path(self):
        self.sync("~/Music : /my-files/Music", "~/Videos : /my-files/Videos")
        self.mkfile("Music/a.mp3")
        self.mkfile("Videos/b.mp4")
        self.ok("sync", str(self.home / "Videos"))
        self.assertTrue(self.drive_exists("/my-files/Videos/b.mp4"))
        self.assertFalse(self.drive_exists("/my-files/Music/a.mp3"))

    def test_file_changed_during_the_run_is_left_for_next_time(self):
        # FAKE_PD_HOOK runs a shell command before every fake call, so a file can change
        # between ezproton's scan and its transfers.
        self.mkfile("Music/a.mp3", "v1")
        self.first_sync()
        self.mkfile("Music/a.mp3", "v2", mtime=time.time() + 5)
        target = self.home / "Music/a.mp3"
        hook = f"case \"$*\" in *upload*) printf v3 > '{target}'; touch -d '+60 seconds' '{target}';; esac"
        result = self.run_ez("sync", env={"FAKE_PD_HOOK": hook})
        self.assertIn(result.returncode, (0, 2))
        # The next run must settle on the newest content, without inventing a conflict.
        self.ok("sync")
        self.assertEqual(self.drive_read(f"{self.remote}/a.mp3"), "v3")
        self.assertEqual(self.read("Music/a.mp3"), "v3")
        self.assertEqual([p.name for p in (self.home / "Music").iterdir()], ["a.mp3"])


# --------------------------------------------------------------------------------------------
# Time limits (issue #1)
# --------------------------------------------------------------------------------------------


class TimeoutTests(EzTestCase):
    """EZPROTON_CALL_TIMEOUT lowers the limit for quick calls, so the tests don't wait for long."""

    def test_uploads_have_no_time_limit(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt", "A")
        self.ok("backup", env={"EZPROTON_CALL_TIMEOUT": "1", "FAKE_PD_SLOW": "upload=2.5"})
        self.assertEqual(self.drive_read(f"{BACKUP_ROOT}/Documents/a.txt"), "A")

    def test_downloads_have_no_time_limit(self):
        self.sync("~/Music : /my-files/Music")
        (self.home / "Music").mkdir()
        self.drive_put("/my-files/Music/a.mp3", "A")
        self.ok("sync", env={"EZPROTON_CALL_TIMEOUT": "1", "FAKE_PD_SLOW": "download=2.5"})
        self.assertEqual(self.read("Music/a.mp3"), "A")

    def test_a_hung_quick_call_times_out(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        started = time.time()
        result = self.run_ez("backup", env={"EZPROTON_CALL_TIMEOUT": "1", "FAKE_PD_SLOW": "info=5"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stderr)
        self.assertLess(time.time() - started, 4.5)


# --------------------------------------------------------------------------------------------
# check, preview, last
# --------------------------------------------------------------------------------------------


class InspectionTests(EzTestCase):
    def setUp(self):
        super().setUp()
        self.backup("~/Documents")
        self.sync("~/Music : /my-files/Music")
        self.mkfile("Documents/a.txt")
        self.mkfile("Music/song.mp3")

    def test_check_lists_entries_and_transfers_nothing(self):
        result = self.ok("check")
        self.assertIn("~/Documents", result.stdout)
        self.assertIn("~/Music", result.stdout)
        self.assertIn("/my-files/Music", result.stdout)
        self.assertEqual(self.calls_of("upload") + self.calls_of("download") + self.calls_of("trash"), [])

    def test_check_can_be_limited_to_one_feature(self):
        result = self.ok("check", "sync")
        self.assertIn("~/Music", result.stdout)
        self.assertNotIn("~/Documents", result.stdout)

    def test_check_reports_a_missing_folder(self):
        self.backup("~/Documents", "~/Gone")
        result = self.run_ez("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Gone", result.stdout + result.stderr)

    def test_preview_lists_actions_and_changes_nothing(self):
        self.drive_put("/my-files/Music/from-phone.mp3", "P")
        result = self.ok("preview")
        out = result.stdout
        self.assertRegex(out, r"upload.*a\.txt")
        self.assertRegex(out, r"upload.*song\.mp3")
        self.assertRegex(out, r"download.*from-phone\.mp3")
        self.assertFalse(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))
        self.assertFalse((self.home / "Music/from-phone.mp3").exists())
        self.assertEqual(self.calls_of("upload") + self.calls_of("download"), [])

    def test_preview_shows_trash_actions(self):
        self.ok("backup")
        self.ok("sync")
        (self.home / "Documents/a.txt").unlink()
        self.drive_trash("/my-files/Music/song.mp3")
        out = self.ok("preview").stdout
        self.assertRegex(out, r"trash.*Drive.*a\.txt|a\.txt.*trash.*Drive")
        self.assertRegex(out, r"trash.*(locally|desktop).*song\.mp3|song\.mp3.*trash.*(locally|desktop)")
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))

    def test_preview_excluded_lists_skipped_paths(self):
        self.exclude("*.log")
        self.mkfile("Documents/debug.log")
        out = self.ok("preview", "backup", "--excluded").stdout
        self.assertIn("debug.log", out)
        self.assertIn("*.log", out)

    def test_preview_warns_about_the_deletion_limit(self):
        self.settings(f"backup-root = {BACKUP_ROOT}\ndelete-limit = 1\n")
        self.mkfile("Documents/b.txt")
        self.ok("backup")
        (self.home / "Documents/a.txt").unlink()
        (self.home / "Documents/b.txt").unlink()
        result = self.run_ez("preview", "backup")
        self.assertIn("--yes", result.stdout + result.stderr)

    def test_last_reports_each_run(self):
        self.ok("backup")
        out = self.ok("last").stdout
        self.assertIn("~/Documents", out)
        self.assertIn("success", out.lower())
        self.assertIn("~/Music", out)
        self.assertIn("never", out.lower())

    def test_last_reports_a_partial_run_and_its_log(self):
        self.mkfile("Documents/bad.txt")
        self.run_ez("backup", env={"FAKE_PD_FAIL": "bad.txt"})
        out = self.ok("last", "backup").stdout
        self.assertIn("partial", out.lower())
        self.assertIn("bad.txt", out)
        self.assertIn("Transfer summary", self.ok("last", "backup", "--log").stdout)


# --------------------------------------------------------------------------------------------
# undo
# --------------------------------------------------------------------------------------------


class UndoTests(EzTestCase):
    def test_undo_backup_trashes_new_uploads_and_restores_trashed_files(self):
        self.backup("~/Documents")
        root = f"{BACKUP_ROOT}/Documents"
        self.mkfile("Documents/old.txt", "O")
        self.ok("backup")
        (self.home / "Documents/old.txt").unlink()
        self.mkfile("Documents/new.txt", "N")
        self.ok("backup")
        self.ok("undo", "last", "backup", "--yes")
        self.assertFalse(self.drive_exists(f"{root}/new.txt"))
        self.assertEqual(self.drive_read(f"{root}/old.txt"), "O")

    def test_undo_lists_new_versions_for_manual_restore(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt", "v1")
        self.ok("backup")
        self.mkfile("Documents/a.txt", "v2", mtime=time.time() + 5)
        self.ok("backup")
        result = self.run_ez("undo", "last", "backup", "--yes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("a.txt", result.stdout)
        self.assertIn("version", result.stdout.lower())

    def test_undo_sync_restores_laptop_files(self):
        self.sync("~/Music : /my-files/Music")
        self.mkfile("Music/a.mp3", "laptop v1")
        self.mkfile("Music/b.mp3", "B")
        self.ok("sync")
        self.drive_put("/my-files/Music/a.mp3", "drive v2")
        self.drive_trash("/my-files/Music/b.mp3")
        self.drive_put("/my-files/Music/new.mp3", "N")
        self.ok("sync")
        self.ok("undo", "last", "sync", "--yes")
        self.assertEqual(self.read("Music/a.mp3"), "laptop v1")
        self.assertEqual(self.read("Music/b.mp3"), "B")
        self.assertFalse((self.home / "Music/new.mp3").exists())

    def test_undo_sync_conflict_restores_the_original_name(self):
        self.sync("~/Music : /my-files/Music")
        self.mkfile("Music/a.mp3", "v1")
        self.ok("sync")
        self.mkfile("Music/a.mp3", "laptop edit", mtime=time.time() + 5)
        self.drive_put("/my-files/Music/a.mp3", "drive edit")
        self.ok("sync")
        self.ok("undo", "last", "sync", "--yes")
        self.assertEqual(self.read("Music/a.mp3"), "laptop edit")
        self.assertEqual([p.name for p in (self.home / "Music").iterdir()], ["a.mp3"])
        with self.drive() as d:
            names = [c["name"] for c in d.children(d.resolve_live("/my-files/Music"))]
        self.assertEqual(names, ["a.mp3"])

    def test_undo_skips_items_changed_since_the_run(self):
        self.sync("~/Music : /my-files/Music")
        (self.home / "Music").mkdir()
        self.ok("sync")
        self.drive_put("/my-files/Music/new.mp3", "N")
        self.ok("sync")
        self.mkfile("Music/new.mp3", "edited after", mtime=time.time() + 5)
        result = self.run_ez("undo", "last", "sync", "--yes")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.read("Music/new.mp3"), "edited after")

    def test_a_run_can_only_be_undone_once(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        self.ok("backup")
        self.ok("undo", "last", "backup", "--yes")
        self.assertEqual(self.run_ez("undo", "last", "backup", "--yes").returncode, 1)

    def test_undo_without_a_terminal_needs_yes(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        self.ok("backup")
        result = self.run_ez("undo", "last", "backup")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--yes", result.stderr)
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))

    def test_next_backup_after_undo_redoes_the_same_work(self):
        self.backup("~/Documents")
        self.mkfile("Documents/a.txt")
        self.ok("backup")
        self.ok("undo", "last", "backup", "--yes")
        self.assertFalse(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))
        self.ok("backup")
        self.assertTrue(self.drive_exists(f"{BACKUP_ROOT}/Documents/a.txt"))

    def _trash_with_a_same_named_blocker(self):
        """Trash essay.txt through a backup, with an older same-named item ahead of it in the trash."""
        self.backup("~/Documents")
        self.drive_put("/my-files/Elsewhere/essay.txt", "someone else's")
        self.drive_trash("/my-files/Elsewhere/essay.txt")
        self.mkfile("Documents/essay.txt", "mine")
        self.ok("backup")
        (self.home / "Documents/essay.txt").unlink()
        self.ok("backup")
        self.assertEqual(self.drive_trash_names(), ["essay.txt", "essay.txt"])

    def test_undo_with_a_same_named_item_ahead_defaults_to_manual(self):
        self._trash_with_a_same_named_blocker()
        result = self.run_ez("undo", "last", "backup", "--yes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("drive.proton.me", result.stdout)
        self.assertFalse(self.drive_exists(f"{BACKUP_ROOT}/Documents/essay.txt"))
        self.assertEqual(self.drive_trash_names(), ["essay.txt", "essay.txt"])

    def test_undo_can_move_same_named_items_aside(self):
        self._trash_with_a_same_named_blocker()
        code, out = self.run_tty("undo", "last", "backup", "--yes", answers=["s"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.drive_read(f"{BACKUP_ROOT}/Documents/essay.txt"), "mine")
        self.assertEqual(self.drive_trash_names(), ["essay.txt"])
        self.assertFalse(self.drive_exists("/my-files/Elsewhere/essay.txt"))

    def test_undo_can_permanently_delete_same_named_items_after_typing_delete(self):
        self._trash_with_a_same_named_blocker()
        code, out = self.run_tty("undo", "last", "backup", "--yes", answers=["d", "delete"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.drive_read(f"{BACKUP_ROOT}/Documents/essay.txt"), "mine")
        self.assertEqual(self.drive_trash_names(), [])

    def test_permanent_delete_needs_the_word_delete(self):
        self._trash_with_a_same_named_blocker()
        code, out = self.run_tty("undo", "last", "backup", "--yes", answers=["d", "yes"])
        self.assertEqual(code, 2, out)
        self.assertEqual(self.drive_trash_names(), ["essay.txt", "essay.txt"])


# --------------------------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------------------------


class EditTests(EzTestCase):
    def editor(self, content):
        """An 'editor' that replaces the file it's given with `content`."""
        script = self.tmp / f"editor-{len(list(self.tmp.glob('editor-*')))}.py"
        script.write_text(f"import sys\nopen(sys.argv[1], 'w').write({content!r})\n")
        return f"{sys.executable} {script}"

    def test_edit_saves_a_valid_file(self):
        self.ok("edit", "backup", "--editor", self.editor("~/Documents\n"))
        self.assertEqual((self.config / "backup").read_text(), "~/Documents\n")

    def test_edit_creates_a_missing_file_with_an_example(self):
        script = self.tmp / "keep.py"
        script.write_text("pass\n")
        self.ok("edit", "sync", "--editor", f"{sys.executable} {script}")
        text = (self.config / "sync").read_text()
        self.assertTrue(all(line.startswith("#") or not line.strip() for line in text.splitlines()))
        self.assertIn(":", text)

    def test_invalid_edit_can_be_discarded(self):
        self.backup("~/Documents")
        code, out = self.run_tty("edit", "backup", "--editor", self.editor("Documents\n"), answers=["d"])
        self.assertIn("Documents", out)
        self.assertEqual((self.config / "backup").read_text(), "~/Documents\n")

    def test_invalid_edit_can_be_kept_anyway(self):
        code, out = self.run_tty("edit", "backup", "--editor", self.editor("Documents\n"), answers=["k"])
        self.assertEqual((self.config / "backup").read_text(), "Documents\n")

    def test_editor_precedence(self):
        visual, editor = self.editor("~/FromVisual\n"), self.editor("~/FromEditor\n")
        self.ok("edit", "backup", env={"VISUAL": visual, "EDITOR": editor})
        self.assertEqual((self.config / "backup").read_text(), "~/FromVisual\n")
        self.ok("edit", "backup", env={"EDITOR": editor})
        self.assertEqual((self.config / "backup").read_text(), "~/FromEditor\n")
        self.ok("edit", "backup", "--editor", self.editor("~/FromFlag\n"), env={"VISUAL": visual})
        self.assertEqual((self.config / "backup").read_text(), "~/FromFlag\n")

    def test_edit_unknown_file_is_an_error(self):
        self.assertEqual(self.run_ez("edit", "folders", "--editor", "true").returncode, 1)


# --------------------------------------------------------------------------------------------
# README (issue #2)
# --------------------------------------------------------------------------------------------


class ReadmeTests(EzTestCase):
    def readme(self):
        return (HERE.parent / "README.md").read_text()

    def test_readme_is_no_longer_a_specification(self):
        text = self.readme().lower()
        self.assertNotIn("status: specification", text)
        self.assertNotIn("written before the code", text)

    def test_readme_names_the_tested_proton_drive_version(self):
        self.assertRegex(self.readme(), r"(?i)tested with[^\n]*0\.9\.0|0\.9\.0[^\n]*tested")

    def test_readme_covers_every_command_and_option_in_the_help(self):
        help_text, readme = self.ok("--help").stdout, self.readme()
        commands = set(re.findall(r"^  ezproton (\w+)", help_text, re.M))
        options = set(re.findall(r"(--[a-z][a-z-]+)", help_text))
        self.assertTrue(commands and options)
        for word in sorted(commands):
            self.assertIn(f"ezproton {word}", readme)
        for option in sorted(options):
            self.assertIn(option, readme)

    def test_readme_documents_behaviour_without_a_terminal(self):
        readme = self.readme()
        self.assertRegex(readme, r"(?s)`check`.{0,600}exits with 1")
        self.assertRegex(readme, r"(?s)Without a terminal.{0,200}`--yes`")
        self.assertRegex(readme, r"(?s)Without a terminal.{0,200}not saved")


# --------------------------------------------------------------------------------------------
# The fake proton-drive
# --------------------------------------------------------------------------------------------


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t if t is not None else time.time()))


class FakeDrive:
    """The fake Drive's database: a JSON file of nodes plus a folder of file contents."""

    def __init__(self, root):
        self.root = Path(root)
        self.db_path = self.root / "db.json"
        (self.root / "blobs").mkdir(exist_ok=True)

    def __enter__(self):
        self.lock = open(self.root / "db.lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        if self.db_path.exists():
            self.db = json.loads(self.db_path.read_text())
        else:
            self.db = {"seq": 0, "nodes": {"ROOT": self.node("ROOT", None, ROOT_NAME, "folder")}}
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.db_path.write_text(json.dumps(self.db))
        self.lock.close()

    # Nodes ------------------------------------------------------------------------------

    def next_seq(self):
        self.db["seq"] += 1
        return self.db["seq"]

    def node(self, uid, parent, name, kind):
        t = time.time()
        return {"uid": uid, "parent": parent, "name": name, "type": kind, "created": t, "modified": t,
                "trashed": None, "trash_seq": None, "revisions": []}

    def new_node(self, parent, name, kind):
        seq = self.next_seq()
        uid = "VOL~" + hashlib.sha1(f"node{seq}".encode()).hexdigest()[:22] + "=="
        node = self.node(uid, parent["uid"], name, kind)
        node["created_seq"] = seq
        self.db["nodes"][uid] = node
        return node

    def nodes(self):
        return self.db["nodes"].values()

    def is_live(self, node):
        while node is not None:
            if node["trashed"] is not None:
                return False
            node = self.db["nodes"].get(node["parent"]) if node["parent"] else None
        return True

    def children(self, folder):
        kids = [n for n in self.nodes() if n["parent"] == folder["uid"] and n["trashed"] is None]
        return sorted(kids, key=lambda n: n.get("created_seq", 0))

    def trashed_top(self):
        return sorted((n for n in self.nodes() if n["trashed"] is not None), key=lambda n: n.get("created_seq", 0))

    def descendants(self, node):
        out = []
        for n in list(self.nodes()):
            if n["parent"] == node["uid"]:
                out.append(n)
                out.extend(self.descendants(n))
        return out

    # Paths ------------------------------------------------------------------------------

    @staticmethod
    def split(path):
        parts, current, escape = [], "", False
        for ch in path.lstrip("/"):
            if escape:
                current += ch
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "/":
                parts.append(current)
                current = ""
            else:
                current += ch
        parts.append(current)
        return [p for p in parts if p != ""]

    def resolve_live(self, path, allow_trashed=False):
        """Resolve /my-files/... by names. Like proton-drive 0.9.0, a node UID works as a path
        segment at any depth (it finds that node wherever it is, even in the trash)."""
        parts = self.split(path)
        if not parts or parts[0] != ROOT_NAME:
            return None
        node = self.db["nodes"]["ROOT"]
        for part in parts[1:]:
            if part in self.db["nodes"] and part != "ROOT":
                node = self.db["nodes"][part]
                continue
            match = next((k for k in self.children(node) if k["name"] == part), None)
            if match is None:
                return None
            node = match
        if not allow_trashed and not self.is_live(node):
            return None
        return node

    def resolve_trash(self, path):
        parts = self.split(path)
        if len(parts) != 2 or parts[0] != "trash":
            return None
        return next((n for n in self.trashed_top() if n["name"] == parts[1]), None)

    def path_of(self, node):
        names = []
        while node is not None and node["uid"] != "ROOT":
            names.append(node["name"].replace("/", "\\/"))
            node = self.db["nodes"].get(node["parent"])
        return "/" + "/".join([ROOT_NAME] + names[::-1])

    def mkdir_path(self, path):
        node = self.db["nodes"]["ROOT"]
        for part in self.split(path)[1:]:
            match = next((k for k in self.children(node) if k["name"] == part), None)
            node = match or self.new_node(node, part, "folder")
        return node

    # Content ----------------------------------------------------------------------------

    def store(self, data):
        sha1 = hashlib.sha1(data).hexdigest()
        (self.root / "blobs" / sha1).write_bytes(data)
        return sha1

    def content(self, node):
        return (self.root / "blobs" / node["revisions"][-1]["sha1"]).read_bytes()

    def add_revision(self, node, data, mtime):
        seq = self.next_seq()
        node["revisions"].append({"uid": f"REV{seq}", "sha1": self.store(data), "size": len(data),
                                  "mtime": mtime if mtime is not None else time.time(), "created": time.time()})
        node["modified"] = time.time()

    def put_file(self, path, data, mtime=None):
        parts = self.split(path)
        parent = self.mkdir_path("/" + "/".join(parts[:-1]))
        node = next((k for k in self.children(parent) if k["name"] == parts[-1]), None)
        if node is None:
            node = self.new_node(parent, parts[-1], "file")
        self.add_revision(node, data, mtime)
        return node

    def trash(self, node):
        node["trashed"] = time.time()
        node["trash_seq"] = self.next_seq()

    # JSON output, shaped like proton-drive 0.9.0 ------------------------------------------

    def to_json(self, node):
        out = {
            "uid": node["uid"],
            "parentUid": node["parent"],
            "name": {"ok": True, "value": node["name"]},
            "type": node["type"],
            "creationTime": now_iso(node["created"]),
            "modificationTime": now_iso(node["modified"]),
            "trashTime": now_iso(node["trashed"]) if node["trashed"] is not None else None,
        }
        if node["type"] == "file" and node["revisions"]:
            rev = node["revisions"][-1]
            out["totalStorageSize"] = rev["size"]
            out["activeRevision"] = {
                "uid": rev["uid"],
                "state": "active",
                "creationTime": now_iso(rev["created"]),
                "storageSize": rev["size"],
                "claimedSize": rev["size"],
                "claimedModificationTime": now_iso(rev["mtime"]),
                "claimedDigests": {"sha1": rev["sha1"], "sha1Verified": False},
            }
        return out


OPTION_KEYS = {"-f": "f", "--file-conflict-strategy": "f", "-d": "d", "--folder-conflict-strategy": "d",
               "-t": "t", "--type": "t"}


def fake_main(argv):
    root = os.environ["FAKE_PD_ROOT"]
    with open(Path(root) / "calls.log", "a") as log:
        log.write(json.dumps(argv) + "\n")
    hook = os.environ.get("FAKE_PD_HOOK")
    if hook:
        subprocess.run(["sh", "-c", hook, "hook", *argv])
    for rule in filter(None, os.environ.get("FAKE_PD_SLOW", "").split(",")):
        command, _, seconds = rule.partition("=")
        if argv[1:2] == [command] or argv[:1] == [command]:
            time.sleep(float(seconds))
    if os.environ.get("FAKE_PD_OFFLINE"):
        print("Error: fetch failed (network unreachable)", file=sys.stderr)
        return 1
    if argv[:1] == ["version"]:
        print("Proton Drive CLI cli-drive@0.9.0+fake")
        return 0
    if os.environ.get("FAKE_PD_LOGGED_OUT"):
        print("You are not logged in. Run: proton-drive auth login", file=sys.stderr)
        return 1
    if len(argv) < 2 or argv[0] not in ("filesystem", "fs"):
        print(f"Command not found: {' '.join(argv)}", file=sys.stderr)
        return 1

    command, rest = argv[1], argv[2:]
    as_json = False
    options, args = {}, []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-j", "--json"):
            as_json = True
        elif arg in OPTION_KEYS:
            options[OPTION_KEYS[arg]] = rest[i + 1]
            i += 1
        elif arg == "--skip-thumbnails":
            pass
        else:
            args.append(arg)
        i += 1

    with FakeDrive(root) as d:
        return FAKE_COMMANDS[command](d, args, options, as_json)


def fake_info(d, args, options, as_json):
    path = args[0]
    node = d.resolve_trash(path) if path.startswith("/trash/") else d.resolve_live(path, allow_trashed=True)
    if node is None:
        print("Trashed node not found" if path.startswith("/trash/") else f"Node not found: {path}", file=sys.stderr)
        return 1
    print(json.dumps(d.to_json(node)) if as_json else f"{node['type']} {node['name']}")
    return 0


def fake_list(d, args, options, as_json):
    path = args[0]
    if path.rstrip("/") == "/trash":
        items = d.trashed_top()
    else:
        node = d.resolve_live(path)
        if node is None or node["type"] != "folder":
            print(f"Node not found: {path}", file=sys.stderr)
            return 1
        items = d.children(node)
    if as_json:
        print(json.dumps([d.to_json(n) for n in items]))
    else:
        for n in items:
            print(f"{'🗂️ ' if n['type'] == 'folder' else '📄 '} {n['name']}")
    return 0


def fake_create_folder(d, args, options, as_json):
    parent = d.resolve_live(args[0])
    if parent is None:
        print(f"Node not found: {args[0]}", file=sys.stderr)
        return 1
    if any(k["name"] == args[1] for k in d.children(parent)):
        print("A file or folder with that name already exists", file=sys.stderr)
        return 1
    print(json.dumps(d.to_json(d.new_node(parent, args[1], "folder")), indent=2))
    return 0


def fake_upload(d, args, options, as_json):
    *sources, target = args
    parent = d.resolve_live(target)
    if parent is None:
        print(f"Node not found: {target}", file=sys.stderr)
        return 1
    strategy = options.get("f")
    fail_pattern = os.environ.get("FAKE_PD_FAIL")
    stats = {"uploaded": 0, "bytes": 0, "skipped": 0, "failed": []}

    def upload_file(folder, path):
        name = os.path.basename(path)
        if fail_pattern and fnmatch.fnmatch(name, fail_pattern):
            stats["failed"].append(f"{name}: Error: simulated upload failure")
            return
        try:
            data = Path(path).read_bytes()
        except OSError as error:
            stats["failed"].append(f"{name}: Error: EACCES: permission denied, open '{path}' ({error.strerror})")
            return
        mtime = os.stat(path).st_mtime
        existing = next((k for k in d.children(folder) if k["name"] == name), None)
        if existing is not None and existing["type"] == "file":
            if existing["revisions"] and existing["revisions"][-1]["sha1"] == hashlib.sha1(data).hexdigest():
                stats["skipped"] += 1
                return
            if strategy == "skip":
                stats["skipped"] += 1
                return
            if strategy == "replace":
                d.trash(existing)
                existing = None
            elif strategy != "create-new-revision":
                stats["failed"].append(f"{name}: Error: conflict, no strategy")
                return
        node = existing or d.new_node(folder, name, "file")
        d.add_revision(node, data, mtime)
        stats["uploaded"] += 1
        stats["bytes"] += len(data)
        print(f"  - {name}")

    def upload_dir(folder, path):
        name = os.path.basename(path.rstrip("/"))
        existing = next((k for k in d.children(folder) if k["name"] == name and k["type"] == "folder"), None)
        if existing is None:
            existing = d.new_node(folder, name, "folder")
            stats["uploaded"] += 1
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if os.path.islink(full):
                continue
            (upload_dir if os.path.isdir(full) else upload_file)(existing, full)

    for source in sources:
        if os.path.isdir(source) and not os.path.islink(source):
            upload_dir(parent, source)
        elif os.path.exists(source):
            upload_file(parent, source)
        else:
            stats["failed"].append(f"{os.path.basename(source)}: Error: ENOENT: no such file or directory")

    print("Transfer summary:")
    print(f"  Uploaded: {stats['uploaded']} items ({stats['bytes']} B)")
    if stats["skipped"]:
        print(f"  Skipped: {stats['skipped']} items")
    if stats["failed"]:
        print(f"  Failed: {len(stats['failed'])} items")
        for line in stats["failed"]:
            print(f"  - {line}")
        print(f"{len(stats['failed'])} item(s) failed to upload")
        return 1
    return 0


def fake_download(d, args, options, as_json):
    *sources, target = args
    strategy = options.get("f", "skip")
    if strategy not in ("rename", "remove", "skip"):
        print(f'Invalid conflict strategy "{strategy}". Expected one of: rename, remove, skip', file=sys.stderr)
        return 1
    count = 0

    def write(node, folder):
        nonlocal count
        dest = os.path.join(folder, node["name"])
        if node["type"] == "folder":
            os.makedirs(dest, exist_ok=True)
            for kid in d.children(node):
                write(kid, dest)
            return
        if os.path.exists(dest):
            if strategy == "skip":
                return
            if strategy == "rename":
                dest += " (1)"
        Path(dest).write_bytes(d.content(node))
        count += 1

    for source in sources:
        node = d.resolve_live(source)
        if node is None:
            print(f"Node not found: {source}", file=sys.stderr)
            return 1
        write(node, target)
    print(f"Transfer summary:\n  Downloaded: {count} items")
    return 0


def fake_trash(d, args, options, as_json):
    nodes = []
    for path in args:
        node = d.resolve_live(path)
        if node is None:
            print(f"Node not found: {d.split(path)[-1]}", file=sys.stderr)
            return 1
        nodes.append(node)
    for node in nodes:
        d.trash(node)
        print(f"✅ {node['name']}")
    return 0


def fake_restore(d, args, options, as_json):
    status = 0
    for path in args:
        if not path.startswith("/trash/"):
            print(f'Path "{path}" is not supported', file=sys.stderr)
            return 1
        node = d.resolve_trash(path)
        if node is None:
            print("Trashed node not found", file=sys.stderr)
            return 1
        parent = d.db["nodes"].get(node["parent"])
        if parent is None or not d.is_live(parent):
            print(f"❌ {node['name']}: Parent folder not found")
            status = 1
            continue
        if any(k["name"] == node["name"] for k in d.children(parent)):
            print(f"❌ {node['name']}: A file or folder with that name already exists")
            status = 1
            continue
        node["trashed"] = None
        node["trash_seq"] = None
        print(f"✅ {node['name']}")
    return status


def fake_delete(d, args, options, as_json):
    for path in args:
        node = d.resolve_trash(path)
        if node is None:
            print("Trashed node not found", file=sys.stderr)
            return 1
        for n in [node] + d.descendants(node):
            d.db["nodes"].pop(n["uid"], None)
        print(f"✅ {node['name']}")
    return 0


FAKE_COMMANDS = {
    "info": fake_info,
    "list": fake_list,
    "create-folder": fake_create_folder,
    "upload": fake_upload,
    "up": fake_upload,
    "download": fake_download,
    "trash": fake_trash,
    "restore": fake_restore,
    "delete": fake_delete,
}


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-proton-drive"]:
        sys.exit(fake_main(sys.argv[2:]))
    unittest.main()
