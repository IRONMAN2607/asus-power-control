# Reinstall on Fedora KDE

This guide recreates the verified **FX517ZC + Intel + RTX 3050** setup using
Fedora KDE Wayland. The installer targets a conventional DNF-based Fedora
installation; Fedora Atomic/rpm-ostree and other distributions need adapted
package and installation steps.

## 1. Update Fedora and install the NVIDIA driver

Update the new installation, then reboot into its current kernel. asusctl's
firmware interface needs a compatible modern kernel (upstream minimum 6.19).

Use the current [RPM Fusion NVIDIA instructions](https://rpmfusion.org/Howto/NVIDIA)
and [Secure Boot instructions](https://rpmfusion.org/Howto/Secure%20Boot) for the
RTX 3050. The verified setup used `akmod-nvidia` and
`xorg-x11-drv-nvidia-cuda`, with the NVIDIA open kernel module and Secure Boot
enabled. Complete akmod build/key enrollment as needed, then reboot and verify
the driver loads before installing this project.

Driver validation during setup:

```bash
nvidia-smi
```

The GUI itself never uses nvidia-smi for periodic status, since probing can wake
an otherwise idle GPU.

NVIDIA runtime D3 must be enabled for Integrated mode to power down the GPU.
Inspect `/proc/driver/nvidia/gpus/0000:01:00.0/power` after loading the driver:
the reference setup reports **Runtime D3: Enabled (fine-grained)**. If it is
disabled, configure the driver using its
[runtime power-management documentation](https://download.nvidia.com/XFree86/Linux-x86_64/615.71.09/README/dynamicpowermanagement.html)
before expecting D3cold/VRAM Off. Cardwire blocking and actual power-off are
separate checks.

## 2. Add Terra and install ASUS/Cardwire/PPD

Follow [Terra's current installation instructions](https://docs.terrapkg.com/usage/installing).
It supplies `asusctl` and `cardwire`. Reference upstream instructions:

- [ASUS Linux Fedora guide](https://opengamingcollective.github.io/asusctl/print.html#fedora-workstation-setup-guide)
- [Cardwire Fedora installation](https://opengamingcollective.github.io/cardwire/print.html#fedorafedora-based)

Fedora normally ships TuneD as its profile provider. If `tuned-ppd` is installed,
replace it with the provider this app uses:

```bash
sudo dnf swap tuned-ppd power-profiles-daemon
```

If `switcheroo-control` is installed, replace it with Cardwire's compatible
desktop D-Bus shim:

```bash
sudo dnf swap switcheroo-control cardwire
```

If either old package is absent, install the new package normally instead of
running the corresponding swap. Then install the remaining dependencies:

```bash
sudo dnf install --setopt=install_weak_deps=False git asusctl cardwire power-profiles-daemon python3-pyside6 python3-psutil \
    polkit upower desktop-file-utils
```

Remove previously installed legacy GPU managers such as supergfxctl/supergfxd
before configuring Cardwire. This project uses Cardwire, not supergfxctl.

`asusd` is activated by ASUS's udev/D-Bus integration and is normally a static
unit; do not try to enable it. Start it if needed to generate its default config:

```bash
sudo systemctl start asusd.service
```

## 3. Clone and install

Clone the repository and run the installer from its checkout:

```bash
git clone https://github.com/IRONMAN2607/asus-power-control.git
cd asus-power-control
python3 install.py --dry-run
python3 install.py
```

Run as the normal KDE desktop user; the installer elevates only its system
phase. It checks the machine/model, Wayland, ACAD, BPF LSM, dependency commands,
loaded NVIDIA driver, supported firmware controls, and required units.

The installer:

1. Installs the fixed root-owned backend and CLI aliases.
2. Patches profile ownership and AC/battery callbacks in the existing asusd RON.
3. Merges the known Cardwire settings into its existing TOML configuration.
4. Installs/enables the graphical.target boot policy and ASUS shutdown handler.
5. Masks TuneD; enables PPD, Cardwire, and NVIDIA powerd when available.
6. Installs the GUI/menu entry for the current user, with paths generated from
   that user's home directory (no original username embedded).
7. Clears KDE's saved automatic power-profile selections while preserving its
   unrelated settings. It conditionally applies the Fedora libddcutil 2.2.1
   workaround for external DDC/CI brightness.
8. Keeps pre-install backups and service enablement records for restoration.

The installer enables configuration for the next boot. Save your work and
reboot normally afterward, then launch `asus-power-control`.

## 4. Restore display and charging preferences

In KDE **System Settings → Display Configuration**, select **1920×1080 / 144 Hz**.
The app preserves your selected refresh rate; it does not lower it on battery.

To restore the original battery brightness/idle preferences during installation:

```bash
python3 install.py --battery-display-settings
```

It sets battery brightness to 32%, dimming after 120 seconds, and display-off
after 300 seconds. Lid, suspend, power-button and AC display choices are retained.

The installer preserves the existing ASUS charging limit. In the app, use the
**60–100% Battery charge limit** slider and **Save limit** to choose your preferred
cap (the reference machine used 70%). The CLI equivalent is
`asus-power-mode charge-limit 70`. For a one-time full charge before travelling,
`asusctl battery oneshot` is supported.

## 5. Verify modes

Follow [the verification checklist](verification.md#check-a-reinstalled-system).
Choose Integrated and wait for D3cold/VRAM Off after GPU applications release
their handles. Select Ultimate while connected to AC, reboot when requested,
then verify MUX 0 and NVIDIA display routing at 144 Hz.

## libddcutil workaround

The reference Fedora build, libddcutil 2.2.1, could crash PowerDevil while it
encountered Cardwire-blocked GPU sysfs paths. The installer automatically adds
`POWERDEVIL_NO_DDCUTIL=1` for exactly this library version. This disables
external-monitor DDC/CI brightness control; internal brightness works.

Use `--ddc-workaround on` to force it or `--ddc-workaround off` to remove this
project's exact override when using a verified fixed library. Upstream issue:
[ddcutil #641](https://github.com/rockowitz/ddcutil/issues/641).

## Recovery

From the checkout, run as your normal desktop user:

```bash
python3 install.py --uninstall
```

This restores files and service enablement saved by the installer, removes
new files it introduced, and rebuilds desktop caches. Reboot normally afterward
to load the restored daemon configuration. Backups are local and private:

- System: `/var/lib/asus-power-control-install/`
- User: `${XDG_STATE_HOME:-~/.local/state}/asus-power-control-install/`

Package swaps and driver installation precede the installer and are not undone
by file restoration. If you want Fedora's original profile/GPU services again,
use DNF to swap `power-profiles-daemon` back to `tuned-ppd` and Cardwire back to
`switcheroo-control`, then enable/unmask the corresponding service. Coordinate
the restored ASUS profile settings so only one daemon owns profiles/EPP.

The original development machine also retains its earlier manual configuration
backups. Those are separate from this installer's future per-install backup store.
