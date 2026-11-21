#!/usr/bin/env python3
"""Apply targeted startup, ultrasonic, and camera-load fixes to face-scan.py.

Run from this directory with: python3 apply_face_scan_fixes.py
The original file is backed up as face-scan.py.bak before it is changed.
"""
from pathlib import Path

path = Path(__file__).with_name("face-scan.py")
source = path.read_text()
original = source


def replace_once(old, new, description):
    global source
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"Expected exactly one match for {description}; found {count}. No files changed.")
    source = source.replace(old, new, 1)


# The GPIO setup happens before log() is defined. Avoid a secondary NameError if GPIO setup fails.
replace_once('''        log(f"Ultrasonic sensor init failed: {exc}")
''', '''        print(f"Ultrasonic sensor init failed: {exc}", flush=True)
''', "early GPIO error logging")

# Reduce capture, detector, and stream-encoding work on Raspberry Pi hardware.
replace_once("CAM_W, CAM_H = 960, 540", "CAM_W, CAM_H = 640, 480", "camera capture dimensions")
replace_once("AUTO_PROCESS_WIDTH = 480", "AUTO_PROCESS_WIDTH = 320", "face detector input width")
replace_once("cv2.CAP_PROP_BUFFERSIZE, 1)", "cv2.CAP_PROP_BUFFERSIZE, 1)\n            cap.set(cv2.CAP_PROP_FPS, 30)", "camera buffer configuration")
replace_once("cv2.IMWRITE_JPEG_QUALITY), 70", "cv2.IMWRITE_JPEG_QUALITY), 55", "stream JPEG quality")

# Keep the ultrasonic sensor polling off the frame-processing and HTTP status threads.
replace_once("ULTRASONIC_AVAILABLE = False\n", '''ULTRASONIC_AVAILABLE = False
_sonar_lock = threading.Lock()
_sonar_cache = {}


def ultrasonic_poll_loop():
    """Poll sensors off the video path; slow GPIO echo waits must not stall frames."""
    global _sonar_cache
    while True:
        values = read_ultrasonic_map()
        with _sonar_lock:
            _sonar_cache = values
        time.sleep(0.20)

''', "sonar cache declaration")

start = source.index("def apply_ultrasonic_avoidance():")
end = source.index("\n\n# ---------------- Arduino car serial bridge", start)
source = source[:start] + '''def apply_ultrasonic_avoidance(face_seen=False):
    """Fail-safe autonomous drive decision using the latest background sonar sample."""
    if not ULTRASONIC_AVAILABLE:
        return False
    with _sonar_lock:
        distances = dict(_sonar_cache)

    # Stop on people, or when the front sensor has no trustworthy reading.
    if face_seen:
        command = "S"
    else:
        front = distances.get("front")
        if front is None:
            command = "S"
        elif front >= 40.0:
            command = "F"
        else:
            left = distances.get("left")
            right = distances.get("right")
            left_clear = left is not None and left >= 25.0
            right_clear = right is not None and right >= 25.0
            if left_clear and right_clear:
                command = "L" if left >= right else "R"
            elif left_clear:
                command = "L"
            elif right_clear:
                command = "R"
            else:
                command = "S"

    with lock:
        enabled = app_state["auto_drive"] and app_state["sys_mode"] == "auto"
    if enabled:
        # Avoid writing the same command to USB serial on every video frame.
        global _auto_drive_last_command
        if command != _auto_drive_last_command:
            car_command(command)
            _auto_drive_last_command = command
    return True


_auto_drive_last_command = "S"
''' + source[end:]

# Correct the tracking-loop signature and remove an initialization block that references
# local variables before they have been assigned (causing UnboundLocalError).
replace_once("def tracking_loop():", "def tracking_loop(pan=None):", "tracking loop argument")
replace_once('''    if target is not None:
        error_x = tx - fcx
        error_y = ty - fcy

''', "", "uninitialized target block")

# Turn automatic drive off and stop immediately when returning to manual mode.
replace_once('''@app.route("/toggle_sys_mode", methods=["POST"])
def toggle_sys_mode():
    with lock:
        app_state["sys_mode"] = "auto" if app_state["sys_mode"] == "manual" else "manual"
    return "", 204
''', '''@app.route("/toggle_sys_mode", methods=["POST"])
def toggle_sys_mode():
    with lock:
        app_state["sys_mode"] = "auto" if app_state["sys_mode"] == "manual" else "manual"
        manual = app_state["sys_mode"] == "manual"
        if manual:
            app_state["auto_drive"] = False
    if manual:
        car_command("S")
    return "", 204
''', "system mode safety")

# Do not read ultrasonic GPIO synchronously once per video frame. Use detected tracks as
# the conservative person-stop signal and make one autonomous decision after tracking.
replace_once('''        # --- AUTO MODE ---
        if ULTRASONIC_AVAILABLE:
            apply_ultrasonic_avoidance()

        process = frame_idx % stride == 0
''', '''        # --- AUTO MODE ---
        process = frame_idx % stride == 0
''', "synchronous sonar call")
replace_once('''        confirmed = [t for t in tracks if t.confirmed]

        # click on the video -> lock that contact
''', '''        confirmed = [t for t in tracks if t.confirmed]
        if ULTRASONIC_AVAILABLE:
            apply_ultrasonic_avoidance(face_seen=bool(confirmed))

        # click on the video -> lock that contact
''', "auto-drive invocation")

# Snapshot the cache rather than triggering four more blocking sensor reads per /status poll.
replace_once('''        if ULTRASONIC_AVAILABLE:
            s["sonar"] = {n: round(v, 1) if v is not None else None for n, v in read_ultrasonic_map().items()}
        else:
            s["sonar"] = {}
''', '''        if ULTRASONIC_AVAILABLE:
            with _sonar_lock:
                sonar = dict(_sonar_cache)
            s["sonar"] = {n: round(v, 1) if v is not None else None for n, v in sonar.items()}
        else:
            s["sonar"] = {}
''', "status sonar cache")

# Do not fail on non-Pi systems where RPi.GPIO is unavailable.
replace_once("    pan = CameraPan()\n", "    pan = CameraPan(enabled=GPIO is not None)\n", "optional camera pan initialization")
replace_once('''    # Start tracking once, passing the pan controller.
    threading.Thread(target=tracking_thread, args=(pan,), daemon=True).start()
''', '''    if ULTRASONIC_AVAILABLE:
        threading.Thread(target=ultrasonic_poll_loop, daemon=True, name="ultrasonic-poller").start()

    # Start tracking once, passing the optional pan controller.
    threading.Thread(target=tracking_thread, args=(pan,), daemon=True).start()
''', "background sonar poller startup")

if source == original:
    raise SystemExit("No changes were made: no applicable fixes were found.")

backup = path.with_suffix(path.suffix + ".bak")
if backup.exists():
    raise SystemExit(f"Backup already exists at {backup}; move it before rerunning. No files changed.")
backup.write_text(original)
path.write_text(source)
print(f"Patched {path}; original saved to {backup}")
print("Run a syntax check: python3 -m py_compile face-scan.py")
