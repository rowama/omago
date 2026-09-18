#!/usr/bin/env python3
"""Install a self-contained Omago runtime; never captures or publishes EX."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import sys

from omago_core import DEFAULTS, OmagoError, atomic_write, external_url, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path.home() / "omago"))
    parser.add_argument("--bin-dir", default=str(Path.home() / ".local/bin"))
    parser.add_argument("--remote", default=DEFAULTS["remote"])
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().absolute()
    bindir = Path(args.bin_dir).expanduser().absolute()
    if not external_url(args.remote):
        parser.error("--remote must be an external Git SSH/HTTPS URL")
    if os.geteuid() == 0:
        parser.error("Install as your regular user, not root")
    source = Path(__file__).resolve().parent
    launcher = root / "omago"
    link = bindir / "omago"
    for path in (root, root / "lib", bindir):
        if path.is_symlink():
            parser.error(f"Installation directory cannot be a symlink: {path}")
    if launcher.exists() and "# Omago managed launcher" not in launcher.read_text():
        parser.error(f"Unrecognized file at {launcher}; refusing to overwrite")
    if (link.exists() or link.is_symlink()) and link.resolve() != launcher.resolve():
        parser.error(f"{link} already belongs to another installation")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "lib").mkdir(exist_ok=True)
    for name in ("omago", "omago_core.py"):
        atomic_write(root / "lib" / name, (source / name).read_bytes(), 0o755 if name == "omago" else 0o644)
    # Use an absolute interpreter so changes in mise/shims do not break Omago.
    script = ("#!/bin/sh\n# Omago managed launcher\nexec " + shlex.quote(sys.executable) + " " +
              shlex.quote(str(root / "lib/omago")) + " --root " + shlex.quote(str(root)) + ' "$@"\n')
    atomic_write(launcher, script.encode(), 0o755)
    settings = root / "settings.json"
    if not settings.exists():
        write_json(settings, {**DEFAULTS, "remote": args.remote})
    elif read_json(settings)["remote"] != args.remote:
        print("Existing settings.json preserved, including its remote.")
    bindir.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink() and not link.exists():
        link.symlink_to(launcher)
    print(f"Installed {launcher}\nCommand: {link}\nEX has not been captured or published.")
    if str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {bindir} to PATH, or run {launcher} --update.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, OmagoError) as exc:
        print(f"install: {exc}", file=sys.stderr)
        raise SystemExit(1)
