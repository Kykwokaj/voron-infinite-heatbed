"""
Voron Infinite Heatbed — Belt Motion Controller
Wraps Klipper manual_stepper objects for 1 or 2 motor belt drive.
Belt is continuous — no home position, position tracking is relative only.
"""

import logging

class HeatbedMotion:
    def __init__(self, printer, motor_count, default_speed):
        self.printer = printer
        self.motor_count = motor_count
        self.default_speed = default_speed
        self.logger = logging.getLogger('infinite_heatbed.motion')

        # Look up manual_stepper objects registered by config
        self._motor1 = printer.lookup_object('manual_stepper infinite_heatbed_motor1')
        self._motor2 = None
        if motor_count == 2:
            self._motor2 = printer.lookup_object('manual_stepper infinite_heatbed_motor2')
            self.logger.info("HeatbedMotion: dual-motor mode")
        else:
            self.logger.info("HeatbedMotion: single-motor mode")

        self._accumulated_mm = 0.

    def move(self, distance_mm, speed=None):
        """Advance belt by distance_mm at speed mm/s. Blocking call."""
        speed = speed or self.default_speed
        gcode = self.printer.lookup_object('gcode')

        cmd1 = "MANUAL_STEPPER STEPPER=infinite_heatbed_motor1 MOVE=%.3f SPEED=%.3f" % (
            distance_mm, speed)
        gcode.run_script_from_command(cmd1)

        if self._motor2 is not None:
            cmd2 = "MANUAL_STEPPER STEPPER=infinite_heatbed_motor2 MOVE=%.3f SPEED=%.3f" % (
                distance_mm, speed)
            gcode.run_script_from_command(cmd2)

        self._accumulated_mm += distance_mm
        self.logger.debug("HeatbedMotion: moved %.1fmm (total %.1fmm)",
                          distance_mm, self._accumulated_mm)

    def get_accumulated_distance(self):
        """Total mm the belt has traveled since startup."""
        return self._accumulated_mm
