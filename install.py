#!/usr/bin/python3
"""Install the FX517ZC integration, or restore its recorded pre-install files."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent
SYSTEM_BACKUPS = Path("/var/lib/asus-power-control-install")
SERVICES = (
    "tuned.service", "asus-power-source.service", "asus-shutdown.service",
    "cardwired.service", "power-profiles-daemon.service", "nvidia-powerd.service",
)
SYSTEM_FILES = {
    "backend/asus-power-mode": ("/usr/local/bin/asus-power-mode", 0o755),
    "packaging/asus-power-source.service": ("/etc/systemd/system/asus-power-source.service", 0o644),
    "packaging/60-cardwire-logging.conf": ("/etc/systemd/system/cardwired.service.d/60-logging.conf", 0o644),
    "packaging/80-asus-gpu-shutdown.conf": ("/etc/systemd/logind.conf.d/80-asus-gpu-shutdown.conf", 0o644),
    "docs/verification.md": ("/usr/local/share/asus-power-control/verification.md", 0o644),
}
USER_PAYLOAD = (
    "asus_power_control.py", "monitoring.py", "osd.py", "icon.svg", "README.md", "requirements.txt", "install.py",
    "backend/asus-power-mode", "tests/test_power_control.py", "tests/smoke_gui.py",
    "tests/test_install.py", "tests/test_monitoring.py", "docs/usage.md", "docs/architecture.md",
    "docs/reinstall-fedora.md", "docs/verification.md",
    "packaging/asus-power-control", "packaging/org.local.AsusPowerControl.desktop.in",
    "packaging/asus-power-source.service", "packaging/cardwire.toml",
    "packaging/asusd-policy.json", "packaging/60-cardwire-logging.conf",
    "packaging/80-asus-gpu-shutdown.conf", "packaging/60-powerdevil-ddc-workaround.conf",
)


def command(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: "
                           f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class FileChanges:
    """Keep the first pre-install version of each changed file, including links."""

    def __init__(self, store, dry_run=False):
        self.store = Path(store)
        self.manifest_path = self.store / "manifest.json"
        self.dry_run = dry_run
        self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {
            "files": {}, "services": {},
        }

    def save(self):
        if self.dry_run:
            return
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store.chmod(0o700)
        atomic_write(self.manifest_path, json.dumps(self.manifest, indent=2).encode(), 0o600)

    def remember(self, path):
        path = Path(path)
        key = str(path)
        if self.dry_run or key in self.manifest["files"]:
            return
        if path.is_symlink():
            record = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            original = path.stat()
            name = hashlib.sha256(key.encode()).hexdigest()
            backup = self.store / "backups" / name
            atomic_write(backup, path.read_bytes(), 0o600)
            record = {"kind": "file", "backup": name, "mode": stat.S_IMODE(original.st_mode),
                      "uid": original.st_uid, "gid": original.st_gid}
        elif path.exists():
            raise RuntimeError(f"Refusing to replace a non-file: {path}")
        else:
            record = {"kind": "absent"}
        self.manifest["files"][key] = record
        self.save()

    def write(self, path, content, mode=0o644):
        path = Path(path)
        if isinstance(content, str):
            content = content.encode()
        if not path.is_symlink() and path.is_file() and path.read_bytes() == content:
            if stat.S_IMODE(path.stat().st_mode) == mode:
                return
        print(f"{'Would install' if self.dry_run else 'Installing'} {path}")
        if not self.dry_run:
            self.remember(path)
            atomic_write(path, content, mode)

    def link(self, path, target):
        path = Path(path)
        if path.is_symlink() and os.readlink(path) == target:
            return
        print(f"{'Would link' if self.dry_run else 'Linking'} {path} -> {target}")
        if not self.dry_run:
            self.remember(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.exists():
                path.unlink()
            path.symlink_to(target)

    def remove(self, path):
        path = Path(path)
        if path.exists() or path.is_symlink():
            print(f"{'Would remove' if self.dry_run else 'Removing'} {path}")
            if not self.dry_run:
                self.remember(path)
                path.unlink()

    def restore(self):
        for name, record in reversed(list(self.manifest["files"].items())):
            path = Path(name)
            print(f"{'Would restore' if self.dry_run else 'Restoring'} {path}")
            if self.dry_run:
                continue
            if record["kind"] == "file":
                content = (self.store / "backups" / record["backup"]).read_bytes()
                atomic_write(path, content, record["mode"])
                if os.geteuid() == 0:
                    os.chown(path, record["uid"], record["gid"])
            else:
                if path.exists() or path.is_symlink():
                    path.unlink()
                if record["kind"] == "symlink":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(record["target"])


def patch_ron(text, settings):
    """Update only known top-level asusd fields; retain charging/fan settings."""
    if not re.search(r"\)\s*$", text):
        raise RuntimeError("Unrecognized asusd RON format; expected an outer (...).")
    for key, value in settings.items():
        pattern = rf"(?m)^([ \t]*){re.escape(key)}:[^\n]*$"
        matches = list(re.finditer(pattern, text))
        replacement = f"    {key}: {json.dumps(value)},"
        if len(matches) > 1:
            raise RuntimeError(f"Ambiguous asusd field: {key}")
        if matches:
            text = re.sub(pattern, lambda match: f"{match[1]}{key}: {json.dumps(value)},", text)
        else:
            closing = re.search(r"\)\s*$", text).start()
            text = text[:closing] + replacement + "\n" + text[closing:]
    return text


def patch_toml(text, settings):
    """Merge global Cardwire settings without replacing unrelated sections."""
    tomllib.loads(text)
    header = re.search(r"(?m)^\s*\[", text)
    top, rest = (text[:header.start()], text[header.start():]) if header else (text, "")
    for key, value in settings.items():
        pattern = rf"(?m)^\s*{re.escape(key)}\s*=[^\n]*$"
        replacement = f"{key} = {json.dumps(value)}"
        if re.search(pattern, top):
            top = re.sub(pattern, lambda _: replacement, top)
        else:
            top = top.rstrip() + "\n" + replacement + "\n"
    merged = top.rstrip() + "\n" + rest
    tomllib.loads(merged)
    return merged


def patch_kconfig(text, groups):
    """Change selected KDE keys while preserving other sections and actions."""
    lines = text.splitlines()
    for group, values in groups.items():
        header = f"[{group}]"
        try:
            start = lines.index(header)
        except ValueError:
            if lines and lines[-1]:
                lines.append("")
            lines.append(header)
            start = len(lines) - 1
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].startswith("[")), len(lines))
        body = lines[start + 1:end]
        for key, value in values.items():
            body = [line for line in body if not line.startswith(key + "=")]
            body.append(f"{key}={value}")
        lines[start + 1:end] = body
    return "\n".join(lines).rstrip() + "\n"


def user_paths():
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
    config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
    if not all(path.is_absolute() for path in (data, config, state)):
        raise RuntimeError("XDG data/config/state paths must be absolute")
    return home, data, config, state


def desktop_entry(launcher, icon):
    # Exec has a separate quoting grammar; percent must not become a field code.
    executable = str(launcher).replace("\\", "\\\\\\\\").replace('"', '\\\\"')
    executable = executable.replace("$", "\\\\$").replace("`", "\\\\`").replace("%", "%%")
    return (ROOT / "packaging/org.local.AsusPowerControl.desktop.in").read_text().replace(
        "@EXEC@", '"' + executable + '"').replace("@ICON@", str(icon))


def ddc_workaround(setting):
    if setting != "auto":
        return setting == "on"
    return command("rpm", "-q", "--qf", "%{VERSION}", "libddcutil", check=False) == "2.2.1"


def preflight(system=False):
    release = Path("/etc/os-release").read_text()
    if not re.search(r"(?m)^ID=['\"]?fedora['\"]?$", release) or Path("/run/ostree-booted").exists():
        raise RuntimeError("This installer supports conventional Fedora KDE; see docs/reinstall-fedora.md")
    model = Path("/sys/class/dmi/id/product_name").read_text().strip()
    if "FX517ZC" not in model:
        raise RuntimeError(f"This hardware-specific backend targets FX517ZC, not {model}")
    if not system and os.environ.get("XDG_SESSION_TYPE") != "wayland":
        raise RuntimeError("Run the installer from your KDE Wayland desktop session")
    if importlib.util.find_spec("PySide6") is None:
        raise RuntimeError("Install python3-pyside6 first; see docs/reinstall-fedora.md")
    if importlib.util.find_spec("psutil") is None:
        raise RuntimeError("Install python3-psutil first; see docs/reinstall-fedora.md")
    for name in ("asusctl", "cardwire", "powerprofilesctl", "busctl", "systemctl", "pkexec"):
        if not shutil.which(name):
            raise RuntimeError(f"Missing command: {name}; see docs/reinstall-fedora.md")
    for package in ("tuned-ppd", "switcheroo-control", "supergfxctl", "supergfxd"):
        result = subprocess.run(["rpm", "-q", package], capture_output=True)
        if result.returncode == 0:
            raise RuntimeError(f"Replace/remove {package} before installation; see the reinstall guide")
    if not Path("/sys/class/power_supply/ACAD/online").exists():
        raise RuntimeError("The backend requires this model's ACAD power source")
    if not Path("/proc/driver/nvidia/gpus/0000:01:00.0/power").exists():
        raise RuntimeError("Load the NVIDIA driver for 0000:01:00.0 before installation")
    if system:
        if "bpf" not in Path("/sys/kernel/security/lsm").read_text().strip().split(","):
            raise RuntimeError("Cardwire requires BPF LSM; see its upstream requirements")
        if not Path("/etc/asusd/asusd.ron").is_file():
            raise RuntimeError("Start asusd once to generate /etc/asusd/asusd.ron")
    for name in ("dgpu_disable", "gpu_mux_mode"):
        for prop in ("CurrentValue", "QueuedGpuValue"):
            command("busctl", "--system", "get-property", "xyz.ljones.Asusd",
                    f"/xyz/ljones/asus_armoury/{name}", "xyz.ljones.AsusArmoury", prop)
    command("busctl", "--system", "get-property", "xyz.ljones.Asusd", "/xyz/ljones",
            "xyz.ljones.Platform", "ChargeControlEndThreshold")
    for unit in SERVICES[1:5]:
        if command("systemctl", "show", "--property=LoadState", "--value", unit) == "not-found":
            if unit != "asus-power-source.service":
                raise RuntimeError(f"Missing required unit: {unit}")
    for name in USER_PAYLOAD:
        if not (ROOT / name).is_file():
            raise RuntimeError(f"Incomplete checkout: missing {name}")
    print(f"Preflight passed: {model}; Fedora; ASUS firmware controls available.")


def install_system():
    changes = FileChanges(SYSTEM_BACKUPS)
    if not changes.manifest["services"]:
        changes.manifest["services"] = {
            name: command("systemctl", "is-enabled", name, check=False) for name in SERVICES
        }
        changes.save()
    asusd = Path("/etc/asusd/asusd.ron")
    settings = json.loads((ROOT / "packaging/asusd-policy.json").read_text())
    changes.write(asusd, patch_ron(asusd.read_text(), settings))
    cardwire = Path("/etc/cardwire/cardwire.toml")
    content = cardwire.read_text() if cardwire.exists() else ""
    values = tomllib.loads((ROOT / "packaging/cardwire.toml").read_text())
    changes.write(cardwire, patch_toml(content, values), 0o600)
    for source, (target, mode) in SYSTEM_FILES.items():
        changes.write(target, (ROOT / source).read_bytes(), mode)
    for alias in ("asus-gaming-mode", "asus-battery-mode"):
        changes.link(Path("/usr/local/bin") / alias, "asus-power-mode")
    state_dir = Path("/var/lib/asus-power-mode")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    intent = state_dir / "intent.json"
    if not intent.exists():
        ac = Path("/sys/class/power_supply/ACAD/online").read_text().strip() == "1"
        mux = command("busctl", "--system", "get-property", "xyz.ljones.Asusd",
                      "/xyz/ljones/asus_armoury/gpu_mux_mode", "xyz.ljones.AsusArmoury", "CurrentValue")
        initial = {"mode": "normal" if ac else "battery", "last_ac": ac}
        if ac and mux.split()[-1] == "0":
            initial.update(mode="custom", cpu="performance", gpu="ultimate")
        changes.write(intent, json.dumps(initial) + "\n")
    command("systemctl", "daemon-reload")
    if changes.manifest["services"]["tuned.service"] not in {"not-found", ""}:
        command("systemctl", "disable", "--now", "tuned.service", check=False)
        command("systemctl", "mask", "tuned.service")
    enabled = ["asus-shutdown.service", "cardwired.service", "power-profiles-daemon.service",
               "asus-power-source.service"]
    for unit in enabled:
        command("systemctl", "unmask", unit)
    if command("systemctl", "show", "--property=LoadState", "--value", "nvidia-powerd.service") != "not-found":
        command("systemctl", "unmask", "nvidia-powerd.service")
        enabled.append("nvidia-powerd.service")
    command("systemctl", "enable", *enabled)
    if shutil.which("restorecon"):
        command("restorecon", "-RF", "/usr/local/bin/asus-power-mode", "/etc/asusd",
                "/etc/cardwire", "/etc/systemd/system/asus-power-source.service",
                "/etc/systemd/system/cardwired.service.d", "/etc/systemd/logind.conf.d",
                "/var/lib/asus-power-mode", "/usr/local/share/asus-power-control")
    print("System integration installed; daemon changes take effect after your next normal reboot.")


def restore_system():
    changes = FileChanges(SYSTEM_BACKUPS)
    if not changes.manifest_path.exists():
        print("No system installer backup exists; nothing to restore.")
        return
    command("systemctl", "stop", "asus-power-source.service", check=False)
    for unit in SERVICES:
        command("systemctl", "disable", unit, check=False)
    # Removing app files first also restores symlinks created by masking TuneD.
    command("systemctl", "unmask", "tuned.service", check=False)
    changes.restore()
    command("systemctl", "daemon-reload")
    for unit, original in changes.manifest["services"].items():
        if original == "masked":
            command("systemctl", "mask", unit, check=False)
        elif original in {"enabled", "enabled-runtime", "linked", "linked-runtime"}:
            command("systemctl", "unmask", unit, check=False)
            command("systemctl", "enable", unit, check=False)
    if shutil.which("restorecon"):
        restored = [name for name in changes.manifest["files"] if Path(name).exists()]
        if restored:
            command("restorecon", "-F", *restored)
    changes.manifest_path.unlink()
    print("System files restored. Reboot normally to load restored daemon settings.")


def install_user(options):
    home, data, config, state = user_paths()
    changes = FileChanges(state / "asus-power-control-install", options.dry_run)
    app_dir = data / "asus-power-control"
    for source in USER_PAYLOAD:
        changes.write(app_dir / source, (ROOT / source).read_bytes())
    launcher = home / ".local/bin/asus-power-control"
    changes.write(launcher, (ROOT / "packaging/asus-power-control").read_bytes(), 0o755)
    desktop = data / "applications/org.local.AsusPowerControl.desktop"
    changes.write(desktop, desktop_entry(launcher, app_dir / "icon.svg"))
    powerdevil = config / "powerdevilrc"
    content = powerdevil.read_text() if powerdevil.exists() else ""
    groups = {f"{source}][Performance": {"PowerProfile": ""}
              for source in ("AC", "Battery", "LowBattery")}
    if options.battery_display_settings:
        groups["Battery][Display"] = {
            "UseProfileSpecificDisplayBrightness": "true", "DisplayBrightness": "32",
            "DimDisplayWhenIdle": "true", "DimDisplayIdleTimeoutSec": "120",
            "TurnOffDisplayWhenIdle": "true", "TurnOffDisplayIdleTimeoutSec": "300",
        }
    changes.write(powerdevil, patch_kconfig(content, groups), 0o600)
    override = config / "systemd/user/plasma-powerdevil.service.d/60-cardwire.conf"
    if ddc_workaround(options.ddc_workaround):
        changes.write(override, (ROOT / "packaging/60-powerdevil-ddc-workaround.conf").read_bytes())
    elif options.ddc_workaround == "off" and override.exists():
        if "POWERDEVIL_NO_DDCUTIL=1" in override.read_text():
            changes.remove(override)
    if not options.dry_run:
        refresh_desktop(data)
        command("systemctl", "--user", "daemon-reload")
    print("User GUI installed" if not options.dry_run else "User dry run complete; no files changed")


def restore_user():
    _, data, _, state = user_paths()
    changes = FileChanges(state / "asus-power-control-install")
    if not changes.manifest_path.exists():
        print("No user installer backup exists; nothing to restore.")
        return
    changes.restore()
    refresh_desktop(data)
    command("systemctl", "--user", "daemon-reload")
    changes.manifest_path.unlink()


def refresh_desktop(data):
    if shutil.which("update-desktop-database"):
        command("update-desktop-database", str(data / "applications"))
    if shutil.which("kbuildsycoca6"):
        command("kbuildsycoca6", "--noincremental")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--system-only", action="store_true", help="system phase (normally called by installer)")
    scope.add_argument("--user-only", action="store_true", help="GUI/KDE phase only; system integration must exist")
    parser.add_argument("--dry-run", action="store_true", help="check prerequisites and print plan without changing files")
    parser.add_argument("--uninstall", action="store_true", help="restore the installer's original files/enablement")
    parser.add_argument("--battery-display-settings", action="store_true", help="32%% battery brightness, 120s dim, 300s off")
    parser.add_argument("--ddc-workaround", choices=("auto", "on", "off"), default="auto")
    options = parser.parse_args()
    if options.system_only:
        if os.geteuid() != 0:
            raise RuntimeError("The system phase requires root; use the normal installer as your desktop user")
        if options.dry_run:
            raise RuntimeError("Run --dry-run as your normal desktop user")
        if options.uninstall:
            restore_system()
        else:
            preflight(system=True)
            install_system()
        return 0
    if os.geteuid() == 0:
        raise RuntimeError("Run python3 install.py as your normal desktop user, not with sudo")
    if options.uninstall:
        if options.dry_run:
            raise RuntimeError("Use --uninstall without --dry-run to restore captured backups")
        if not options.user_only:
            subprocess.run(["sudo", "/usr/bin/python3", str(ROOT / "install.py"),
                            "--system-only", "--uninstall"], check=True)
        restore_user()
    else:
        preflight()
        if options.dry_run:
            print("System phase would patch ASUS/Cardwire configuration, install the helper/units, "
                  "and enable graphical.target boot policy. BPF and private config checks run as root during install.")
        elif not options.user_only:
            subprocess.run(["sudo", "/usr/bin/python3", str(ROOT / "install.py"), "--system-only"], check=True)
        install_user(options)
    print("Done. Save your work and reboot normally to load configuration." if not options.dry_run else
          "Dry run finished. No system/user files or service settings were changed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Installation error: {error}", file=sys.stderr)
        sys.exit(1)
