#!/usr/bin/python3
"""Read-only telemetry sources for the ASUS Power Control OSD."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Optional

import psutil

GPU_PCI = "0000:01:00.0"
NVIDIA_QUERY = (
    "temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,"
    "power.draw,clocks.gr,clocks.mem,clocks.sm"
)


@dataclass
class Metrics:
    cpu_usage: Optional[float] = None
    cpu_temperature: Optional[float] = None
    cpu_power: Optional[float] = None
    cpu_voltage: Optional[float] = None
    memory_used: Optional[int] = None
    memory_total: Optional[int] = None
    memory_percent: Optional[float] = None
    gpu_temperature: Optional[float] = None
    gpu_usage: Optional[float] = None
    gpu_memory_usage: Optional[float] = None
    vram_used: Optional[int] = None
    vram_total: Optional[int] = None
    gpu_power: Optional[float] = None
    gpu_voltage: Optional[float] = None
    gpu_state: str = "Unavailable"
    gpu_note: str = ""
    fps: Optional[float] = None
    fps_note: str = "N/A — a game overlay is required for per-game FPS"


def _number(value):
    value = str(value).strip()
    if not value or value.lower() in {"n/a", "na", "not supported", "unknown"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None else None


def _run(args, timeout=2):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def parse_nvidia_csv(output):
    """Parse nvidia-smi's one-row CSV without trusting N/A fields."""
    fields = [field.strip() for field in output.strip().split(",")]
    if len(fields) < 9:
        return {}
    return {
        "temperature": _number(fields[0]),
        "usage": _number(fields[1]),
        "memory_usage": _number(fields[2]),
        "vram_used": _integer(fields[3]),
        "vram_total": _integer(fields[4]),
        "power": _number(fields[5]),
        "clock_graphics": _integer(fields[6]),
        "clock_memory": _integer(fields[7]),
        "clock_sm": _integer(fields[8]),
    }


class TelemetryCollector:
    """Collect metrics without privilege escalation or intentional GPU wakeups."""

    def __init__(self):
        self._last_energy = None
        self._last_energy_time = None
        self._energy_path = self._find_energy_path()
        self._voltage_counter = 0
        self._last_gpu_voltage = None
        self._fps_path = os.environ.get("ASUS_POWER_CONTROL_FPS_FILE")
        # Prime psutil so the first OSD sample is measured against a baseline.
        psutil.cpu_percent(interval=None)

    @staticmethod
    def _find_energy_path():
        candidates = (
            "/sys/class/powercap/intel-rapl:0/energy_uj",
            "/sys/devices/virtual/powercap/intel-rapl/intel-rapl:0/energy_uj",
        )
        for candidate in candidates:
            if os.access(candidate, os.R_OK):
                return Path(candidate)
        return None

    @staticmethod
    def _cpu_temperature():
        try:
            sensors = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        package = []
        all_coretemp = []
        for name, entries in sensors.items():
            if name.lower() not in {"coretemp", "k10temp", "zenpower", "cpu_thermal"}:
                continue
            for entry in entries:
                if entry.current is None:
                    continue
                all_coretemp.append(float(entry.current))
                label = (entry.label or "").lower()
                if "package" in label or label in {"tctl", "tdie"}:
                    package.append(float(entry.current))
        if package:
            return max(package)
        return max(all_coretemp) if all_coretemp else None

    def _cpu_power(self):
        if self._energy_path is None:
            return None
        try:
            energy = int(self._energy_path.read_text().strip())
        except (OSError, ValueError):
            return None
        now = time.monotonic()
        power = None
        if self._last_energy is not None and self._last_energy_time is not None:
            delta = energy - self._last_energy
            if delta < 0:
                try:
                    maximum = int(Path(str(self._energy_path).replace("energy_uj", "max_energy_range_uj")).read_text())
                    delta += maximum
                except (OSError, ValueError):
                    delta = 0
            elapsed = now - self._last_energy_time
            if elapsed > 0 and delta >= 0:
                power = delta / elapsed / 1_000_000
        self._last_energy = energy
        self._last_energy_time = now
        return power

    @staticmethod
    def _cardwire_gpu():
        result = _run(["/usr/bin/cardwire", "list", "--json"])
        if result is None or result.returncode != 0:
            return None, None
        try:
            devices = json.loads(result.stdout)
        except (TypeError, ValueError):
            return None, None
        for gpu_id, device in devices.items():
            if device.get("pci") == GPU_PCI:
                return str(gpu_id), device
        return None, None

    @staticmethod
    def _cardwire_power(gpu_id):
        if gpu_id is None:
            return "Unavailable"
        result = _run(["/usr/bin/cardwire", "gpu", gpu_id, "--power"])
        if result is None or result.returncode != 0:
            return "Unavailable"
        return result.stdout.strip() or "Unavailable"

    def _gpu(self, metrics):
        gpu_id, device = self._cardwire_gpu()
        if device is None:
            metrics.gpu_state = "Unavailable"
            metrics.gpu_note = "Cardwire did not report the RTX 3050"
            return
        if device.get("blocked"):
            power = self._cardwire_power(gpu_id)
            metrics.gpu_state = "Off (D3cold)" if power.lower() == "d3cold" else "Blocked"
            metrics.gpu_note = "NVIDIA telemetry is intentionally skipped while Cardwire blocks the GPU"
            return
        # Cardwire IDs are daemon-local (the RTX is ID 1 here), while nvidia-smi
        # accepts the stable PCI bus identifier. Never pass the Cardwire ID as an
        # nvidia-smi index.
        result = _run(["/usr/bin/nvidia-smi", "--id=00000000:01:00.0",
                       f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"])
        if result is None or result.returncode != 0:
            metrics.gpu_state = "Available"
            metrics.gpu_note = "NVIDIA telemetry unavailable"
            return
        parsed = parse_nvidia_csv(result.stdout)
        if not parsed:
            metrics.gpu_state = "Available"
            metrics.gpu_note = "NVIDIA returned no telemetry"
            return
        metrics.gpu_state = "Active"
        metrics.gpu_temperature = parsed.get("temperature")
        metrics.gpu_usage = parsed.get("usage")
        metrics.gpu_memory_usage = parsed.get("memory_usage")
        metrics.vram_used = parsed.get("vram_used")
        metrics.vram_total = parsed.get("vram_total")
        metrics.gpu_power = parsed.get("power")
        self._voltage_counter += 1
        if self._voltage_counter % 5 == 1:
            voltage = _run(["/usr/bin/nvidia-settings", "-q", "[gpu:0]/GPUCurrentCoreVoltage", "-t"])
            if voltage is not None and voltage.returncode == 0:
                self._last_gpu_voltage = _number(voltage.stdout)
        metrics.gpu_voltage = self._last_gpu_voltage

    def _fps(self):
        if not self._fps_path:
            return None, "N/A — a game overlay is required for per-game FPS"
        try:
            value = _number(Path(self._fps_path).read_text())
        except (OSError, ValueError):
            return None, "N/A — FPS source is unavailable"
        return value, "External FPS source"

    def sample(self):
        metrics = Metrics()
        metrics.cpu_usage = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        metrics.memory_used = memory.used
        metrics.memory_total = memory.total
        metrics.memory_percent = memory.percent
        metrics.cpu_temperature = self._cpu_temperature()
        metrics.cpu_power = self._cpu_power()
        self._gpu(metrics)
        metrics.fps, metrics.fps_note = self._fps()
        return metrics
