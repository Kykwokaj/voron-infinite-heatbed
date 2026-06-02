# voron-infinite-heatbed

Installation Steps
1. Copy the mod to your Raspberry Pi
On your Pi, clone or download the mod:

```ini
cd ~
git clone https://github.com/YOUR_USERNAME/voron-infinite-heatbed.git
# OR if you're testing locally:
# cd ~/voron-infinite-heatbed  (if already there)
```

2. Run the installer
```ini
cd ~/voron-infinite-heatbed
bash install.sh
```

This will:
Symlink the Klipper extra into ~/klipper/klippy/extras/infinite_heatbed
Symlink the Moonraker component into ~/moonraker/moonraker/components/infinite_heatbed_server.py
Ask you to add [include] lines (see step 3)
Restart Klipper and Moonraker
3. Edit printer.cfg
Add these lines at the end of your printer.cfg:

```ini
# Voron Infinite Heatbed
[include ~/voron-infinite-heatbed/config/base/infinite_heatbed.cfg]
[include ~/voron-infinite-heatbed/config/base/infinite_heatbed_params.cfg]
[include ~/voron-infinite-heatbed/config/base/infinite_heatbed_macros.cfg]
[include ~/voron-infinite-heatbed/config/optional/client_macros.cfg]
```

4. Edit moonraker.conf
Add this line:
```ini
[include ~/voron-infinite-heatbed/mainsail/infinite_heatbed_panel.cfg]
```

5. Configure your hardware pins
Edit ~/voron-infinite-heatbed/config/base/infinite_heatbed.cfg and replace the pin numbers with your actual MCU pins:
```ini
[infinite_heatbed]
motor_count: 1              # or 2 if using dual motors
ejection_sensor: tof        # tof | camera | both | none
door_type: none             # servo | solenoid | stepper | none

[manual_stepper infinite_heatbed_motor1]
step_pin: PE2               # ← CHANGE TO YOUR PIN
dir_pin: PE3                # ← CHANGE TO YOUR PIN
enable_pin: !PE4            # ← CHANGE TO YOUR PIN
rotation_distance: 40       # tune to your belt
microsteps: 16
```

Find your pin numbers:

Look in your MCU's board definition (BTT Octopus, SKR Pro, etc.)
Use QUERY_ENDSTOPS in Klipper console to verify pin names
6. If using TOF sensor
Uncomment the TOF section in infinite_heatbed.cfg:
```ini
#[temperature_sensor infinite_heatbed_tof]
#sensor_type: temperature_host
#sensor_path: /tmp/ihb_tof_reading
```
You'll also need a co-processor (RP2040 Pico) running CircuitPython to read the VL53L0X sensor and write to /tmp/ihb_tof_reading. See README "Pattern 4" for details.

7. If using door opener
Uncomment your door type in infinite_heatbed.cfg:
```ini
[servo infinite_heatbed_door_servo]
pin: PB6                    # ← YOUR PIN
maximum_servo_angle: 180
minimum_pulse_width: 0.001
maximum_pulse_width: 0.002
initial_angle: 0
```
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


# Voron Infinite Heatbed

A Klipper/Moonraker mod for the **Voron 2.4** that adds a conveyor-belt style infinite heatbed, automated print ejection, and queue-based continuous printing.

