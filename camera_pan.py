"""Safe pan control for a camera/sensor mount (not a launcher).

Wire a standard positional servo's signal to BCM GPIO18 (physical pin 12).
Power the servo from an appropriate external 5 V supply and connect its ground
and the Raspberry Pi ground together. Do not power a servo from a GPIO pin.

This module deliberately only provides bounded camera/sensor pan positioning.
"""
import time

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

SERVO_PIN = 18          # BCM numbering; physical pin 12
SERVO_MIN_ANGLE = 35.0  # conservative software limits; calibrate for your mount
SERVO_MAX_ANGLE = 145.0
SERVO_CENTER_ANGLE = 90.0
SERVO_MIN_DUTY = 2.5
SERVO_MAX_DUTY = 12.5


class CameraPan:
    """Bounded servo control for panning a camera or non-weapon sensor."""

    def __init__(self, pin=SERVO_PIN, enabled=True):
        self.pin = pin
        self.pwm = None
        self.angle = SERVO_CENTER_ANGLE
        self.enabled = bool(enabled and GPIO is not None)
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
        """Set an absolute angle, clamped to the configured safe travel range."""
        self.angle = self._clamp(angle, SERVO_MIN_ANGLE, SERVO_MAX_ANGLE)
        if not self.enabled or self.pwm is None:
            return self.angle
        duty = SERVO_MIN_DUTY + (self.angle / 180.0) * (SERVO_MAX_DUTY - SERVO_MIN_DUTY)
        self.pwm.ChangeDutyCycle(duty)
        time.sleep(0.12)
        self._release_signal()
        return self.angle

    def follow_horizontal(self, normalized_x, max_step=4.0, deadband=0.06):
        """Pan toward a detected face center (x normalized from 0.0 to 1.0).

        Call with the face center from the camera frame. The deadband avoids
        jitter near the center, and max_step limits movement per update.
        """
        try:
            x = float(normalized_x)
        except (TypeError, ValueError):
            return self.angle
        x = self._clamp(x, 0.0, 1.0)
        error = x - 0.5
        if abs(error) <= deadband:
            return self.angle
        # Image-left/right to servo direction may depend on mounting; reverse
        # the sign here if the camera pans away from the face.
        desired = SERVO_CENTER_ANGLE - error * 70.0
        delta = self._clamp(desired - self.angle, -max_step, max_step)
        return self.set_angle(self.angle + delta)

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
