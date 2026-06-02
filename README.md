# voron-infinite-heatbed

Installation Steps
1. Copy the mod to your Raspberry Pi
On your Pi, clone or download the mod:

cd ~
git clone https://github.com/YOUR_USERNAME/voron-infinite-heatbed.git
# OR if you're testing locally:
# cd ~/voron-infinite-heatbed  (if already there)

2. Run the installer
cd ~/voron-infinite-heatbed
bash install.sh

This will:
Symlink the Klipper extra into ~/klipper/klippy/extras/infinite_heatbed
Symlink the Moonraker component into ~/moonraker/moonraker/components/infinite_heatbed_server.py
Ask you to add [include] lines (see step 3)
Restart Klipper and Moonraker
3. Edit printer.cfg
Add these lines at the end of your printer.cfg:

# Voron Infinite Heatbed
[include ~/voron-infinite-heatbed/config/base/infinite_heatbed.cfg]
[include ~/voron-infinite-heatbed/config/base/infinite_heatbed_params.cfg]
[include ~/voron-infinite-heatbed/config/base/infinite_heatbed_macros.cfg]
[include ~/voron-infinite-heatbed/config/optional/client_macros.cfg]

4. Edit moonraker.conf
Add this line:

5. Configure your hardware pins
Edit ~/voron-infinite-heatbed/config/base/infinite_heatbed.cfg and replace the pin numbers with your actual MCU pins:

Find your pin numbers:

Look in your MCU's board definition (BTT Octopus, SKR Pro, etc.)
Use QUERY_ENDSTOPS in Klipper console to verify pin names
6. If using TOF sensor
Uncomment the TOF section in infinite_heatbed.cfg:

You'll also need a co-processor (RP2040 Pico) running CircuitPython to read the VL53L0X sensor and write to /tmp/ihb_tof_reading. See README "Pattern 4" for details.

7. If using door opener
Uncomment your door type in infinite_heatbed.cfg:

Servo example:

Then uncomment the servo macros in config/optional/client_macros.cfg:

8. Wire the hardware
Connect:

Belt stepper(s) → stepper driver on MCU
TOF sensor (optional) → I2C on MCU or co-processor
Door actuator (optional) → servo/solenoid/GPIO on MCU
Camera (optional) → USB to Pi (crowsnest will handle it)
9. Restart and test
10. Test in console
Once Klipper is running, test via the Mainsail console:

Slicer Configuration
Add to your start G-code in OrcaSlicer/SuperSlicer:

Add to end G-code:

