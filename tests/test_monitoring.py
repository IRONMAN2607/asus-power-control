"""Telemetry parser and OSD safety tests; no hardware writes or GPU mode changes."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitoring import Metrics, TelemetryCollector, parse_nvidia_csv


class MonitoringTests(unittest.TestCase):
    def test_nvidia_csv_parses_all_requested_live_fields(self):
        values = parse_nvidia_csv("50, 14, 6, 429, 4096, 12.38, 1500, 6001, 1500")
        self.assertEqual(values["temperature"], 50)
        self.assertEqual(values["usage"], 14)
        self.assertEqual(values["memory_usage"], 6)
        self.assertEqual(values["vram_used"], 429)
        self.assertEqual(values["vram_total"], 4096)
        self.assertEqual(values["power"], 12.38)

    def test_nvidia_na_values_are_reported_as_unavailable(self):
        values = parse_nvidia_csv("N/A, N/A, N/A, N/A, 4096, N/A, 1500, 6001, 1500")
        for key in ("temperature", "usage", "memory_usage", "vram_used", "power"):
            self.assertIsNone(values[key])
        self.assertEqual(values["vram_total"], 4096)

    def test_blocked_gpu_skips_nvidia_smi_and_reports_power_state(self):
        collector = TelemetryCollector()
        results = [
            type("Result", (), {"returncode": 0, "stdout": json.dumps({"1": {
                "pci": "0000:01:00.0", "blocked": True
            }}), "stderr": ""})(),
            type("Result", (), {"returncode": 0, "stdout": "D3cold", "stderr": ""})(),
        ]
        with patch("monitoring._run", side_effect=results) as run:
            metrics = Metrics()
            collector._gpu(metrics)
        self.assertEqual(metrics.gpu_state, "Off (D3cold)")
        self.assertIsNone(metrics.gpu_temperature)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("nvidia-smi", run.call_args_list[-1].args[0][0])

    def test_available_gpu_uses_pci_id_not_cardwire_id(self):
        collector = TelemetryCollector()
        collector._voltage_counter = 1
        results = [
            type("Result", (), {"returncode": 0, "stdout": json.dumps({"1": {
                "pci": "0000:01:00.0", "blocked": False
            }}), "stderr": ""})(),
            type("Result", (), {"returncode": 0, "stdout": "50, 40, 20, 800, 4096, 55.0, 1500, 6000, 1500", "stderr": ""})(),
        ]
        with patch("monitoring._run", side_effect=results) as run:
            metrics = Metrics()
            collector._gpu(metrics)
        self.assertEqual(metrics.gpu_state, "Active")
        self.assertEqual(metrics.gpu_power, 55.0)
        self.assertIn("--id=00000000:01:00.0", run.call_args_list[1].args[0])
        self.assertNotIn("--id=1", run.call_args_list[1].args[0])

    def test_missing_fps_source_is_explicitly_not_available(self):
        with patch.dict("os.environ", {}, clear=False):
            collector = TelemetryCollector()
        fps, note = collector._fps()
        self.assertIsNone(fps)
        self.assertIn("game overlay", note)

    def test_fps_file_is_optional_and_numeric(self):
        with patch.dict("os.environ", {"ASUS_POWER_CONTROL_FPS_FILE": "/tmp/fps-value"}):
            collector = TelemetryCollector()
            with patch("monitoring.Path.read_text", return_value="143.7\n"):
                fps, note = collector._fps()
        self.assertEqual(fps, 143.7)
        self.assertEqual(note, "External FPS source")


if __name__ == "__main__":
    unittest.main()
