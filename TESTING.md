# Voron Infinite Heatbed — Testing Checklist

This guide walks you through testing each command and the full ejection sequence.

## Prerequisites

- Klipper and Moonraker running with IHB configs installed
- `MANUAL_STEPPER STEPPER=infinite_heatbed_motor1 MOVE=10 SPEED=10` works (motor confirmed)
- Console access via Fluidd/Mainsail

---

## Phase 1: Status & Configuration Commands

### Test 1.1: HEATBED_STATUS
```
Console → HEATBED_STATUS
```
**Expected output:**
```
IHB Status
  state:         idle
  print_length:  0.0 mm
  belt_odometer: 0.0 mm
  door:          closed
  motors:        1
  sensor:        none
```
- [ ] Status command returns without error
- [ ] State is "idle"
- [ ] Print length shows 0.0 mm (no print set yet)
- [ ] Door state matches your config

---

## Phase 2: Print Start & Length Tracking

### Test 2.1: HEATBED_PRINT_START (set print length)
```
Console → HEATBED_PRINT_START PRINT_LENGTH=330
```
**Expected behavior:**
- Response: "IHB: Print started. Belt will eject 330mm."
- [ ] Command accepted without error
- [ ] Response shows correct length

### Test 2.2: Verify Print Length Was Saved
```
Console → HEATBED_STATUS
```
**Expected output:**
```
  print_length:  330.0 mm
```
- [ ] Print length now shows 330.0 mm
- [ ] Length persists after HEATBED_PRINT_START (this was fixed)

### Test 2.3: Override Print Length
```
Console → HEATBED_PRINT_START PRINT_LENGTH=150
```
**Expected behavior:**
- Response: "IHB: Print started. Belt will eject 150mm."
- [ ] New length accepted
- Verify with HEATBED_STATUS → should now show 150.0 mm

---

## Phase 3: Manual Door Control (if door_type: none)

### Test 3.1: Open Door Manually
```
Console → HEATBED_OPEN_DOOR
```
**Expected behavior:**
- Response: "IHB: Door opened."
- If using servo/solenoid/stepper: door should physically open
- [ ] Command accepted

### Test 3.2: Check Door State
```
Console → HEATBED_STATUS
```
**Expected output:**
```
  door:          open
```
- [ ] Door state reflects "open"

### Test 3.3: Close Door Manually
```
Console → HEATBED_CLOSE_DOOR
```
**Expected behavior:**
- Response: "IHB: Door closed."
- If using servo/solenoid/stepper: door should physically close
- [ ] Command accepted

### Test 3.4: Verify Door State
```
Console → HEATBED_STATUS
```
**Expected output:**
```
  door:          closed
```
- [ ] Door state reflects "closed"

---

## Phase 4: Speed Tuning

### Test 4.1: Set Belt Speed
```
Console → HEATBED_SET_SPEED SPEED=75
```
**Expected behavior:**
- Response: "IHB: Belt speed set to 75.0 mm/s"
- [ ] Command accepted

### Test 4.2: Verify Speed Was Applied
The speed is used in the next eject sequence. You can inspect the saved macro variable if needed (optional):
```
Console → GET_GCODE_VARIABLE MACRO=_IHB_PARAMS VARIABLE=belt_speed
```
- [ ] Variable shows 75.0

---

## Phase 5: Full Ejection Sequence (no actual print)

**Scenario:** Simulate a print cycle with the belt (no actual print object on heatbed).

### Test 5.1: Set Print Length
```
Console → HEATBED_PRINT_START PRINT_LENGTH=100
```
- [ ] Accepted

### Test 5.2: Check Initial State
```
Console → HEATBED_STATUS
```
- [ ] State: "idle"
- [ ] Print length: 100.0 mm

### Test 5.3: Trigger Ejection
```
Console → HEATBED_EJECT
```
**Expected sequence:**
1. State changes to "ejecting"
2. If door_type != "none": door opens (state → "door_open")
3. Belt advances 100 + 20 (margin) = 120 mm
4. If sensor configured: waits for sensor confirmation (or timeout after 60s)
5. Door closes
6. State returns to "idle"
7. Response: (silent completion or minimal logging)

