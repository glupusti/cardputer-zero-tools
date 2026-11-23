# Cardputer Zero Hotspot and Power

Two keyboard-controlled apps for the M5Stack Cardputer Zero running Raspberry Pi OS. Both draw directly to its 320 × 170 LCD.

| App | What it does |
| --- | --- |
| **Hotspot** | Shares an Ethernet connection over 2.4 GHz Wi-Fi using NetworkManager. The Wi-Fi radio returns to client mode when the hotspot stops. |
| **Power** | Shows battery draw, charge level and CPU temperature. It can change the CPU frequency limit and governor, switch the USB LAN hub, and select an overclock profile. |

## Install on the Cardputer Zero

Run these commands from the repository directory on the device.

```sh
sudo apt install python3-pil python3-numpy network-manager dnsmasq-base fonts-dejavu-core
sudo install -d -m 755 /opt/cardputer-zero-tools
sudo install -m 644 hotspot.py power.py /opt/cardputer-zero-tools/
sudo install -m 755 fb-power /usr/local/sbin/fb-power
```

The apps expect `/dev/fb_lcd`, the built-in keyboard at `/dev/input/by-path/platform-3f804000.i2c-event`, and the default interface names `eth0` and `wlan0`. Run them as the Cardputer desktop user, with access to the LCD and keyboard. Hardware actions assume that this user already has non-interactive sudo permission for the relevant commands (`fb-power`, `nmcli`, and `systemctl reboot`). The commands below start the apps from a local terminal.

```sh
python3 /opt/cardputer-zero-tools/hotspot.py
python3 /opt/cardputer-zero-tools/power.py
```

To launch from APPLaunch on the stock image, install the desktop entries and their icons, then reopen the launcher. Skip this step if you already have working launcher entries for the original apps.

```sh
sudo install -m 644 icons/cardputer-zero-*.png /usr/share/APPLaunch/share/images/
sudo install -m 644 hotspot.desktop /usr/share/APPLaunch/applications/cardputer-zero-hotspot.desktop
sudo install -m 644 power.desktop /usr/share/APPLaunch/applications/cardputer-zero-power.desktop
```

The optional service powers down the USB hub carrying Ethernet and the ACT LED at boot, and reapplies saved CPU settings. The hub can also carry other USB devices. Install and enable the service from the local console.

```sh
sudo install -m 644 fb-power.service /etc/systemd/system/fb-power.service
sudo systemctl daemon-reload
sudo systemctl enable fb-power.service
```

To restore the hub and LED and disable the startup behavior, run `sudo systemctl disable --now fb-power.service`, then `sudo fb-power lan on` and `sudo fb-power leds on`.

## Controls

- **Hotspot** — Use Up/Down to select a row and Enter to toggle or edit it. Connect an Ethernet cable with IPv4 access before turning the hotspot on. It uses `172.29.77.1/24` by default. Switching modes interrupts Wi-Fi, including SSH over Wi-Fi. The first run generates a Wi-Fi password and saves it in `~/.config/fb-hotspot/config.json`. Use the Password row to view or change it.
- **Power** — Use Up/Down to select a row, Left/Right to change a setting, and Enter to toggle the LAN port. Press Enter twice on Reboot to restart.
- **Both apps** — Use Q or Esc to close the screen.

Power's optional overclock profiles take effect after reboot. If a profile prevents booting, mount the SD card's boot partition on another computer and empty `fb-oc.txt`. The helper saves the pre-existing `config.txt` as `config.txt.bak-before-fb-oc` when adding the include.

## Preview without the LCD

With Pillow and NumPy installed, run `python3 hotspot.py --png hotspot.png` or `python3 power.py --png power.png`. Hotspot previews show the current Wi-Fi password, so use a temporary home directory when making screenshots to share. Live readings and network state depend on the machine used for the preview.
