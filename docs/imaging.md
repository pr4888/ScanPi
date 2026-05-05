# Building a flashable ScanPi image

Three paths, easiest to most controlled.

---

## Path 1 — Raspberry Pi Imager + first-boot installer (easiest)

This is the recommended path for non-developers. No image building required.

1. **Download Raspberry Pi Imager** — https://www.raspberrypi.com/software/
2. Choose **Raspberry Pi OS Lite (64-bit)** — Bookworm
3. Click the gear icon (⚙). In the advanced options:
   - Set hostname: `scanpi`
   - Enable SSH (key or password)
   - Set username: `scanpi` (or whatever you like — the installer adapts)
   - Configure your Wi-Fi
   - Set locale and timezone
4. Under **Advanced options → Run this command on first boot**, paste:

   ```
   bash -c "curl -fsSL https://raw.githubusercontent.com/pr4888/ScanPi/main/install/firstboot.sh | bash"
   ```

5. Flash. Plug into your Pi. First boot takes ~5-10 minutes (the installer
   pulls deps + the ScanPi repo). When done, ScanPi is running on
   `http://scanpi.local:8080/`.

If the "Run this command on first boot" field isn't available in your version
of RPI Imager, drop a file named `firstrun.sh` onto the boot partition with
the same contents — Raspberry Pi OS will execute it on first boot.

---

## Path 2 — pre-baked image for distribution

You want to give a flashable `.img` to someone who shouldn't have to type
anything.

```bash
# Need: a fresh Raspberry Pi OS Lite 64-bit image
wget https://downloads.raspberrypi.com/raspios_lite_arm64/images/.../latest.zip
unzip latest.zip

# Mount the image
sudo kpartx -av <image.img>
# This creates /dev/mapper/loop0p1 (boot) and /dev/mapper/loop0p2 (rootfs).

# Copy our pre-bake script into the boot partition
sudo mkdir -p /mnt/boot /mnt/rootfs
sudo mount /dev/mapper/loop0p1 /mnt/boot
sudo mount /dev/mapper/loop0p2 /mnt/rootfs

# Drop firstrun.sh into the boot partition — RPiOS auto-runs it
sudo cp install/firstboot.sh /mnt/boot/firstrun.sh

# Optionally pre-seed Wi-Fi via /boot/wpa_supplicant.conf and
# enable SSH via /boot/ssh.empty

sudo umount /mnt/boot /mnt/rootfs
sudo kpartx -dv <image.img>

# Compress for distribution
xz -9 <image.img>
```

The recipient flashes `<image.img>` with any imager, boots, and ScanPi installs
on first boot. Build can be automated with `pi-gen` for repeatable releases.

---

## Path 3 — pi-gen custom image (most polished)

For shipping a branded `.img` (e.g., scanpi-v0.4.0-arm64.img) with everything
baked in (packages already installed, no first-boot wait):

```bash
git clone https://github.com/RPi-Distro/pi-gen
cd pi-gen
cp -r ../ScanPi/install/pi-gen-stage3 stage3-scanpi

# Edit config
echo 'IMG_NAME="scanpi"' > config
echo 'TARGET_HOSTNAME=scanpi' >> config
echo 'FIRST_USER_NAME=scanpi' >> config

# Build (takes ~30 min, needs Docker)
sudo ./build-docker.sh

# Output: deploy/scanpi.img
```

`install/pi-gen-stage3` is provided in this repo and contains the necessary
hooks to install ScanPi during image build.

---

## Easiest distribution

For sharing with friends:

1. Use Path 1 above.
2. Tell them: "open Raspberry Pi Imager, pick Pi OS Lite 64-bit, paste this URL
   in the advanced first-boot command field, flash, plug in, wait 5 minutes."
3. They open `http://scanpi.local:8080/` from any device on their LAN.

Done. No git clone, no compile, no Docker.