- [ ] Command accepted (no immediate error)
- [ ] Check HEATBED_STATUS after ~2-3 seconds to see state progression

### Test 5.4: Monitor State Changes
Open console and watch HEATBED_STATUS every 1-2 seconds during eject:
```
Console → HEATBED_STATUS  (repeat while ejecting)
```
**Expected states (in order):**
1. "ejecting"
2. "door_open" (if door configured)
3. "idle" (after complete)

- [ ] State transitions are visible
- [ ] Final state returns to "idle"

### Test 5.5: Check Belt Odometer
After ejection completes:
```
Console → HEATBED_STATUS
```
**Expected output:**
```
  belt_odometer: 120.0 mm
```
- [ ] Odometer incremented by ~120 mm (print_length + margin)

---

## Phase 6: Error Handling

### Test 6.1: Eject Without Setting Print Length
```
Console → HEATBED_EJECT
```
**Expected behavior:**
- Response: "IHB: No print length set. Use HEATBED_PRINT_START first."
- [ ] Error message appears (command rejected gracefully)

### Test 6.2: Eject While Already Ejecting
1. First, set print length and start ejection:
```
Console → HEATBED_PRINT_START PRINT_LENGTH=100
Console → HEATBED_EJECT
```

2. Quickly (within 1-2 seconds), try ejecting again:
```
Console → HEATBED_EJECT
```
**Expected behavior:**
- Response: "IHB: Cannot eject in state 'ejecting'"
- [ ] Second command rejected

---

## Phase 7: Multiple Cycles

### Test 7.1: Repeat Eject 3 Times
1. HEATBED_PRINT_START PRINT_LENGTH=50
2. HEATBED_EJECT
3. Wait for completion
4. Repeat steps 1-3 three more times

**Expected:**
- [ ] Each cycle completes without error
- [ ] Odometer keeps incrementing: 50, 100, 150, 200 mm

---

## Phase 8: Sensor Integration (if configured)

### Test 8.1: TOF Sensor (if ejection_sensor: tof or both)

**Setup:**
- Ensure RP2040 co-processor is running and writing to `/tmp/ihb_tof_reading`
- Check co-processor is actively updating the file (it should be)

**Test:**
1. Set print length:
```
Console → HEATBED_PRINT_START PRINT_LENGTH=100
```

2. Trigger eject:
```
Console → HEATBED_EJECT
```

3. Watch console output for TOF messages:
```
TOF: no object at sensor start — waiting for object to appear
TOF: object detected (XXmm)
TOF: waiting for object to clear sensor...
TOF: object cleared (reading=Xmm)
```

- [ ] TOF sensor is being polled
- [ ] Clear message appears before eject completes

### Test 8.2: Camera Detection (if ejection_sensor: camera or both)

**Setup:**
- Ensure crowsnest is running and serving at `http://localhost/webcam/?action=snapshot`
- Test snapshot URL manually: `curl http://localhost/webcam/?action=snapshot > /tmp/test.jpg`

**Test:**
1. Eject with camera enabled:
```
Console → HEATBED_PRINT_START PRINT_LENGTH=100
Console → HEATBED_EJECT
```

2. Watch console for camera messages:
```
Camera: snapshot avg brightness=XXX.X
Camera: bed confirmed clear
```

- [ ] Camera snapshot is fetched
- [ ] Clear message appears before eject completes

### Test 8.3: Sensor Timeout (advanced)

**Scenario:** Test timeout behavior if sensor never clears.

1. Unplug/disconnect TOF sensor or stop camera service
2. Eject:
```
Console → HEATBED_PRINT_START PRINT_LENGTH=100
Console → HEATBED_EJECT
```

3. Wait 60+ seconds (default eject_timeout_s)

**Expected:**
- [ ] Eject timeout occurs
- [ ] State transitions to "error"
- [ ] Error message logged: "IHB: Eject timeout — bed may not be clear."

---

