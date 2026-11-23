#!/usr/bin/env python3
"""Hotspot for Cardputer Zero (320x170 LCD): shares the LAN port (eth0) over WiFi (wlan0 = access point).

ON : wlan0 becomes an access point with the SSID / password set here; internet comes from eth0
     (NetworkManager 'shared' mode: DHCP + NAT).  WiFi client connection is paused meanwhile.
OFF: hotspot profile is brought down and wlan0 reconnects to the normal WiFi on its own.
The USB hub + LAN adapter (~125 mA) are powered down by default (service fb-power); turning the
hotspot ON powers them up and turning it OFF powers them down again. The "LAN port" row switches
them by hand (e.g. to SSH over LAN).

Keys:  Up/Down = choose row | Enter = toggle / edit | Q or Esc = quit
Edit:  type, Enter = save, Esc = cancel, Backspace = delete.
Settings live in ~/.config/fb-hotspot/config.json (mode 600).  A random password is made on first run.
"""
import argparse, json, os, random, select, string, struct, subprocess, threading, time
from PIL import Image, ImageDraw, ImageFont
import numpy as np

W, H = 320, 170
FB, KBD = "/dev/fb_lcd", "/dev/input/by-path/platform-3f804000.i2c-event"
BG, FG, DIM = (12, 14, 20), (230, 232, 238), (125, 132, 148)
GREEN, YEL, RED, BLUE = (60, 200, 120), (230, 180, 40), (235, 80, 80), (80, 140, 230)
BAR = (28, 32, 44)
CON, WIF, LAN = "fb-hotspot", "wlan0", "eth0"
AP_NET = "172.29.77.1/24"
FBPOWER = "/usr/local/sbin/fb-power"
HUB_ID = ("05e3", "0610")
CFG = os.path.expanduser("~/.config/fb-hotspot/config.json")

# ---- keyboard (evdev codes) -------------------------------------------------
ROW = {16: "qwertyuiop", 30: "asdfghjkl", 44: "zxcvbnm", 2: "1234567890"}
CHARS, SHIFTED = {}, {}
for start, row in ROW.items():
    for i, ch in enumerate(row):
        CHARS[start + i] = ch
for code, ch, sh in [(12, "-", "_"), (13, "=", "+"), (26, "[", "{"), (27, "]", "}"), (39, ";", ":"),
                     (40, "'", '"'), (41, "`", "~"), (43, "\\", "|"), (51, ",", "<"), (52, ".", ">"),
                     (53, "/", "?"), (57, " ", " ")]:
    CHARS[code], SHIFTED[code] = ch, sh
for code, sh in zip(range(2, 12), "!@#$%^&*()"):
    SHIFTED[code] = sh


def typed(code, shift):
    ch = SHIFTED.get(code) if shift else CHARS.get(code)
    if ch is None:
        ch = CHARS.get(code)
        if ch is None:
            return ""
        ch = ch.upper() if shift else ch
    return ch


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/System/Library/Fonts/Menlo.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


F9, F11, F16 = font(10), font(11), font(17)


def to_565(im):
    a = np.asarray(im, dtype=np.uint16)
    v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes()


# ---- settings ---------------------------------------------------------------
def load_cfg():
    try:
        c = json.load(open(CFG))
        if isinstance(c.get("ssid"), str) and isinstance(c.get("password"), str):
            return c
    except Exception:
        pass
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"     # no look-alike characters
    c = {"ssid": "CardputerZero", "password": "".join(random.SystemRandom().choice(alphabet) for _ in range(10))}
    save_cfg(c)
    return c


def save_cfg(c):
    os.makedirs(os.path.dirname(CFG), mode=0o700, exist_ok=True)
    tmp = CFG + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f)
    os.replace(tmp, CFG)


def validate(ssid, pw):
    if not 1 <= len(ssid.encode()) <= 32:
        return "SSID must be 1-32 characters"
    if not 8 <= len(pw) <= 63:
        return "Password must be 8-63 characters"
    if any(not 32 <= ord(c) < 127 for c in pw):
        return "Password: plain ASCII only"
    return ""


def sh(*cmd, timeout=20):
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, type(e).__name__


def nm(*args, timeout=20):
    return sh("sudo", "-n", "nmcli", *args, timeout=timeout)


def lan_power():
    """'on' / 'off' / 'n/a' - whether the USB hub that carries the LAN adapter is powered."""
    try:
        for d in os.listdir("/sys/bus/usb/devices"):
            p = "/sys/bus/usb/devices/" + d
            try:
                if (open(p + "/idVendor").read().strip(), open(p + "/idProduct").read().strip()) == HUB_ID:
                    return "on" if open(p + "/authorized").read().strip() == "1" else "off"
            except OSError:
                continue
    except OSError:
        pass
    return "n/a"


def lan_carrier():
    try:
        return open(f"/sys/class/net/{LAN}/carrier").read().strip() == "1"
    except OSError:
        return False


def lan_addr():
    _, o = sh("ip", "-4", "-o", "addr", "show", LAN)
    return o.split()[3].split("/")[0] if " inet " in o else ""


