"""Installer tests against a throwaway home directory."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portal_install", REPO / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.account = self.home / "wechat/account"
        (self.account / "db_storage").mkdir(parents=True)
        self.binary = self.home / "bin/wx-cli"
        self.binary.parent.mkdir()
        self.binary.write_text("#!/bin/sh\n")
        self.binary.chmod(0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def run_install(self, **kwargs):
        options = {"binary": str(self.binary), "python": "/usr/bin/python3"}
        options.update(kwargs)
        return installer.install(self.home, REPO, **options)

    def legacy(self):
        runtime = self.home / ".local/share/wechat-work"
        (runtime / "skill").mkdir(parents=True)
        (runtime / "skill/SKILL.md").write_text("old")
        (runtime / "config.json").write_text(json.dumps({"account_dir": str(self.account)}))
        for host in (".claude", ".codex"):
            link = self.home / host / "skills/wechat-work"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(runtime / "skill", target_is_directory=True)
        (self.home / ".claude/commands").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude/commands/wechat-work.md").write_text("old command")
        (self.home / ".claude/commands/wx-image.md").write_text("old image command")
        (self.home / ".local/bin").mkdir(parents=True, exist_ok=True)
        (self.home / ".local/bin/wechat-work").write_text("#!/bin/sh\n")

    def test_first_install_requires_account(self):
        with self.assertRaisesRegex(ValueError, "account-dir"):
            self.run_install()

    def test_install_creates_private_config_launcher_and_shared_skill(self):
        result = self.run_install(account_dir=str(self.account))
        config = self.home / ".local/share/wechat-portal/config.json"
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(json.loads(config.read_text())["account_dir"], str(self.account))
        launcher = self.home / ".local/bin/wechat-portal"
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        self.assertIn(" -I ", launcher.read_text())
        self.assertIn(str(REPO / "run.py"), launcher.read_text())
        for host in (".claude", ".codex"):
            link = self.home / host / "skills/wechat-portal"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), (REPO / "skill").resolve())
        self.assertIn("wechat-portal", (self.home / ".claude/commands/wx-image.md").read_text())
        self.assertFalse(result["keys_modified"])

    def test_reinstall_is_idempotent_and_keeps_binding(self):
        self.run_install(account_dir=str(self.account))
        result = self.run_install()
        self.assertIsNone(result["backup_dir"])
        self.assertEqual(result["legacy_removed"], [])

    def test_migration_backs_up_and_removes_legacy_entry_points(self):
        self.legacy()
        result = self.run_install(migrate=True)
        for path in (".claude/skills/wechat-work", ".codex/skills/wechat-work", ".claude/commands/wechat-work.md",
                     ".local/bin/wechat-work", ".local/share/wechat-work"):
            self.assertFalse(os.path.lexists(self.home / path), path)
        backup = Path(result["backup_dir"])
        self.assertTrue((backup / "wechat-work-runtime/skill/SKILL.md").is_file())
        self.assertEqual((backup / "claude__commands__wx-image.md").read_text(), "old image command")
        self.assertTrue((backup / "claude__skills__wechat-work.symlink").is_file())
        self.assertFalse(any(p.name.startswith(".") for p in backup.iterdir()))
        # Account binding migrated from the legacy config.
        config = json.loads((self.home / ".local/share/wechat-portal/config.json").read_text())
        self.assertEqual(config["account_dir"], str(self.account))

    def test_unrelated_skill_is_left_unchanged(self):
        target = self.home / ".claude/skills/wechat-portal"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("someone else's skill")
        with self.assertRaisesRegex(ValueError, "unrelated skill"):
            self.run_install(account_dir=str(self.account))
        self.assertEqual((target / "SKILL.md").read_text(), "someone else's skill")
        self.assertFalse((self.home / ".local/bin/wechat-portal").exists())

    def test_symlinked_account_is_refused(self):
        link = self.home / "account-link"
        link.symlink_to(self.account, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_install(account_dir=str(link))


if __name__ == "__main__":
    unittest.main()