## Phase 9: Macros with Hooks (Advanced)

### Test 9.1: Custom Print Start Hook
Edit `config/optional/client_macros.cfg` and uncomment/modify:
```gcode
[gcode_macro _IHB_ON_PRINT_START]
gcode:
    # Called after HEATBED_PRINT_START. Add custom pre-print actions here.
    M117 Print started — IHB ready!
```

Run:
```
Console → HEATBED_PRINT_START PRINT_LENGTH=100
```

**Expected:**
- [ ] Console shows: "Print started — IHB ready!"

### Test 9.2: Custom Print End Hook
Edit `config/optional/client_macros.cfg`:
```gcode
[gcode_macro _IHB_ON_PRINT_END]
gcode:
    M117 Print ending — prepare for eject!
```

Run:
```
Console → HEATBED_PRINT_END
```

**Expected:**
- [ ] Console shows: "Print ending — prepare for eject!"
- [ ] Eject sequence starts automatically

---

## Phase 10: Moonraker API (if accessible)

### Test 10.1: Query Status via REST
From a terminal on the Pi or from another machine:
```bash
curl http://localhost:7125/machine/infinite_heatbed/status
```

**Expected output (JSON):**
```json
{
  "state": "idle",
  "print_length_mm": 0.0,
  "belt_odometer_mm": 120.0,
  "door_state": "closed",
  "motor_count": 1,
  "ejection_sensor": "none",
  "queue_length": 0
}
```

- [ ] API returns valid JSON
- [ ] State and metrics match console commands

### Test 10.2: Trigger Eject via REST
```bash
curl -X POST http://localhost:7125/machine/infinite_heatbed/eject
```

**Expected output:**
```json
{"result": "ok"}
```

- [ ] Command accepted
- [ ] Belt ejects

### Test 10.3: Set Print Length via REST
```bash
curl -X POST http://localhost:7125/machine/infinite_heatbed/set_print_length -d "print_length=200"
```

**Expected output:**
```json
{"result": "ok"}
```

Verify:
```bash
curl http://localhost:7125/machine/infinite_heatbed/status | jq .print_length_mm
```

- [ ] Print length set to 200.0 mm

---

## Test Summary

| Phase | Tests | Status |
|-------|-------|--------|
| 1. Status & Config | 1.1 | [ ] |
| 2. Print Start | 2.1, 2.2, 2.3 | [ ] |
| 3. Door Control | 3.1, 3.2, 3.3, 3.4 | [ ] |
| 4. Speed Tuning | 4.1, 4.2 | [ ] |
| 5. Full Ejection | 5.1–5.5 | [ ] |
| 6. Error Handling | 6.1, 6.2 | [ ] |
| 7. Multiple Cycles | 7.1 | [ ] |
| 8. Sensor Integration | 8.1, 8.2, 8.3 | [ ] |
| 9. Macro Hooks | 9.1, 9.2 | [ ] |
| 10. Moonraker API | 10.1, 10.2, 10.3 | [ ] |

---

## Notes

- **No Print Object:** These tests use the belt without an actual print object. Once you confirm motion and state transitions, add a real print for end-to-end validation.
- **Sensor Calibration:** TOF threshold (default 50 mm) and camera brightness baseline (default 120) may need tuning for your setup. See `config/base/infinite_heatbed_params.cfg`.
- **Door Mechanism:** If using servo/solenoid/stepper door, first verify the mechanism works independently via Klipper console before running ejection tests.
- **Logging:** Klipper logs appear in `/tmp/klippy.log`. Moonraker logs in `/tmp/moonraker.log`. Check these if commands fail silently.

---

## Next Steps After Testing

1. **Fix any issues found** in this checklist.
2. **Calibrate belt rotation_distance** for accurate mm tracking if needed.
3. **Place a test print on heatbed** and run the full ejection sequence with sensors.
4. **Integrate with your slicer** (OrcaSlicer/PrusaSlicer) to pass print length automatically.
5. **Configure Mainsail/Fluidd panel** to monitor heatbed status in real-time.
