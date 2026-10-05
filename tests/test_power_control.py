"""Exercise policy transitions and UI wiring without writing to live hardware."""

from contextlib import ExitStack, redirect_stdout
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("power_backend", str(ROOT / "backend/asus-power-mode"))
spec = importlib.util.spec_from_loader(loader.name, loader)
backend = importlib.util.module_from_spec(spec)
loader.exec_module(backend)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from asus_power_control import MainWindow


class Hardware:
    def __init__(self, mux=1, cardwire=0, ac=True, settings=None):
        self.ac = ac
        self.profile = "power-saver"
        self.cardwire = cardwire
        self.firmware = {
            "gpu_mux_mode": {"CurrentValue": mux, "QueuedGpuValue": -1},
            "dgpu_disable": {"CurrentValue": 0, "QueuedGpuValue": -1},
        }
        self.settings = settings or {"mode": "battery", "last_ac": ac}
        self.commands = []
        self.saved = Mock()
        self.power = "D3cold"
        self.memory = "Off"

    def gpu_map(self):
        nvidia_panel = self.firmware["gpu_mux_mode"]["CurrentValue"] == 0
        return {
            "0": {"pci": "0000:00:02.0", "vendor": "Intel", "default": not nvidia_panel},
            "1": {"pci": backend.GPU_PCI, "vendor": "Nvidia", "default": nvidia_panel,
                  "blocked": self.cardwire == 0},
        }

    def run(self, *args):
        self.commands.append(args)
        program = Path(args[0]).name
        if program == "powerprofilesctl":
            if args[1] == "set":
                self.profile = args[2]
                return ""
            return self.profile
        if program == "systemctl":
            return "active"
        if program == "cardwire":
            if args[1] == "set":
                self.cardwire = {"integrated": 0, "hybrid": 1}[args[2]]
                return ""
            if args[1] == "gpu":
                return self.power
        if program == "asusctl":
            name, value = args[3], int(args[4])
            self.firmware[name]["QueuedGpuValue"] = (
                -1 if self.firmware[name]["CurrentValue"] == value else value)
            return ""
        raise AssertionError(f"Unexpected command: {args}")

    def read(self, path):
        if str(path).endswith("/power"):
            return "Video Memory:               " + self.memory
        if str(path).endswith("platform_profile"):
            return {"power-saver": "quiet", "balanced": "balanced", "performance": "performance"}[self.profile]
        return "power"

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.multiple(backend, run=self.run, read=self.read,
                                         on_ac=lambda: self.ac, state=lambda: self.settings,
                                         attr=lambda name, prop: self.firmware[name][prop],
                                         gpu_map=self.gpu_map, cardwire_mode=lambda: self.cardwire,
                                         save_state=self.saved))
        stack.enter_context(patch.object(backend, "open", mock_open(), create=True))
        stack.enter_context(patch.object(backend.fcntl, "flock"))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack


class PolicyTests(unittest.TestCase):
    def test_every_cpu_gpu_combination_is_independent_on_ac(self):
        for cpu, profile in backend.CPU_PROFILES.items():
            for gpu in backend.GPU_MODES:
                with self.subTest(cpu=cpu, gpu=gpu):
                    hw = Hardware()
                    with hw.patches():
                        backend.apply("custom", cpu, gpu)
                    self.assertEqual(hw.profile, profile)
                    self.assertEqual(hw.cardwire, 0 if gpu == "integrated" else 1)
                    self.assertEqual(hw.firmware["gpu_mux_mode"]["QueuedGpuValue"],
                                     0 if gpu == "ultimate" else -1)
                    hw.saved.assert_called_once_with("custom", True, cpu, gpu)

    def test_silent_and_hybrid_are_allowed_on_battery(self):
        hw = Hardware(ac=False)
        with hw.patches():
            backend.apply("custom", "silent", "hybrid")
        self.assertEqual(hw.profile, "power-saver")
        self.assertEqual(hw.cardwire, 1)

    def test_ultimate_on_battery_is_rejected_before_any_writes(self):
        hw = Hardware(ac=False)
        with hw.patches(), self.assertRaisesRegex(RuntimeError, "AC adapter"):
            backend.apply("custom", "balanced", "ultimate")
        self.assertFalse(hw.commands)
        hw.saved.assert_not_called()

    def test_return_from_ultimate_never_blocks_the_active_display(self):
        hw = Hardware(mux=0, cardwire=1)
        with hw.patches():
            backend.apply("custom", "balanced", "integrated")
        self.assertEqual(hw.cardwire, 1)
        self.assertEqual(hw.profile, "balanced")
        self.assertEqual(hw.firmware["gpu_mux_mode"]["QueuedGpuValue"], 1)
        self.assertNotIn(("/usr/bin/cardwire", "set", "integrated"), hw.commands)

    def test_opposing_pending_ultimate_can_be_cancelled(self):
        hw = Hardware()
        hw.firmware["gpu_mux_mode"]["QueuedGpuValue"] = 0
        with hw.patches():
            backend.apply("custom", "silent", "integrated")
        self.assertEqual(hw.firmware["gpu_mux_mode"]["QueuedGpuValue"], -1)

    def test_same_source_boot_retains_custom_modes_including_battery(self):
        for ac in (False, True):
            saved = {"mode": "custom", "cpu": "balanced", "gpu": "hybrid", "last_ac": ac}
            self.assertEqual(backend.resolve_request("auto", saved, ac),
                             ("custom", "balanced", "hybrid"))

    def test_real_source_changes_restore_original_presets(self):
        saved = {"mode": "custom", "cpu": "balanced", "gpu": "ultimate", "last_ac": True}
        self.assertEqual(backend.resolve_request("auto", saved, False),
                         ("battery", "silent", "integrated"))
        saved["last_ac"] = False
        self.assertEqual(backend.resolve_request("auto", saved, True),
                         ("normal", "performance", "hybrid"))

    def test_original_presets_keep_original_behaviour(self):
        for mode, (cpu, gpu) in backend.PRESETS.items():
            self.assertEqual(backend.resolve_request(mode, {}, True), (mode, cpu, gpu))
        saved = {"mode": "gaming", "last_ac": True}
        self.assertEqual(backend.resolve_request("auto", saved, True),
                         ("gaming", "performance", "ultimate"))

    def test_unplug_during_ultimate_request_reconciles_and_cancels_queue(self):
        hw = Hardware()
        with hw.patches(), patch.object(backend, "on_ac", side_effect=[True, False]):
            backend.apply("custom", "performance", "ultimate")
        self.assertEqual(hw.firmware["gpu_mux_mode"]["QueuedGpuValue"], -1)
        self.assertEqual(hw.cardwire, 0)
        self.assertEqual(hw.profile, "power-saver")
        hw.saved.assert_called_once_with("battery", False, "silent", "integrated")

    def test_missing_display_identity_prevents_gpu_changes(self):
        hw = Hardware()
        with hw.patches(), patch.object(backend, "gpu_map", return_value={}), self.assertRaises(RuntimeError):
            backend.apply_gpu("integrated")
        self.assertFalse(hw.commands)

    def test_power_off_needs_both_pci_and_vram_evidence(self):
        for power, memory, expected in (("D3cold", "Off", True), ("unreadable", "Off", False),
                                         ("D3cold", "Active", False), ("D0", "Off", False)):
            with self.subTest(power=power, memory=memory):
                hw = Hardware()
                hw.power, hw.memory = power, memory
                with hw.patches():
                    self.assertEqual(backend.snapshot()["power_off_verified"], expected)

    def test_ultimate_is_not_mislabelled_as_cardwire_hybrid(self):
        hw = Hardware(mux=0, cardwire=1)
        with hw.patches():
            data = backend.snapshot()
        self.assertEqual(data["gpu_mode"], "ultimate")
        self.assertEqual(data["display_gpu"], "NVIDIA")

    def test_pending_return_separates_actual_and_requested_modes(self):
        hw = Hardware(mux=0, cardwire=1,
                      settings={"mode": "custom", "cpu": "balanced", "gpu": "integrated"})
        hw.firmware["gpu_mux_mode"]["QueuedGpuValue"] = 1
        with hw.patches():
            data = backend.snapshot()
        self.assertEqual(data["gpu_mode"], "ultimate")
        self.assertEqual(data["pending_gpu"], "integrated")
        self.assertTrue(data["pending_reboot"])


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = MainWindow(auto_refresh=False)
        self.addCleanup(self.window.close)
        hw = Hardware(ac=False)
        with hw.patches():
            self.data = backend.snapshot()
        self.window.display_status(self.data)

    def test_cpu_gpu_controls_send_independent_arguments(self):
        self.window.cpu.setCurrentIndex(self.window.cpu.findData("balanced"))
        self.window.gpu.setCurrentIndex(self.window.gpu.findData("hybrid"))
        with patch.object(self.window.apply_process, "start") as start:
            self.window.apply_selection()
            start.assert_called_once_with("/usr/local/bin/asus-power-mode", ["custom", "balanced", "hybrid"])
        self.window.busy = False

    def test_refresh_preserves_unapplied_choices(self):
        self.window.cpu.setCurrentIndex(self.window.cpu.findData("performance"))
        self.window.gpu.setCurrentIndex(self.window.gpu.findData("hybrid"))
        self.window.display_status(self.data)
        self.assertEqual(self.window.cpu.currentData(), "performance")
        self.assertEqual(self.window.gpu.currentData(), "hybrid")

    def test_ultimate_disabled_without_ac_and_enabled_on_ac(self):
        item = self.window.gpu.model().item(self.window.gpu.findData("ultimate"))
        self.assertFalse(item.isEnabled())
        self.window.display_status({**self.data, "ac": True})
        self.assertTrue(item.isEnabled())

    def test_active_and_pending_gpu_are_shown_separately(self):
        hw = Hardware(mux=0, cardwire=1, settings={"mode": "battery"})
        hw.firmware["gpu_mux_mode"]["QueuedGpuValue"] = 1
        with hw.patches():
            data = backend.snapshot()
        self.window.display_status(data)
        self.assertIn("Ultimate", self.window.current_gpu.text())
        self.assertIn("Integrated requested", self.window.pending.text())
        self.assertIn("reboot required", self.window.pending.text())
        self.assertEqual(self.window.gpu.currentData(), "integrated")

    def test_live_off_evidence_is_visible(self):
        self.assertIn("Verified off", self.window.gpu_power.text())
        self.assertIn("D3cold", self.window.gpu_power.text())


if __name__ == "__main__":
    unittest.main()
