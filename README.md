# Omago

Omago maintains a portable **engagement experience (EX)** across existing Omarchy
machines. EX includes how humans and agents use the system: applications,
configuration, launchers, skills, plugins, and selected home-directory files.
GitHub stores the desired EX; each machine retains its hardware setup and any
exceptions you explicitly choose.

This is an MVP, not a disk clone or a full backup system. Read the coverage and
limitations below before depending on it for a migration.

## Install

Requires Python 3.10+, Git, an existing compatible Arch/Omarchy installation,
and access to your EX repository. Package installation may require sudo. Run
Omago as your regular user. Set your Git commit identity before first capture.

```sh
git clone git@github.com:rowama/omago.git ~/Projects/omago
cd ~/Projects/omago
python3 install.py --root ~/omago
cd ~/omago
omago --update
```

`~/.local/bin` must be on PATH. Otherwise use `./omago --update` from the install
directory. The installer copies the executable and its module, creates a PATH
symlink, and initializes local settings. It does **not** capture or publish EX.
The default EX remote is `git@github.com:rowama/omago-ex.git`.

If SSH is not configured but `gh auth status` succeeds, Omago uses authenticated
HTTPS via the GitHub CLI for the EX repository. The stored origin remains the
requested SSH URL. No global Git or SSH settings are changed. For the initial
application clone you can alternatively use `gh repo clone rowama/omago`.

To install elsewhere or use a different state repository:

```sh
python3 install.py --root /your/location/omago --remote git@github.com:you/your-ex.git
```

Re-running the installer upgrades the executable and preserves settings, local
exceptions, the state checkout, and backups. Use `--bin-dir` to choose a different
command directory. Remove the old PATH symlink yourself before switching it to a
different installation. An explicit `--root` on the command line overrides the
launcher default; the source executable also accepts `OMAGO_DIR`.

## First run: empty GitHub repository

`omago --update` detects an empty remote, inventories portable packages and
configuration, and asks before publishing the initial
snapshot. It writes an omissions report at `~/omago/report.json` for review.
An existing nonempty repository without an Omago manifest is rejected.

## Subsequent runs and another machine

Install Omago on the other Omarchy system and run the same command. It fetches
and fast-forwards the EX checkout, installs missing applications, restores absent
configuration.

When a configuration file already exists and differs on a new machine, choose:

* **Use GitHub:** back up the local file, then apply desired EX.
* **Publish local:** make this machine's version the desired EX for everyone.
* **Keep local:** retain a machine exception. Ask again if GitHub's version changes.

After a file has been matched, remote-only changes apply automatically with a
backup. Local edits and concurrent edits require a choice. File deletion requires
a choice; no package is uninstalled automatically. Extra local applications can
be added to EX in a batch or selected individually. New local config files are
offered by directory; their choices are separate from application membership.
Run `omago --reconsider` to clear remembered exceptions and ignored apps,
then `omago --update` to reconsider them. New files inside a locally excluded
directory may prompt again; exceptions are stored per file.

All modifications are incremental. If an install or push fails, completed local
work remains. Fix the reported issue and rerun. Backups are at
`~/omago/backups/<UTC timestamp>/<home-relative path>`; restore a chosen file by
copying it back while its application is stopped. Backups never go into GitHub.
Some applications need restarting or a logout/login before restored settings
take effect. Keep both machines on compatible Omarchy versions.

## What the MVP covers

| Area | Behavior |
| --- | --- |
| Arch packages | Explicitly installed packages from `pacman -Qqe`; installed dependencies satisfy requests too |
| Foreign packages | Recorded separately, installed through `yay`; not every foreign package exists in AUR |
| Flatpak | App ID, origin name, branch, and user/system scope; remote must already be configured |
| VS Code | Extension IDs when `code` is available |
| mise | Global configuration is captured; subsequent update offers `mise install` to restore declared tools |
| Omarchy | Portable settings, custom plugin source, themes, hooks, launchers, terminal/editor settings |
| Agents | Selected skills, commands, rules, settings, and available plugin manifests |
| Files | Contents, executable/permission bits, safe symlinks; home paths rewritten for the target user |

Explicit Arch package membership is desired state, **not a version lock**.
Packages and dependencies are installed from the target's configured sources;
Omago does not change pacman repositories or perform a whole-system upgrade.
Keep the target updated with normal Omarchy tools. Third-party repositories,
missing AUR packages, Flatpak remotes, and proprietary installers may need manual
setup. AUR package builds retain their normal interactive prompts.

