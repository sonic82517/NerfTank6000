"""
Tactical Nerf Tank Command HUD - v2.6

Setup:
    pip install flask opencv-python numpy        (on the Pi: sudo apt install python3-opencv python3-flask)
    python tank_hud.py   ->  open http://<pi-ip>:5000

Wi-Fi hotspot (built in; run once, needs sudo, Raspberry Pi OS Bookworm or newer):
    sudo python3 tank_hud.py --setup-ap -s TankNet -C GB     # prints a random password
    sudo python3 tank_hud.py --setup-ap -i wlan1             # later: host it on a USB antenna
    sudo python3 tank_hud.py --remove-ap                     # undo, restore your old Wi-Fi
    then join the network and open http://10.42.0.1:5000

Raspberry Pi 4 packages (one line):
    sudo apt install -y python3-opencv python3-flask python3-numpy python3-smbus i2c-tools

20x4 status LCD (I2C backpack, shows mode / fps / ammo / target / IP / ERRORS):
    sudo raspi-config nonint do_i2c 0 && sudo reboot      # enable I2C once
    wiring: GND->pin 6, VCC->pin 2 (5V), SDA->pin 3 (GPIO2), SCL->pin 5 (GPIO3)
    check:  i2cdetect -y 1        (expect 27 or 3f).  Adjust the blue contrast pot if the screen is blank.

What's new vs v2.4
  * Web enrollment: type a name, press ENROLL, it captures 5 good samples automatically.
    Enroll the same name again to add more samples (better accuracy).
  * Web roster: toggle each person FRIEND <-> ENEMY, or delete them.
  * Web targeting: LOCK a live contact (button, or click them in the video), CLEAR LOCK,
    and an AUTO-TARGET on/off switch (off = no reticle unless you lock someone).
  * Accuracy: multi-sample enrollment, quality-gated recognition (size + frontal check),
    identity voting that can change, ambiguity margin, tentative tracks must be confirmed.
  * Speed: threaded camera, stream encoded outside the lock and once per frame,
    auto mode adapts how often it runs detection to hold ~15 fps.
  * Arduino car control: hold-to-drive webpage controls send F/B/L/R/S over USB serial.
    Flash Infrared_remote_control_car_serial.ino to the Arduino and install pyserial.
"""
import json
import logging
import re
import socket
import threading
import time
import traceback
import urllib.request
from collections import Counter, deque
from pathlib import Path

import os
import shutil
import subprocess
import sys
import atexit

try:   # print immediately, even under systemd, so the terminal / journal is never silent
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# ---------------- Wi-Fi hotspot (runs only for --setup-ap / --remove-ap) ----------------
# Lives above the OpenCV/Flask imports on purpose, so `sudo python3 tank_hud.py --setup-ap`
# works even if those libraries are only installed for your normal user.
AP_CON = "TankAP"
AP_IP = "10.42.0.1"
AP_STATE_DIR = Path("/var/lib/tank_ap")


def _sh(*cmd, check=True):
    return subprocess.run(list(cmd), check=check, text=True, capture_output=True)


def _iface_exists(name):
    return (Path("/sys/class/net") / name).exists()


def _wifi_profiles():
    names = []
    for line in _sh("nmcli", "-t", "-f", "NAME,TYPE", "con", "show").stdout.splitlines():
        if ":" in line:
            name, typ = line.rsplit(":", 1)
            if typ == "802-11-wireless":
                names.append(name.replace("\\:", ":"))
    return names


def _ssh_client_ip():
    for var in ("SSH_CONNECTION", "SSH_CLIENT"):
        v = os.environ.get(var)
        if v:
            return v.split()[0]
    m = re.search(r"\(([0-9a-fA-F:.]+)\)", _sh("who", "am", "i", check=False).stdout)
    return m.group(1) if m else None


def _ap_remove():
    _sh("nmcli", "con", "delete", AP_CON, check=False)
    print(f"Removed AP profile '{AP_CON}' (if it existed).")
    state = AP_STATE_DIR / "disabled_profiles.txt"
    if state.exists():
        for name in state.read_text().splitlines():
            if name.strip():
                _sh("nmcli", "con", "modify", name, "connection.autoconnect", "yes", check=False)
                print(f"Re-enabled auto-connect for '{name}'.")
        state.unlink()
    print("Done. Reconnect to a network with:  nmcli device wifi connect <ssid> --ask")


def _ap_setup(a):
    import secrets
    import string
    if not _iface_exists(a.iface):
        sys.exit(f"Interface '{a.iface}' not found. Available: {' '.join(sorted(os.listdir('/sys/class/net')))}")
    if not 1 <= len(a.ssid) <= 32:
        sys.exit("SSID must be 1-32 characters.")
    chan = a.channel or ("36" if a.band == "a" else "6")
    generated = not a.password
    password = a.password or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
    if not 8 <= len(password) <= 63:
        sys.exit("Password must be 8-63 characters.")

    if shutil.which("iw"):   # can this adapter host an access point?
        m = re.search(r"wiphy (\d+)", _sh("iw", "dev", a.iface, "info", check=False).stdout)
        if m:
            modes = _sh("iw", "phy", f"phy{m.group(1)}", "info", check=False).stdout
            if not re.search(r"^\s+\* AP$", modes, re.M):
                sys.exit(f"{a.iface} does not support access-point mode. Try a different adapter.")

    if a.country:
        _sh("iw", "reg", "set", a.country, check=False)
        if shutil.which("raspi-config"):
            _sh("raspi-config", "nonint", "do_wifi_country", a.country, check=False)

    if not a.yes:   # warn if this SSH session runs over the radio we're about to take over
        ip = _ssh_client_ip()
        if ip:
            m = re.search(r"dev (\S+)", _sh("ip", "route", "get", ip, check=False).stdout)
            if m and m.group(1) == a.iface:
                print(f"WARNING: your SSH session is using {a.iface}. It will drop when the AP starts.")
                print(f"Reconnect afterwards by joining '{a.ssid}' and running: ssh <user>@{AP_IP}")
                if input("Continue? [y/N] ").strip().lower() != "y":
                    sys.exit(1)

    # stop other Wi-Fi profiles grabbing the radio at boot (remembered so --remove-ap can restore them)
    AP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    disabled = []
    for name in _wifi_profiles():
        if name != AP_CON and _sh("nmcli", "-g", "connection.autoconnect", "con", "show", name,
                                  check=False).stdout.strip() == "yes":
            _sh("nmcli", "con", "modify", name, "connection.autoconnect", "no")
            disabled.append(name)
            print(f"Disabled auto-connect for Wi-Fi profile '{name}' (restore with --remove-ap).")
    (AP_STATE_DIR / "disabled_profiles.txt").write_text("".join(n + "\n" for n in disabled))

    _sh("nmcli", "con", "delete", AP_CON, check=False)
    _sh("nmcli", "con", "add", "type", "wifi", "ifname", a.iface, "con-name", AP_CON, "autoconnect", "yes",
        "ssid", a.ssid, "802-11-wireless.mode", "ap", "802-11-wireless.band", a.band,
        "802-11-wireless.channel", chan, "ipv4.method", "shared", "ipv4.addresses", f"{AP_IP}/24",
        "ipv6.method", "disabled", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
        "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.psk", password,
        "connection.autoconnect-priority", "100")
    _sh("nmcli", "con", "up", AP_CON)

    print("\n================ TANK ACCESS POINT ================")
    print(f" Network:   {a.ssid}   ({'5' if a.band == 'a' else '2.4'} GHz, channel {chan}, on {a.iface})")
    print(f" Password:  {password}" + ("   (generated - write it down)" if generated else ""))
    print(f" Tank HUD:  http://{AP_IP}:5000")
    print(" It starts automatically on every boot.")
    print("===================================================")


