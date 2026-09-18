import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import omago_core as o


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.settings = copy.deepcopy(o.DEFAULTS)
        self.settings["include"] = [".config", ".bashrc"]
        self.app = o.Omago(self.base / "runtime", self.home)
        self.app.settings = self.settings

    def put(self, relative, data, home=None):
        p = (home or self.home) / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p

    def scan(self):
        return o.scan_files(self.home, self.settings)

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()


class CaptureTests(Fixture):
    def test_capture_portability_modes_and_hardware(self):
        p = self.put(".bashrc", f"export MY_FILE={self.home}/notes\n")
        p.chmod(0o750)
        self.put(".config/hypr/monitors.lua", "hl.monitor({})")
        self.put(".config/hypr/input.lua", "input settings")
        self.put(".config/hypr/bindings.lua", "o.bind('SUPER + B')")
        self.put(".config/mixed.conf", "monitor = eDP-1, preferred, auto, 1")
        files, blobs, skipped = self.scan()
        self.assertEqual(files[".bashrc"]["mode"], 0o750)
        self.assertIn(o.HOME_MARKER.encode(), blobs[files[".bashrc"]["sha256"]])
        self.assertIn(".config/hypr/bindings.lua", files)
        self.assertEqual(len(skipped), 3)

    def test_secrets_binary_and_size(self):
        self.put(".config/app/key.conf", "api_key = \"sensitive-test-value\"")
        self.put(".config/gh/hosts.yml", "not a real credential")
        self.put(".config/app/.env", "VALUE=example")
        self.put(".config/app/icon", b"\x00\xff\x10")
        self.put(".config/app/large", b"x" * 31)
        self.settings["max_file_bytes"] = 30
        files, _, skipped = self.scan()
        self.assertEqual(set(files), {".config/app/icon"})
        self.assertEqual(len(skipped), 4)

    def test_symlink_policy_and_no_parent_traversal(self):
        (self.home / ".config").mkdir()
        (self.home / ".config/good").symlink_to("/usr/share/omarchy/default")
        (self.home / ".config/bad").symlink_to("/etc")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "sample").write_text("untouched")
        (self.home / ".config/alias").symlink_to(outside)
        self.settings["include"].append(".config/alias/sample")
        files, _, skipped = self.scan()
        self.assertIn(".config/good", files)
        self.assertNotIn(".config/bad", files)
        self.assertIn(".config/alias/sample", skipped)
        with self.assertRaises(o.OmagoError):
            o.safe_destination(self.home, ".config/alias/sample")

    def test_package_target_symlink_is_user_state_and_can_be_dangling(self):
        (self.home / ".config").mkdir()
        link = self.home / ".config/tool-defaults"
        link.symlink_to("/usr/share/example-package/defaults.conf")
        files, _, _ = self.scan()
        self.assertEqual(files[".config/tool-defaults"]["kind"], "symlink")
        self.assertEqual(files[".config/tool-defaults"]["target"], "/usr/share/example-package/defaults.conf")
        other = self.base / "other-user"
        other.mkdir()
        self.app.home = other
        self.app.apply_file(".config/tool-defaults", files[".config/tool-defaults"])
        restored = other / ".config/tool-defaults"
        self.assertTrue(restored.is_symlink())
        self.assertEqual(os.readlink(restored), "/usr/share/example-package/defaults.conf")

    def test_hardware_package_exclusion(self):
        for name in ("linux", "linux-omarchy-headers", "nvidia-open", "intel-ucode", "vulkan-intel", "limine"):
            self.assertTrue(o.hardware_package(name), name)
        for name in ("nvim", "chromium", "hyprland", "omarchy", "python"):
            self.assertFalse(o.hardware_package(name), name)

    def test_git_metadata_is_excluded_but_included_project_files_are_captured(self):
        self.put(".config/plugin/.git/HEAD", "ref: refs/heads/main")
        self.put(".config/plugin/code.py", "print('x')")
        files, _, skipped = self.scan()
        self.assertIn(".config/plugin/code.py", files)
        self.assertNotIn(".config/plugin/.git/HEAD", files)

    def test_omago_path_symlink_excluded(self):
        self.settings["include"].append(".local/bin")
        (self.home / ".local/bin").mkdir(parents=True)
        self.app.root.mkdir()
        (self.app.root / "omago").write_text("launcher")
        (self.home / ".local/bin/omago").symlink_to(self.app.root / "omago")
        files, _, _ = o.scan_files(self.home, self.settings, [self.app.root])
        self.assertNotIn(".local/bin/omago", files)