class State:
    def __init__(self):
        self.cfg = load_cfg()
        self.sel, self.edit, self.buf = 0, None, ""
        self.active, self.carrier, self.lan_ip, self.ap_ip, self.clients = False, False, "", "", 0
        self.lan, self.lan_auto = "n/a", False       # LAN adapter power; True = hotspot powered it up
        self.busy, self.msg, self.msg_c, self.msg_until = "", "", DIM, 0
        self.dirty = True

    def say(self, text, colour=DIM, secs=8):
        self.msg, self.msg_c, self.msg_until, self.dirty = text, colour, time.time() + secs, True

    # ---- live status ----
    def poll(self):
        while True:
            try:
                _, o = sh("nmcli", "-t", "-f", "NAME", "con", "show", "--active")
                self.active = CON in o.split("\n")
                self.lan = lan_power()
                self.carrier = lan_carrier()
                self.lan_ip = lan_addr()
                _, o = sh("ip", "-4", "-o", "addr", "show", WIF)
                self.ap_ip = o.split()[3].split("/")[0] if " inet " in o else ""
                _, o = sh("ip", "neigh", "show", "dev", WIF)
                self.clients = sum(1 for ln in o.split("\n") if "lladdr" in ln and "FAILED" not in ln)
            except Exception:
                pass
            self.dirty = True
            time.sleep(3)

    # ---- actions (run in a thread so the screen never freezes) ----
    def work(self, label, fn):
        if self.busy:
            return
        self.busy, self.dirty = label, True

        def run():
            try:
                fn()
            except Exception as e:
                self.say("ERR " + type(e).__name__, RED)
            self.busy, self.dirty = "", True
        threading.Thread(target=run, daemon=True).start()

    def enable(self):
        ssid, pw = self.cfg["ssid"], self.cfg["password"]
        err = validate(ssid, pw)
        if err:
            return self.say(err, RED)

        def go():
            if self.lan != "on":                                  # power the LAN adapter first
                self.busy = "powering LAN adapter..."
                rc, o = sh("sudo", "-n", FBPOWER, "lan", "on")
                if rc:
                    return self.say("LAN adapter: " + o[:50], RED)
                self.lan, self.lan_auto = "on", True
            t0 = time.time()
            while time.time() - t0 < 20 and not (lan_carrier() and lan_addr()):
                self.busy = f"waiting for LAN {int(time.time() - t0)}s..."
                self.dirty = True
                time.sleep(0.5)
            if not lan_carrier():
                if self.lan_auto:
                    sh("sudo", "-n", FBPOWER, "lan", "off")
                    self.lan, self.lan_auto = "off", False
                return self.say("No LAN cable - nothing to share", RED)
            self.busy = "starting hotspot..."
            nm("con", "delete", CON)
            rc, o = nm("con", "add", "type", "wifi", "ifname", WIF, "con-name", CON,
                       "autoconnect", "no", "ssid", ssid,
                       "wifi.mode", "ap", "wifi.band", "bg",
                       "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw,
                       "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
                       "ipv4.method", "shared", "ipv4.addresses", AP_NET,   # rare range: hotel LANs often use 10.x / 192.168.x
                       "ipv6.method", "ignore")
            if rc:
                return self.say("Profile: " + o[:60], RED)
            rc, o = nm("con", "up", CON, "ifname", WIF, timeout=45)
            if rc:
                nm("con", "delete", CON)
                nm("device", "connect", WIF, timeout=30)       # give WiFi back
                return self.say("Start failed: " + o.replace("Error: ", "")[:60], RED, 12)
            self.say("Hotspot ON. SSH now via LAN", GREEN)
        self.work("starting hotspot...", go)

    def disable(self):
        def go():
            nm("con", "down", CON, timeout=20)
            nm("device", "connect", WIF, timeout=40)           # WiFi client comes back
            if self.lan_auto:                                  # we powered it for the hotspot -> save power
                sh("sudo", "-n", FBPOWER, "lan", "off")
                self.lan, self.lan_auto = "off", False
            self.say("Hotspot OFF. WiFi reconnecting", GREEN)
        self.work("stopping hotspot...", go)

    def toggle(self):
        self.disable() if self.active else self.enable()

    def toggle_lan(self):
        if self.active:
            return self.say("Hotspot is using the LAN port", RED)
        want = "off" if self.lan == "on" else "on"

        def go():
            rc, o = sh("sudo", "-n", FBPOWER, "lan", want)
            self.lan, self.lan_auto = lan_power(), False
            self.say(f"LAN port {want.upper()}" if not rc else "LAN: " + o[:50], GREEN if not rc else RED)
        self.work("switching LAN port...", go)

    def commit_edit(self):
        field, val = self.edit, self.buf
        c = dict(self.cfg)
        c[field] = val
        err = validate(c["ssid"], c["password"])
        if err:
            return self.say(err, RED)
        self.cfg, self.edit = c, None
        save_cfg(c)
        if self.active:
            self.say("Saved, restarting hotspot", GREEN)
            self.enable()
        else:
            self.say("Saved", GREEN)


