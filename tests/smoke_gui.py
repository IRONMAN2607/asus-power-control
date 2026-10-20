"""Launch the real GUI; optionally resave the active modes or charge limit."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from asus_power_control import MainWindow
from osd import MonitorOSD


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply-current", action="store_true")
    action.add_argument("--save-current-charge-limit", action="store_true")
    parser.add_argument("--screenshot")
    parser.add_argument("--osd", action="store_true")
    options = parser.parse_args()
    app = QApplication([])
    app.setApplicationName("ASUS Power Control")
    app.setDesktopFileName("org.local.AsusPowerControl")
    window = MainWindow()
    window.show()
    osd = MonitorOSD(window) if options.osd else None
    if osd is not None:
        window.hide()
        osd.show()
    phase = {"started": False, "applied": False, "cpu": None, "gpu": None, "charge_limit": None}

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
        if options.save_current_charge_limit and not phase["started"]:
            limit = data.get("charge_limit")
            if not isinstance(limit, int) or not 60 <= limit <= 100:
                finish(1, "Refusing live save: current charge limit is unavailable or outside 60–100%.")
                return
            phase.update(started=True, charge_limit=limit)
            print(f"Resaving current charge limit only: {limit}%", flush=True)
            window.save_charge_limit()
            return
        if options.save_current_charge_limit:
            if not phase["applied"]:
                return
            if data.get("charge_limit") != phase["charge_limit"]:
                finish(1, "The charge limit changed during the live test; inspect status.")
                return
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
        screenshot_target = osd if osd is not None else window
        if options.screenshot and not screenshot_target.grab().save(options.screenshot):
            finish(1, "Could not save the GUI preview.")
            return
        finish(0, f"GUI smoke test passed on {app.platformName()}: "
               f"CPU={data['cpu_mode']}, GPU={data['gpu_mode']}, "
               f"pending_reboot={data['pending_reboot']}, GPU power={data['gpu_power']}, "
               f"VRAM={data['video_memory']}, charge limit={data.get('charge_limit')}%.")

    timer = QTimer()
    timer.setInterval(250)
    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(120000, lambda: finish(1, "GUI live verification timed out."))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
