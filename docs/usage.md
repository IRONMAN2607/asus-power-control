# Using ASUS Power Control

A small Qt 6 application for this ASUS FX517ZC and its existing power-management stack.

Open **ASUS Power Control** from the KDE application launcher, or run:

```sh
asus-power-control
```

Press **Ctrl+Alt+S** at any time to toggle the realtime system-monitor OSD.
The shortcut is registered through the KDE desktop entry and works whether the
main application window is open or closed.

Choose a **CPU mode** and **GPU mode**, then click **Apply**:

| CPU mode | Power Profiles Daemon | ASUS platform profile |
|---|---|---|
| Silent | power-saver | Quiet |
| Balanced | balanced | Balanced |
| Performance | performance | Performance |

| GPU mode | Behaviour |
|---|---|
| Integrated | Intel drives the display; Cardwire blocks NVIDIA access so native RTD3 can shut the dGPU and VRAM down |
| Hybrid | Intel drives the display; NVIDIA is available on demand |
| Ultimate (Dedicated) | NVIDIA directly drives the display via the hardware MUX; AC required |

The selectors are independent: e.g. Balanced + Integrated or Silent + Hybrid.
Changing a GPU selection does not force a particular CPU profile.

## System monitor OSD

The click-through OSD appears in the top-left corner with a subdued transparent
panel and refreshes once per
second. It shows:

- CPU temperature and usage
- CPU power when unprivileged RAPL is readable; otherwise `N/A`
- RAM usage
- GPU temperature, utilization and power draw
- VRAM percentage and used/total memory
- GPU voltage when `nvidia-settings` exposes it; otherwise `N/A`
- FPS when an external FPS source is configured

In Integrated mode, Cardwire blocks NVIDIA polling and the OSD reports the GPU
as blocked/off without waking it. In Hybrid or Ultimate, live NVIDIA telemetry
may keep the dGPU active while the OSD is visible. KDE/NVIDIA do not provide a
reliable system-wide game FPS value to this separate Wayland app, so the default
FPS display is `N/A`. An overlay can provide a numeric file by setting:

```sh
export ASUS_POWER_CONTROL_FPS_FILE=/path/to/current-fps.txt
```

## Battery charge limit

The **Battery charge limit** slider ranges from **60% to 100%**, in 1% steps.
It reads the current ASUS limit when the app opens and displays the applied
limit separately from your unsaved slider choice. Move it and click **Save limit**
to persist the new cap. Administrator authentication may appear.

Saving takes effect without reboot and is independent of CPU/GPU Apply. ASUS
retains it across reboot and AC/battery changes. If the battery is already above
the new limit, it stops further charging rather than actively discharging.

CLI equivalent:

```sh
asus-power-mode charge-limit 80
```

## Reboots and power-off evidence

Entering or leaving Ultimate queues a firmware MUX change for the ASUS shutdown
handler. The app reports **reboot required** but never initiates a reboot or logout.
The live status separates the active display route from the pending request.

Integrated ↔ Hybrid is normally immediate while Intel drives the panel. Existing
NVIDIA workloads must release the GPU before it can power down; allow roughly
25 seconds. NVIDIA-connected displays can keep the GPU powered. The app only
shows **Verified off** when both PCI D3cold and NVIDIA Video Memory Off are read.
Its read-only status checks do not run nvidia-smi or wake the GPU intentionally.

Integrated uses firmware Hybrid plus Cardwire blocking, rather than firmware Eco,
so ordinary AC/battery availability changes remain reboot-free. Cardwire's own
Hybrid label in Ultimate means unblocked GPU access, not Intel display routing.

## Existing automatic policy

Applied choices are stored by the root-owned helper as a custom policy. They
survive an orderly reboot on the same power source. An actual unplug/replug
restores the existing presets:

- Battery: Silent + Integrated (Intel route requested if currently Ultimate).
- AC: Performance + Hybrid (never automatically requests Ultimate).

Silent, Balanced, and Performance are available on battery. Hybrid is also
available manually on battery. Ultimate is disabled unless AC is connected.
If AC is lost while an Ultimate request is applying, the helper reconciles to
the battery preset instead. CPU/GPU selections do not change the 144 Hz panel
refresh, fan curves, charge threshold, clocks, voltages, or GPU power limit.

## Architecture and diagnostics

The GUI runs as your user and uses the already installed PySide6 toolkit.
Only the fixed, root-owned `/usr/local/bin/asus-power-mode` helper is elevated
through KDE's pkexec authentication prompt. Commands use argument arrays, not
shell interpolation. The helper serializes requests with the existing AC/boot
policy and records state only after accepting the firmware request.

The app refreshes read-only status every five seconds while open. Closing it
stops those lookups; the GUI is not required for boot or AC/battery policy.

```sh
asus-power-mode status
asus-power-mode status-json
asus-power-mode charge-limit 80
asus-power-mode custom balanced integrated
asus-power-mode custom performance hybrid
asus-power-mode custom performance ultimate
```

The existing `asus-gaming-mode`, `asus-battery-mode`, and
`asus-power-mode normal` commands still select their original presets.

Files:

- GUI/source: `~/.local/share/asus-power-control/`
- Launcher: `~/.local/bin/asus-power-control`
- KDE menu entry: `~/.local/share/applications/org.local.AsusPowerControl.desktop`
- Installed backend: `/usr/local/bin/asus-power-mode`
- Installer backups: `/var/lib/asus-power-control-install/` and `~/.local/state/asus-power-control-install/`
- Persistent policy: `/var/lib/asus-power-mode/intent.json`
- Hardware verification report: `docs/verification.md`

Tests use simulated firmware/Cardwire transitions and never reboot or alter GPU
hardware. Run them with:

```sh
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s ~/.local/share/asus-power-control/tests -v
```

## GUI verification — 2026-10-05

- All 50 policy, telemetry, UI, and installer tests passed, including independent
  mode combinations, source-change handling, deferred-request cancellation,
  active/pending routing, and OSD rendering.
- The app launched and rendered successfully in the actual KDE Wayland session.
- Live GUI Apply successfully reapplied the current Performance + Hybrid pair
  through the installed privileged helper and stored the custom policy.
- After Apply, Intel remained the display GPU, no MUX change was pending, and
  the idle RTX still reported D3cold with Video Memory Off.
- The KDE desktop entry passed `desktop-file-validate`; launcher caches were rebuilt.
