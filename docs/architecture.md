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