def render(s):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 17), fill=BAR)
    d.text((4, 2), "Hotspot  LAN > WiFi", font=F11, fill=FG)
    pill, col = ("ON", GREEN) if s.active else ("OFF", RED)
    if s.busy:
        pill, col = "...", YEL
    d.rectangle((W - 44, 2, W - 4, 15), fill=col)
    d.text((W - 40 + (6 if pill == "ON" else 0), 3), pill, font=F11, fill=(0, 0, 0))

    lan_txt = {"on": "ON   (Enter = power off)", "off": "OFF  (saving power, Enter = on)",
               "n/a": "not found"}[s.lan]
    rows = [("Hotspot", "ON   (Enter = turn off)" if s.active else "OFF  (Enter = turn on)",
             GREEN if s.active else DIM),
            ("SSID", s.cfg["ssid"], FG),
            ("Password", s.cfg["password"], FG),
            ("LAN port", lan_txt, GREEN if s.lan == "on" else DIM)]
    for i, (label, val, c) in enumerate(rows):
        y = 21 + i * 19
        on = i == s.sel
        if on:
            d.rectangle((0, y - 1, W, y + 16), fill=BAR)
            d.rectangle((0, y - 1, 2, y + 16), fill=BLUE)
        d.text((6, y + 2), label, font=F9, fill=BLUE if on else DIM)
        if s.edit and on and 0 < i < 3:
            d.text((66, y + 2), (s.buf + "_")[-37:], font=F11, fill=YEL)
        else:
            d.text((66, y + 2), val[:38], font=F11, fill=c)

    y = 104
    if s.lan == "off":
        d.text((6, y), "LAN adapter: off (saving power)", font=F11, fill=DIM)
    elif s.carrier:
        d.text((6, y), f"LAN {LAN}: up {s.lan_ip}".rstrip(), font=F11, fill=GREEN)
    else:
        d.text((6, y), f"LAN {LAN}: no cable", font=F11, fill=RED)
    if s.active:
        d.text((6, y + 13), f"WiFi AP {s.ap_ip}  clients: {s.clients}", font=F11, fill=FG)
        d.text((6, y + 26), f"SSH now: {s.lan_ip or 'via LAN'}", font=F11, fill=DIM)
    else:
        d.text((6, y + 13), "WiFi: normal client mode", font=F11, fill=DIM)

    d.rectangle((0, H - 13, W, H), fill=BAR)
    if s.busy:
        txt, c = s.busy, YEL
    elif s.msg and time.time() < s.msg_until:
        txt, c = s.msg, s.msg_c
    elif s.edit:
        txt, c = "Enter save  Esc cancel", DIM
    else:
        txt, c = "Up/Down  Enter select  Q quit", DIM
    d.text((4, H - 12), txt[:52], font=F9, fill=c)
    return im


def handle(s, code, mods):
    """Returns False to quit."""
    if s.busy:
        return True
    shift = "shift" in mods
    if s.edit:
        if code == 28:
            s.commit_edit()
        elif code == 1:
            s.edit = None
        elif code == 14:
            s.buf = s.buf[:-1]
        elif len(s.buf) < (32 if s.edit == "ssid" else 63):
            s.buf += typed(code, shift)
    else:
        if code in (16, 1):
            return False
        elif code in (103, 39):
            s.sel = max(0, s.sel - 1)
        elif code in (108, 52):
            s.sel = min(3, s.sel + 1)
        elif code in (28, 57):
            if s.sel == 0:
                s.toggle()
            elif s.sel == 3:
                s.toggle_lan()
            else:
                s.edit = "ssid" if s.sel == 1 else "password"
                s.buf = s.cfg[s.edit]
    s.dirty = True
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", help="render one screenshot to this file instead of drawing on the LCD")
    ap.add_argument("--sel", type=int, default=0)
    ap.add_argument("--edit", action="store_true")
    a = ap.parse_args()
    s = State()
    threading.Thread(target=s.poll, daemon=True).start()
    if a.png:
        time.sleep(4)
        s.sel = a.sel
        if a.edit and a.sel:
            s.edit = "ssid" if a.sel == 1 else "password"
            s.buf = s.cfg[s.edit]
        render(s).resize((W * 3, H * 3), Image.NEAREST).save(a.png)
        return
    fb, kb = open(FB, "wb", buffering=0), open(KBD, "rb", buffering=0)
    last, mods = 0, set()
    MOD = {42: "shift", 54: "shift", 29: "ctrl", 97: "ctrl"}
    try:
        while True:
            if select.select([kb], [], [], 0.5)[0]:
                _, _, typ, code, val = struct.unpack("llHHi", kb.read(24))
                if typ == 1 and code in MOD:
                    (mods.add if val else mods.discard)(MOD[code])
                elif typ == 1 and (val == 1 or (val == 2 and code not in (28, 57))) \
                        and not handle(s, code, mods):       # no auto-repeat on Enter/Space
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
