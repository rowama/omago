"""Portable Omarchy engagement state, using only Python's standard library.

The Git repository is the desired state; machine.json is a local reconciliation
baseline, never a competing source of truth. No shell evaluation is used.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

VERSION = "0.1.0"
DEFAULT_REMOTE = "git@github.com:rowama/omago-ex.git"
HOME_MARKER = "__OMAGO_HOME__"
DEFAULTS = {
    "schema": 1,
    "remote": DEFAULT_REMOTE,
    "branch": "main",
    "transport": "auto",
    "include": [
        ".config", ".bashrc", ".bash_profile", ".bash_logout", ".zshrc",
        ".zprofile", ".profile", ".XCompose", ".gitconfig", ".gitignore",
        ".gitignore_global", ".tmux.conf", ".inputrc", ".vimrc",
        ".local/bin", ".local/share/applications", ".local/share/fonts",
        ".local/share/omarchy/themes", ".local/share/omarchy/current",
        ".local/state/omarchy/current",
        ".agents/skills", ".codex/skills", ".codex/config.toml",
        ".codex/AGENTS.md", ".codex/rules", ".codex/plugins/installed_plugins.json",
        ".claude/skills", ".claude/commands", ".claude/settings.json",
        ".claude/CLAUDE.md", ".claude/plugins/installed_plugins.json",
    ],
    "exclude": [],
    "max_file_bytes": 10 * 1024 * 1024,
}
# These are platform/driver choices, not portable application requests.
HARDWARE_PACKAGES = (
    "linux", "linux-*", "*-ucode", "*-firmware", "linux-firmware*",
    "nvidia*", "lib32-nvidia*", "vulkan-*", "lib32-vulkan-*",
    "xf86-video-*", "intel-media-driver", "libva-intel-driver", "vpl-gpu-rt",
    "amdvlk", "lib32-amdvlk", "mesa", "lib32-mesa", "sof-firmware",
    "mkinitcpio*", "dracut*", "limine*", "grub*", "efibootmgr",
    "kernel-modules-hook", "zram-generator", "thermald", "broadcom-wl*",
)
BLOCKED_CONFIG = {
    "chromium", "google-chrome", "google-chrome-beta", "google-chrome-unstable",
    "BraveSoftware", "microsoft-edge", "microsoft-edge-dev", "mozilla",
    "Bitwarden", "gh", "gcloud", "aws", "1Password", "op", "rustdesk",
    "pulse", "wireplumber", "dconf", "ibus", "fcitx", "fcitx5",
    "obsidian", "herdr", "opencode", "Omacom", "systemd",
    "hyprland-preview-share-picker", "procps",
}
HARDWARE_FILES = (
    ".config/hypr/monitors*", ".config/hypr/input*", ".config/hypr/env*",
    ".config/hypr/device*", ".config/uwsm/env*", ".config/omarchy/hardware*",
    ".config/omarchy/extensions/*hardware*", ".config/user-dirs.*",
)
JUNK_PARTS = {".git", "node_modules", "__pycache__", "Cache", "cache", "Caches",
              "GPUCache", "Code Cache", "logs", "sessions", "backups", ".venv",
              "venv", "target", ".npm", ".cache", ".rustup", ".cargo"}
SECRET_PARTS = {".ssh", ".gnupg", ".pki", ".aws", "keyrings", "credentials",
                "secrets", "auth.json", "credentials.json", "hosts.yml",
                ".netrc", ".npmrc", ".pypirc", "Cookies", "Login Data"}
SECRET_RE = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    rb"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})|"
    rb"(?im:^[^\n#;]*(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
    rb"\s*[=:]\s*[\"']?[^\s\"'$}{][^\n]{5,})"
)
HARDWARE_RE = re.compile(
    rb"(?im:^\s*(?:monitor\s*=|hl\.monitor\s*\(|device\s*\{|"
    rb"env\s*=\s*(?:AQ_DRM_DEVICES|WLR_DRM_DEVICES|LIBVA_DRIVER_NAME|GBM_BACKEND)))|"
    rb"/dev/(?:dri|input|disk)/|(?i:PCI:[0-9]+:[0-9]+)"
)


class OmagoError(Exception):
    pass


def run(args, cwd=None, check=True, capture=True):
    """Argument arrays only. Error messages never echo command output/secrets."""
    p = subprocess.run([str(x) for x in args], cwd=cwd, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None)
    if check and p.returncode:
        raise OmagoError(f"{args[0]} failed (exit {p.returncode}). "
                         "Check authentication, connectivity, and the local report.")
    return p


def git(repo, *args, check=True):
    return run(["git", "-c", "core.hooksPath=/dev/null", "-C", repo, *args], check=check)


def read_json(path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as e:
        raise OmagoError(f"Invalid JSON in {path}: {e}") from e


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".omago-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_relative(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise OmagoError("Invalid home-relative path in state")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("..", ".git") for x in p.parts) or str(p) == ".":
        raise OmagoError(f"Unsafe home-relative path: {value!r}")
    return p


def safe_destination(home, relative):
    p = home / str(safe_relative(relative))
    current = home
    for part in PurePosixPath(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise OmagoError(f"Refusing to write through symlink parent: {relative}")
    if p.exists() and p.is_dir() and not p.is_symlink():
        raise OmagoError(f"Directory occupies file destination: {relative}")
    return p


def external_url(url):
    # Git protocols that execute commands or carry passwords are forbidden.
    return isinstance(url, str) and bool(re.fullmatch(
        r"(?:git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+|"
        r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+|"
        r"ssh://git@[A-Za-z0-9.-]+/[A-Za-z0-9_./-]+)", url))


def choose(prompt, choices, default):
    if not sys.stdin.isatty():
        raise OmagoError(f"Interactive guidance required: {prompt}. Run in a terminal.")
    letters = "/".join(f"{k}={v}" for k, v in choices.items())
    while True:
        answer = input(f"{prompt}\n  [{letters}] ({default}): ").strip().lower() or default
        if answer in choices:
            return answer


def hardware_package(name):
    return any(fnmatch.fnmatchcase(name, pat) for pat in HARDWARE_PACKAGES)


def excluded(path, settings):
    parts = PurePosixPath(path).parts
    if any(x in SECRET_PARTS or x in JUNK_PARTS for x in parts):
        return "credentials, runtime data, or cache"
    if any(x.startswith(".env") for x in parts) or path.endswith((".pem", ".key", ".p12", ".pfx")):
        return "credential filename"
    if path.endswith((".bak", ".old", ".log", ".lock", "~")) or ".before-" in path:
        return "backup, runtime lock, or log"
    # Package ownership alone never excludes user configuration under an
    # included path; this narrow list protects credentials and runtime data.
    if len(parts) > 1 and parts[0] == ".config" and parts[1] in BLOCKED_CONFIG:
        return "sensitive, runtime, or platform-specific application data"
    if any(fnmatch.fnmatchcase(path, pat) for pat in HARDWARE_FILES):
        return "hardware configuration"
    if any(fnmatch.fnmatchcase(path, pat) for pat in settings["exclude"]):
        return "user exclusion"
    return None


def normalize_link(target, home):
    if target.startswith(str(home) + "/"):
        return HOME_MARKER + target[len(str(home)):]
    return target


def valid_link(relative, target, home):
    if not isinstance(target, str) or "\x00" in target:
        return False
    actual = target.replace(HOME_MARKER, str(home))
    path = Path(os.path.normpath(actual if actual.startswith("/") else str(home / relative / ".." / actual)))
    return path.is_relative_to(home) or path.is_relative_to(Path("/usr/share"))


def file_entry(path, relative, home, settings):
    if path.is_symlink():
        # The link is user state. Preserve it even if its package-owned target
        # is absent on this machine; package installation can happen later.
        target = normalize_link(os.readlink(path), home)
        if not valid_link(relative, target, home):
            return None, "symlink points outside home or /usr/share"
        return {"kind": "symlink", "target": target}, None
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        return None, "not a regular file"
    if st.st_size > settings["max_file_bytes"]:
        return None, "file exceeds configured size limit"
    data = path.read_bytes()
    if SECRET_RE.search(data):
        return None, "possible credential in content (not displayed)"
    if HARDWARE_RE.search(data):
        return None, "embedded hardware configuration; split into a machine-local file"
    text_file = b"\x00" not in data
    if text_file:
        try:
            data = data.decode("utf-8").replace(str(home), HOME_MARKER).encode()
        except UnicodeDecodeError:
            text_file = False
    entry = {"kind": "file", "sha256": hashlib.sha256(data).hexdigest(),
             "mode": stat.S_IMODE(st.st_mode) & 0o777,
             "text": text_file}
    return (entry, data), None


def scan_files(home, settings, omit=()):
    files, blobs, skipped = {}, {}, {}
    omit = [Path(p).resolve() for p in omit]

    def visit(path):
        relative = path.relative_to(home).as_posix()
        if any(path == p or path.is_relative_to(p) or
               (path.is_symlink() and path.resolve().is_relative_to(p)) for p in omit):
            return
        reason = excluded(relative, settings)
        if reason:
            skipped[relative] = reason
            return
        if path.is_dir() and not path.is_symlink():
            try:
                for child in sorted(path.iterdir()):
                    visit(child)
            except PermissionError:
                skipped[relative] = "permission denied"
            return
        try:
            result, reason = file_entry(path, relative, home, settings)
            if reason:
                skipped[relative] = reason
            elif isinstance(result, tuple):
                entry, data = result
                files[relative] = entry
                blobs[entry["sha256"]] = data
            else:
                files[relative] = result
        except (OSError, ValueError):
            skipped[relative] = "unreadable or changed during capture"

    for relative in settings["include"]:
        path = home / str(safe_relative(relative))
        # Never traverse symlink parents, including explicitly added include paths.
        if any(p.is_symlink() for p in path.parents if p != home and p.is_relative_to(home)):
            skipped[relative] = "symlink parent"
        elif path.exists() or path.is_symlink():
            visit(path)
    return files, blobs, skipped


def package_inventory():
    if not shutil.which("pacman"):
        raise OmagoError("Omago's MVP requires an existing Arch/Omarchy installation with pacman.")
    explicit = set(run(["pacman", "-Qqe"]).stdout.split())
    foreign = set(run(["pacman", "-Qqm"], check=False).stdout.split())
    packages = {"pacman": sorted(p for p in explicit - foreign if not hardware_package(p)),
                "aur": sorted(p for p in explicit & foreign if not hardware_package(p))}
    if shutil.which("code"):
        packages["vscode"] = sorted(run(["code", "--list-extensions"]).stdout.splitlines())
    if shutil.which("flatpak"):
        for scope in ("user", "system"):
            rows = run(["flatpak", "list", "--" + scope, "--app",
                        "--columns=application,origin,branch"]).stdout.splitlines()
            packages["flatpak-" + scope] = sorted("|".join(row.split()) for row in rows if row.strip())
    return packages, sorted(p for p in explicit if hardware_package(p))


def validate_manifest(state, repo, home, settings):
    if (repo / "manifest.json").is_symlink() or (repo / "objects").is_symlink():
        raise OmagoError("EX metadata must not be symlinked")
    if state.get("schema") != 1:
        raise OmagoError("Unsupported EX schema")
    if not isinstance(state.get("files"), dict) or not isinstance(state.get("packages"), dict):
        raise OmagoError("Malformed EX manifest")
    for relative, entry in state["files"].items():
        safe_relative(relative)
        if excluded(relative, settings):
            raise OmagoError(f"EX contains a forbidden or excluded path: {relative}")
        if entry.get("kind") == "symlink":
            if not valid_link(relative, entry.get("target"), home):
                raise OmagoError(f"Unsafe symlink in EX: {relative}")
        elif entry.get("kind") == "file":
            digest = entry.get("sha256", "")
            if not re.fullmatch("[a-f0-9]{64}", digest):
                raise OmagoError("Invalid object hash")
            blob = repo / "objects" / digest
            if blob.is_symlink() or not blob.is_file() or hashlib.sha256(blob.read_bytes()).hexdigest() != digest:
                raise OmagoError(f"Missing or corrupt object for {relative}")
            if not isinstance(entry.get("mode"), int) or not 0 <= entry["mode"] <= 0o777:
                raise OmagoError("Invalid file mode")
            if SECRET_RE.search(blob.read_bytes()) or HARDWARE_RE.search(blob.read_bytes()):
                raise OmagoError(f"Forbidden content in EX: {relative}")
        else:
            raise OmagoError("Unsupported file entry")
    paths = sorted(state["files"])
    for path in paths:
        if any(str(p) in state["files"] for p in PurePosixPath(path).parents if str(p) != "."):
            raise OmagoError("EX contains overlapping file destinations")
    for provider, names in state["packages"].items():
        if provider not in {"pacman", "aur", "vscode", "flatpak-user", "flatpak-system"}:
            raise OmagoError(f"Unknown package provider: {provider}")
        if not isinstance(names, list):
            raise OmagoError("Invalid package list")
        for name in names:
            pattern = r"[A-Za-z0-9][A-Za-z0-9_.+@|/-]*"
            if not isinstance(name, str) or not re.fullmatch(pattern, name):
                raise OmagoError("Invalid package name")
            if provider in {"pacman", "aur"} and hardware_package(name):
                raise OmagoError(f"Hardware package in portable EX: {name}")
            if provider.startswith("flatpak") and len(name.split("|")) != 3:
                raise OmagoError("Invalid Flatpak reference")


class Omago:
    def __init__(self, root, home=None):
        self.root = Path(root).expanduser().absolute()
        self.home = Path(home or Path.home()).absolute()
        self.repo = self.root / "state"
        self.settings = read_json(self.root / "settings.json", DEFAULTS)
        self.settings = {**copy.deepcopy(DEFAULTS), **self.settings}
        self.machine = read_json(self.root / "machine.json", {"files": {}, "keep": {}, "ignored_packages": {}})
        self.report = {"schema": 1, "omitted": {}, "notes": []}
        self.stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.omit = [self.root, Path(__file__).resolve().parent]

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (self.root / "settings.json").exists():
            write_json(self.root / "settings.json", self.settings)
        if not external_url(self.settings["remote"]):
            raise OmagoError("State remote must be an external SSH or HTTPS Git URL")
        branch = self.settings["branch"]
        if branch.startswith("-") or run(["git", "check-ref-format", "--branch", branch], check=False).returncode:
            raise OmagoError("Invalid state branch")
        if not (self.repo / ".git").is_dir():
            if self.repo.exists() and any(self.repo.iterdir()):
                raise OmagoError("State directory is occupied; refusing to overwrite it")
            self.repo.mkdir(exist_ok=True)
            git(self.repo, "init", "-b", branch)
            git(self.repo, "remote", "add", "origin", self.settings["remote"])
        if git(self.repo, "remote", "get-url", "origin").stdout.strip() != self.effective_url():
            # get-url honors insteadOf, so inspect the configured URL too.
            if git(self.repo, "config", "--get", "remote.origin.url").stdout.strip() != self.settings["remote"]:
                raise OmagoError("State origin differs from settings.json")
        self.configure_transport()
        if git(self.repo, "status", "--porcelain").stdout:
            raise OmagoError(f"Uncommitted EX changes in {self.repo}; inspect and commit or move them before retrying.")
        refs = git(self.repo, "ls-remote", "origin").stdout.strip()
        self.empty_remote = not refs
        head = git(self.repo, "rev-parse", "--verify", "HEAD", check=False)
        if refs:
            branch_ref = "refs/heads/" + branch
            if not any(line.endswith("\t" + branch_ref) for line in refs.splitlines()):
                raise OmagoError(f"Remote is not empty but has no {branch} branch. Set branch in settings.json.")
            git(self.repo, "fetch", "origin", branch)
            if head.returncode:
                git(self.repo, "checkout", "-B", branch, "FETCH_HEAD")
            else:
                if git(self.repo, "symbolic-ref", "--short", "HEAD").stdout.strip() != branch:
                    raise OmagoError("State checkout is on an unexpected branch")
                git(self.repo, "merge", "--ff-only", "FETCH_HEAD")
                if git(self.repo, "rev-parse", "HEAD").stdout != git(self.repo, "rev-parse", "FETCH_HEAD").stdout:
                    raise OmagoError("Local EX commit is not published. Run --push to retry before updating.")
        elif head.returncode == 0:
            raise OmagoError("Local EX commit is not published. Run --push to retry.")
        if refs and (not (self.repo / "manifest.json").is_file() or (self.repo / "manifest.json").is_symlink()):
            raise OmagoError("Nonempty remote is not an Omago EX repository (manifest.json missing)")

    def effective_url(self):
        return self.settings["remote"]

    def configure_transport(self):
        remote = self.settings["remote"]
        transport = self.settings["transport"]
        if transport not in {"auto", "ssh", "https"}:
            raise OmagoError("transport must be auto, ssh, or https")
        if remote.startswith("git@github.com:") and transport in {"auto", "https"}:
            available = shutil.which("gh") and run(["gh", "auth", "status"], check=False).returncode == 0
            if transport == "https" or available:
                git(self.repo, "config", "url.https://github.com/.insteadOf", "git@github.com:")
                if available:
                    git(self.repo, "config", "credential.https://github.com.helper", "!gh auth git-credential")

    def inventory(self):
        packages, hardware = package_inventory()
        files, blobs, omitted = self.capture_files()
        self.report["omitted"] = omitted
        self.report["hardware_packages"] = hardware
        self.report["notes"] = [
            "Browser profiles, credentials, keyrings, hardware settings, /etc, and application databases are not migrated.",
            "Documents and agent conversations/memory require explicit include paths; databases should be exported first.",
            "mise configuration is captured; installed runtimes are reconciled with an explicit mise install choice.",
            "Plugin configuration/source in included paths is captured; vendor caches and authenticated marketplace downloads are not.",
            "Foreign pacman packages are AUR candidates: packages absent from AUR need a documented installation source.",
            "OS package versions are not pinned; targets need compatible, up-to-date Omarchy installations.",
        ]
        return {"schema": 1, "packages": packages, "files": files}, blobs

    def capture_files(self):
        settings = copy.deepcopy(self.settings)
        tracked = read_json(self.repo / "manifest.json", {"files": {}}).get("files", {})
        settings["include"] = sorted(set(settings["include"]) | set(tracked))
        return scan_files(self.home, settings, self.omit)

    def save_report(self):
        write_json(self.root / "report.json", self.report)

    def store_blobs(self, blobs):
        for digest, data in blobs.items():
            path = self.repo / "objects" / digest
            if not path.exists():
                atomic_write(path, data)

    def publish(self, state, blobs):
        needed = {x["sha256"] for x in state["files"].values() if x["kind"] == "file"}
        self.store_blobs({k: v for k, v in blobs.items() if k in needed})
        validate_manifest(state, self.repo, self.home, self.settings)
        write_json(self.repo / "manifest.json", state)
        # Only Omago-owned files are staged, never arbitrary files in the checkout.
        git(self.repo, "add", "--", "manifest.json", "objects") if needed else git(self.repo, "add", "--", "manifest.json")
        if git(self.repo, "diff", "--cached", "--quiet", check=False).returncode:
            git(self.repo, "commit", "-m", "Update portable engagement state")
            self.push()

    def push(self):
        self.configure_transport()
        print("Publishing EX to GitHub...")
        result = git(self.repo, "push", "-u", "origin", "HEAD:refs/heads/" + self.settings["branch"], check=False)
        if result.returncode:
            raise OmagoError("Push failed or another machine published first. Local commit is preserved. "
                             "Inspect the state checkout, fetch/reconcile the remote, then run omago --push. Never force-push.")

    def backup(self, relative, dest):
        if not (dest.exists() or dest.is_symlink()):
            return
        backup = self.root / "backups" / self.stamp / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            backup.symlink_to(os.readlink(dest))
        else:
            shutil.copy2(dest, backup)

    def apply_file(self, relative, entry):
        dest = safe_destination(self.home, relative)
        # Protect Omago's own executable, state, and local journal from EX writes.
        if any(dest == p or dest.is_relative_to(p) or
               (dest.is_symlink() and dest.resolve().is_relative_to(p)) for p in self.omit):
            raise OmagoError("EX attempts to overwrite Omago itself")
        self.backup(relative, dest)
        if entry is None:
            if dest.exists() or dest.is_symlink():
                dest.unlink()
        elif entry["kind"] == "symlink":
            dest.parent.mkdir(parents=True, exist_ok=True)
            temporary = dest.parent / (".omago-link-" + self.stamp)
            temporary.symlink_to(entry["target"].replace(HOME_MARKER, str(self.home)))
            os.replace(temporary, dest)
        else:
            data = (self.repo / "objects" / entry["sha256"]).read_bytes()
            if entry.get("text"):
                data = data.replace(HOME_MARKER.encode(), str(self.home).encode())
            atomic_write(dest, data, entry["mode"])

    def reconcile_files(self, desired, local, blobs):
        base = self.machine.get("files", {})
        keep = self.machine.setdefault("keep", {})
        applications = {}
        for relative in sorted(set(desired) | set(local) | set(base)):
            remote, here, previous = desired.get(relative), local.get(relative), base.get(relative)
            dest = self.home / relative
            if here is None and (dest.exists() or dest.is_symlink()):
                # Absence from an inventory is not proof that the path is absent.
                # It may hold credentials, hardware settings, or oversized data.
                self.report["omitted"][relative] = "existing path not safely inventoried; left unchanged"
                continue
            if remote == here:
                keep.pop(relative, None)
                continue
            if relative in keep and keep[relative] == remote:
                continue
            if remote is None and previous is None and here is not None:
                # One choice for newly discovered files in an app directory.
                parts = PurePosixPath(relative).parts
                group = "/".join(parts[:2]) if len(parts) > 1 else relative
                if group not in applications:
                    applications[group] = choose(f"Local configuration absent from shared EX: {group}",
                                                 {"p": "publish local", "k": "keep only here"}, "k")
                action = applications[group]
            elif remote is not None and here is None and previous is None:
                action = "r"
            elif previous is not None and here == previous and remote is not None:
                action = "r"
            else:
                action = choose(f"Configuration differs: {relative}" +
                                (" (GitHub removed it)" if remote is None else "") +
                                (" (missing locally)" if here is None else ""),
                                {"r": "use GitHub (backup local)", "p": "publish local, including deletion", "k": "keep local exception"}, "k")
            if action == "r":
                self.apply_file(relative, remote)
                keep.pop(relative, None)
            elif action == "p":
                if here is None:
                    desired.pop(relative, None)
                else:
                    desired[relative] = here
                keep.pop(relative, None)
            else:
                keep[relative] = remote
        self.machine["files"] = copy.deepcopy(desired)

    def reconcile_packages(self, desired, local):
        all_installed = set(run(["pacman", "-Qq"]).stdout.split())
        for provider in sorted(set(desired) | set(local)):
            wanted = set(desired.get(provider, []))
            present = set(local.get(provider, []))
            missing = sorted(wanted - (all_installed if provider in {"pacman", "aur"} else present))
            if missing:
                print(f"Installing {provider}: {', '.join(missing)}")
                if provider == "pacman":
                    command = ["omarchy", "pkg", "add", *missing] if shutil.which("omarchy") else ["sudo", "pacman", "-S", "--needed", *missing]
                    run(command, capture=False)
                elif provider == "aur":
                    if not shutil.which("yay"):
                        raise OmagoError("AUR packages required, but yay is missing. Install yay then retry.")
                    run(["yay", "-S", "--needed", *missing], capture=False)
                elif provider == "vscode":
                    if not shutil.which("code"):
                        raise OmagoError("VS Code extension manifest requires the code executable")
                    for name in missing:
                        run(["code", "--install-extension", name], capture=False)
                else:
                    if not shutil.which("flatpak"):
                        raise OmagoError("Flatpak manifest requires flatpak")
                    for name in missing:
                        app, remote, branch = name.split("|")
                        run(["flatpak", "install", "--" + provider.split("-")[1], remote, app + "//" + branch], capture=False)
            ignored = set(self.machine.setdefault("ignored_packages", {}).get(provider, []))
            extra = sorted(present - wanted - ignored)
            if extra:
                action = choose(f"Local {provider} apps absent from EX: {', '.join(extra)}",
                                {"a": "add all to EX", "s": "select individually", "k": "keep local"}, "k")
                for name in extra:
                    add = action == "a" or (action == "s" and choose(f"Add {name}?", {"a": "add", "k": "keep local"}, "k") == "a")
                    if add:
                        wanted.add(name)
                    else:
                        ignored.add(name)
            desired[provider] = sorted(wanted)
            self.machine["ignored_packages"][provider] = sorted(ignored)

    def update(self):
        self.prepare()
        if not self.empty_remote:
            validate_manifest(read_json(self.repo / "manifest.json"), self.repo, self.home, self.settings)
        local, blobs = self.inventory()
        try:
            if self.empty_remote:
                print("Empty EX repository: preparing this machine's initial portable state.")
                self.save_report()
                print(f"Capture: {len(local['files'])} files, {sum(map(len, local['packages'].values()))} apps. "
                      f"Review {self.root / 'report.json'} for gaps.")
                if choose("Publish this initial snapshot to " + self.settings["remote"] + "?",
                          {"y": "publish", "n": "stop for review"}, "n") != "y":
                    raise OmagoError("Initial capture not published")
                self.publish(local, blobs)
                self.machine["files"] = copy.deepcopy(local["files"])
            else:
                desired = read_json(self.repo / "manifest.json")
                validate_manifest(desired, self.repo, self.home, self.settings)
                self.reconcile_packages(desired["packages"], local["packages"])
                # Newly installed packages can create default config files.
                local["files"], blobs, omitted = self.capture_files()
                self.report["omitted"].update(omitted)
                self.reconcile_files(desired["files"], local["files"], blobs)
                self.publish(desired, blobs)
                if ".config/mise/config.toml" in desired["files"] and shutil.which("mise"):
                    if choose("Reconcile tools declared by mise? Review/trust its configuration first.",
                              {"y": "run mise install", "n": "defer"}, "n") == "y":
                        run(["mise", "install"], cwd=self.home, capture=False)
                    else:
                        self.report["notes"].append("mise runtime installation deferred on this machine")
                actual, _ = package_inventory()
                installed = set(run(["pacman", "-Qq"]).stdout.split())
                for provider, names in desired["packages"].items():
                    remaining = set(names) - (installed if provider in {"pacman", "aur"} else set(actual.get(provider, [])))
                    if remaining:
                        raise OmagoError(f"Apps still missing after installation ({provider}): {', '.join(sorted(remaining))}")
            write_json(self.root / "machine.json", self.machine)
            print(f"EX update complete. Local report: {self.root / 'report.json'}")
            print("Existing configuration backups are under backups/. Log out/in to activate session changes.")
        finally:
            self.save_report()

    def plan(self):
        local, _ = self.inventory()
        desired = read_json(self.repo / "manifest.json", {"files": {}, "packages": {}})
        differences = sorted(k for k in set(local["files"]) | set(desired["files"])
                             if local["files"].get(k) != desired["files"].get(k))
        self.report["plan"] = {"portable_files": len(local["files"]), "packages": local["packages"],
                               "different_files": differences,
                               "remote": "not fetched; comparison uses existing local state checkout"}
        self.save_report()
        print(f"Plan: {len(local['files'])} portable files; {len(differences)} file differences. "
              f"No installs, home changes, commits, or network access. See {self.root / 'report.json'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Omago: synchronize portable Omarchy engagement experience through Git.")
    parser.add_argument("--version", action="version", version="Omago " + VERSION)
    parser.add_argument("--root", default=os.environ.get("OMAGO_DIR", str(Path.home() / "omago")),
                        help="installation/state directory (or OMAGO_DIR)")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--update", action="store_true", help="capture empty EX or reconcile with shared EX")
    commands.add_argument("--plan", action="store_true", help="local inventory/report only; no network or installs")
    commands.add_argument("--push", action="store_true", help="retry publishing a reviewed local EX commit")
    commands.add_argument("--reconsider", action="store_true", help="clear local keep/ignore choices; no update")
    args = parser.parse_args(argv)
    app = Omago(args.root)
    try:
        if os.geteuid() == 0:
            raise OmagoError("Run Omago as your normal user, not root; package tools request sudo when needed.")
        app.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (app.root / ".lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise OmagoError("Another Omago process is using this installation") from e
            if args.update:
                app.update()
            elif args.plan:
                app.plan()
            elif args.push:
                app.push()
            else:
                app.machine.update(keep={}, ignored_packages={})
                write_json(app.root / "machine.json", app.machine)
                print("Local exceptions cleared. Run omago --update to reconsider them.")
        return 0
    except (OmagoError, OSError, ValueError, KeyError, TypeError) as e:
        print(f"omago: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nomago: interrupted; completed installs remain, and backups/local commits are preserved.", file=sys.stderr)
        return 130