## Scope, privacy, and gaps

The default capture scans `~/.config`, selected shell dotfiles, local launchers,
fonts, desktop entries, and selected agent paths. It deliberately excludes
browser profiles, passwords/tokens, keyrings, application databases, caches,
history, hardware settings, driver/kernel/boot packages, and `/etc`. Display and
device configuration (`hypr/monitors*`, `hypr/input*`, etc.) stays on each target.
If a portable file embeds hardware directives, the whole file is omitted and
reported: split those settings into a separate machine-local file.

Credentials are excluded by path and a conservative content heuristic. This is
not a guarantee that an arbitrary secret cannot be included. Use a **private EX
repository**, review the report and capture policy, and audit the state before
sharing it. Omago does not encrypt Git contents. A matching token pattern causes
the entire file to be omitted, never logged. If a secret is ever committed,
rotate it: deleting a file does not erase Git history.

Documents, notes/vaults, agent conversations/memory, browser bookmarks and
extensions, dconf settings, user services, keybindings mixed with device settings,
application databases, pipx/global npm installs outside mise, containers,
authenticated plugin caches, and external drives are **not automatically fully
migrated**. Export application data while the app is stopped and add specific
portable paths to `include`. Agent settings/manifests can name plugins without
including vendor-downloaded code: reinstall these through the agent's supported
plugin mechanism and authenticate on the target. Omago does not claim that an
omitted item has been restored. Consult `report.json` on every migration.

Edit `~/omago/settings.json` to add home-relative `include` paths or glob-style
`exclude` rules, change the file size limit, remote,
branch, or transport (`auto`, `ssh`, `https`). Include a specific path such as
`Documents/Notes` rather than the entire home directory. Hard security/platform
exclusions still apply to explicit includes. Settings are machine-local; copy
your custom capture policy to other machines if you want them to discover the
same additional files. Once a file is tracked in EX, it can be restored on other
machines regardless of their include list.

Omago does not manage project Git repositories. It does not discover, clone,
verify, or synchronize them. If an explicitly included directory contains a Git
repository, Omago treats its ordinary files like any other included files; the
`.git` metadata directory remains excluded as runtime metadata. Project source,
history, remotes, and untracked work remain the responsibility of your normal
Git workflow.

Relative links within home and absolute links into home or `/usr/share` are
preserved; other symlinks are omitted. Omago never follows a symlink parent to
write files. Binary files are copied as-is; absolute paths inside binary data
cannot be rewritten. All text occurrences of the source home path are rewritten
using a reserved `__OMAGO_HOME__` marker. Other absolute paths, hostnames and
machine identifiers may need manual adaptation.

Configuration and plugins can execute code when their applications load them.
Only point Omago at an EX repository whose contributors you trust.

## Commands and layout

```sh
omago --plan        # Local report only; does not fetch GitHub or change EX
omago --update      # Capture or reconcile; interactive choices when needed
omago --push        # Retry a reviewed, unpushed local EX commit
omago --reconsider  # Clear machine exceptions; does not perform an update
omago --version
```

`--plan` writes the local report and compares any existing checkout; it is not a
remote dry-run. Unattended updates stop rather than guess if a choice is needed.
No option silently approves conflicts or deletions.

```text
~/Projects/omago/          application source and documentation (rowama/omago)
~/omago/
  omago                   executable launcher
  lib/                    installed Python application
  settings.json           local capture policy and remote
  machine.json            local baseline and exceptions
  report.json             omissions, backup gaps, inventory/plan
  backups/                original files before replacement/deletion
  state/                  Git checkout of rowama/omago-ex
    manifest.json         desired package/file state
    objects/<sha256>       normalized file contents
```

Objects are content-addressed and retained for recovery; old unused objects are
not pruned in this MVP. The state format is versioned (`schema: 1`). Local locks
prevent two updates sharing one installation. Pushes are never forced. If
another machine wins a concurrent push, Omago preserves your local commit and
stops. Inspect `git -C ~/omago/state status`, fetch, and reconcile/rebase the
manifest explicitly, then use `omago --push`. It does not automatically merge
competing manifests. Git hooks are disabled for Omago-managed Git operations.

## Development and tests

No third-party Python dependencies or build step are required.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q omago_core.py install.py
```

Tests use temporary homes and local bare Git remotes. They do not install system
packages, modify your real configuration, or publish to GitHub. Package backends
are mocked in tests; live package installation and real second-machine parity
remain acceptance testing for the MVP.
