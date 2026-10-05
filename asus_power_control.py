#!/usr/bin/python3
"""Small, unprivileged Qt frontend for the installed ASUS policy helper."""

import json
from pathlib import Path
import sys

from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

CPU_NAMES = {"silent": "Silent", "balanced": "Balanced", "performance": "Performance"}
GPU_NAMES = {"integrated": "Integrated", "hybrid": "Hybrid", "ultimate": "Ultimate (Dedicated)"}
CPU_HELP = {
    "silent": "Power Saver / ASUS Quiet: lower power use and quieter fans.",
    "balanced": "Balanced CPU and platform performance for everyday use.",
    "performance": "Performance CPU and platform profile; higher fan activity as needed.",
}
GPU_HELP = {
    "integrated": "Intel display; NVIDIA access blocked so the dGPU can power down.",
    "hybrid": "Intel display; NVIDIA available on demand for games and applications.",
    "ultimate": "NVIDIA directly drives the display. Requires AC; changing the MUX needs a reboot.",
}


class MainWindow(QMainWindow):
    def __init__(self, backend="/usr/local/bin/asus-power-mode", auto_refresh=True):
        super().__init__()
        self.backend = backend
        self.data = None
        self.dirty = False
        self.busy = False
        self.syncing = False
        self.last_output = ""
        self.setWindowTitle("ASUS Power Control")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("icon.svg"))))
        self.resize(650, 590)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        heading = QLabel("ASUS Power Control")
        font = heading.font()
        font.setPointSize(20)
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        subtitle = QLabel("Independent CPU performance and GPU modes • 144 Hz preserved")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.source = QLabel("Reading current state…")
        layout.addWidget(self.source)
        choices = QHBoxLayout()
        self.cpu, self.cpu_help, cpu_group = self.make_selector("CPU mode", CPU_NAMES)
        self.gpu, self.gpu_help, gpu_group = self.make_selector("GPU mode", GPU_NAMES)
        choices.addWidget(cpu_group)
        choices.addWidget(gpu_group)
        layout.addLayout(choices)
        self.cpu.currentIndexChanged.connect(self.selection_changed)
        self.gpu.currentIndexChanged.connect(self.selection_changed)

        state_group = QGroupBox("Live status")
        state_layout = QFormLayout(state_group)
        state_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        state_layout.setVerticalSpacing(10)
        self.current_cpu = QLabel("—")
        self.current_gpu = QLabel("—")
        self.gpu_power = QLabel("—")
        self.pending = QLabel("—")
        for title, label in (("CPU profile", self.current_cpu), ("Active GPU mode", self.current_gpu),
                             ("NVIDIA power", self.gpu_power), ("Firmware request", self.pending)):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            state_layout.addRow(title, label)
        layout.addWidget(state_group)

        policy = QLabel("Manual choices are retained across reboot on the same power source. "
                        "Unplugging or replugging restores automatic battery/AC settings.")
        policy.setWordWrap(True)
        layout.addWidget(policy)
        self.message = QLabel("Choose CPU and GPU modes, then click Apply.")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        layout.addStretch()

        buttons = QHBoxLayout()
        self.details_button = QPushButton("Details")
        self.details_button.clicked.connect(self.show_details)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.apply_selection)
        self.apply_button.setEnabled(False)
        buttons.addWidget(self.details_button)
        buttons.addStretch()
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self.status_process = QProcess(self)
        self.status_process.finished.connect(self.status_finished)
        self.status_process.errorOccurred.connect(self.status_error)
        self.status_timeout = QTimer(self)
        self.status_timeout.setSingleShot(True)
        self.status_timeout.setInterval(30000)
        self.status_timeout.timeout.connect(self.status_timed_out)
        self.apply_process = QProcess(self)
        self.apply_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.apply_process.finished.connect(self.apply_finished)
        self.apply_process.errorOccurred.connect(self.apply_error)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.selection_changed()
        if auto_refresh:
            self.timer.start()
            QTimer.singleShot(0, self.refresh)

    @staticmethod
    def make_selector(title, options):
        group = QGroupBox(title)
        layout = QVBoxLayout(group)
        combo = QComboBox()
        for value, name in options.items():
            combo.addItem(name, value)
        layout.addWidget(combo)
        help_label = QLabel()
        help_label.setWordWrap(True)
        help_label.setMinimumHeight(65)
        layout.addWidget(help_label)
        layout.addStretch()
        return combo, help_label, group

    def selection_changed(self, *_):
        self.cpu_help.setText(CPU_HELP[self.cpu.currentData()])
        self.gpu_help.setText(GPU_HELP[self.gpu.currentData()])
        if self.data is not None and not self.syncing:
            self.dirty = True
        self.update_controls()

    def update_controls(self):
        valid = self.data is not None and not self.busy
        self.cpu.setEnabled(valid)
        self.gpu.setEnabled(valid)
        self.refresh_button.setEnabled(not self.busy)
        ultimate = self.gpu.findData("ultimate")
        allowed = bool(self.data and self.data["ac"])
        self.gpu.model().item(ultimate).setEnabled(allowed)
        self.apply_button.setEnabled(valid and (self.gpu.currentData() != "ultimate" or allowed))

    def refresh(self):
        if self.busy or self.status_process.state() != QProcess.ProcessState.NotRunning:
            return
        self.status_process.start(self.backend, ["status-json"])
        self.status_timeout.start()

    def status_timed_out(self):
        self.status_process.kill()  # Read-only diagnostics, never an applying process.
        self.message.setText("Status lookup timed out. Click Refresh to try again.")
        self.data = None
        self.update_controls()

    def status_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.status_timeout.stop()
            self.data = None
            self.message.setText("Could not start the installed power helper: " + self.status_process.errorString())
            self.update_controls()

    def status_finished(self, code, exit_status):
        self.status_timeout.stop()
        output = bytes(self.status_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        error = bytes(self.status_process.readAllStandardError()).decode("utf-8", errors="replace")
        try:
            if code or exit_status != QProcess.ExitStatus.NormalExit:
                raise ValueError(error.strip() or "Status helper did not finish successfully.")
            self.display_status(json.loads(output))
        except (ValueError, KeyError, TypeError) as exc:
            self.data = None
            self.message.setText("Cannot read current state: " + str(exc))
            self.update_controls()

    def display_status(self, data):
        source_changed = self.data is not None and self.data["ac"] != data["ac"]
        self.data = data
        self.source.setText("Power source: " + ("AC connected" if data["ac"] else "Battery"))
        self.current_cpu.setText(f"{CPU_NAMES.get(data['cpu_mode'], 'Unknown')} — "
                                 f"{data['profile']} / ASUS {data['platform_profile']}")
        self.current_gpu.setText(f"{GPU_NAMES.get(data['gpu_mode'], 'Unknown')} — "
                                 f"{data['display_gpu']} display")
        self.gpu_power.setText("Verified off — D3cold + video memory off" if data["power_off_verified"]
                               else f"{data['gpu_power']} • Video memory: {data['video_memory']}")
        if data["pending_reboot"]:
            target = GPU_NAMES.get(data["pending_gpu"], "GPU firmware change")
            self.pending.setText(f"{target} requested — reboot required. "
                                 "The active display mode above remains in use until reboot.")
        else:
            self.pending.setText("No pending MUX change")
        if not self.dirty:
            self.syncing = True
            for combo, key in ((self.cpu, "cpu"), (self.gpu, "gpu")):
                index = combo.findData(data["selection"][key])
                if index >= 0:
                    combo.setCurrentIndex(index)
            self.syncing = False
        if source_changed:
            self.message.setText("Power source changed; automatic settings may have been reapplied. "
                                 "Your unapplied choices remain in the selectors." if self.dirty else
                                 "Power source changed; live status shows the current automatic settings.")
        self.update_controls()

    def apply_selection(self):
        if not self.apply_button.isEnabled():
            return
        # Finish the read-only lookup before starting mutations to avoid stale UI.
        if self.status_process.state() != QProcess.ProcessState.NotRunning:
            self.status_timeout.stop()
            self.status_process.kill()
            self.status_process.waitForFinished(1000)
        self.busy = True
        self.update_controls()
        self.message.setText("Applying choices… Complete the KDE administrator prompt if it appears.")
        self.apply_process.start(self.backend, ["custom", self.cpu.currentData(), self.gpu.currentData()])

    def apply_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.busy = False
            self.message.setText("Could not apply choices: " + self.apply_process.errorString())
            self.update_controls()

    def apply_finished(self, code, exit_status):
        self.busy = False
        self.last_output = bytes(self.apply_process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        if code == 0 and exit_status == QProcess.ExitStatus.NormalExit:
            self.dirty = False
            if "REBOOT REQUIRED" in self.last_output:
                self.message.setText("Choices saved. Save your work and reboot normally to finish the GPU switch.")
                QMessageBox.information(self, "GPU switch needs a reboot",
                                        "Your CPU profile is applied. The GPU MUX request is queued.\n\n"
                                        "Save your work and reboot when ready. The app will not reboot for you.")
            else:
                self.message.setText("Choices applied. NVIDIA power-down may take around 25 seconds "
                                     "after existing GPU workloads release it.")
        else:
            message = self.last_output or "Authentication was cancelled or the helper failed."
            self.message.setText("Choices were not fully applied. See Details and check live status.")
            QMessageBox.warning(self, "Could not apply choices", message)
        self.update_controls()
        self.refresh()

    def show_details(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("ASUS Power Control — Details")
        dialog.setText("Live diagnostics and the most recent apply result.")
        dialog.setDetailedText(json.dumps(self.data, indent=2) + "\n\nLast apply:\n" +
                               (self.last_output or "No changes made in this session."))
        dialog.exec()

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, "Applying choices", "Finish or cancel the administrator prompt "
                                    "and wait for the operation to complete before closing.")
            event.ignore()
            return
        self.timer.stop()
        self.status_timeout.stop()
        if self.status_process.state() != QProcess.ProcessState.NotRunning:
            self.status_process.kill()
            self.status_process.waitForFinished(1000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("ASUS Power Control")
    app.setDesktopFileName("org.local.AsusPowerControl")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