def ap_main(argv):
    import argparse
    p = argparse.ArgumentParser(prog="tank_hud.py", description="Host a Wi-Fi network from the Pi (NetworkManager). Run with sudo.")
    p.add_argument("--setup-ap", action="store_true", help="create and start the access point")
    p.add_argument("--remove-ap", action="store_true", help="remove it and restore your old Wi-Fi profiles")
    p.add_argument("-s", "--ssid", default="TankNet")
    p.add_argument("-p", "--password", default="", help="8-63 chars (random if omitted)")
    p.add_argument("-i", "--iface", default="wlan0", help="e.g. wlan1 for a USB antenna")
    p.add_argument("-b", "--band", choices=["bg", "a"], default="bg", help="bg = 2.4 GHz (best range), a = 5 GHz")
    p.add_argument("-c", "--channel", default="")
    p.add_argument("-C", "--country", default="", help="e.g. GB")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before dropping an SSH session")
    a = p.parse_args(argv)
    if os.geteuid() != 0:
        sys.exit("Run with sudo:  sudo python3 tank_hud.py --setup-ap -s TankNet -C GB")
    if not shutil.which("nmcli") or _sh("systemctl", "is-active", "--quiet", "NetworkManager", check=False).returncode != 0:
        sys.exit("NetworkManager not found or not running. This needs Raspberry Pi OS Bookworm or newer.")
    try:
        _ap_remove() if a.remove_ap else _ap_setup(a)
    except subprocess.CalledProcessError as e:
        print("Command failed:", " ".join(e.cmd))
        print((e.stderr or e.stdout or "").strip())
        print("\nSee why with:  sudo journalctl -u NetworkManager -n 60 --no-pager")
        sys.exit(1)


if __name__ == "__main__" and any(x in sys.argv for x in ("--setup-ap", "--remove-ap")):
    ap_main(sys.argv[1:])
    sys.exit(0)

try:
    import cv2
    import numpy as np
    from flask import Flask, Response, jsonify, render_template_string, request
except ImportError as _e:
    sys.exit(f"Missing Python library '{_e.name}'.\nInstall everything with:\n"
             "  sudo apt install -y python3-opencv python3-flask python3-numpy python3-smbus i2c-tools")

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "models"
DB_PATH = HERE / "faces.json"
BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "detector": ("face_detection_yunet_2023mar.onnx",
                 f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    "recognizer": ("face_recognition_sface_2021dec.onnx",
                   f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx"),
}

# --- Camera & precision settings ---
CAM_W, CAM_H = 960, 540
AUTO_PROCESS_WIDTH = 480      # detector input width (was 400): more reliable on small faces
FACE_SCORE = 0.60
TARGET_AUTO_FPS = 15          # auto mode adapts detection frequency to hover around this
STRIDE_MIN, STRIDE_MAX = 1, 4 # detect every Nth frame (Kalman coasts in between)

MATCH_THRESHOLD = 0.363       # SFace cosine threshold
MATCH_MARGIN = 0.04           # best person must beat the runner-up by this much
MIN_FACE = 18                 # px (full-res) to track a face at all
MIN_RECOG_FACE = 40           # px needed before we trust a recognition (~3 m at 960 wide)
RECOG_KNOWN_EVERY = 3         # re-check known people every Nth processed frame
RECOG_BUDGET = 2              # max recognitions per processed frame (keeps fps steady)
CONFIRM_HITS = 2              # detections before a contact is shown / targetable
BOX_SMOOTH = 0.5              # 0..1, higher = follows the raw detection more closely
MAX_AGE = 1.2
LEAD_FRAMES = 2.0
ASSOC_MAX = 0.52

ENROLL_SAMPLES = 5
ENROLL_MIN_FACE = 64
ENROLL_GAP = 0.4              # s between samples
ENROLL_TIMEOUT = 25           # s
MAX_SAMPLES = 15              # per person


# ---------------- Arduino car serial bridge ----------------
# The Pi hosts the webpage and sends single-letter commands to the Arduino.
# Arduino firmware must be flashed from Infrared_remote_control_car_serial.ino.
SERIAL_PORT = os.environ.get("CAR_SERIAL_PORT", "/dev/ttyACM0")
SERIAL_BAUD = int(os.environ.get("CAR_SERIAL_BAUD", "9600"))
CAR_SERIAL_TIMEOUT = 0.35
_car_serial = None
_car_serial_lock = threading.Lock()
_car_state = {"connected": False, "command": "S", "message": "Arduino not connected"}
_car_last_command_at = 0.0


def _car_connect():
    """Connect lazily so the face-scan webpage still starts if the Arduino is unplugged."""
    global _car_serial
    try:
        import serial
        with _car_serial_lock:
            if _car_serial is not None and _car_serial.is_open:
                return True
            try:
                _car_serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.15,
                                            write_timeout=0.3, exclusive=True)
            except TypeError:  # older pyserial on some Pi OS releases
                _car_serial = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.15,
                                            write_timeout=0.3)
            time.sleep(2.0)  # many Arduino boards reset when serial opens
            _car_serial.reset_input_buffer()
            _car_state.update(connected=True, message=f"Connected: {SERIAL_PORT}")
            log(f"Arduino serial connected on {SERIAL_PORT} at {SERIAL_BAUD} baud")
            return True
    except Exception as exc:
        _car_state.update(connected=False, message=f"Arduino unavailable: {exc}")
        return False


def car_command(command):
    """Send F/B/L/R/S. Any failure is reported in /status."""
    global _car_last_command_at
    command = str(command).upper()
    if command not in {"F", "B", "L", "R", "S"}:
        return False, "Invalid car command"
    if not _car_connect():
        return False, _car_state["message"]
    try:
        with _car_serial_lock:
            if _car_serial is None or not _car_serial.is_open:
                _car_state.update(connected=False, message="Arduino serial port closed")
                return False, _car_state["message"]
            _car_serial.write((command + "\n").encode("ascii"))
            _car_serial.flush()
        _car_last_command_at = time.monotonic()
        _car_state.update(connected=True, command=command,
                          message=f"Sent {command} to Arduino")
        return True, _car_state["message"]
    except Exception as exc:
        _car_state.update(connected=False, message=f"Serial error: {exc}")
        return False, _car_state["message"]


def _car_safety_watchdog():
    # Stop the motors if a browser disappears or stops sending drive commands.
    while True:
        time.sleep(0.1)
        if _car_state.get("command") != "S" and time.monotonic() - _car_last_command_at > CAR_SERIAL_TIMEOUT:
            car_command("S")


def _car_shutdown():
    try:
        car_command("S")
    except Exception:
        pass
    try:
        if _car_serial is not None:
            _car_serial.close()
    except Exception:
        pass


atexit.register(_car_shutdown)


PORT = 5000
LCD_ENABLED = True
LCD_ADDR = None               # None = auto-detect (usually 0x27 or 0x3F)
LCD_BUS = 1

GREEN, RED, YELLOW, CYAN, WHITE, ORANGE = (0, 200, 0), (0, 0, 255), (0, 220, 255), (255, 255, 0), (255, 255, 255), (0, 128, 255)

# ---------------- shared state ----------------
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # don't print every /status poll
lock = threading.RLock()
frame_cond = threading.Condition()
stream = {"frame": None, "id": 0}
_jpeg_lock = threading.Lock()
_jpeg = {"id": -1, "bytes": b""}

app_state = {
    "sys_mode": "manual",    # "manual" (~30fps) or "auto" (AI active)
    "aim_mode": "chest",     # "chest" or "head"
    "filter_mode": "normal", # "normal", "nv", "thermal"
    "ammo": 8,
    "max_ammo": 8,
    "firing": False,
    "fps": 0.0,
    "people": 0,
    "target": "None",
    "auto_target": True,     # False = reticle only follows a contact you LOCK
    "lock_id": None,
    "click": None,           # (nx, ny) from the video, consumed by the loop
    "contacts": [],
    "stride": 2,
    "error": "",
}
db = {}                      # name -> {"features": (n,128) unit rows, "enemy": bool}
enroll_job = None            # {"name","enemy","samples","t0","last"}
enroll_msg = ""
ERRORS = {}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def set_error(src, msg):
    """Show an error in the terminal, on the web page and on the LCD."""
    with lock:
        changed = ERRORS.get(src) != msg
        ERRORS[src] = msg
        app_state["error"] = " | ".join(f"{k}: {v}" for k, v in ERRORS.items())
    if changed:
        log(f"ERROR [{src}] {msg}")


