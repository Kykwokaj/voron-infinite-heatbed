"""
Voron Infinite Heatbed — Door Controller
Pluggable door actuator. Configure door_type in [infinite_heatbed].
Currently a placeholder — implement the sub-class matching your hardware.

open()/close() use gcode.run_script, so they must be called from a reactor
callback (the eject sequence), not from inside a G-code command handler.
"""

import logging

class DoorController:
    def __init__(self, printer, door_type):
        self.printer = printer
        self.door_type = door_type
        self.logger = logging.getLogger('infinite_heatbed.door')
        self.state = 'closed'

        if door_type == 'servo':
            self._impl = _ServoController(printer)
        elif door_type == 'solenoid':
            self._impl = _SolenoidController(printer)
        elif door_type == 'stepper':
            self._impl = _StepperController(printer)
        elif door_type == 'none':
            self._impl = _NullController()
        else:
            raise ValueError("Unknown door_type: %s" % door_type)

        self.logger.info("DoorController: type=%s", door_type)

    def open(self):
        self.logger.info("Door: opening")
        self._impl.open()
        self.state = 'open'

    def close(self):
        self.logger.info("Door: closing")
        self._impl.close()
        self.state = 'closed'


class _NullController:
    """No door hardware — skip silently."""
    def open(self):  pass
    def close(self): pass


class _ServoController:
    """
    Door driven by a servo motor.
    Expects [servo infinite_heatbed_door_servo] in config.
    Adjust OPEN_ANGLE and CLOSE_ANGLE for your linkage.
    """
    OPEN_ANGLE  = 90
    CLOSE_ANGLE = 0

    def __init__(self, printer):
        self._gcode = printer.lookup_object('gcode')

    def open(self):
        self._gcode.run_script(
            "SET_SERVO SERVO=infinite_heatbed_door_servo ANGLE=%d" % self.OPEN_ANGLE)

    def close(self):
        self._gcode.run_script(
            "SET_SERVO SERVO=infinite_heatbed_door_servo ANGLE=%d" % self.CLOSE_ANGLE)


class _SolenoidController:
    """
    Door released by a solenoid.
    Expects [output_pin infinite_heatbed_door_solenoid] in config.
    Energize to open, de-energize to allow latch to re-engage.
    """
    def __init__(self, printer):
        self._gcode = printer.lookup_object('gcode')

    def open(self):
        self._gcode.run_script(
            "SET_PIN PIN=infinite_heatbed_door_solenoid VALUE=1")

    def close(self):
        self._gcode.run_script(
            "SET_PIN PIN=infinite_heatbed_door_solenoid VALUE=0")


class _StepperController:
    """
    Door driven by a small stepper or linear actuator.
    Expects [manual_stepper infinite_heatbed_door_stepper] in config.
    MANUAL_STEPPER MOVE is an ABSOLUTE position. The door is assumed closed
    at Klipper startup (position 0), so open moves to OPEN_POS_MM and close
    moves back to CLOSED_POS_MM.
    Adjust OPEN_POS_MM and SPEED for your actuator.
    """
    OPEN_POS_MM   = 50
    CLOSED_POS_MM = 0
    SPEED         = 10

    def __init__(self, printer):
        self._gcode = printer.lookup_object('gcode')

    def open(self):
        self._gcode.run_script(
            "MANUAL_STEPPER STEPPER=infinite_heatbed_door_stepper "
            "MOVE=%.1f SPEED=%.1f" % (self.OPEN_POS_MM, self.SPEED))

    def close(self):
        self._gcode.run_script(
            "MANUAL_STEPPER STEPPER=infinite_heatbed_door_stepper "
            "MOVE=%.1f SPEED=%.1f" % (self.CLOSED_POS_MM, self.SPEED))
