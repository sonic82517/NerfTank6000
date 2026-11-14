# Autonomous navigation sensor wiring (planned)

Navigation is **disabled by default** in `face-scan.py`. The GPIO assignments below are a proposed wiring plan for three HC-SR04 ultrasonic modules connected directly to a Raspberry Pi 4. They are documentation only: the current Python sensor reader is still a stub and will not read these pins yet.

## Proposed Raspberry Pi pin map

Use **BCM GPIO numbering** in software (not physical header pin numbers):

| Sensor | TRIG (BCM GPIO) | TRIG physical pin | ECHO (BCM GPIO) | ECHO physical pin |
|---|---:|---:|---:|---:|
| Front | GPIO 5 | 29 | GPIO 6 | 31 |
| Left | GPIO 13 | 33 | GPIO 19 | 35 |
| Right | GPIO 16 | 36 | GPIO 26 | 37 |

For every module, connect `VCC` to a suitable **5 V supply**, `GND` to ground, and `TRIG`/`ECHO` to the corresponding pins above. Connect all grounds together, including the Pi, sensors, and Arduino/motor controller.

## Important electrical and setup notes

- **Do not connect HC-SR04 ECHO directly to a Raspberry Pi GPIO.** ECHO is typically 5 V, while Pi GPIO is 3.3 V tolerant. Use a voltage divider or proper level shifter on each ECHO line. Example divider: sensor ECHO -> 1 kΩ -> GPIO input; GPIO input -> 2 kΩ -> GND (about 3.3 V from a 5 V ECHO).
- A Pi GPIO output at 3.3 V usually works as HC-SR04 TRIG; check the specific module documentation.
- Power the sensors from a stable 5 V supply sized for them, not from a GPIO pin. Keep the Pi, sensor, and Arduino grounds common.
- These assignments avoid GPIO 2/3 used by the I2C LCD and GPIO 14/15 used by the Pi UART. Check your actual wiring and board before connecting anything.
- Fire the sensors one at a time and allow roughly 60 ms between pings to reduce ultrasonic cross-talk. Three sensors pinging simultaneously can give unreliable readings.
- Mount all three sensors rigidly to the **chassis**, pointing forward, left, and right. Do not mount navigation sensors on the rotating turret. The turret camera can then turn without changing the navigation sensor directions.
- Bench-test the sensor readings with the wheels lifted or motor power disconnected first. A missing, timed-out, or invalid reading should make navigation stop rather than assume the path is clear.

## Before enabling autonomous movement

The Python code currently only has a placeholder sensor reader. The Arduino sketch was not recovered from the Uno, so it will need to be recreated and reflashed to support the existing `F`, `B`, `L`, `R`, `S` serial commands (and any planned telemetry). Do not use the NAV toggle for driving until real sensor readings, motor behavior, and stop-on-fault behavior have all been implemented and tested. Keep autonomous navigation disabled by default during development.
