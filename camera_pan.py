"""Bounded Raspberry Pi servo control for a pan-mounted camera/sensor.

This module is intentionally an independent manual pan utility; it does not
connect servo motion to face recognition, target locking, or firing controls.

Wiring (standard positional servo):
  * signal -> BCM GPIO18 (physical pin 12)
  * V+ -> suitable external regulated supply for the servo (often 5 V)
  * GND -> external supply ground and Raspberry Pi GND (common ground)

Never power a servo from a Pi GPIO pin. Check your servo's voltage/current
requirements and calibrate the travel limits to avoid binding the mount.

Run `python3 camera_pan.py` on a Raspberry Pi for a simple interactive test.
"""
import time

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

SERVO_PIN = 23          # BCM numbering; physical pin 12
SERVO_MIN_ANGLE = 35.0  # conservative software limits; calibrate for your mount
SERVO_MAX_ANGLE = 180.0
SERVO_CENTER_ANGLE = 90.0
SERVO_MIN_DUTY = 2.5
SERVO_MAX_DUTY = 12.5


class CameraPan:
    """Set bounded absolute pan positions for a camera or sensor mount."""

    def __init__(self, pin=SERVO_PIN, enabled=True):
        if GPIO is None and enabled:
            raise RuntimeError("RPi.GPIO is unavailable; run this on Raspberry Pi OS and install RPi.GPIO")
        self.pin = pin
        self.pwm = None
        self.angle = SERVO_CENTER_ANGLE
        self.enabled = bool(enabled)
        if self.enabled:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            self.pwm = GPIO.PWM(self.pin, 50)
            self.pwm.start(0)
            self.set_angle(self.angle)
            time.sleep(0.25)
            self._release_signal()

    @staticmethod
    def _clamp(value, low, high):
        return max(low, min(high, float(value)))

    def _release_signal(self):
        # Stop PWM pulses after the servo has had time to reach its position.
        if self.pwm is not None:
            self.pwm.ChangeDutyCycle(0)

    def set_angle(self, angle):
        """Move to an angle clamped to the configured software travel limits."""
        self.angle = self._clamp(angle, SERVO_MIN_ANGLE, SERVO_MAX_ANGLE)
        if not self.enabled or self.pwm is None:
            return self.angle
        duty = SERVO_MIN_DUTY + (self.angle / 180.0) * (SERVO_MAX_DUTY - SERVO_MIN_DUTY)
        self.pwm.ChangeDutyCycle(duty)
        time.sleep(0.12)
        self._release_signal()
        return self.angle

    def center(self):
        return self.set_angle(SERVO_CENTER_ANGLE)

    def close(self):
        if self.pwm is not None:
            try:
                self.center()
                self.pwm.stop()
            finally:
                self.pwm = None
        if GPIO is not None:
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass
        self.enabled = False


def main():
    """Interactive manual test; enter an angle or `c` to center, `q` to quit."""
    pan = CameraPan()
    print(f"Servo ready on BCM GPIO{SERVO_PIN}. Allowed range: {SERVO_MIN_ANGLE:.0f}-{SERVO_MAX_ANGLE:.0f} degrees.")
    print("Enter an angle, 'c' to center, or 'q' to quit.")
    try:
        while True:
            raw = input("Pan> ").strip().lower()
            if raw in {"q", "quit", "exit"}:
                break
            if raw in {"c", "center"}:
                angle = pan.center()
            else:
                try:
                    angle = pan.set_angle(float(raw))
                except ValueError:
                    print("Enter a numeric angle, c, or q.")
                    continue
            print(f"Position: {angle:.1f} degrees")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        pan.close()
        print("Servo GPIO cleaned up.")


if __name__ == "__main__":
    main()
