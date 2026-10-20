# Architecture

## Ownership

- **Power Profiles Daemon (PPD)** owns platform profiles and CPU EPP.
- **asusd** owns ASUS firmware, firmware-managed fans and charging. Its separate
  automatic profile/EPP changes are disabled to avoid racing PPD.
- **Cardwire** controls application GPU access through BPF LSM. NVIDIA's driver
  supplies native runtime D3; access blocking alone is not proof of power-off.
- **KDE PowerDevil** owns brightness, idle display behaviour, lid/power-button
  actions, and the power-profile UI. Saved automatic profile selections are
  cleared so they do not overwrite explicit mode requests on startup.
- **asus-power-source.service** is an event-driven oneshot run at boot and by
  asusd AC/battery callbacks. It is enabled under `graphical.target`, after PPD.
- **asus-shutdown.service**, supplied by asusctl, applies deferred firmware GPU
  settings during orderly shutdown. The logind maximum allowance is 45 seconds.

The application closes without affecting the boot/AC policy. Its five-second
read-only status refresh runs only while the GUI is open.

## System monitor OSD

The application registers the KDE desktop shortcut `Ctrl+Alt+S`. KDE invokes the
same launcher with `--toggle-osd`; a local Qt socket toggles the OSD in the
already-running application, so the hotkey does not create duplicate windows.
If the application is not running, the shortcut starts it directly into the OSD.

The OSD is a click-through, top-left, subdued-transparent, always-on-top Wayland window. It samples
once per second only while visible. CPU/RAM telemetry uses psutil and Linux
hwmon; CPU package power uses unprivileged Intel RAPL only when its energy file
is readable. On this Secure Boot system that file may be root-only, so CPU power
is honestly shown as unavailable when necessary.

For the NVIDIA GPU, Cardwire is queried before `nvidia-smi`. A blocked GPU is
reported as Blocked/D3cold and NVIDIA polling is skipped. An available GPU is
queried for temperature, utilization, VRAM, clocks and power draw. Voltage is an
optional best-effort `nvidia-settings` value. Game FPS is not a universal KDE
Wayland metric; the OSD displays N/A unless `ASUS_POWER_CONTROL_FPS_FILE` points
to a file containing a numeric FPS value supplied by a game overlay.

## Independent choices

The GUI submits a fixed argument array to the root-owned helper:

```text
/usr/local/bin/asus-power-mode custom <cpu-mode> <gpu-mode>
```

The helper validates those enums before invoking `pkexec`. Privileged requests
share `/run/asus-power-mode.lock` with the automatic oneshot and save validated
intent atomically in `/var/lib/asus-power-mode/intent.json`.

Custom intent survives boot on the same power source. A genuine source change
uses the corresponding battery/AC preset. If AC disappears during an Ultimate
request, the helper reconciles toward battery instead.

## Battery charging

The GUI's separate Save limit action submits `asus-power-mode charge-limit <60-100>`.
The helper validates the integer range before authentication, serializes through
the same lock, and uses `asusctl battery limit` for persistent ASUS configuration.
It verifies the applied `xyz.ljones.Platform.ChargeControlEndThreshold` D-Bus
property before reporting success. CPU/GPU intent is not involved in this action.

Status reports the live `charge_limit` property. An unavailable charging
interface disables the slider while leaving the CPU/GPU controls usable.

## GPU routing

| Mode | dgpu_disable | gpu_mux_mode | Cardwire |
|---|---:|---:|---|
| Integrated | 0 | 1 | Integrated after Intel is the display GPU |
| Hybrid | 0 | 1 | Hybrid |
| Ultimate | 0 | 0 | Hybrid/unblocked |

Integrated keeps firmware Hybrid so ordinary GPU-availability transitions do
not require firmware reboots. It presents an iGPU-only application view through
Cardwire and verifies physical runtime sleep separately. It is not firmware Eco.

While leaving Ultimate, NVIDIA stays unblocked until the Intel routing request
has taken effect after reboot. Blocking the active display GPU is avoided.

In Ultimate, Cardwire's Hybrid label describes GPU access. Active MUX 0, NVIDIA
as the display GPU, and KWin's NVIDIA renderer establish the actual display path.

## Installed files

```text
/usr/local/bin/asus-power-mode
/usr/local/bin/asus-gaming-mode -> asus-power-mode
/usr/local/bin/asus-battery-mode -> asus-power-mode
/etc/systemd/system/asus-power-source.service
/etc/asusd/asusd.ron
/etc/cardwire/cardwire.toml
/etc/systemd/system/cardwired.service.d/60-logging.conf
/etc/systemd/logind.conf.d/80-asus-gpu-shutdown.conf
~/.local/share/asus-power-control/
~/.local/bin/asus-power-control
~/.local/share/applications/org.local.AsusPowerControl.desktop
~/.config/powerdevilrc
~/.config/systemd/user/plasma-powerdevil.service.d/60-cardwire.conf
```

Installer backups are kept in `/var/lib/asus-power-control-install/` for system
files and `~/.local/state/asus-power-control-install/` for user files. No live
intent, personal PowerDevil configuration, credential, or private backup belongs
in the Git repository.