Inspired by the architecture of [Happy Hare](https://github.com/moggieuk/Happy-Hare) (the gold standard for Voron Klipper mods).

---

## Features

- **Infinite belt heatbed** — continuous conveyor belt replaces the static PEI sheet; prints eject off the front automatically
- **1 or 2 motor drive** — configured at install time; 2-motor mode runs both steppers synchronized for better belt tracking
- **Automated print ejection** — after print completes: door opens, belt advances, sensors confirm bed is clear, door closes, next queued print starts
- **TOF sensor support** (optional) — VL53L0X time-of-flight sensor detects print leaving the belt edge
- **Camera detection** (optional) — USB/CSI camera via crowsnest confirms empty bed visually
- **Door open module** (placeholder) — servo, solenoid, or stepper actuator; wired into the ejection sequence
- **Mainsail / Fluidd status panel** — live state, belt speed, sensor reading, queue depth shown in the web UI
- **Moonraker REST API** — all state queryable and controllable over HTTP
- **Print queue integration** — after ejection, automatically starts the next job from Moonraker's job queue

---

## Hardware Requirements

### Belt Heatbed Assembly
- Conveyor-style belt replacing the standard Voron 2.4 bed
  - Belt material: silicone-coated fiberglass or PEI-coated steel belt (must tolerate heatbed temps)
  - Belt drive roller at the front (door side), idler roller at the rear
  - Heatbed heater bonded to a fixed aluminium plate; belt rides over it
- **No home sensor needed** — the belt is continuous; position is tracked by accumulated steps only

### Motors
| Config | Hardware |
|--------|----------|
| 1 motor | Single NEMA17 stepper at the drive roller |
| 2 motors | Two NEMA17 steppers (one each side of roller) — synchronized in Klipper |

### Ejection Sensors (choose one or both)
| Sensor | Notes |
|--------|-------|
| VL53L0X TOF | I2C, mounts at door opening; detects print passing, then clearing |
| USB/CSI camera | Works with crowsnest; image snapshot checks for objects on belt |

### Door Mechanism (TBD — placeholder in software)
| Option | Notes |
|--------|-------|
| Servo | Small servo motor unlatches/pushes front panel |
| Solenoid | Energized solenoid releases magnetic latch; door swings open on spring |
| Stepper/linear | Mini linear actuator slides door panel |

### Wiring Summary
- Belt stepper(s): connect to spare stepper driver slots on MCU (e.g. BTT Octopus E3/E4)
- VL53L0X TOF: I2C bus on MCU or expansion board
- Door actuator: spare servo/GPIO pin
- Camera: USB or CSI to Raspberry Pi running crowsnest

---

## Software Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Klipper (klippy)                      │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  infinite_heatbed.py  (Klipper Extra)               │ │
│  │   ├── State machine: idle/printing/ejecting/error   │ │
│  │   ├── heatbed_motion.py  (belt stepper control)     │ │
│  │   ├── heatbed_sensors.py (TOF + camera)             │ │
│  │   └── door_controller.py (door actuator)            │ │
│  └─────────────────────────────────────────────────────┘ │
└────────────────────────┬────────────────────────────────┘
                         │ klippy API
┌────────────────────────▼────────────────────────────────┐
│                   Moonraker                              │
│   infinite_heatbed_server.py  (Moonraker component)     │
│   ├── REST: GET/POST /machine/infinite_heatbed/...      │
│   ├── Websocket push on state change                    │
│   └── update_manager registration (OTA updates)         │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP / Websocket
┌────────────────────────▼────────────────────────────────┐
│           Mainsail / Fluidd (web UI)                    │
│   Status panel: state badge, sensor reading, queue len  │
└─────────────────────────────────────────────────────────┘
```

### File Layout

```
voron-infinite-heatbed/
├── README.md
├── install.sh                            ← setup script (symlinks into Klipper/Moonraker)
├── klippy_modules/
│   └── infinite_heatbed/
│       ├── infinite_heatbed.py           ← main Klipper extra
│       ├── heatbed_motion.py             ← belt stepper abstraction
│       ├── heatbed_sensors.py            ← TOF + camera sensor manager
│       └── door_controller.py            ← door open/close (pluggable)
├── moonraker/
│   └── infinite_heatbed_server.py        ← Moonraker component + REST API
├── config/
│   ├── base/
│   │   ├── infinite_heatbed.cfg          ← hardware pin definitions (READ-ONLY)
│   │   ├── infinite_heatbed_params.cfg   ← tuning params (user editable)
│   │   └── infinite_heatbed_macros.cfg   ← core GCode macros (READ-ONLY)
│   └── optional/
│       └── client_macros.cfg             ← user-customizable hook macros
└── mainsail/
    └── infinite_heatbed_panel.cfg        ← Mainsail panel moonraker config
```

---

## Installation

### Prerequisites
- Voron 2.4 with Klipper + Moonraker + Mainsail or Fluidd
- Python 3.7+ on the Raspberry Pi
- Hardware wired and connected (see Hardware Requirements)

### Steps

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/voron-infinite-heatbed.git
cd voron-infinite-heatbed
bash install.sh
```

The installer will:
1. Symlink `klippy_modules/infinite_heatbed/` into Klipper's extras directory
2. Symlink `moonraker/infinite_heatbed_server.py` into Moonraker's components directory
3. Ask you to add **one `[include]` line to `printer.cfg`** and **one line to `moonraker.conf`** (see below)
4. Register itself with Moonraker's `update_manager` for automatic OTA updates

### printer.cfg additions

Add **only one line** to your `printer.cfg`:

```ini
[include ~/voron-infinite-heatbed/config/infinite_heatbed.cfg]
```

That single include pulls in all config files (hardware, params, macros, user customizations).

### moonraker.conf additions

Add **only one line** to your `moonraker.conf`:

```ini
[include ~/voron-infinite-heatbed/moonraker/infinite_heatbed.conf]
```

That single include pulls in the Mainsail panel config and update manager settings.

---

## Configuration Reference

Edit `config/base/infinite_heatbed.cfg` for hardware pins, and `config/base/infinite_heatbed_params.cfg` for tuning.

### `[infinite_heatbed]` section

| Parameter | Values | Description |
|-----------|--------|-------------|
| `motor_count` | `1` or `2` | Number of stepper motors driving the belt |
| `ejection_sensor` | `tof`, `camera`, `both`, `none` | Sensor used to confirm print has left the belt |
| `door_type` | `servo`, `solenoid`, `stepper`, `none` | Door actuator type |
| `belt_speed` | mm/s (default `50`) | Belt speed during ejection |
| `eject_margin_mm` | mm (default `20`) | Extra belt travel after sensor clears |
| `eject_timeout_s` | seconds (default `60`) | Abort eject if belt runs longer than this |
| `tof_threshold_mm` | mm (default `50`) | TOF distance considered "object present" |

### Motor config

```ini
[stepper infinite_heatbed_motor1]
step_pin: PE2
dir_pin: PE3
enable_pin: !PE4
rotation_distance: 40       # tune to your belt pitch
microsteps: 16
full_steps_per_rotation: 200

# Add only if motor_count: 2
[stepper infinite_heatbed_motor2]
step_pin: PE6
dir_pin: PA14
enable_pin: !PE0
rotation_distance: 40
microsteps: 16
full_steps_per_rotation: 200
```

### TOF sensor config (if ejection_sensor includes `tof`)

```ini
[vl53l0x infinite_heatbed_tof]
i2c_mcu: mcu
i2c_bus: i2c1
```

---

## Slicer Configuration

The slicer must pass the print's Y-extent (the dimension along the belt direction) so the mod knows how far to advance the belt during ejection.

### OrcaSlicer / SuperSlicer / PrusaSlicer

Add to **Start G-code**:
```
HEATBED_PRINT_START PRINT_LENGTH={max_print_height}
```

Add to **End G-code**:
```
HEATBED_PRINT_END
```

> `max_print_height` is a slicer placeholder for the model's maximum Z height in mm.  
> If your belt runs in the Y direction, use the Y bounding box extent instead.  
> The value tells the mod the minimum distance the belt must advance to clear the print.

---

## GCode Command Reference

| Command | Description |
|---------|-------------|
| `HEATBED_PRINT_START PRINT_LENGTH=<mm>` | Called from slicer start gcode; records print length for ejection |
| `HEATBED_PRINT_END` | Called from slicer end gcode; triggers full eject sequence |
| `HEATBED_EJECT` | Manually trigger belt eject (uses last stored print length) |
| `HEATBED_STATUS` | Print current state to console |
| `HEATBED_SET_SPEED SPEED=<mm/s>` | Override belt speed for testing |
| `HEATBED_OPEN_DOOR` | Manually open door |
| `HEATBED_CLOSE_DOOR` | Manually close door |

### Ejection Sequence (automatic, triggered by `HEATBED_PRINT_END`)

```
1. HEATBED_OPEN_DOOR
2. Belt advances (print_length + eject_margin_mm) mm at belt_speed
   → TOF: wait for object detection, then wait for object to clear
   → Camera: capture snapshot, confirm no objects detected
3. If sensor timeout: raise error, stop belt, alert user
4. HEATBED_CLOSE_DOOR
5. Moonraker job queue: start next queued print (if any)
```

---

## Mainsail / Fluidd Panel

The Moonraker component exposes status at:
```
GET /machine/infinite_heatbed/status
```

Response:
```json
{
  "state": "idle",
  "belt_distance_mm": 12450.0,
  "door_state": "closed",
  "sensor_reading_mm": 230.0,
  "camera_clear": true,
  "print_length_mm": 150.0,
  "queue_length": 2
}
```

Mainsail shows this via a custom panel added to `moonraker.conf`. The panel displays:
- **State badge**: `idle` / `printing` / `ejecting` / `door_open` / `error`
- **Sensor reading**: live TOF distance or camera status
- **Queue depth**: number of jobs waiting
- **Belt odometer**: total mm driven (useful for maintenance intervals)

---

## Development Notes (AI Prompt Guide)

This section documents key patterns so an AI coding assistant can implement or extend the mod correctly.

### Pattern 1: Klipper Extra structure

A Klipper extra is a Python module placed in `klipper/klippy/extras/`. It must define:
```python
def load_config(config):
    return MyModule(config)

class MyModule:
    def __init__(self, config):
        self.printer = config.get_printer()
        # register event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        # register GCode commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('MY_CMD', self.cmd_MY_CMD)
    
    def _handle_ready(self):
        # called when Klipper finishes startup
        pass
    
    def cmd_MY_CMD(self, gcmd):
        # GCode command handler
        param = gcmd.get_float('PARAM', default=0.0)
```

### Pattern 2: Klipper stepper control

Steppers in Klipper extras use `toolhead` and `manual_stepper` objects:
```python
# For a manual stepper (not part of the toolhead kinematic):
stepper = config.get_printer().lookup_object('manual_stepper belt_motor')
# Move: use MANUAL_STEPPER MOVE= commands via gcode, or
# use stepper.do_move(movepos, speed, accel) in reactor callbacks
```

For the belt, use `[manual_stepper]` config sections — these are designed for accessories (not XYZ axes) and don't require homing.

### Pattern 3: Moonraker component structure

A Moonraker component lives in `moonraker/components/`. It must define:
```python
def load_component(config):
    return MyComponent(config)

class MyComponent:
    def __init__(self, config):
        self.server = config.get_server()
        self.server.register_endpoint(
            "/machine/my_module/status", ['GET'], self._handle_status
        )
    
    async def _handle_status(self, web_request):
        return {"state": "idle"}
```

### Pattern 4: TOF sensor (VL53L0X) in Klipper

Klipper does not have a built-in VL53L0X driver. Options:
- **Option A**: Use a secondary MCU (e.g. RP2040 with CircuitPython) that reads the TOF and exposes it as a temperature sensor (Klipper can poll it)
- **Option B**: Read directly from I2C in the Klipper extra using `printer.lookup_object('mcu').i2c_read(...)` — requires low-level I2C work
- **Option C**: Use an Arduino/ESP32 as a co-processor; send readings via serial/USB to a Klipper `[temperature_host]` or custom sensor
- **Recommended**: Option A — RP2040 Pico as TOF reader, exposes value as a fake temperature, Klipper extra polls it

### Pattern 5: Camera detection

Camera detection uses crowsnest (the Klipper camera streaming daemon):
- Crowsnest exposes a MJPEG/snapshot URL (default: `http://localhost/webcam/?action=snapshot`)
- The Moonraker component can fetch a JPEG snapshot and run OpenCV or a simple pixel brightness check
- For simple "is bed empty" detection: compare average brightness of belt area ROI; a print will be significantly darker/lighter than the bare belt
- For robust detection: use a small ML model (e.g. MobileNet via tflite) trained on "belt empty" vs "print present"

### Pattern 6: Manual stepper config (no homing)

```ini
[manual_stepper infinite_heatbed_motor1]
step_pin: PE2
dir_pin: PE3
enable_pin: !PE4
rotation_distance: 40
microsteps: 16
velocity: 50
accel: 200
# No endstop_pin — belt has no home position
```

The `MANUAL_STEPPER` GCode command then drives it:
```gcode
MANUAL_STEPPER STEPPER=infinite_heatbed_motor1 MOVE=150 SPEED=50
```

From Python:
```python
self.printer.lookup_object('gcode').run_script_from_command(
    "MANUAL_STEPPER STEPPER=infinite_heatbed_motor1 MOVE=%f SPEED=%f" % (dist, speed)
)
```

### Pattern 7: State machine

Track state as a string. Raise errors on invalid transitions. Broadcast state via Moonraker websocket on every change:
```python
STATES = ['idle', 'heating', 'printing', 'ejecting', 'door_open', 'error']

def _set_state(self, new_state):
    assert new_state in STATES
    self._state = new_state
    # notify Moonraker component to push websocket update
    self.printer.send_event("infinite_heatbed:state_changed", new_state)
```

### Pattern 8: Slicer variable passing

Slicer passes `PRINT_LENGTH` as a GCode parameter. Store it as a `[save_variables]` variable so it persists across restarts:
```python
save_vars = self.printer.lookup_object('save_variables')
save_vars.allVariables['ihb_print_length'] = print_length
save_vars.save()
```

---

## Roadmap

- [ ] Hardware design files (CAD) for belt roller mount on Voron 2.4
- [ ] Belt tension adjustment mechanism
- [ ] Door mechanism: servo implementation (first to be implemented)
- [ ] KlipperScreen panel (touchscreen control)
- [ ] Fluidd custom panel
- [ ] ML-based camera detection (MobileNet)
- [ ] Multi-material support (coordinate with ERCF/Happy Hare)
- [ ] Belt wear monitoring via odometer + maintenance alerts

---

## Known Limitations / Open Questions

- **Belt material**: PEI-coated belts rated for 110°C; ABS prints require 110°C bed — check belt rating before use
- **Two-motor sync**: Klipper `manual_stepper` does not natively support synchronized dual-stepper; may require a custom kinematic or run two `MANUAL_STEPPER` commands back-to-back (small timing gap)
- **Door mechanism**: Not yet designed — software has placeholder; hardware TBD
- **TOF in Klipper**: No native VL53L0X driver; RP2040 co-processor approach recommended (Pattern 4)
- **Camera latency**: Snapshot-based detection adds ~1-2s to eject sequence; acceptable for most queues

---

## Contributing

This is an early-stage mod. PRs welcome for:
- Hardware CAD files
- Door mechanism implementations
- Camera detection improvements
- KlipperScreen panel

---

## License

GPL-3.0 — same as Klipper and Happy Hare.
