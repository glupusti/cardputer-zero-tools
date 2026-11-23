#!/usr/bin/env python3
"""Power for Cardputer Zero (320x170 LCD): live battery numbers + CPU speed + LAN port power.

Rows:  CPU max   - highest CPU frequency allowed (Left/Right)            [saved, re-applied at boot]
       Governor  - how the CPU picks its speed (Left/Right)              [saved, re-applied at boot]
       LAN port  - power of the USB hub + LAN adapter (Enter)            [~125 mA when on]
       Overclock - Off / 1100 / 1200 / 1300 MHz (Left/Right)             [needs a reboot]
       Reboot    - Enter twice
Keys:  Up/Down = choose row | Left/Right = change | Enter = toggle | Q or Esc = quit
Changes go through /usr/local/sbin/fb-power (root helper); settings persist in /etc/fb-power.conf.
"""
import argparse, os, select, struct, subprocess, threading, time
from PIL import Image, ImageDraw, ImageFont
import numpy as np

W, H = 320, 170
FB, KBD = "/dev/fb_lcd", "/dev/input/by-path/platform-3f804000.i2c-event"
BG, FG, DIM = (12, 14, 20), (230, 232, 238), (125, 132, 148)
GREEN, YEL, RED, BLUE = (60, 200, 120), (230, 180, 40), (235, 80, 80), (80, 140, 230)
BAR = (28, 32, 44)
FBPOWER = "/usr/local/sbin/fb-power"
BAT = "/sys/class/power_supply/bq27220-0/"
CPU = "/sys/devices/system/cpu/cpu0/cpufreq/"
GOVS = ["ondemand", "powersave", "performance", "conservative"]
GOV_HELP = {"ondemand": "speed follows load", "powersave": "always slowest (saves most)",
            "performance": "always at max", "conservative": "slow to ramp up"}
HUB_ID = ("05e3", "0610")
OCS = ["off", "1100", "1200", "1300"]
OCFILE = "/boot/firmware/fb-oc.txt"


def rd(path, default=""):
    try:
        return open(path).read().strip()
    except OSError:
        return default


def lan_power():
    try:
        for d in os.listdir("/sys/bus/usb/devices"):
            p = "/sys/bus/usb/devices/" + d
            if (rd(p + "/idVendor"), rd(p + "/idProduct")) == HUB_ID:
                return "on" if rd(p + "/authorized") == "1" else "off"
    except OSError:
        pass
    return "n/a"


def sh(*cmd, timeout=15):
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, type(e).__name__


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/System/Library/Fonts/Menlo.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


F9, F11 = font(10), font(11)


def to_565(im):
    a = np.asarray(im, dtype=np.uint16)
    v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes()


class State:
    def __init__(self):
        self.sel = 0
        self.msg, self.msg_c, self.msg_until, self.busy = "", DIM, 0, ""
        self.dirty = True
        self.hotspot = False
        self.reboot_armed = 0
        self.refresh()

    def refresh(self):
        self.freqs = sorted(int(x) for x in rd(CPU + "scaling_available_frequencies", "600000 1000000").split())
        self.gov = rd(CPU + "scaling_governor", "?")
        self.govs = [g for g in GOVS if g in rd(CPU + "scaling_available_governors", " ".join(GOVS)).split()]
        self.fmax = int(rd(CPU + "scaling_max_freq", "0") or 0)
        self.fcur = int(rd(CPU + "scaling_cur_freq", "0") or 0)
        self.stock = int(rd(CPU + "cpuinfo_max_freq", "0") or 0)
        self.lan = lan_power()
        self.cap = rd(BAT + "capacity", "?")
        self.status = rd(BAT + "status", "?")
        i = int(rd(BAT + "current_now", "0") or 0)             # microamps, negative = discharging
        v = int(rd(BAT + "voltage_now", "0") or 0)
        self.ma = abs(i) // 1000
        self.watt = abs(i) * v / 1e12
        self.tte = int(rd(BAT + "time_to_empty_now", "0") or 0)  # seconds
        self.temp = int(rd("/sys/class/thermal/thermal_zone0/temp", "0") or 0) / 1000
        self.oc_cfg = "off"
        for ln in rd(OCFILE).split("\n"):
            if ln.startswith("arm_freq="):
                self.oc_cfg = ln.split("=", 1)[1].strip()
        self.oc_act = str(self.stock // 1000) if self.stock > 1000000 else "off"
        _, o = sh("nmcli", "-t", "-f", "NAME", "con", "show", "--active")
        self.hotspot = "fb-hotspot" in o.split("\n")

    def poll(self):
        while True:
            try:
                self.refresh()
            except Exception:
                pass
            self.dirty = True
            time.sleep(2)

    def say(self, text, colour=DIM, secs=6):
        self.msg, self.msg_c, self.msg_until, self.dirty = text, colour, time.time() + secs, True

    def run(self, label, *cmd):
        if self.busy:
            return
        self.busy, self.dirty = label, True

        def go():
            rc, o = sh("sudo", "-n", FBPOWER, *cmd)
            self.busy = ""
            try:
                self.refresh()
            except Exception:
                pass
            self.say(o.replace("\n", " ")[:50] if rc else "OK", RED if rc else GREEN)
        threading.Thread(target=go, daemon=True).start()

    def step(self, direction):
        if self.sel == 0 and self.freqs:
            i = min(range(len(self.freqs)), key=lambda k: abs(self.freqs[k] - self.fmax))
            j = max(0, min(len(self.freqs) - 1, i + direction))
            if j != i:
                self.run("setting CPU max...", "cpu", "max", str(self.freqs[j]))
        elif self.sel == 3:
            self.step_oc(direction)
        elif self.sel == 1 and self.govs:
            i = self.govs.index(self.gov) if self.gov in self.govs else 0
            j = (i + direction) % len(self.govs)
            self.run("setting governor...", "cpu", "gov", self.govs[j])

    def step_oc(self, direction):
        i = OCS.index(self.oc_cfg) if self.oc_cfg in OCS else 0
        j = max(0, min(len(OCS) - 1, i + direction))
        if j != i:
            self.run("saving overclock...", "oc", "off") if OCS[j] == "off" else \
                self.run("saving overclock...", "oc", "set", OCS[j])

    def reboot(self):
        if time.time() > self.reboot_armed:
            self.reboot_armed = time.time() + 5
            return self.say("Press Enter again to REBOOT", YEL, 5)
        self.busy, self.dirty = "rebooting...", True
        threading.Thread(target=lambda: sh("sudo", "-n", "systemctl", "reboot"), daemon=True).start()

    def toggle_lan(self):
        if self.hotspot:
            return self.say("Hotspot is using the LAN port", RED)
        self.run("switching LAN port...", "lan", "off" if self.lan == "on" else "on")


def render(s):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 17), fill=BAR)
    d.text((4, 2), "Power", font=F11, fill=FG)
    try:
        pc = int(s.cap)
    except ValueError:
        pc = 0
    bc = GREEN if pc > 30 else (YEL if pc > 15 else RED)
    d.rectangle((W - 62, 2, W - 4, 15), fill=bc)
    d.text((W - 58, 3), f"{s.cap}% {'+' if s.status == 'Charging' else ''}", font=F11, fill=(0, 0, 0))

    mhz = s.fmax // 1000
    rows = [("CPU max", f"<  {mhz} MHz  >", GREEN if s.fmax >= s.stock else YEL),
            ("Governor", f"<  {s.gov}  >", FG),
            ("LAN port", ("ON   (Enter = power off)" if s.lan == "on" else
                          "OFF  (saving, Enter = on)" if s.lan == "off" else "not found"),
             GREEN if s.lan == "on" else DIM),
            ("Overclock", f"<  {s.oc_cfg if s.oc_cfg == 'off' else s.oc_cfg + ' MHz'}  >" +
             ("   reboot to apply" if s.oc_cfg != s.oc_act else ""),
             YEL if s.oc_cfg != s.oc_act else (GREEN if s.oc_act != "off" else FG)),
            ("Reboot", "Enter twice", DIM)]
    for i, (label, val, c) in enumerate(rows):
        y = 21 + i * 19
        on = i == s.sel
        if on:
            d.rectangle((0, y - 1, W, y + 16), fill=BAR)
            d.rectangle((0, y - 1, 2, y + 16), fill=BLUE)
        d.text((6, y + 2), label, font=F9, fill=BLUE if on else DIM)
        d.text((66, y + 2), val[:38], font=F11, fill=c)

    y = 120
    if s.status == "Charging":
        d.text((6, y), f"Charging  {s.ma} mA  {s.watt:.1f} W", font=F11, fill=GREEN)
    else:
        left = ""
        if 0 < s.tte < 100000:
            left = f"  ~{s.tte // 3600}h{s.tte % 3600 // 60:02d} left"
        cc = GREEN if s.ma < 300 else (YEL if s.ma < 450 else RED)
        d.text((6, y), f"Draw  {s.ma} mA  {s.watt:.1f} W{left}", font=F11, fill=cc)
    d.text((6, y + 12), f"CPU now {s.fcur // 1000} MHz   {s.temp:.0f} C", font=F11,
           fill=RED if s.temp >= 75 else FG)

    d.rectangle((0, H - 13, W, H), fill=BAR)
    if s.busy:
        txt, c = s.busy, YEL
    elif s.msg and time.time() < s.msg_until:
        txt, c = s.msg, s.msg_c
    else:
        txt, c = "Up/Down  Left/Right change  Enter  Q quit", DIM
    d.text((4, H - 12), txt[:52], font=F9, fill=c)
    return im


def handle(s, code):
    if s.busy:
        return True
    if code in (16, 1):
        return False
    elif code in (103, 39):
        s.sel = max(0, s.sel - 1)
    elif code in (108, 52):
        s.sel = min(4, s.sel + 1)
    elif code in (105, 51):
        s.step(-1)
    elif code in (106, 53):
        s.step(+1)
    elif code in (28, 57) and s.sel == 2:
        s.toggle_lan()
    elif code in (28, 57) and s.sel == 4:
        s.reboot()
    s.dirty = True
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--png")
    ap.add_argument("--sel", type=int, default=0)
    a = ap.parse_args()
    s = State()
    if a.png:
        s.sel = a.sel
        render(s).resize((W * 3, H * 3), Image.NEAREST).save(a.png)
        return
    threading.Thread(target=s.poll, daemon=True).start()
    fb, kb = open(FB, "wb", buffering=0), open(KBD, "rb", buffering=0)
    last = 0
    try:
        while True:
            if select.select([kb], [], [], 0.5)[0]:
                _, _, typ, code, val = struct.unpack("llHHi", kb.read(24))
                if typ == 1 and (val == 1 or (val == 2 and code in (105, 106, 51, 53))) \
                        and not handle(s, code):            # Left/Right may auto-repeat, Enter may not
                    break
            if s.dirty or time.time() - last > 2:
                s.dirty, last = False, time.time()
                fb.seek(0)
                fb.write(to_565(render(s)))
    finally:
        fb.seek(0)
        fb.write(b"\x00" * (W * H * 2))


if __name__ == "__main__":
    main()
