# Hardware verification

## Verified reference machine

| Component | Reference |
|---|---|
| Laptop | ASUS TUF Dash F15 FX517ZC |
| BIOS | FX517ZC.319 |
| CPU | Intel Core i5-12450H |
| dGPU | NVIDIA RTX 3050 Laptop GPU, 4096 MiB VRAM |
| OS | Fedora 44 KDE, Wayland, UEFI/Secure Boot |
| Kernel | 7.2.8-200.fc44.x86_64; BPF LSM active |
| ASUS tools | asusctl 6.5.0 |
| Cardwire | 0.12.3-2.fc44 |
| PPD | 0.30-3.fc44 |
| NVIDIA | 615.71.09 open kernel module, RPM Fusion |
| GUI toolkit | PySide6 6.11.2 |

These record the tested stack; they are not permanent package pins. On a new
installation, use compatible current packages and run the preflight and checks.

## Observed results

- Real AC unplug/replug selected Silent/Integrated and Performance/Hybrid.
- On battery, Integrated reached **PCI D3cold + Video Memory Off** after settling.
- Fresh NVIDIA access was blocked; GPU computation worked after unblocking.
- True Ultimate was verified after reboot: MUX 0, NVIDIA internal display path,
  NVIDIA KWin renderer, and the 144 Hz panel mode.
- Return to Intel was verified after a second reboot: MUX 1, Intel display and
  renderer, RTX D3cold/VRAM Off, and 144 Hz.
- Corrected graphical.target boot ordering ran successfully with no cycle.
- PowerDevil remained stable with the libddcutil compatibility workaround.
- A 20-second Ultimate CUDA test reached P0 at approximately 1950–1972 MHz.
  The enforced limit rose from 60 W idle to **75 W under AC load**; sampled
  consumption reached 68.45 W and temperature reached 65°C. Clocks, voltage,
  and power limits were stock. A 75 W limit is not constant 75 W consumption.

## GUI selections verified on the live machine

| Source | CPU | GPU | Result |
|---|---|---|---|
| AC | Silent | Integrated | Power Saver/Quiet, Intel display, RTX D3cold/VRAM Off |
| AC | Balanced | Hybrid | Balanced, Intel display, NVIDIA available |
| AC | Performance | Hybrid | Performance, Intel display, NVIDIA available |
| Battery | Balanced | Hybrid | Balanced, battery EPP balance_power, NVIDIA available |
| Battery | Performance | Integrated | CPU Performance retained, RTX D3cold/VRAM Off |
| AC | Performance | Ultimate | Firmware request queued; after user reboot, MUX 0 and NVIDIA renderer confirmed |

The GUI's live Apply flow was tested on KDE Wayland. Simulated tests cover mode
combinations, queued request cancellation, boot persistence, unplug races, and
never blocking NVIDIA while it still drives the internal panel.

## Charge-limit slider verification — 2026-10-06

- The slider range is **60–100%**, with 1% steps and a separate Save limit action.
- All **42 policy, UI, and installer tests passed**, including invalid-input
  rejection, readback failures, cancelled authentication, and independent controls.
- A live Wayland GUI save successfully reapplied the existing **70%** cap.
- ASUS D-Bus, the battery's kernel threshold, and `asusctl battery info` all
  reported 70%; asusd's stored current and base charge limits both remained 70%.
- Performance/Hybrid intent, Intel display routing, and idle RTX D3cold/VRAM Off
  were observed after saving, with no pending firmware change.

## Check a reinstalled system

```bash
asus-power-mode status
kscreen-doctor -o
systemctl show asus-power-source.service -p Result -p ExecMainStatus
systemctl --user status plasma-powerdevil
journalctl -b -u asus-power-source -u cardwired -u asus-shutdown
```

For Integrated, wait around 25 seconds after releasing NVIDIA workloads and
look for `GPU main power and VRAM off: VERIFIED`. An unreadable sysfs path or
an Integrated label alone is not proof of shutdown. NVIDIA-connected external
displays require the GPU while they are driven by it.

For Ultimate, verify active MUX 0 and display GPU NVIDIA after the requested
reboot. KWin renderer can also be queried using:

```bash
busctl --user call org.kde.KWin /KWin org.kde.KWin supportInformation
```
