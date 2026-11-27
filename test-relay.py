import RPi.GPIO as GPIO
import time
import sys

# Set the GPIO mode to BCM (Broadcom pin numbering)
GPIO.setmode(GPIO.BCM)

# Define the relay pin (GPIO 22)
relay_pin = 22

# Set up the relay pin as an output
GPIO.setup(relay_pin, GPIO.OUT)

# Function to toggle the relay
def toggle_relay():
    try:
        while True:
            print("Turning Relay ON...")
            GPIO.output(relay_pin, GPIO.HIGH)
            time.sleep(2)  # Keep relay ON for 2 seconds
            
            print("Turning Relay OFF...")
            GPIO.output(relay_pin, GPIO.LOW)
            time.sleep(2)   # Keep relay OFF for 2 seconds
            
    except KeyboardInterrupt:
        print("\nProgram stopped by user")
    finally:
        GPIO.cleanup()

if __name__ == "__main__":
    toggle_relay()
