"""Launch the real GUI; optionally reapply only the already-active CPU/GPU pair."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from asus_power_control import MainWindow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-current", action="store_true")
    parser.add_argument("--screenshot")
    options = parser.parse_args()
    app = QApplication([])
    app.setApplicationName("ASUS Power Control")
    app.setDesktopFileName("org.local.AsusPowerControl")
    window = MainWindow()
    window.show()
    phase = {"started": False, "applied": False, "cpu": None, "gpu": None}

    def finish(code, message):
        print(message, flush=True)
        window.close()
        app.exit(code)

    def applied(code, _):
        if code:
            finish(1, "Live Apply failed: " + window.last_output)
        else:
            phase["applied"] = True

    window.apply_process.finished.connect(applied)

    def check():
        if window.data is None or window.busy:
            return
        data = window.data
        if options.apply_current and not phase["started"]:
            if data["pending_reboot"] or data["gpu_mode"] not in {"integrated", "hybrid", "ultimate"}:
                finish(1, "Refusing live Apply: display routing is unknown or a MUX change is pending.")
                return
            if data["gpu_mode"] == "ultimate" and not data["ac"]:
                finish(1, "Refusing live Apply: Ultimate is active without AC.")
                return
            phase.update(started=True, cpu=data["cpu_mode"], gpu=data["gpu_mode"])
            print(f"Reapplying current pair only: {phase['cpu']} + {phase['gpu']}", flush=True)
            window.apply_selection()
            return
        if options.apply_current:
            if not phase["applied"] or data["intent"].get("mode") != "custom":
                return
            if (data["cpu_mode"], data["gpu_mode"]) != (phase["cpu"], phase["gpu"]):
                finish(1, "The power source or modes changed during the live test; inspect status.")
                return
        if options.screenshot and not window.grab().save(options.screenshot):
            finish(1, "Could not save the GUI preview.")
            return
        finish(0, f"GUI smoke test passed on {app.platformName()}: "
               f"CPU={data['cpu_mode']}, GPU={data['gpu_mode']}, "
               f"pending_reboot={data['pending_reboot']}, GPU power={data['gpu_power']}, "
               f"VRAM={data['video_memory']}.")

    timer = QTimer()
    timer.setInterval(250)
    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(120000, lambda: finish(1, "GUI live verification timed out."))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
