# ezproton

Back up and sync folders on a Linux computer with Proton Drive, using Proton's official
command-line tool ([`proton-drive`](https://proton.me/support/drive-cli)).

- **Backup:** a one-way copy of chosen folders into one Drive folder (by default
  `My files › Backups › <hostname>`). Deleting a file on the computer deletes it from the backup too.
- **Sync:** two-way pairs. Each laptop folder is kept in sync with its own Drive folder, and the
  pairs can map to any place on Drive (`~/School` ↔ `/my-files/Cégep/Session 3`).

Everything ends up as normal files on Drive, so you can browse and download them on
drive.proton.me or in Proton's apps. Anything ezproton removes goes to a trash (Drive's trash, or
your desktop trash), never straight to permanent deletion.

> **Unofficial.** ezproton is not made by or affiliated with Proton.

---

## Install

```sh
git clone https://github.com/RubyDevil/ezproton.git
cd ezproton
./install.sh            # for everyone: installs into /usr/local/bin (asks for your sudo password)
./install.sh --user     # just for you: installs into ~/.local/bin (no sudo)
```

Or without cloning:

```sh
curl -fsSL https://raw.githubusercontent.com/RubyDevil/ezproton/main/install.sh | bash -s -- --user
```

After installing, `ezproton` is a normal command you can run from any folder. Check it with
`ezproton --version`.

The installer:

- copies `ezproton` into the chosen folder as an executable named `ezproton`, with no `.py`. It's
  fetched from GitHub when the installer isn't run from a clone.
- installs Proton's `proton-drive` into the same folder, unless an equal or newer version is
  already there. It installs the newest stable release listed in Proton's `version.json`, checked
  against the SHA-512 published there. If that fails, it installs the release pinned in
  `install.sh`, checked against the SHA-512 recorded in this repository. Add `--no-proton-drive`
  to skip this step.
- warns if the folder isn't on your `PATH`, which can happen with `~/.local/bin`.

**Update:** run the installer again. It replaces `ezproton` and updates `proton-drive` if a newer
stable release exists.

**Uninstall:** `./install.sh --uninstall`, or `./install.sh --uninstall --user`. This removes
`ezproton`. Your settings, your Drive files and `proton-drive` are left alone. The installer
prints how to remove those too.

### Requirements

- Proton's `proton-drive` command-line tool. The installer installs it. ezproton is tested with
  `proton-drive` 0.9.0.
- Linux with Python 3.9 or newer. Every current desktop distribution has it, and ezproton uses
  only Python's built-in modules.
- Standard command-line tools (coreutils), and `curl` for the installer.
- A Proton account with Drive.
- Files ezproton removes from the computer go to your desktop trash, the standard one your file
  manager shows. ezproton writes it itself (the freedesktop.org trash standard), so it knows
  where each file went for [undo](#undo).

### First steps

```sh
proton-drive auth login     # once: opens a browser to sign in (2FA included)
ezproton edit backup        # list folders to back up
ezproton edit sync          # list folder pairs to sync
ezproton check              # see that everything is set up right
ezproton preview            # see what a run would do, without changing anything
ezproton backup             # back up
ezproton sync               # sync
```

The Proton login is stored in your desktop keyring.

---

## Commands

```
ezproton backup  [PATH...] [--yes] [--quiet]
ezproton sync    [PATH...] [--yes] [--quiet]
ezproton check   [backup|sync] [PATH...]
ezproton preview [backup|sync] [PATH...] [--excluded]
ezproton last    [backup|sync] [PATH...] [--log]
ezproton undo last [backup|sync] [PATH...] [--yes] [--quiet]
ezproton edit    [backup|sync|exclude|settings] [--editor COMMAND]
ezproton --help | --version
```

Without `backup` or `sync`, `check`, `preview`, `last` and `undo` cover both. Without `PATH`, a
command covers everything configured. `ezproton` on its own shows the help and does nothing
else.

| Command | What it does |
|---|---|
| `backup` | Runs the backup. |
| `sync` | Runs the sync. |
| `check` | Shows what's set up, and changes nothing. One line per backup folder: whether it exists, file count, size, excluded paths, its Drive folder, and the last backup. One line per sync pair: both folders, whether each exists, and the last sync. Before those it shows the login status and any config problems. It exits with 1 when it finds a problem (such as a missing folder), so scripts can rely on it. |
| `preview` | A dry run. It reads Drive but changes nothing. Lists exactly what `backup` or `sync` would do: uploads, downloads, conflicts, and what would go to which trash. Warns if the deletion limit would be hit. `--excluded` also lists every skipped path and why. |
| `last` | How the last run went for each folder or pair: the result, when, how long, what was transferred, and what was skipped. `--log` prints the full output of that run. |
| `undo last` | Reverses the last run, as far as possible (see [Undo](#undo)). It shows what it will do and asks first, unless `--yes` is given. Without a terminal to ask in, `--yes` is required. |
| `edit` | Opens a config file in a terminal editor (see [Editing the config](#editing-the-config)). With no file named, it shows a short menu (in a terminal). |

**`PATH` arguments** must match a configured backup folder, or the laptop folder of a sync pair.
Relative paths work and are resolved from the current folder, so `cd ~/Music && ezproton sync .`
works. A path that matches nothing is an error that lists what's configured. A path from the wrong
list is an error that says so, for example `ezproton backup ~/Music` when `~/Music` is a sync pair.
`--` ends the options, so the path arguments after it may start with `-`.

**Progress:** in a terminal, `backup`, `sync`, `preview` and `undo` show each step as it happens,
with progress bars where the total is known:

```
sync ~/Music  →  /my-files/Music: checking
  Scanning laptop   [████████████████████] 12340 files
  Listing Drive     [██████████░░░░░░░░░░] 248/500 folders  ·  1 min 10 s left
sync ~/Music: running
  Uploading         [███████░░░░░░░░░░░░░] 1.2 GiB / 3.4 GiB  ·  14/37 files  ·  2 min 5 s left
```

`proton-drive` sends a whole batch of files in one go, so a bar moves when a batch finishes;
in between, the elapsed time keeps counting. Without a terminal (a timer, or `| tee`), each step
gets one plain line instead, with no bars or redrawing. **`--quiet`** shows only errors and the
final summary.

**`--yes`** lets a run go ahead when it would delete more than the deletion limit (see
[Safety](#safety)).

---

## Configuration

All config files live in `~/.config/ezproton/`. They are plain text, one entry per line. In all of
them:

- blank lines and lines starting with `#` are ignored
- spaces around entries are ignored
- Windows line endings are ignored
- repeated lines are ignored

Paths in config files must start with `/` or `~/`. Relative paths are refused there, so the
result doesn't depend on where ezproton runs.

### `backup`: folders to back up

```
# folder        [: mode]
~/Documents
~/Work          : mirror
/srv/projects
```

Each folder is copied to `<backup root>/<folder name>` on Drive: `~/Documents` goes to
`/my-files/Backups/<hostname>/Documents`. A file can be listed too, such as `~/notes.md`.

The optional mode decides what may be deleted from that folder's backup:

| Mode | Behaviour |
|---|---|
| `own` (default) | Only files and folders that ezproton uploaded are moved to Drive's trash when they're gone from the computer. Anything else in that Drive folder is left alone. |
| `mirror` | The Drive folder is made to match the computer exactly. Anything that isn't on the computer goes to Drive's trash, even files added by hand. Drive files whose content differs from the computer's are replaced. This needs a scan of the Drive folder on every run, which is slower. |

### `sync`: folder pairs to keep in sync

```
# laptop folder : Drive folder
~/Music         : /my-files/Music
~/Pictures      : /my-files/Photos/Laptop
~/School        : /my-files/Cégep/Session 3
```

Drive folders must be inside `/my-files`. They're created if missing.

### `exclude`: names to skip

```
node_modules
.venv
*.iso
```

Names or wildcards (`*`, `?`, `[...]`) matched against file and folder names, anywhere inside
backup folders and sync pairs. These are names, not paths, so a line containing `/` is ignored
with a warning. An excluded name is never uploaded. In sync it's never downloaded, and it's never
deleted on either side.

### `settings`

```
backup-root  = /my-files/Backups/<hostname>   # Drive folder that backups go into
separator    = :                              # separator used in the backup and sync files
delete-limit = 50                             # most deletions a run may make without --yes
```

All of them are optional. `<hostname>` is replaced with the computer's hostname, so renaming the
computer starts a new backup folder unless `backup-root` is set to a fixed path.

**`separator`** splits the fields in `backup` and `sync` lines. It defaults to `:`. Windows doesn't
allow `:` in names, and it's rare in Linux folder names. If a configured path contains the
separator, write it as `\:` (backslash, then the separator), or choose another separator. It can't
be empty, whitespace, `/`, `~` or `#`.

### Rules checked before every run

A run stops before changing anything if:

- a path doesn't start with `/` or `~/`, or a Drive path isn't inside `/my-files`
- the same folder is listed in both `backup` and `sync`
- two backup folders have the same name, such as `~/Work/notes` and `~/School/notes`, so they
  would share one Drive folder
- folders overlap: a backup folder or sync pair inside another, on the laptop or on Drive. This
  includes the backup root inside a synced Drive folder, or a synced Drive folder inside the
  backup root.
- a listed laptop folder doesn't exist. A missing folder never counts as "everything was
  deleted".

### Editing the config

`ezproton edit <file>` opens the file in an editor, chosen in this order:

1. `--editor COMMAND`
2. `$VISUAL`
3. `$EDITOR`
4. the first found of `nvim`, `vim`, `nano`, `vi`

A missing file is created with a commented example. You edit a temporary copy. When the editor
closes, the copy is checked against the rules above. If there are problems, ezproton lists them
and asks: **e**dit again, **k**eep anyway, or **d**iscard. The real file only changes once you
accept. Without a terminal to ask in, a file with problems is not saved.

---

## How backup works

**The backup folder on Drive is a one-way copy of your computer. Don't add or change files in it
by hand:** a `mirror` folder deletes them on the next run, and an `own` folder may replace them.
Keep anything you add by hand elsewhere on Drive, or in a sync pair.

Each run, for each backup folder:

| On the computer | On Drive |
|---|---|
| New file | Uploaded |
| Changed file (different size or modification time) | Uploaded as a new version. Drive keeps the old versions. |
| Unchanged file | Nothing. It isn't read again. |
| Deleted file or folder | Moved to Drive's trash. In `own` mode, only if ezproton uploaded it. |
| File that now matches an `exclude` rule | Moved to Drive's trash, because it's no longer part of the backup |
| File that can't be read | Skipped and reported. The copy already on Drive is kept. |
| Symlink | Skipped |

ezproton remembers what it uploaded, in `~/.local/state/ezproton/`. If that record is lost, the
next run uploads again (Proton skips content it already has) and deletes nothing, then carries on
normally.

---

## How sync works

ezproton remembers what both sides of a pair looked like after the last sync. Each run compares
the laptop and Drive against that memory:

| Since the last sync | What sync does |
|---|---|
| Changed or new on the laptop only | Uploads it. On Drive it becomes a new version. |
| Changed or new on Drive only | Downloads it. The laptop's old copy goes to the desktop trash. |
| Changed on both, to the same content | Nothing to transfer |
| Changed on both, differently | **Conflict.** Both copies are kept: the laptop's copy is renamed `name (conflict <hostname> <date>).ext`, and both copies end up on both sides. |
| Deleted on the laptop | Moved to Drive's trash |
| Deleted on Drive | The laptop copy is moved to the desktop trash |
| Deleted on one side, changed on the other | The changed copy is kept and copied back |
| Folder created or deleted | The same happens on the other side |

- **First sync of a pair:** it only merges. Whatever is missing on either side is copied over,
  identical files are matched up, and files with the same name but different content become
  conflicts. Nothing is deleted.
- **Content check:** files are compared by size and modification time first. When those differ,
  by SHA-1, the fingerprint Drive stores for every file. So a file touched without being changed
  isn't transferred.
- **Downloads** keep the original modification time.
- **Excluded names and symlinks** are ignored on both sides.

---

## Undo

`ezproton undo last [backup|sync] [PATH...]` reverses the most recent run of each backup folder or
sync pair it covers, using the record that run left in `~/.local/state/ezproton/`.

- It lists what it will do and asks before starting. `--yes` skips the question, but not the
  prompts described below. Without a terminal to ask in, `--yes` is required.
- A run can be undone once. An undo can't be undone.
- **Anything changed since the run is left alone.** If you edited a downloaded file afterwards, or
  a newer run already touched a file, that item is skipped and reported. Undo never throws away
  newer work.

| The run did this | Undo does this |
|---|---|
| Uploaded a new file or folder | Moves it to Drive's trash |
| Moved something to Drive's trash | Restores it to its original folder (see below) |
| Uploaded a new version of a file | **Can't be reversed by ezproton.** `proton-drive` has no command for older versions. Undo lists these files, so you can pick the previous version in the file's version history on drive.proton.me. |
| Downloaded a file over a laptop file | Puts the old laptop copy back from the desktop trash |
| Downloaded a new file | Moves it to the desktop trash |
| Moved a laptop file to the desktop trash | Restores it |
| Created a conflict copy | Removes the extra copy on both sides, to the trashes, and restores the original names |

### Restoring from Drive's trash

`proton-drive` restores trashed items by name only, so ezproton records each trashed item's ID
and checks it before restoring:

- **Our item is first in line for its name:** restored. That's always the case when no other
  trashed item has the same name.
- **Other trashed items with the same name are ahead of it:** the order isn't predictable, so
  ezproton asks:

  ```
  3 other files named "essay.docx" are in the Drive trash ahead of yours.
    [m] I'll restore it myself on drive.proton.me   (default)
    [s] Move the others aside, restore mine, then put them back in the trash
    [d] Permanently delete the others, then restore mine
  ```

  - `[s]` restores the others to their original folders one at a time until ours is first,
    restores ours, then puts the others back in the trash. Nothing is lost, but those files
    reappear in their folders for a few seconds, so Proton apps on other devices may briefly
    sync them. If one of them can't be restored, for example because its folder is gone or the
    name is taken, ezproton stops and leaves the rest to you.
  - `[d]` lists the other items with their dates and sizes, and only goes ahead if you type
    `delete`. Those items are gone for good.
  - Without a terminal to ask in (for example a timer), the answer is `[m]`.
- **A file with that name is already back in the original folder:** the restore is refused and
  reported. Nothing is overwritten.

---

## Safety

- **Nothing is permanently deleted.** Drive files go to Drive's trash, and laptop files go to the
  desktop trash. Restore from drive.proton.me or your file manager. Drive's trash counts toward
  your storage until you empty it.
- **Deletion limit:** a run that would delete more than `delete-limit` items in total (default 50)
  stops before changing anything. Run `ezproton preview` to see the list, then repeat with `--yes`.
- **The login is checked first,** so a logged-out or offline run fails before doing anything.
- **One run at a time:** a second ezproton run while one is working stops right away.
- **Temporary files:** sync downloads into a hidden `.ezproton-tmp` folder inside the pair, then
  moves each file into place. If a sync is killed before it can clean up, the next sync removes
  the leftover folder, and `check` points it out in the meantime.
- **Files changed during a run** (between scanning and transferring) are left for the next run.
- **Interrupted runs:** on Ctrl-C or shutdown, ezproton abandons the current transfer and saves
  what it finished, so `last` shows the run as interrupted and `undo last` can reverse it. Even if
  ezproton is killed outright, each finished action is already on disk. A second Ctrl-C quits at
  once.
- **Partial runs:** if some files fail, the rest still go through. The failures are reported, and
  nothing is deleted because of them.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error or usage error. Nothing was changed, or the run stopped early. |
| 2 | Partial: some files were skipped or failed, or an undo left items for you to finish |
| 3 | Stopped by the deletion limit. Repeat with `--yes` to go ahead. |
| 130 | Interrupted (Ctrl-C, or the system shutting down). What finished is saved. |

---

## Running it automatically

ezproton doesn't schedule itself. For example, with a systemd user timer:

`~/.config/systemd/user/ezproton.service`
```ini
[Unit]
Description=ezproton backup and sync

[Service]
Type=oneshot
ExecStart=ezproton backup --quiet
ExecStart=ezproton sync --quiet
```

`~/.config/systemd/user/ezproton.timer`
```ini
[Unit]
Description=Daily ezproton run

[Timer]
OnCalendar=daily
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user enable --now ezproton.timer
```

A run stopped by the deletion limit exits with code 3 and waits for you to run it with `--yes`.

---

## Good to know

- **Speed:** listing a Drive folder takes about 3 seconds, so ezproton lists many folders in
  parallel. Large libraries still take a while on the first sync, and the first upload of video
  and music collections takes as long as your connection needs.
- **No time limit on transfers:** uploads and downloads take as long as your connection needs.
  Quick calls to `proton-drive` (listing, checking, trashing) count as hung after 300 seconds;
  change that with the `EZPROTON_CALL_TIMEOUT` environment variable.
- **Not kept:** permissions, ownership and symlinks. This is a file backup, not a system image.
- **The Proton login** in your keyring can reach your whole Drive, and so can anything running as
  your user. Revoke it with `proton-drive auth logout`, or in your Proton account's session list.
- **`proton-drive` is young** (0.x). If an update changes its behaviour, ezproton fails with an
  error rather than reporting success. Tested with 0.9.0.
- **Your files:** ezproton writes config to `~/.config/ezproton/`, its records to
  `~/.local/state/ezproton/`, and nothing else outside your folders and your Drive.

---

## Development

```sh
python3 -m unittest discover tests
```

The tests use a fake `proton-drive` that keeps its "Drive" in a temporary folder. They never touch
a real Proton account.