def clear_error(src):
    with lock:
        if ERRORS.pop(src, None) is None:
            return
        app_state["error"] = " | ".join(f"{k}: {v}" for k, v in ERRORS.items())
    log(f"OK    [{src}] recovered")


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>TACTICAL // COMMAND CONSOLE</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {
            --bg-base: #07090c; --panel-bg: #0e1217; --border-color: #212d3b;
            --accent: #00e5ff; --accent-dim: #004d55; --danger: #ff334b;
            --warn: #ff8000; --ok: #2bd96b; --text-main: #c9d1d9; --text-dim: #6e7681;
        }
        body { background-color: var(--bg-base); color: var(--text-main);
            font-family: 'JetBrains Mono','Courier New',Courier,monospace; margin: 0; padding: 16px;
            display: flex; flex-direction: column; align-items: center; }
        header { width: 100%; max-width: 960px; display: flex; justify-content: space-between;
            align-items: center; border-bottom: 1px solid var(--border-color);
            padding-bottom: 8px; margin-bottom: 12px; }
        h1 { font-size: 14px; letter-spacing: 3px; color: var(--accent); margin: 0; text-transform: uppercase; }
        h2 { font-size: 11px; letter-spacing: 2px; color: var(--accent); margin: 0 0 8px; }
        .telemetry-bar { font-size: 11px; color: var(--text-dim); letter-spacing: 1px; }
        .err { width: 100%; max-width: 960px; color: #fff; background: #7a1624; font-size: 12px;
            padding: 6px 10px; margin-bottom: 8px; box-sizing: border-box; display: none; }
        .console-frame { background: var(--panel-bg); border: 1px solid var(--border-color);
            padding: 8px; box-shadow: 0 4px 24px rgba(0,0,0,.6); max-width: 100%; }
        .video-container { position: relative; line-height: 0; border: 1px solid var(--border-color); }
        img { width: 100%; max-width: 960px; height: auto; display: block; cursor: crosshair; }
        .control-deck { margin-top: 12px; display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; width: 100%; max-width: 960px; }
        button { background: var(--panel-bg); color: var(--text-main); border: 1px solid var(--border-color);
            padding: 10px 14px; font-size: 11px; font-family: inherit; letter-spacing: 1px; font-weight: 600;
            cursor: pointer; text-transform: uppercase; transition: all .1s ease; text-align: center; }
        button:hover { border-color: var(--accent); color: var(--accent); }
        button:active, button.active { background: var(--accent-dim); border-color: var(--accent); color: var(--accent); }
        button.danger { border-color: #59252b; color: var(--danger); }
        button.danger:hover { border-color: var(--danger); background: rgba(255,51,75,.1); }
        button.danger:active { background: var(--danger); color: #fff; }
        button.small { padding: 4px 8px; font-size: 10px; }
        button.enemy { border-color: #59252b; color: var(--danger); }
        button.friend { border-color: #1d5b36; color: var(--ok); }
        button.locked { border-color: var(--warn); color: var(--warn); background: rgba(255,128,0,.12); }
        .drive-panel { margin-top: 12px; padding: 10px; width: 100%; max-width: 960px;
            border: 1px solid var(--border-color); background: var(--panel-bg); }
        .drive-pad { display: grid; grid-template-columns: repeat(3, minmax(70px, 110px));
            gap: 7px; justify-content: center; margin: 10px auto; }
        .drive-pad button { min-height: 48px; touch-action: none; user-select: none; }
        .drive-status { text-align: center; font-size: 11px; color: var(--text-dim); }
        .panels { margin-top: 12px; display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
            gap: 8px; width: 100%; max-width: 960px; }
        .panel { background: var(--panel-bg); border: 1px solid var(--border-color); padding: 10px; }
        .list { display: flex; flex-direction: column; gap: 6px; min-height: 20px; }
        .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 12px; }
        .row .btns { display: flex; gap: 4px; }
        .dim { color: var(--text-dim); font-size: 11px; }
        input, select { background: #0a0d11; color: var(--text-main); border: 1px solid var(--border-color);
            padding: 8px; font-family: inherit; font-size: 12px; width: 100%; box-sizing: border-box; margin-bottom: 6px; }
        #enrollStatus { font-size: 11px; margin-top: 6px; color: var(--warn); min-height: 14px; }
    </style>
</head>
<body>
    <header>
        <h1>[ TANK CONTROL SYSTEM v2.5 ]</h1>
        <div class="telemetry-bar" id="status">CONNECTING...</div>
    </header>

    <div id="errBanner" class="err"></div>
    <div class="console-frame">
        <div class="video-container">
            <img id="video" src="{{ url_for('video_feed') }}" title="Click a contact to lock it">
        </div>
    </div>

    <div class="drive-panel">
        <h2>ARDUINO CAR DRIVE</h2>
        <div id="carStatus" class="drive-status">Checking Arduino serial connection…</div>
        <div class="drive-pad">
            <span></span>
            <button type="button" data-car="F" aria-label="Forward">▲ FORWARD</button>
            <span></span>
            <button type="button" data-car="L" aria-label="Left">◀ LEFT</button>
            <button type="button" data-car="S" class="danger" id="carStop">■ STOP</button>
            <button type="button" data-car="R" aria-label="Right">RIGHT ▶</button>
            <span></span>
            <button type="button" data-car="B" aria-label="Reverse">▼ REVERSE</button>
            <span></span>
        </div>
        <div class="drive-status">Hold a direction to drive; release to stop. Stop is also sent if the browser connection drops.</div>
    </div>

    <div class="control-deck">
        <button id="btnSysMode" onclick="sendAction('/toggle_sys_mode')">SYS: MANUAL</button>
        <button id="btnFire" class="danger" onclick="sendAction('/fire')">FIRE WEAPON</button>
        <button onclick="sendAction('/reload')">RELOAD AMMO</button>
        <button id="btnFilter" onclick="sendAction('/toggle_filter')">FILTER: NORM</button>
        <button id="btnAim" onclick="sendAction('/toggle_aim_mode')">AIM: CHEST</button>
        <button id="btnAuto" onclick="sendAction('/toggle_auto_target')">AUTO-TARGET: ON</button>
        <button onclick="sendAction('/lock_clear')">CLEAR LOCK</button>
    </div>

    <div class="panels">
        <div class="panel"><h2>CONTACTS</h2><div id="contacts" class="list"></div>
            <div class="dim" style="margin-top:8px">AUTO mode only. Click a person in the video, or press LOCK.</div></div>
        <div class="panel"><h2>ROSTER</h2><div id="roster" class="list"></div></div>
        <div class="panel"><h2>ENROLL</h2>
            <input id="enrollName" maxlength="24" placeholder="name (letters, numbers, space _ . -)">
            <select id="enrollRole"><option value="enemy">enroll as ENEMY</option><option value="friend">enroll as FRIEND</option></select>
            <div class="control-deck" style="margin:0; grid-template-columns:1fr 1fr">
                <button onclick="startEnroll()">ENROLL (5 SAMPLES)</button>
                <button onclick="sendAction('/enroll_cancel')">CANCEL</button>
            </div>
            <div id="enrollStatus"></div>
            <div class="dim">Face the camera, 1-3 m away, good front light. Re-using a name adds samples.</div>
        </div>
    </div>

<script>
    const $ = id => document.getElementById(id);
    function el(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        return e;
    }
    async function post(url, body) {
        try {
            const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify(body || {}) });
            if (!res.ok) {
                let msg = res.statusText;
                try { msg = (await res.json()).error || msg; } catch (e) {}
                return { ok: false, error: msg };
            }
            return { ok: true };
        } catch (e) { return { ok: false, error: String(e) }; }
    }
    function sendAction(endpoint) { post(endpoint); }

    // Hold-to-drive controls; pointer release/cancel always sends STOP.
    let activeDriveButton = null;
    let driveHeartbeat = null;
    async function sendCarCommand(command) {
        const result = await post('/car/command', { command });
        if (!result.ok && $('carStatus')) $('carStatus').innerText = result.error || 'Car command failed';
    }
    document.querySelectorAll('[data-car]').forEach(btn => {
        const command = btn.dataset.car;
        if (command === 'S') {
            btn.addEventListener('click', () => { activeDriveButton = null; if (driveHeartbeat) { clearInterval(driveHeartbeat); driveHeartbeat = null; } sendCarCommand('S'); });
            return;
        }
        btn.addEventListener('pointerdown', e => {
            e.preventDefault();
            activeDriveButton = btn;
            try { btn.setPointerCapture(e.pointerId); } catch (_) {}
            if (driveHeartbeat) clearInterval(driveHeartbeat);
            sendCarCommand(command);
            driveHeartbeat = setInterval(() => {
                if (activeDriveButton === btn) sendCarCommand(command);
            }, 150);
        });
        const release = () => {
            if (activeDriveButton === btn) {
                activeDriveButton = null;
                if (driveHeartbeat) { clearInterval(driveHeartbeat); driveHeartbeat = null; }
                sendCarCommand('S');
            }
        };
        btn.addEventListener('pointerup', release);
        btn.addEventListener('pointercancel', release);
        btn.addEventListener('lostpointercapture', release);
        btn.addEventListener('contextmenu', e => e.preventDefault());
    });
    window.addEventListener('blur', () => { activeDriveButton = null; if (driveHeartbeat) { clearInterval(driveHeartbeat); driveHeartbeat = null; } sendCarCommand('S'); });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) { activeDriveButton = null; if (driveHeartbeat) { clearInterval(driveHeartbeat); driveHeartbeat = null; } sendCarCommand('S'); }
    });

    async function startEnroll() {
        const r = await post('/enroll', { name: $('enrollName').value, role: $('enrollRole').value });
        if (!r.ok) $('enrollStatus').innerText = r.error;
    }

    $('video').addEventListener('click', e => {
        const r = $('video').getBoundingClientRect();
        post('/lock_click', { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
    });

    let contactsKey = '', rosterKey = '';
    function renderContacts(data) {
        const key = JSON.stringify([data.contacts, data.lock_id]);
        if (key === contactsKey) return;
        contactsKey = key;
        const box = $('contacts'); box.replaceChildren();
        if (!data.contacts.length) { box.appendChild(el('div', 'dim', data.sys_mode === 'auto' ? 'no contacts' : 'switch SYS to AUTO')); return; }
        for (const c of data.contacts) {
            const row = el('div', 'row');
            row.appendChild(el('span', '', `#${c.id} ${c.name}${c.lost ? ' (lost)' : ''}`));
            const btn = el('button', 'small ' + (data.lock_id === c.id ? 'locked' : (c.role === 'enemy' ? 'enemy' : c.role === 'friend' ? 'friend' : '')),
                           data.lock_id === c.id ? 'LOCKED' : 'LOCK');
            btn.onclick = () => post('/lock', { id: c.id });
            row.appendChild(btn);
            box.appendChild(row);
        }
    }
    function renderRoster(data) {
        const key = JSON.stringify(data.roster);
        if (key === rosterKey) return;
        rosterKey = key;
        const box = $('roster'); box.replaceChildren();
        if (!data.roster.length) { box.appendChild(el('div', 'dim', 'nobody enrolled yet')); return; }
        for (const p of data.roster) {
            const row = el('div', 'row');
            row.appendChild(el('span', '', `${p.name} `));
            row.lastChild.appendChild(el('span', 'dim', `(${p.samples})`));
            const btns = el('div', 'btns');
            const role = el('button', 'small ' + (p.enemy ? 'enemy' : 'friend'), p.enemy ? 'ENEMY' : 'FRIEND');
            role.onclick = () => post('/person_toggle', { name: p.name });
            const del = el('button', 'small danger', 'DEL');
            del.onclick = () => { if (confirm('Delete ' + p.name + '?')) post('/person_delete', { name: p.name }); };
            btns.appendChild(role); btns.appendChild(del); row.appendChild(btns);
            box.appendChild(row);
        }
    }

    setInterval(() => {
        fetch('/status').then(res => res.json()).then(data => {
            if ($('carStatus')) {
                $('carStatus').innerText = `ARDUINO: ${data.car.connected ? 'CONNECTED' : 'DISCONNECTED'} | ${data.car.message}`;
                $('carStatus').style.color = data.car.connected ? 'var(--ok)' : 'var(--danger)';
            }
            $('status').innerText =
                `FPS: ${data.fps} | AMMO: ${data.ammo}/${data.max_ammo} | CONTACTS: ${data.people} | LOCK: ${data.target}`;
            const sysBtn = $('btnSysMode');
            sysBtn.innerText = `SYS: ${data.sys_mode.toUpperCase()}`;
            sysBtn.classList.toggle('active', data.sys_mode === 'auto');
            $('btnFilter').innerText = `FILTER: ${data.filter_mode.toUpperCase()}`;
            $('btnAim').innerText = `AIM: ${data.aim_mode.toUpperCase()}`;
            $('btnAuto').innerText = `AUTO-TARGET: ${data.auto_target ? 'ON' : 'OFF'}`;
            $('btnAuto').classList.toggle('active', data.auto_target);
            renderContacts(data);
            renderRoster(data);
            const eb = $('errBanner');
            eb.innerText = data.error ? 'ERROR: ' + data.error : '';
            eb.style.display = data.error ? 'block' : 'none';
            const en = data.enroll;
            $('enrollStatus').innerText = en.active
                ? `ENROLLING ${en.name}: ${en.have}/${en.need} - face the camera` : en.msg;
        }).catch(err => {});
    }, 400);
</script>
</body>
</html>
"""


# ---------------- models / database ----------------
def ensure_models():
    MODEL_DIR.mkdir(exist_ok=True)
    paths = {}
    for key, (fname, url) in MODELS.items():
        path = MODEL_DIR / fname
        if not path.exists() or path.stat().st_size < 100_000:
            log(f"Downloading {fname} (one-off) ...")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    path.write_bytes(r.read())
            except Exception as e:
                raise RuntimeError(f"model download failed ({type(e).__name__}); needs internet once. "
                                   f"Or copy the 'models' folder to {MODEL_DIR}")
        paths[key] = str(path)
    return paths


def unit(v):
    v = np.asarray(v, np.float32).reshape(-1)
    return v / (np.linalg.norm(v) + 1e-9)


def load_db():
    out = {}
    if DB_PATH.exists():
        try:
            for name, v in json.loads(DB_PATH.read_text()).items():
                if isinstance(v, dict) and "features" in v:      # v2.5 format
                    feats, enemy = np.array(v["features"], np.float32).reshape(-1, 128), bool(v.get("enemy", False))
                elif isinstance(v, dict):                          # v2.4 format (one sample)
                    feats, enemy = np.array(v["feature"], np.float32).reshape(1, -1), bool(v.get("enemy", False))
                else:                                              # oldest format
                    feats, enemy = np.array(v, np.float32).reshape(1, -1), False
                out[name] = {"features": np.stack([unit(r) for r in feats]), "enemy": enemy}
        except Exception as e:
            print("faces.json unreadable, starting empty:", e)
    return out


def save_db():
    """Call with `lock` held (or before threads start)."""
    data = {n: {"features": d["features"].tolist(), "enemy": d["enemy"]} for n, d in db.items()}
    try:
        tmp = DB_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(DB_PATH)
        clear_error("db")
    except OSError as e:
        set_error("db", f"can't save faces.json ({e.strerror or e})")


def identify(feature, database):
    """Score = mean of the 2 best sample similarities per person; must clear the threshold
    and beat the runner-up by MATCH_MARGIN, otherwise 'unknown'."""
    q = unit(feature)
    scores = []
    for name, d in list(database.items()):
        sims = np.sort(d["features"] @ q)[::-1]
        scores.append((float(np.mean(sims[:2])), name))
    if not scores:
        return "unknown", 0.0
    scores.sort(reverse=True)
    best, name = scores[0]
    if best < MATCH_THRESHOLD:
        return "unknown", best
    if len(scores) > 1 and best - scores[1][0] < MATCH_MARGIN:
        return "unknown", best
    return name, best


def face_ok(raw, strict=False):
    """Roughly frontal and upright? raw = YuNet row: x,y,w,h, r-eye, l-eye, nose, r-mouth, l-mouth, score."""
    re, le, nose = raw[4:6], raw[6:8], raw[8:10]
    d = float(np.hypot(re[0] - le[0], re[1] - le[1]))
    if d < 1:
        return False
    yaw = abs(float(nose[0]) - (float(re[0]) + float(le[0])) / 2) / d
    roll = abs(float(re[1]) - float(le[1])) / d
    lim = 0.22 if strict else 0.40
    return yaw < lim and roll < lim * 1.2


# ---------------- camera ----------------
def open_camera():
    for idx in (0, 1, 2, 3):
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
            for _ in range(5):                      # some cameras need a few frames to wake up
                if cap.read()[0]:
                    log(f"Camera opened on /dev/video{idx}")
                    return cap
        cap.release()
    raise RuntimeError("no working camera on /dev/video0-3 (check USB cable; is another program using it?)")


class Camera:
    """Reads frames on its own thread so capture/decode overlaps with processing."""

    def __init__(self):
        self.cap = open_camera()
        self.frame, self.idx = None, 0
        self.last_ok = time.time()
        self.running = True
        self.cond = threading.Condition()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.running:
            ok, f = self.cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            with self.cond:
                self.frame, self.idx = f, self.idx + 1
                self.last_ok = time.time()
                self.cond.notify_all()

    def close(self):
        self.running = False
        self.thread.join(1.0)
        try:
            self.cap.release()
        except Exception:
            pass

    def read(self, last_idx, timeout=1.0):
        with self.cond:
            if self.idx == last_idx:
                self.cond.wait(timeout)
            if self.frame is None or self.idx == last_idx:
                return None, last_idx
            return self.frame, self.idx


def placeholder_frame(lines, color=(0, 0, 255)):
    img = np.zeros((CAM_H, CAM_W, 3), np.uint8)
    for i, text in enumerate(lines):
        cv2.putText(img, text[:55], (30, CAM_H // 2 - 40 + 40 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    return img


def open_camera_blocking():
    """Keep trying until a camera works; meanwhile show why on the web page, terminal and LCD."""
    while True:
        try:
            return Camera()
        except RuntimeError as e:
            set_error("camera", str(e))
            publish(placeholder_frame(["NO CAMERA", str(e)[:55], "retrying every 3 s..."]))
            time.sleep(3)


def publish(frame):
    with frame_cond:
        stream["frame"] = frame
        stream["id"] += 1
        frame_cond.notify_all()


def jpeg_for(fid, frame):
    """Encode each frame once, shared by all viewers, and never while holding the main lock."""
    with _jpeg_lock:
        if _jpeg["id"] != fid:
            ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if ok:
                _jpeg.update(id=fid, bytes=enc.tobytes())
        return _jpeg["bytes"]


# ---------------- tracking ----------------
def dist(a, b):
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def aim_point(box, mode):
    x, y, w, h = box
    if mode == "head":
        return (x + w / 2, y + 0.25 * h)
    return (x + w / 2, y + 0.5 * h)


class Track:
    _next = 1

    def __init__(self, box, mode, now, det_face=None):
        self.id = Track._next
        Track._next += 1
        aim = aim_point(box, mode)
        kf = cv2.KalmanFilter(4, 2)
        kf.transitionMatrix = np.array([[1, 0, 1, 0], [0, 1, 0, 1], [0, 0, 1, 0], [0, 0, 0, 1]], np.float32)
        kf.measurementMatrix = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], np.float32)
        kf.processNoiseCov = np.diag([1, 1, 3, 3]).astype(np.float32)
        kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * 6
        kf.errorCovPost = np.eye(4, dtype=np.float32) * 8
        kf.statePost = np.array([[aim[0]], [aim[1]], [0], [0]], np.float32)
        self.kf = kf
        self.votes = deque(maxlen=8)
        self.coast = [0.0, 0.0]
        self.hits = 0
        self.misses = 0
        self.last_seen = now
        self.box = box
        self.det_face = det_face
        self.update(box, mode, now, det_face)

    def update(self, box, mode, now, det_face=None):
        if self.hits:  # smooth size/position jitter between detections
            a = BOX_SMOOTH
            box = tuple(a * n + (1 - a) * o for n, o in zip(box, self.box))
        self.box = box
        if det_face is not None:
            self.det_face = det_face
        self.hits += 1
        self.misses = 0
        self.last_seen = now
        self.coast = [0.0, 0.0]
        aim = aim_point(box, mode)
        self.kf.correct(np.array([[aim[0]], [aim[1]]], np.float32))

    @property
    def confirmed(self):
        return self.hits >= CONFIRM_HITS

    def vote(self, name):
        self.votes.append(name if name != "unknown" else "?")

    def state(self):
        x, y, vx, vy = self.kf.statePost.flatten()
        return float(x), float(y), float(vx), float(vy)

    def pred_center(self):
        _, _, vx, vy = self.state()
        bx, by, bw, bh = self.box
        k = 1 + self.misses
        return (bx + bw / 2 + vx * k, by + bh / 2 + vy * k)

    @property
    def name(self):
        c = Counter(v for v in self.votes if v != "?")
        if c:
            n, k = c.most_common(1)[0]
            if k >= 2 and k >= 0.5 * len(self.votes):
                return n
        return "unknown"


def associate(tracks, dets):
    pairs = []
    for ti, t in enumerate(tracks):
        tc, size = t.pred_center(), max(t.box[2], t.box[3], 1)
        for di, d in enumerate(dets):
            c = dist(tc, d["center"]) / size
            if c < ASSOC_MAX:
                pairs.append((c, ti, di))
    pairs.sort()
    used_t, used_d, matches = set(), set(), []
    for _, ti, di in pairs:
        if ti in used_t or di in used_d:
            continue
        used_t.add(ti)
        used_d.add(di)
        matches.append((ti, di))
    return (matches,
            [i for i in range(len(tracks)) if i not in used_t],
            [i for i in range(len(dets)) if i not in used_d])


def role(track, database):
    n = track.name
    d = database.get(n)
    if n == "unknown" or d is None:
        return "unknown", "unknown"
    return ("enemy" if d["enemy"] else "friend"), n


def detect_faces(detector, frame, width):
    h, w = frame.shape[:2]
    scale = width / w
    small = cv2.resize(frame, (int(w * scale), int(h * scale)))
    detector.setInputSize((small.shape[1], small.shape[0]))
    _, raw = detector.detect(small)
    faces = []
    for f in (raw if raw is not None else []):
        f = f.copy()
        f[:14] /= scale
        x, y, fw, fh = f[:4].astype(int)
        faces.append({"box": (x, y, fw, fh), "center": (x + fw / 2, y + fh / 2), "raw": f})
    return faces


# ---------------- enrollment ----------------
def commit_enrollment(job):
    global enroll_job, enroll_msg
    feats = np.stack(job["samples"]).astype(np.float32)
    with lock:
        if enroll_job is not job:      # cancelled meanwhile
            return
        name = job["name"]
        if name in db:
            db[name]["features"] = np.vstack([db[name]["features"], feats])[-MAX_SAMPLES:]
        else:
            db[name] = {"features": feats, "enemy": job["enemy"]}
        save_db()
        enroll_job = None
        enroll_msg = f"Enrolled {name} ({len(db[name]['features'])} samples)."


def enroll_step(job, faces, frame, recognizer, now):
    global enroll_job, enroll_msg
    if now - job["t0"] > ENROLL_TIMEOUT:
        if len(job["samples"]) >= 3:
            commit_enrollment(job)
        else:
            with lock:
                if enroll_job is job:
                    enroll_job = None
                    enroll_msg = "Enrollment timed out. Face the camera in good light, 1-3 m away."
        return
    if now - job["last"] < ENROLL_GAP:
        return
    cands = [f for f in faces if f["box"][2] >= ENROLL_MIN_FACE and f["raw"][14] >= 0.8
             and face_ok(f["raw"], strict=True)]
    if not cands:
        return
    f = max(cands, key=lambda c: c["box"][2])
    try:
        feat = unit(recognizer.feature(recognizer.alignCrop(frame, f["raw"])))
    except cv2.error:
        return
    job["samples"].append(feat)
    job["last"] = now
    if len(job["samples"]) >= ENROLL_SAMPLES:
        commit_enrollment(job)


# ---------------- drawing ----------------
def apply_tank_filters(frame, filter_mode):
    if filter_mode == "nv":
        b, g, r = cv2.split(frame)
        g = cv2.add(g, 50)
        b = cv2.multiply(b, 0.2)
        r = cv2.multiply(r, 0.2)
        return cv2.merge([b, g, r])
    elif filter_mode == "thermal":
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.applyColorMap(gray, cv2.COLORMAP_JET)
    return frame


def draw_tank_hud(frame, fcx, fcy, firing):
    cv2.circle(frame, (fcx, fcy), 18, CYAN, 1)
    cv2.circle(frame, (fcx, fcy), 3, CYAN, -1)
    cv2.line(frame, (fcx - 24, fcy), (fcx - 8, fcy), CYAN, 1)
    cv2.line(frame, (fcx + 8, fcy), (fcx + 24, fcy), CYAN, 1)
    cv2.line(frame, (fcx, fcy - 24), (fcx, fcy - 8), CYAN, 1)
    cv2.line(frame, (fcx, fcy + 8), (fcx, fcy + 24), CYAN, 1)
    if firing:
        cv2.circle(frame, (fcx, fcy), 45, ORANGE, 3)
        cv2.putText(frame, ">> WEAPON DISCHARGED <<", (fcx - 130, fcy - 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, ORANGE, 2)


def draw_enroll_overlay(frame, job):
    if job is not None:
        cv2.putText(frame, f"ENROLLING {job['name']}: {len(job['samples'])}/{ENROLL_SAMPLES}  face the camera, 1-3 m",
                    (20, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.55, YELLOW, 2)


# ---------------- Flask endpoints ----------------
def clean_name(raw):
    n = (raw or "").strip() if isinstance(raw, str) else ""
    return n if re.fullmatch(r"[A-Za-z0-9 _.\-]{1,24}", n) else None


def snapshot():
    with lock:
        s = {k: v for k, v in app_state.items() if k != "click"}
        s["car"] = dict(_car_state)
        s["roster"] = [{"name": n, "enemy": d["enemy"], "samples": int(len(d["features"]))}
                       for n, d in sorted(db.items())]
        job = enroll_job
        s["enroll"] = {"active": job is not None, "name": job["name"] if job else "",
                       "have": len(job["samples"]) if job else 0, "need": ENROLL_SAMPLES, "msg": enroll_msg}
        return s


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/status")
def status():
    return jsonify(snapshot())


@app.route("/toggle_sys_mode", methods=["POST"])
def toggle_sys_mode():
    with lock:
        app_state["sys_mode"] = "auto" if app_state["sys_mode"] == "manual" else "manual"
    return "", 204


@app.route("/toggle_aim_mode", methods=["POST"])
def toggle_aim_mode():
    with lock:
        app_state["aim_mode"] = "head" if app_state["aim_mode"] == "chest" else "chest"
    return "", 204


@app.route("/toggle_filter", methods=["POST"])
def toggle_filter():
    with lock:
        modes = ["normal", "nv", "thermal"]
        idx = modes.index(app_state["filter_mode"])
        app_state["filter_mode"] = modes[(idx + 1) % len(modes)]
    return "", 204


@app.route("/fire", methods=["POST"])
def fire():
    with lock:
        if app_state["ammo"] > 0:
            app_state["ammo"] -= 1
            app_state["firing"] = True
            threading.Timer(0.15, lambda: app_state.update({"firing": False})).start()
    return "", 204


@app.route("/reload", methods=["POST"])
def reload_ammo():
    with lock:
        app_state["ammo"] = app_state["max_ammo"]
    return "", 204


@app.route("/toggle_auto_target", methods=["POST"])
def toggle_auto_target():
    with lock:
        app_state["auto_target"] = not app_state["auto_target"]
    return "", 204


@app.route("/lock", methods=["POST"])
def lock_contact():
    cid = (request.get_json(silent=True) or {}).get("id")
    with lock:
        app_state["lock_id"] = cid if isinstance(cid, int) else None
    return "", 204


@app.route("/lock_clear", methods=["POST"])
def lock_clear():
    with lock:
        app_state["lock_id"] = None
    return "", 204


@app.route("/lock_click", methods=["POST"])
def lock_click():
    d = request.get_json(silent=True) or {}
    try:
        x, y = float(d["x"]), float(d["y"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="bad coordinates"), 400
    with lock:
        app_state["click"] = (x, y)
    return "", 204


@app.route("/enroll", methods=["POST"])
def enroll():
    global enroll_job, enroll_msg
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name"))
    if not name:
        return jsonify(error="Name must be 1-24 characters: letters, numbers, space, _ . -"), 400
    with lock:
        if enroll_job is not None:
            return jsonify(error="An enrollment is already running"), 409
        enroll_job = {"name": name, "enemy": data.get("role", "enemy") == "enemy",
                      "samples": [], "t0": time.time(), "last": 0.0}
        enroll_msg = ""
    return "", 204


@app.route("/enroll_cancel", methods=["POST"])
def enroll_cancel():
    global enroll_job, enroll_msg
    with lock:
        if enroll_job is not None:
            enroll_job, enroll_msg = None, "Enrollment cancelled."
    return "", 204


@app.route("/person_toggle", methods=["POST"])
def person_toggle():
    name = (request.get_json(silent=True) or {}).get("name")
    with lock:
        if name in db:
            db[name]["enemy"] = not db[name]["enemy"]
            save_db()
    return "", 204


@app.route("/person_delete", methods=["POST"])
def person_delete():
    name = (request.get_json(silent=True) or {}).get("name")
    with lock:
        if db.pop(name, None) is not None:
            save_db()
    return "", 204


@app.route("/joystick", methods=["POST"])
def handle_joystick():
    data = request.get_json(silent=True) or {}
    try:
        nx = float(data.get("x", 0))
        ny = float(data.get("y", 0))
    except (TypeError, ValueError):
        return jsonify(error="bad coordinates"), 400
    active = bool(data.get("active", False))

    if not active:
        car_command("S")
    else:
        if ny > 0.4 and abs(nx) < 0.5:
            car_command("F")
        elif ny < -0.4 and abs(nx) < 0.5:
            car_command("B")
        elif nx < -0.4 and abs(ny) < 0.5:
            car_command("L")
        elif nx > 0.4 and abs(ny) < 0.5:
            car_command("R")
        else:
            car_command("S")
    return "", 204


@app.route("/toggle_laser", methods=["POST"])
def toggle_laser():
    with lock:
        app_state["laser"] = not app_state.get("laser", False)
        state_val = app_state["laser"]
    cmd = "X" if state_val else "Z"
    car_command(cmd)
    return "", 204


def gen_frames():
    last = 0
    while True:
        with frame_cond:
            frame_cond.wait_for(lambda: stream["id"] != last, timeout=2.0)
            if stream["id"] == last:
                continue                    # timed out; wait again
            fid, frame = stream["id"], stream["frame"]
        data = jpeg_for(fid, frame)
        last = fid
        if data:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n")


@app.route("/car/command", methods=["POST"])
def car_control():
    data = request.get_json(silent=True) or {}
    command = str(data.get("command", "")).upper()
    if command not in {"F", "B", "L", "R", "S"}:
        return jsonify(ok=False, error="Command must be F, B, L, R or S"), 400
    ok, message = car_command(command)
    if not ok:
        return jsonify(ok=False, error=message), 503
    return jsonify(ok=True, message=message), 200


@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ---------------- main loop ----------------
def tracking_loop():
    paths = ensure_models()
    try:
        detector = cv2.FaceDetectorYN.create(paths["detector"], "", (320, 320), FACE_SCORE, 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(paths["recognizer"], "")
    except (cv2.error, AttributeError) as e:
        raise RuntimeError(f"OpenCV {cv2.__version__} can't load the face models "
                           f"(too old, or a corrupt file: delete the 'models' folder): {e}")
    cam = open_camera_blocking()
    try:
        run_loop(cam, detector, recognizer)
    finally:
        cam.close()


def run_loop(cam, detector, recognizer):
    global enroll_job
    tracks, target_id, reticle = [], None, None
    frame_idx = proc_idx = last_idx = 0
    stride = 2
    prev_mode = "manual"
    fps, t_prev = 0.0, time.time()
    last_tune = last_down = time.time()

    log("Tracking loop running.")
    first_frame = True

    while True:
        frame, last_idx = cam.read(last_idx)
        if frame is None:
            if time.time() - cam.last_ok > 3.0:
                raise RuntimeError("camera stopped sending frames")
            continue
        if first_frame:
            first_frame = False
            clear_error("camera")
            clear_error("loop")
        now = time.time()
        fps = 0.9 * fps + 0.1 / max(now - t_prev, 1e-3)
        t_prev = now

        h_orig, w_orig = frame.shape[:2]
        fcx, fcy = w_orig // 2, h_orig // 2
        if reticle is None:
            reticle = [float(fcx), float(fcy)]

        with lock:
            sys_mode = app_state["sys_mode"]
            aim_mode = app_state["aim_mode"]
            filter_mode = app_state["filter_mode"]
            firing = app_state["firing"]
            auto_target = app_state["auto_target"]
            lock_id = app_state["lock_id"]
            click = app_state["click"]
            app_state["click"] = None
            job = enroll_job

        if sys_mode != prev_mode:
            tracks, last_tune = [], now
            prev_mode = sys_mode

        # --- MANUAL MODE (~30 FPS), unchanged apart from enrollment support ---
        if sys_mode == "manual":
            if job is not None and frame_idx % 3 == 0:     # only detect while enrolling
                enroll_step(job, detect_faces(detector, frame, AUTO_PROCESS_WIDTH), frame, recognizer, now)
            frame_idx += 1
            frame = apply_tank_filters(frame, filter_mode)
            draw_tank_hud(frame, fcx, fcy, firing)
            cv2.putText(frame, "STATUS: MANUAL CONTROL", (20, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.6, CYAN, 2)
            draw_enroll_overlay(frame, job)
            with lock:
                app_state.update(fps=round(fps, 1), people=0, target="Manual", contacts=[])
            publish(frame)
            continue

        # --- AUTO MODE ---
        process = frame_idx % stride == 0
        frame_idx += 1

        if process:
            proc_idx += 1
            faces = detect_faces(detector, frame, AUTO_PROCESS_WIDTH)
            if job is not None:
                enroll_step(job, faces, frame, recognizer, now)

            dets = []
            for f in faces:
                if f["box"][2] >= MIN_FACE:
                    fx, fy, fw, fh = f["box"]
                    bx, by = max(0, int(fx - fw * 0.8)), max(0, int(fy - fh * 0.4))
                    bw, bh = int(fw * 2.6), int(fh * 4.5)
                    dets.append({"box": (bx, by, bw, bh), "center": (bx + bw / 2, by + bh / 2), "face": f})

            for t in tracks:
                t.kf.predict()
            matches, un_t, un_d = associate(tracks, dets)
            for ti, di in matches:
                tracks[ti].update(dets[di]["box"], aim_mode, now, dets[di]["face"])
            for ti in un_t:
                tracks[ti].misses += 1
            for di in un_d:
                tracks.append(Track(dets[di]["box"], aim_mode, now, dets[di]["face"]))
            tracks = [t for t in tracks if now - t.last_seen < (MAX_AGE if t.confirmed else 0.4)]

            # recognition: quality-gated, budgeted, unknowns first, known people re-checked on a stagger
            cands = []
            for t in tracks:
                f = t.det_face
                if t.last_seen != now or f is None or f["box"][2] < MIN_RECOG_FACE or not face_ok(f["raw"]):
                    continue
                if t.name != "unknown" and (proc_idx + t.id) % RECOG_KNOWN_EVERY:
                    continue
                cands.append((len(t.votes), -f["box"][2], t.id, t))
            cands.sort(key=lambda c: c[:3])
            for _, _, _, t in cands[:RECOG_BUDGET]:
                try:
                    feat = recognizer.feature(recognizer.alignCrop(frame, t.det_face["raw"]))
                except cv2.error:
                    continue
                n, _ = identify(feat, db)
                t.vote(n)

            # hold ~TARGET_AUTO_FPS by changing how often detection runs
            if now - last_tune > 2.0:
                last_tune = now
                if fps < TARGET_AUTO_FPS - 2.5 and stride < STRIDE_MAX:
                    stride += 1
                elif fps > TARGET_AUTO_FPS + 6 and stride > STRIDE_MIN and now - last_down > 6.0:
                    stride -= 1
                    last_down = now
        else:
            for t in tracks:
                t.kf.predict()
                _, _, vx, vy = t.state()
                bx, by, bw, bh = t.box
                t.box = (bx + vx, by + vy, bw, bh)
                t.coast[0] += vx
                t.coast[1] += vy

        confirmed = [t for t in tracks if t.confirmed]

        # click on the video -> lock that contact
        if click is not None:
            cx_, cy_ = click[0] * w_orig, click[1] * h_orig
            hit = None
            for t in confirmed:
                x, y, bw, bh = t.box
                x, y = x + t.coast[0], y + t.coast[1]
                if x <= cx_ <= x + bw and y <= cy_ <= y + bh and (hit is None or bw * bh < hit.box[2] * hit.box[3]):
                    hit = t
            if hit is not None:
                with lock:
                    app_state["lock_id"] = lock_id = hit.id

        # choose the target: a manual lock wins; otherwise sticky among enemies (if auto-target is on)
        target = None
        if lock_id is not None:
            target = next((t for t in confirmed if t.id == lock_id), None)
            if target is None:
                with lock:
                    app_state["lock_id"] = lock_id = None
        if target is None and auto_target:
            enemy_tracks = [t for t in confirmed if role(t, db)[0] == "enemy"]
            if target_id not in [t.id for t in enemy_tracks]:
                target_id = (max(enemy_tracks, key=lambda t: t.box[2] * t.box[3]).id if enemy_tracks else None)
            target = next((t for t in enemy_tracks if t.id == target_id), None)

        frame = apply_tank_filters(frame, filter_mode)

        contacts = []
        for t in confirmed:
            r, n = role(t, db)
            contacts.append({"id": t.id, "name": n, "role": r, "lost": bool(t.misses)})
            color = {"enemy": RED, "friend": GREEN, "unknown": YELLOW}[r]
            bx, by, bw, bh = t.box
            ox, oy = t.coast
            px, py, pw, ph = int(bx + ox), int(by + oy), int(bw), int(bh)

            L = max(8, min(pw, ph) // 4)
            for (cx, cy, dx, dy) in ((px, py, 1, 1), (px + pw, py, -1, 1), (px, py + ph, 1, -1), (px + pw, py + ph, -1, -1)):
                cv2.line(frame, (cx, cy), (cx + dx * L, cy), color, 2)
                cv2.line(frame, (cx, cy), (cx, cy + dy * L), color, 2)

            tag = {"enemy": f"ENEMY: {n}", "friend": f"FRIEND: {n}", "unknown": "UNKNOWN"}[r]
            tag = f"#{t.id} {tag}" + (" [LOST]" if t.misses else "") + (" [LOCK]" if t is target and lock_id else "")
            cv2.putText(frame, tag, (px, max(py - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

            if r == "enemy" or t is target:
                ax, ay, vx, vy = t.state()
                lim = 0.3 * max(bw, bh)
                ax += float(np.clip(vx * LEAD_FRAMES, -lim, lim))
                ay += float(np.clip(vy * LEAD_FRAMES, -lim, lim))
                cv2.circle(frame, (int(ax), int(ay)), max(8, min(pw, ph) // 6),
                           ORANGE if (t is target and r != "enemy") else RED, 2 if not t.misses else 1)

        if target is not None:
            ax, ay, vx, vy = target.state()
            lim = 0.3 * max(target.box[2], target.box[3])
            tx = float(np.clip(ax + np.clip(vx * LEAD_FRAMES, -lim, lim), 0, w_orig))
            ty = float(np.clip(ay + np.clip(vy * LEAD_FRAMES, -lim, lim), 0, h_orig))
        else:
            tx, ty = fcx, fcy
        reticle[0] += (tx - reticle[0]) * 0.3
        reticle[1] += (ty - reticle[1]) * 0.3

        draw_tank_hud(frame, int(reticle[0]), int(reticle[1]), firing)
        cv2.putText(frame, f"DET 1/{stride}   AUTO-TARGET {'ON' if auto_target else 'OFF'}",
                    (20, h_orig - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, CYAN, 1)
        draw_enroll_overlay(frame, job)

        label = "None"
        if target is not None:
            label = f"#{target.id} {role(target, db)[1]}"
        with lock:
            app_state.update(fps=round(fps, 1), people=len(confirmed), target=label,
                             contacts=contacts, stride=stride)
        publish(frame)


# ---------------- 20x4 status LCD (HD44780 on a PCF8574 I2C backpack) ----------------
class LCD2004:
    COLS, ROWS = 20, 4
    ROW_ADDR = (0x80, 0xC0, 0x94, 0xD4)
    BACKLIGHT = 0x08

    def __init__(self, addr=None, bus_num=1):
        self.addr, self.bus_num = addr, bus_num
        self.lock = threading.Lock()
        self.bus, self.ok, self.fails = None, False, 0
        self.shown = [None] * 4
        self.give_up = False
        self.warned = set()
        self._open()

    def _warn(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            log(f"LCD off: {msg}")

    def _open(self):
        try:
            try:
                from smbus2 import SMBus
            except ImportError:
                from smbus import SMBus
        except ImportError:
            self.give_up = True
            self._warn("lib", "no I2C library. Install:  sudo apt install python3-smbus")
            return False
        try:
            self.bus = SMBus(self.bus_num)
        except OSError as e:
            self._warn("bus", f"can't open I2C bus {self.bus_num} ({e}). Enable it: "
                              "sudo raspi-config nonint do_i2c 0 && sudo reboot")
            return False
        addr = self.addr or self._probe()
        if addr is None:
            self._warn("probe", "no I2C device found. Check wiring and run:  i2cdetect -y 1")
            return False
        self.addr = addr
        try:
            self._init_display()
        except OSError as e:
            self._warn("init", f"found 0x{addr:02x} but could not talk to it ({e})")
            return False
        self.ok, self.fails, self.shown = True, 0, [None] * 4
        self.warned.clear()
        log(f"LCD ready at I2C address 0x{addr:02x}")
        return True

    def reopen(self):
        with self.lock:
            return self._open()

    def _probe(self):
        for a in (0x27, 0x3F, *range(0x20, 0x27), *range(0x38, 0x3F)):
            try:
                self.bus.write_byte(a, self.BACKLIGHT)
                return a
            except OSError:
                continue
        return None

    def _write4(self, nibble, rs):
        data = (nibble << 4) | (1 if rs else 0) | self.BACKLIGHT   # P0=RS P1=RW P2=EN P3=BL P4-7=D4-D7
        self.bus.write_byte(self.addr, data | 0x04)
        time.sleep(0.0005)
        self.bus.write_byte(self.addr, data & ~0x04)
        time.sleep(0.0001)

    def _send(self, byte, rs):
        self._write4(byte >> 4, rs)
        self._write4(byte & 0x0F, rs)

    def _init_display(self):
        time.sleep(0.05)
        for nib, wait in ((0x3, 0.005), (0x3, 0.001), (0x3, 0.001), (0x2, 0.001)):
            self._write4(nib, 0)
            time.sleep(wait)
        for cmd in (0x28, 0x0C, 0x01, 0x06):   # 4-bit/2-line, display on, clear, entry mode
            self._send(cmd, 0)
            if cmd == 0x01:
                time.sleep(0.003)

    @classmethod
    def clean(cls, text):
        return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(text))[:cls.COLS].ljust(cls.COLS)

    def show(self, rows):
        if not self.ok:
            return
        with self.lock:
            try:
                for i in range(self.ROWS):
                    text = self.clean(rows[i] if i < len(rows) else "")
                    if text != self.shown[i]:           # only rewrite rows that changed
                        self._send(self.ROW_ADDR[i], 0)
                        for ch in text:
                            self._send(ord(ch), 1)
                        self.shown[i] = text
                self.fails = 0
            except OSError as e:
                self.fails += 1
                if self.fails >= 3:
                    self.ok = False
                    self._warn("lost", f"lost contact ({e}); will retry")


def get_ips():
    out = []
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            parts = line.split()
            iface, ip = parts[1], parts[3].split("/")[0]
            if iface != "lo" and not ip.startswith("169.254"):
                out.append((iface, ip))
    except Exception:
        pass
    if not out:
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("10.255.255.255", 1))
            out.append(("net", sk.getsockname()[0]))
            sk.close()
        except Exception:
            pass
    return out


def lcd_rows(s, ip_text, tick, enroll=None):
    """What the 4 lines show. Pure function so it's easy to test. Each line is max 20 chars."""
    r0 = f"TANK {s['sys_mode'].upper():<6}{s['fps']:5.1f}fps"
    r1 = f"AMMO {s['ammo']:>2}/{s['max_ammo']:<2} PPL:{s['people']}"
    r2 = f"ENROLL {enroll[1]}/{enroll[2]} {enroll[0]}" if enroll else f"TGT {s['target']}"
    r3 = ip_text
    err = s.get("error") or ""
    if err:                                           # errors take over the bottom two lines
        r2 = "!! ERROR !!" if tick % 2 == 0 else ""
        if len(err) <= 20:
            r3 = err
        else:
            txt = err + "   "
            off = tick % len(txt)
            r3 = (txt + txt)[off:off + 20]
    return [r[:20] for r in (r0, r1, r2, r3)]


def lcd_loop(lcd):
    tick, last_try, ips, ips_t = 0, time.time(), get_ips(), time.time()
    lcd.show(["TANK HUD v2.6", "Starting up...", "", ""])
    while True:
        try:
            if not lcd.ok and not lcd.give_up and time.time() - last_try > 10:
                last_try = time.time()
                lcd.reopen()
            if time.time() - ips_t > 10:
                ips, ips_t = get_ips(), time.time()
            ip_text = f"{ips[(tick // 8) % len(ips)][1]}:{PORT}" if ips else "no network"
            with lock:
                s = dict(app_state)
                job = enroll_job
            enroll = (job["name"], len(job["samples"]), ENROLL_SAMPLES) if job else None
            lcd.show(lcd_rows(s, ip_text, tick, enroll))
        except Exception as e:                        # the LCD must never take the tank down
            log(f"LCD loop error: {e}")
        tick += 1
        time.sleep(0.5)


# ---------------- supervisor + main ----------------
def tracking_thread():
    """Restarts the tracking loop if it ever crashes, and reports why."""
    while True:
        try:
            tracking_loop()
        except Exception as e:
            traceback.print_exc()
            msg = f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
            set_error("loop", msg[:120])
            publish(placeholder_frame(["TRACKING ERROR", msg[:55], "restarting in 3 s..."]))
            time.sleep(3)


def main():
    log(f"Tank HUD v2.6 starting (Python {sys.version.split()[0]}, OpenCV {cv2.__version__}, NumPy {np.__version__})")
    db.update(load_db())
    log(f"{len(db)} enrolled face(s) loaded from {DB_PATH}")

    lcd = None
    if LCD_ENABLED:
        lcd = LCD2004(LCD_ADDR, LCD_BUS)
        threading.Thread(target=lcd_loop, args=(lcd,), daemon=True).start()
    threading.Thread(target=tracking_thread, daemon=True).start()
    threading.Thread(target=_car_safety_watchdog, daemon=True, name="car-safety-watchdog").start()

    ips = get_ips()
    log("Web interface on:" if ips else "Web interface on port %d (no network address yet)" % PORT)
    for iface, ip in ips:
        log(f"    http://{ip}:{PORT}   ({iface})")
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    except OSError as e:
        reason = f"port {PORT} already in use" if e.errno == 98 else str(e)
        set_error("web", reason)
        log(f"Web server could not start: {reason}")
        log(f"Find what is using it with:  sudo ss -ltnp | grep {PORT}")
        time.sleep(4)                                  # let the LCD show it
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        if lcd:
            lcd.show(["TANK HUD", "stopped", "", ""])


if __name__ == "__main__":
    main()
