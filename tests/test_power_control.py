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
from PySide6.QtCore import QByteArray, QProcess
from asus_power_control import MainWindow
from monitoring import Metrics
from osd import MonitorOSD


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
        self.charge_limit = 70
        self.base_charge_limit = 70

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
        if program == "busctl" and args[-1] == backend.CHARGE_PROPERTY:
            return f"y {self.charge_limit}"
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
            if args[1:3] == ("battery", "limit"):
                self.charge_limit = self.base_charge_limit = int(args[3])
                return ""
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


class ChargeLimitTests(unittest.TestCase):
    def test_both_slider_bounds_save_without_changing_cpu_gpu_or_policy(self):
        for limit in (60, 100):
            with self.subTest(limit=limit):
                hw = Hardware(mux=0, cardwire=1)
                previous = hw.settings.copy()
                with hw.patches():
                    backend.set_charge_limit(limit)
                self.assertEqual(hw.charge_limit, limit)
                self.assertEqual(hw.base_charge_limit, limit)
                self.assertEqual(hw.profile, "power-saver")
                self.assertEqual(hw.cardwire, 1)
                self.assertEqual(hw.firmware["gpu_mux_mode"]["CurrentValue"], 0)
                self.assertEqual(hw.firmware["gpu_mux_mode"]["QueuedGpuValue"], -1)
                self.assertEqual(hw.settings, previous)
                hw.saved.assert_not_called()
                self.assertIn(("/usr/bin/asusctl", "battery", "limit", str(limit)), hw.commands)

    def test_invalid_values_never_reach_hardware(self):
        for limit in (59, 101, None, True, "70.5", 70.5, "70; reboot"):
            with self.subTest(limit=limit):
                hw = Hardware()
                with hw.patches(), self.assertRaises(RuntimeError):
                    backend.set_charge_limit(limit)
                self.assertFalse(hw.commands)

    def test_resaving_current_one_shot_limit_makes_it_the_persistent_base(self):
        hw = Hardware()
        hw.charge_limit, hw.base_charge_limit = 100, 70
        with hw.patches():
            backend.set_charge_limit(100)
        self.assertEqual(hw.base_charge_limit, 100)
        self.assertIn(("/usr/bin/asusctl", "battery", "limit", "100"), hw.commands)

    def test_failed_readback_does_not_report_success(self):
        hw = Hardware()
        with hw.patches(), patch.object(backend, "charge_limit", return_value=60):
            with self.assertRaisesRegex(RuntimeError, "did not apply"):
                backend.set_charge_limit(70)
        hw.saved.assert_not_called()

    def test_unavailable_charge_interface_keeps_cpu_gpu_status_usable(self):
        hw = Hardware()
        with hw.patches(), patch.object(backend, "charge_limit", side_effect=RuntimeError("charge unavailable")):
            data = backend.snapshot()
        self.assertIsNone(data["charge_limit"])
        self.assertEqual(data["gpu_mode"], "integrated")
        self.assertIn("charge unavailable", data["errors"])

    def test_invalid_cli_argument_is_rejected_before_authentication(self):
        with patch.object(backend.sys, "argv", [backend.SELF, "charge-limit", "101"]), \
                patch.object(backend.os, "execv") as elevate, self.assertRaises(RuntimeError):
            backend.main()
        elevate.assert_not_called()

    def test_cli_elevates_only_the_fixed_charge_command_without_ac_requirement(self):
        with patch.object(backend.sys, "argv", [backend.SELF, "charge-limit", "80"]), \
                patch.object(backend.os, "geteuid", return_value=1000), \
                patch.object(backend.os, "execv", side_effect=SystemExit(0)) as elevate, \
                patch.object(backend, "on_ac") as on_ac, self.assertRaises(SystemExit):
            backend.main()
        elevate.assert_called_once_with("/usr/bin/pkexec", ["pkexec", backend.SELF, "charge-limit", "80"])
        on_ac.assert_not_called()


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

    def test_slider_loads_actual_limit_and_has_exact_requested_range(self):
        self.assertEqual(self.window.charge_slider.minimum(), 60)
        self.assertEqual(self.window.charge_slider.maximum(), 100)
        self.assertEqual(self.window.charge_slider.value(), 70)
        self.assertEqual(self.window.charge_value.text(), "70%")
        self.assertIn("70%", self.window.current_charge.text())
        self.assertFalse(self.window.charge_dirty)

    def test_refresh_preserves_unsaved_charge_choice_but_updates_current_limit(self):
        self.window.charge_slider.setValue(85)
        self.window.display_status({**self.data, "charge_limit": 75})
        self.assertEqual(self.window.charge_slider.value(), 85)
        self.assertEqual(self.window.charge_value.text(), "85%")
        self.assertIn("75%", self.window.current_charge.text())

    def test_saving_charge_uses_separate_arguments_and_keeps_unapplied_cpu_choice(self):
        self.window.cpu.setCurrentIndex(self.window.cpu.findData("balanced"))
        self.window.charge_slider.setValue(80)
        with patch.object(self.window.apply_process, "start") as start:
            self.window.save_charge_limit()
            start.assert_called_once_with("/usr/local/bin/asus-power-mode", ["charge-limit", "80"])
        self.assertEqual(self.window.apply_action, "charge")
        self.assertTrue(self.window.dirty)
        self.window.busy = False

    def test_unavailable_charge_read_disables_only_charge_controls(self):
        self.window.display_status({**self.data, "charge_limit": None})
        self.assertFalse(self.window.charge_slider.isEnabled())
        self.assertFalse(self.window.charge_button.isEnabled())
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_loading_out_of_range_existing_limit_does_not_write_it(self):
        with patch.object(self.window.apply_process, "start") as start:
            self.window.display_status({**self.data, "charge_limit": 50})
        self.assertEqual(self.window.charge_slider.value(), 60)
        self.assertIn("50%", self.window.current_charge.text())
        start.assert_not_called()

    def test_successful_charge_save_does_not_discard_unapplied_cpu_gpu_choices(self):
        self.window.cpu.setCurrentIndex(self.window.cpu.findData("balanced"))
        self.window.charge_slider.setValue(80)
        self.window.apply_action = "charge"
        with patch.object(self.window.apply_process, "readAllStandardOutput",
                          return_value=QByteArray(b"Battery charge limit saved: 80%.")), \
                patch.object(self.window, "refresh"):
            self.window.apply_finished(0, QProcess.ExitStatus.NormalExit)
        self.assertTrue(self.window.dirty)
        self.assertFalse(self.window.charge_dirty)
        self.assertIn("80%", self.window.message.text())

    def test_cancelled_charge_save_keeps_the_unsaved_slider_choice(self):
        self.window.charge_slider.setValue(80)
        self.window.apply_action = "charge"
        with patch.object(self.window.apply_process, "readAllStandardOutput",
                          return_value=QByteArray(b"Authentication cancelled")), \
                patch.object(self.window, "refresh"), patch("asus_power_control.QMessageBox.warning"):
            self.window.apply_finished(126, QProcess.ExitStatus.NormalExit)
        self.assertTrue(self.window.charge_dirty)
        self.assertEqual(self.window.charge_slider.value(), 80)
        self.assertIn("70%", self.window.current_charge.text())


class OSDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.osd = MonitorOSD()
        self.addCleanup(self.osd.close)

    def test_osd_displays_cpu_gpu_memory_power_voltage_and_fps_fields(self):
        metrics = Metrics(cpu_usage=23, cpu_temperature=71, cpu_power=18.5,
                          memory_used=4 * 1024**3, memory_total=16 * 1024**3,
                          memory_percent=25, gpu_state="Active", gpu_temperature=55,
                          gpu_usage=42, gpu_memory_usage=12, vram_used=500,
                          vram_total=4096, gpu_power=45.5, gpu_voltage=None,
                          fps=None, fps_note="N/A — a game overlay is required for per-game FPS")
        self.osd.display(metrics)
        self.assertEqual(self.osd.rows["cpu_temperature"].text(), "71 °C")
        self.assertEqual(self.osd.rows["cpu_usage"].text(), "23%")
        self.assertEqual(self.osd.rows["gpu_temperature"].text(), "55 °C")
        self.assertEqual(self.osd.rows["gpu_power"].text(), "45.5 W")
        self.assertEqual(self.osd.rows["gpu_voltage"].text(), "N/A")
        self.assertEqual(self.osd.rows["fps"].text(), "N/A")
        self.assertIn("12%", self.osd.rows["vram"].text())

    def test_blocked_gpu_display_is_explicit(self):
        metrics = Metrics(gpu_state="Off (D3cold)", gpu_note="NVIDIA telemetry is intentionally skipped")
        self.osd.display(metrics)
        self.assertEqual(self.osd.rows["gpu_state"].text(), "Off (D3cold)")
        self.assertIn("intentionally skipped", self.osd.note.text())

    def test_metric_filters_hide_selected_rows_without_affecting_collection(self):
        self.osd.set_filters({
            "cpu_basic": True, "cpu_power": False, "memory": False,
            "gpu_basic": True, "gpu_memory": False, "gpu_power": False, "fps": False,
        })
        self.assertFalse(self.osd.rows["cpu_temperature"].isHidden())
        self.assertTrue(self.osd.rows["cpu_power"].isHidden())
        self.assertTrue(self.osd.rows["memory"].isHidden())
        self.assertFalse(self.osd.rows["gpu_temperature"].isHidden())
        self.assertTrue(self.osd.rows["vram"].isHidden())
        self.assertTrue(self.osd.rows["gpu_power"].isHidden())
        self.assertTrue(self.osd.rows["fps"].isHidden())


if __name__ == "__main__":
    unittest.main()
