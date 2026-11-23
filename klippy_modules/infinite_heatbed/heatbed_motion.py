"""
Voron Infinite Heatbed — Belt Motion Controller
Wraps Klipper manual_stepper objects for 1 or 2 motor belt drive.
Belt is continuous — no home position, position tracking is relative only.

All methods here must be called from a reactor callback, never from inside
a G-code command handler (they use gcode.run_script, which takes the G-code
lock).
"""

import logging

# Allowance (s) for move-queue latency on top of the computed move time.
TIME_PADDING_S = 1.0


class HeatbedMotion:
    def __init__(self, printer, motor_count, default_speed):
        self.printer = printer
        self.reactor = printer.get_reactor()
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

    def _motor_names(self):
        names = ['infinite_heatbed_motor1']
        if self._motor2 is not None:
            names.append('infinite_heatbed_motor2')
        return names

    def _estimate_duration(self, distance_mm, speed):
        """Move time incl. accel/decel ramps, plus padding."""
        duration = abs(distance_mm) / speed
        accel = getattr(self._motor1, 'accel', 0.) or 0.
        if accel > 0.:
            # trapezoid: each ramp costs v/(2a) extra versus constant speed
            duration += speed / accel
        return duration + TIME_PADDING_S

    def start_move(self, distance_mm, speed=None):
        """Queue the belt move on all motors and return without waiting.
        Returns the estimated reactor time at which the move is finished."""
        speed = speed or self.default_speed
        gcode = self.printer.lookup_object('gcode')

        # Let any queued moves (end of print, door actuator) finish first so
        # the belt starts "now" and the end-time estimate is accurate.
        gcode.run_script("M400")

        lines = []
        for m in self._motor_names():
            # MANUAL_STEPPER MOVE is ABSOLUTE - zero first so every eject really moves
            lines.append("MANUAL_STEPPER STEPPER=%s SET_POSITION=0" % m)
        for m in self._motor_names():
            # SYNC=0 so both motors start together instead of one after the other
            lines.append("MANUAL_STEPPER STEPPER=%s MOVE=%.3f SPEED=%.3f SYNC=0"
                         % (m, distance_mm, speed))
        gcode.run_script("\n".join(lines))

        self._accumulated_mm += distance_mm
        self.logger.debug("HeatbedMotion: moving %.1fmm (total %.1fmm)",
                          distance_mm, self._accumulated_mm)
        return self.reactor.monotonic() + self._estimate_duration(distance_mm, speed)

    def wait_for_move(self, end_time):
        """Yield to the reactor (non-blocking) until end_time has passed."""
        if end_time > self.reactor.monotonic():
            self.reactor.pause(end_time)

    def move(self, distance_mm, speed=None):
        """Advance belt by distance_mm at speed mm/s and wait until done."""
        self.wait_for_move(self.start_move(distance_mm, speed))

    def move_from_command(self, distance_mm, speed=None):
        """Move the belt from INSIDE a G-code command handler (jog, seam
        advance). The toolhead timeline waits for the move, so following
        G-code runs after the belt has stopped."""
        speed = speed or self.default_speed
        gcode = self.printer.lookup_object('gcode')
        names = self._motor_names()
        lines = ["MANUAL_STEPPER STEPPER=%s SET_POSITION=0" % m for m in names]
        for i, m in enumerate(names):
            # all but the last motor use SYNC=0 so they start together; the
            # last one syncs the toolhead to the end of the move
            sync = " SYNC=0" if i < len(names) - 1 else ""
            lines.append("MANUAL_STEPPER STEPPER=%s MOVE=%.3f SPEED=%.3f%s"
                         % (m, distance_mm, speed, sync))
        gcode.run_script_from_command("\n".join(lines))
        self._accumulated_mm += distance_mm

    def get_accumulated_distance(self):
        """Total mm the belt has traveled since startup."""
        return self._accumulated_mm
