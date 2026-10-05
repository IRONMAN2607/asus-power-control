"""Verify portable config merging and install/restore using temporary files."""

import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class ConfigurationTests(unittest.TestCase):
    def test_asus_merge_preserves_charge_and_tuning_settings(self):
        original = '(\n    charge_control_end_threshold: 70,\n    ac_command: "old",\n' \
                   '    platform_profile_linked_epp: true,\n    custom_settings: {},\n)\n'
        settings = {"ac_command": "new", "platform_profile_linked_epp": False,
                    "change_platform_profile_on_battery": False}
        result = installer.patch_ron(original, settings)
        self.assertIn("charge_control_end_threshold: 70", result)
        self.assertIn("custom_settings: {}", result)
        self.assertIn('ac_command: "new"', result)
        self.assertIn("platform_profile_linked_epp: false", result)
        self.assertEqual(installer.patch_ron(result, settings), result)

    def test_unrecognized_or_duplicate_asus_fields_are_not_silently_rewritten(self):
        for content in ("not ron", "(\n ac_command: 1,\n ac_command: 2,\n)\n"):
            with self.subTest(content=content), self.assertRaises(RuntimeError):
                installer.patch_ron(content, {"ac_command": "new"})

    def test_cardwire_global_merge_preserves_unrelated_tables(self):
        original = 'experimental_nvidia_block = false\nother = 5\n\n[extra]\nother = "kept"\n'
        values = {"experimental_nvidia_block": True, "battery_auto_switch_mode": "hybrid"}
        result = installer.patch_toml(original, values)
        parsed = tomllib.loads(result)
        self.assertTrue(parsed["experimental_nvidia_block"])
        self.assertEqual(parsed["other"], 5)
        self.assertEqual(parsed["extra"]["other"], "kept")
        self.assertEqual(parsed["battery_auto_switch_mode"], "hybrid")
        self.assertEqual(installer.patch_toml(result, values), result)

    def test_kde_profile_change_preserves_brightness_lid_and_other_preferences(self):
        original = "[AC][Performance]\nPowerProfile=performance\n\n" \
                   "[Battery][Display]\nDisplayBrightness=45\n\n" \
                   "[Battery][SuspendAndShutdown]\nLidAction=8\n\n[Other]\nExample=kept\n"
        groups = {"AC][Performance": {"PowerProfile": ""},
                  "Battery][Performance": {"PowerProfile": ""}}
        result = installer.patch_kconfig(original, groups)
        self.assertNotIn("PowerProfile=performance", result)
        for setting in ("DisplayBrightness=45", "LidAction=8", "Example=kept"):
            self.assertIn(setting, result)
        self.assertEqual(installer.patch_kconfig(result, groups), result)

    def test_portable_desktop_entry_handles_spaces_and_field_code_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "test user 100%" / "asus-power-control"
            entry = installer.desktop_entry(launcher, Path(directory) / "icon.svg")
            self.assertIn('Exec="', entry)
            self.assertIn("100%%", entry)
            self.assertNotIn("@EXEC@", entry)
            self.assertNotIn("@ICON@", entry)
            desktop = Path(directory) / "org.local.AsusPowerControl.desktop"
            desktop.write_text(entry)
            if shutil.which("desktop-file-validate"):
                subprocess.run(["desktop-file-validate", str(desktop)], check=True)

    def test_all_installer_payload_files_are_in_the_checkout(self):
        for filename in installer.USER_PAYLOAD:
            self.assertTrue((ROOT / filename).is_file(), filename)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "private-backups"
        self.changes = installer.FileChanges(self.store)

    def test_updates_keep_first_original_content_and_permissions(self):
        target = self.root / "settings"
        target.write_text("original")
        target.chmod(0o640)
        self.changes.write(target, "first update", 0o600)
        updates = installer.FileChanges(self.store)
        updates.write(target, "second update", 0o644)
        updates.restore()
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(updates.manifest_path.stat().st_mode), 0o600)

    def test_new_files_are_removed_and_previous_symlinks_restored(self):
        new_file = self.root / "new"
        link = self.root / "alias"
        link.symlink_to("original-target")
        self.changes.write(new_file, "payload")
        self.changes.link(link, "new-target")
        self.changes.restore()
        self.assertFalse(new_file.exists())
        self.assertEqual(os.readlink(link), "original-target")

    def test_installing_over_symlink_does_not_modify_its_target(self):
        original = self.root / "original"
        original.write_text("keep this")
        link = self.root / "managed"
        link.symlink_to(original)
        self.changes.write(link, "new file")
        self.assertEqual(original.read_text(), "keep this")
        self.assertFalse(link.is_symlink())
        self.changes.restore()
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "keep this")

    def test_dry_run_creates_no_files_links_or_backup_store(self):
        existing = self.root / "existing"
        existing.write_text("keep")
        dry = installer.FileChanges(self.store, dry_run=True)
        dry.write(existing, "replace")
        dry.write(self.root / "new", "new")
        dry.link(self.root / "link", "target")
        dry.remove(existing)
        self.assertEqual(existing.read_text(), "keep")
        self.assertFalse((self.root / "new").exists())
        self.assertFalse((self.root / "link").is_symlink())
        self.assertFalse(self.store.exists())


if __name__ == "__main__":
    unittest.main()