class ReconciliationTests(Fixture):
    def entries(self, text):
        self.put(".config/app/settings", text)
        files, blobs, _ = self.scan()
        self.app.store_blobs(blobs)
        return files, blobs

    def test_first_machine_conflict_keep_and_reconsider_on_remote_change(self):
        desired, blobs = self.entries("remote")
        local, _ = self.entries("local")
        with patch.object(o, "choose", return_value="k") as ask:
            self.app.reconcile_files(desired, local, blobs)
            self.assertEqual(ask.call_count, 1)
        with patch.object(o, "choose", side_effect=AssertionError("should not prompt")):
            self.app.reconcile_files(desired, local, blobs)
        changed, blobs = self.entries("changed remote")
        self.put(".config/app/settings", "local")
        with patch.object(o, "choose", return_value="r") as ask:
            self.app.reconcile_files(changed, local, blobs)
            self.assertEqual(ask.call_count, 1)
        self.assertEqual((self.home / ".config/app/settings").read_text(), "changed remote")

    def test_remote_only_change_automatic_with_backup(self):
        old, _ = self.entries("old")
        remote, blobs = self.entries("new")
        self.put(".config/app/settings", "old")
        self.app.machine["files"] = copy.deepcopy(old)
        with patch.object(o, "choose", side_effect=AssertionError("should not prompt")):
            self.app.reconcile_files(remote, old, blobs)
        self.assertEqual((self.home / ".config/app/settings").read_text(), "new")
        backups = list((self.app.root / "backups").rglob("settings"))
        self.assertEqual(backups[0].read_text(), "old")

    def test_local_change_published_and_deletion_requires_choice(self):
        old, _ = self.entries("old")
        local, blobs = self.entries("local")
        self.app.machine["files"] = copy.deepcopy(old)
        with patch.object(o, "choose", return_value="p"):
            self.app.reconcile_files(old, local, blobs)
        self.assertEqual(old, local)
        with patch.object(o, "choose", return_value="r") as ask:
            self.app.reconcile_files({}, local, {})
            self.assertEqual(ask.call_count, 1)
        self.assertFalse((self.home / ".config/app/settings").exists())

    def test_uninventoried_existing_file_never_overwritten(self):
        remote, blobs = self.entries("desired")
        self.put(".config/app/settings", "api_key = secret-test-credential")
        local, _, _ = self.scan()
        with patch.object(o, "choose", side_effect=AssertionError("should leave unsafe file alone")):
            self.app.reconcile_files(remote, local, blobs)
        self.assertIn("secret-test", (self.home / ".config/app/settings").read_text())
        self.assertIn(".config/app/settings", self.app.report["omitted"])

    def test_conflict_two_local_changes(self):
        old, _ = self.entries("old")
        remote, blobs = self.entries("other machine")
        here, _ = self.entries("this machine")
        self.app.machine["files"] = old
        with patch.object(o, "choose", return_value="k") as ask:
            self.app.reconcile_files(remote, here, blobs)
            self.assertEqual(ask.call_count, 1)
        self.assertEqual((self.home / ".config/app/settings").read_text(), "this machine")

    def test_restore_home_path_and_permissions(self):
        files, blobs = self.entries(str(self.home) + "/notes")
        self.app.store_blobs(blobs)
        other = self.base / "other-user"
        other.mkdir()
        self.app.home = other
        self.app.apply_file(".config/app/settings", files[".config/app/settings"])
        self.assertEqual((other / ".config/app/settings").read_text(), str(other) + "/notes")

    def test_missing_dependency_not_reinstalled_and_extras_selected(self):
        desired = {"pacman": ["dependency", "missing"], "aur": []}
        local = {"pacman": ["extra", "extra2"], "aur": []}
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "dependency\nextra\nextra2\n" if "-Qq" in cmd else "", "")
        with patch.object(o, "run", side_effect=fake_run), patch.object(o.shutil, "which", return_value=None), patch.object(o, "choose", side_effect=["s", "a", "k"]):
            self.app.reconcile_packages(desired, local)
        self.assertEqual(calls[1], ["sudo", "pacman", "-S", "--needed", "missing"])
        self.assertEqual(desired["pacman"], ["dependency", "extra", "missing"])
        self.assertEqual(self.app.machine["ignored_packages"]["pacman"], ["extra2"])


class ManifestTests(Fixture):
    def manifest(self):
        self.put(".bashrc", "echo example")
        files, blobs, _ = self.scan()
        self.app.store_blobs(blobs)
        return {"schema": 1, "files": files, "packages": {}}

    def test_traversal_and_tampered_object(self):
        m = self.manifest()
        m["files"]["../../escape"] = m["files"].pop(".bashrc")
        with self.assertRaises(o.OmagoError):
            o.validate_manifest(m, self.app.repo, self.home, self.settings)
        m = self.manifest()
        (self.app.repo / "objects" / m["files"][".bashrc"]["sha256"]).write_text("tampered")
        with self.assertRaises(o.OmagoError):
            o.validate_manifest(m, self.app.repo, self.home, self.settings)

    def test_package_option_and_hardware_injection(self):
        for name in ("--overwrite", "foo;touch /tmp/x", "linux-omarchy"):
            m = self.manifest()
            m["packages"] = {"pacman": [name]}
            with self.assertRaises(o.OmagoError):
                o.validate_manifest(m, self.app.repo, self.home, self.settings)

    def test_rejects_link_parent_and_repo_overlap(self):
        m = self.manifest()
        m["files"]["dir"] = {"kind": "symlink", "target": "/usr/share"}
        m["files"]["dir/file"] = m["files"][".bashrc"]
        with self.assertRaises(o.OmagoError):
            o.validate_manifest(m, self.app.repo, self.home, self.settings)
    def test_safe_remote_urls(self):
        self.assertTrue(o.external_url("git@github.com:rowama/omago-ex.git"))
        self.assertTrue(o.external_url("https://git.example.org/team/repo.git"))
        for value in ("/tmp/local", "ext::sh -c bad", "https://user:password@host/repo", "-bad", None):
            self.assertFalse(o.external_url(value))


class GitIntegrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], check=True, capture_output=True)
        self.env_patch = patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                                               "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                                               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        original_url = o.external_url
        self.url_patch = patch.object(o, "external_url", side_effect=lambda url: url == str(self.remote) or original_url(url))
        self.url_patch.start()
        self.addCleanup(self.url_patch.stop)
        self.inventory_patch = patch.object(o, "package_inventory", return_value=({"pacman": [], "aur": []}, []))
        self.inventory_patch.start()
        self.addCleanup(self.inventory_patch.stop)

    def app_for(self, name):
        home = self.base / name
        home.mkdir(exist_ok=True)
        app = o.Omago(self.base / (name + "-omago"), home)
        app.settings = {**copy.deepcopy(self.settings), "remote": str(self.remote)}
        return app

    def test_empty_capture_two_machines_local_edit_return_and_idempotence(self):
        a = self.app_for("alice")
        self.put(".bashrc", str(a.home) + "/notes", a.home)
        with patch.object(o, "choose", return_value="y"):
            a.update()
        first = self.git(a.repo, "rev-parse", "HEAD")
        b = self.app_for("bob")
        self.put(".bashrc", "existing config", b.home)
        with patch.object(o, "choose", return_value="r"), patch.object(o.Omago, "reconcile_packages"):
            b.update()
        self.assertEqual((b.home / ".bashrc").read_text(), str(b.home) + "/notes")
        self.assertEqual(self.git(b.repo, "rev-parse", "HEAD"), first)
        self.put(".bashrc", "new shared config", b.home)
        with patch.object(o, "choose", return_value="p"), patch.object(o.Omago, "reconcile_packages"):
            b.update()
        with patch.object(o, "choose", side_effect=AssertionError("unexpected conflict")), patch.object(o.Omago, "reconcile_packages"):
            a.update()
            before = self.git(a.repo, "rev-parse", "HEAD")
            a.update()
        self.assertEqual((a.home / ".bashrc").read_text(), "new shared config")
        self.assertEqual(self.git(a.repo, "rev-parse", "HEAD"), before)

    def test_failed_concurrent_push_preserves_local_commit(self):
        a, b = self.app_for("alice"), self.app_for("bob")
        self.put(".bashrc", "initial", a.home)
        with patch.object(o, "choose", return_value="y"):
            a.update()
        b.prepare()
        self.put(".bashrc", "alice change", a.home)
        with patch.object(o, "choose", return_value="p"), patch.object(o.Omago, "reconcile_packages"):
            a.update()
        self.put(".bashrc", "bob change", b.home)
        local, blobs = b.inventory()
        with self.assertRaises(o.OmagoError):
            b.publish(local, blobs)
        local_head = self.git(b.repo, "rev-parse", "HEAD")
        self.assertTrue(local_head)
        self.assertFalse(self.git(b.repo, "status", "--porcelain"))
        with self.assertRaises(o.OmagoError):
            b.prepare()
        self.assertEqual(self.git(b.repo, "rev-parse", "HEAD"), local_head)

    def test_nonempty_unrelated_remote_rejected(self):
        a = self.app_for("alice")
        a.prepare()
        (a.repo / "README").write_text("unrelated")
        self.git(a.repo, "add", "README")
        self.git(a.repo, "commit", "-m", "unrelated")
        self.git(a.repo, "push", "origin", "main")
        b = self.app_for("bob")
        with self.assertRaisesRegex(o.OmagoError, "not an Omago"):
            b.prepare()

    def test_dirty_state_checkout_rejected(self):
        a = self.app_for("alice")
        a.prepare()
        (a.repo / "unexpected").write_text("keep this")
        with self.assertRaisesRegex(o.OmagoError, "Uncommitted"):
            a.prepare()
        self.assertEqual((a.repo / "unexpected").read_text(), "keep this")

    def test_tracked_opt_in_path_is_inventoried_on_target(self):
        app = self.app_for("target")
        self.put("Documents/notes.md", "local edit", app.home)
        o.write_json(app.repo / "manifest.json", {"files": {"Documents/notes.md": {}}})
        files, _, _ = app.capture_files()
        self.assertIn("Documents/notes.md", files)


if __name__ == "__main__":
    unittest.main()
