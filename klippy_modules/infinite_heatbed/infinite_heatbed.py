"""
Voron Infinite Heatbed — Main Klipper Extra
Registers [infinite_heatbed] config section, state machine, and GCode commands.

Parameter precedence: values in [infinite_heatbed] are startup defaults; the
[gcode_macro _IHB_PARAMS] variables (and HEATBED_SET_SPEED) override them and
are re-read before every eject.
"""

import logging
import os

STATES = ['idle', 'printing', 'ejecting', 'door_open', 'error']
BUSY_STATES = ('ejecting', 'door_open')

# _IHB_PARAMS variable -> (attribute, converter)
_PARAM_MAP = {
    'belt_speed':           ('belt_speed', float),
    'eject_margin_mm':      ('eject_margin_mm', float),
    'eject_timeout_s':      ('eject_timeout_s', float),
    'tof_threshold_mm':     ('tof_threshold_mm', float),
    'camera_snapshot_url':  ('camera_snapshot_url', str),
    'door_open_delay_ms':   ('door_open_delay_ms', float),
    'door_close_delay_ms':  ('door_close_delay_ms', float),
}


class InfiniteHeatbed:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.logger = logging.getLogger('infinite_heatbed')

        # Config (startup defaults — _IHB_PARAMS overrides at runtime)
        self.motor_count = config.getint('motor_count', 1, minval=1, maxval=2)
        self.ejection_sensor = config.getchoice(
            'ejection_sensor', {k: k for k in ('none', 'tof', 'camera', 'both')}, 'none')
        self.door_type = config.getchoice(
            'door_type', {k: k for k in ('none', 'servo', 'solenoid', 'stepper')}, 'none')
        self.belt_speed = config.getfloat('belt_speed', 50., above=0.)
        self.eject_margin_mm = config.getfloat('eject_margin_mm', 20., minval=0.)
        self.eject_timeout_s = config.getfloat('eject_timeout_s', 60., above=0.)
        # Must be read here: Klipper rejects options that a module never reads
        self.tof_threshold_mm = config.getfloat('tof_threshold_mm', 50., above=0.)
        self.camera_snapshot_url = config.get(
            'camera_snapshot_url', 'http://localhost/webcam/?action=snapshot')
        self.door_open_delay_ms = config.getfloat('door_open_delay_ms', 500., minval=0.)
        self.door_close_delay_ms = config.getfloat('door_close_delay_ms', 500., minval=0.)

        # State
        self._state = 'idle'
        self._print_length_mm = 0.
        self._belt_odometer_mm = 0.

        # Sub-modules (loaded after printer ready)
        self._motion = None
        self._sensors = None
        self._door = None

        # Register events and GCode commands
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('HEATBED_STATUS', self.cmd_HEATBED_STATUS,
                               desc="Print infinite heatbed status.")
        gcode.register_command('HEATBED_EJECT', self.cmd_HEATBED_EJECT,
                               desc="Manually trigger belt ejection.")
        gcode.register_command('HEATBED_RESET', self.cmd_HEATBED_RESET,
                               desc="Clear IHB error state back to idle.")

    def _handle_ready(self):
        from .heatbed_motion import HeatbedMotion
        from .heatbed_sensors import HeatbedSensors
        from .door_controller import DoorController

        self._load_params()
        self._motion = HeatbedMotion(self.printer, self.motor_count, self.belt_speed)
        self._sensors = HeatbedSensors(self.printer, self.ejection_sensor,
                                       self._sensor_params())
        self._door = DoorController(self.printer, self.door_type)

        # Restore saved print length if any
        saved = self._saved_print_length()
        if saved > 0:
            self._print_length_mm = saved

        self.logger.info("InfiniteHeatbed ready. motor_count=%d sensor=%s door=%s",
                         self.motor_count, self.ejection_sensor, self.door_type)
        self._set_state('idle')

    # ── Parameters / shared state with the macros ──────────────────────────────

    def _macro_vars(self, name):
        macro = self.printer.lookup_object('gcode_macro %s' % name, None)
        return macro.variables if macro is not None else None

    def _load_params(self):
        """Pull tuning values from [gcode_macro _IHB_PARAMS]
        (HEATBED_SET_SPEED writes there)."""
        pvars = self._macro_vars('_IHB_PARAMS')
        if not pvars:
            return
        for var, (attr, conv) in _PARAM_MAP.items():
            if var in pvars and pvars[var] is not None:
                try:
                    setattr(self, attr, conv(pvars[var]))
                except (TypeError, ValueError):
                    self.logger.warning("IHB: bad _IHB_PARAMS value %s=%r",
                                        var, pvars[var])
        if self.belt_speed <= 0.:
            self.logger.warning("IHB: belt_speed <= 0, using 50")
            self.belt_speed = 50.

    def _sensor_params(self):
        return {'tof_threshold_mm': self.tof_threshold_mm,
                'camera_snapshot_url': self.camera_snapshot_url}

    def _saved_print_length(self):
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is None:
            return 0.
        try:
            return float(save_vars.allVariables.get('ihb_print_length', 0.) or 0.)
        except (TypeError, ValueError):
            return 0.

    def _resolve_print_length(self):
        """Live value from HEATBED_PRINT_START (_IHB_STATE) first, then the
        value saved across restarts, then whatever is in memory."""
        svars = self._macro_vars('_IHB_STATE')
        if svars:
            try:
                live = float(svars.get('print_length', 0.) or 0.)
            except (TypeError, ValueError):
                live = 0.
            if live > 0:
                return live
        saved = self._saved_print_length()
        if saved > 0:
            return saved
        return self._print_length_mm

    # ── State machine ──────────────────────────────────────────────────────────

    def _set_state(self, new_state):
        assert new_state in STATES, "Unknown state: %s" % new_state
        old = self._state
        self._state = new_state
        self.logger.info("IHB state: %s -> %s", old, new_state)
        # Mirror into the macro so printer['gcode_macro _IHB_STATE'].state is right
        svars = self._macro_vars('_IHB_STATE')
        if svars is not None:
            svars['state'] = new_state
        self.printer.send_event('infinite_heatbed:state_changed', new_state)

    def get_status(self, _eventtime=None):
        return {
            'state': self._state,
            'print_length_mm': self._print_length_mm,
            'belt_odometer_mm': self._belt_odometer_mm,
            'door_state': self._door.state if self._door else 'unknown',
            'motor_count': self.motor_count,
            'ejection_sensor': self.ejection_sensor,
        }

    # ── GCode commands ─────────────────────────────────────────────────────────

    def cmd_HEATBED_STATUS(self, gcmd):
        s = self.get_status()
        gcmd.respond_info(
            "IHB Status\n"
            "  state:         %s\n"
            "  print_length:  %.1f mm\n"
            "  belt_odometer: %.1f mm\n"
            "  door:          %s\n"
            "  motors:        %d\n"
            "  sensor:        %s" % (
                s['state'], s['print_length_mm'], s['belt_odometer_mm'],
                s['door_state'], s['motor_count'], s['ejection_sensor']
            )
        )

    def cmd_HEATBED_EJECT(self, gcmd):
        if self._motion is None:
            gcmd.respond_info("IHB: Not ready yet.")
            return
        if self._state not in ('idle', 'printing'):
            gcmd.respond_info("IHB: Cannot eject in state '%s'" % self._state)
            return
        self._print_length_mm = self._resolve_print_length()
        if self._print_length_mm <= 0:
            gcmd.respond_info("IHB: No print length set. Use HEATBED_PRINT_START first.")
            return
        self._load_params()
        # Claim the state now, so a second HEATBED_EJECT sent before the
        # callback runs is rejected instead of starting a second eject.
        self._set_state('ejecting')
        reactor = self.printer.get_reactor()
        reactor.register_callback(lambda _: self._run_eject_sequence())

    def cmd_HEATBED_RESET(self, gcmd):
        force = gcmd.get_int('FORCE', 0, minval=0, maxval=1)
        if self._state in BUSY_STATES and not force:
            gcmd.respond_info(
                "IHB: Eject in progress (state '%s'). Use HEATBED_RESET FORCE=1"
                " only if it is stuck." % self._state)
            return
        self._set_state('idle')
        gcmd.respond_info("IHB: state reset to idle")

    # ── Ejection sequence ──────────────────────────────────────────────────────

    def set_print_length(self, length_mm):
        """Set the print length and persist it (same effect as
        HEATBED_PRINT_START, usable from Python)."""
        length_mm = float(length_mm)
        self._print_length_mm = length_mm
        svars = self._macro_vars('_IHB_STATE')
        if svars is not None:
            svars['print_length'] = length_mm
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is None:
            return
        save_vars.allVariables['ihb_print_length'] = length_mm
        # Klipper's save_variables has no public save() method. Write the file
        # in the same format its SAVE_VARIABLE command uses.
        try:
            import configparser
            varfile = configparser.ConfigParser()
            varfile.add_section('Variables')
            for name, val in sorted(save_vars.allVariables.items()):
                varfile.set('Variables', name, repr(val))
            tmp = save_vars.filename + ".tmp"
            with open(tmp, "w") as f:
                varfile.write(f)
            os.rename(tmp, save_vars.filename)
        except Exception:
            self.logger.exception("IHB: Unable to save ihb_print_length")

    def _wait_ms(self, ms):
        """Non-blocking delay (yields to the reactor)."""
        if ms > 0:
            reactor = self.printer.get_reactor()
            reactor.pause(reactor.monotonic() + ms / 1000.)

    def _run_eject_sequence(self):
        """Full print ejection sequence. Runs in reactor callback."""
        gcode = self.printer.lookup_object('gcode')
        reactor = self.printer.get_reactor()
        try:
            self._set_state('ejecting')
            self._sensors.update_params(self._sensor_params())
            eject_dist = self._print_length_mm + self.eject_margin_mm
            gcode.respond_info("IHB: ejecting %.0f mm at %.0f mm/s"
                               % (eject_dist, self.belt_speed))

            # 1. Open door and let it finish moving before the belt starts
            if self.door_type != 'none':
                self._set_state('door_open')
                self._door.open()
                gcode.run_script("M400")
                self._wait_ms(self.door_open_delay_ms)

            # 2. Start the belt, and watch the TOF sensor WHILE it moves
            start = reactor.monotonic()
            move_end = self._motion.start_move(eject_dist, self.belt_speed)
            deadline = max(start + self.eject_timeout_s, move_end + 1.)
            confirmed = True
            if self._sensors.uses_tof:
                confirmed = self._sensors.tof_wait_for_clear(move_end, deadline)
            self._motion.wait_for_move(move_end)

            # 3. Camera looks at the bed once the belt has stopped
            if confirmed and self._sensors.uses_camera:
                confirmed = self._sensors.camera_wait_for_clear(deadline)

            # The belt moved either way
            self._belt_odometer_mm += eject_dist
            svars = self._macro_vars('_IHB_STATE')
            if svars is not None:
                svars['belt_odometer_mm'] = self._belt_odometer_mm

            if not confirmed:
                gcode.respond_info("IHB: !! eject timeout - check the bed is clear,"
                                   " then HEATBED_RESET")
                self._set_state('error')
                self.logger.error("IHB: Eject timeout — bed may not be clear.")
                return

            # 4. Close door
            if self.door_type != 'none':
                self._wait_ms(self.door_close_delay_ms)
                self._door.close()
                gcode.run_script("M400")
            self._set_state('idle')

            # 5. Notify Moonraker to start next job
            self.printer.send_event('infinite_heatbed:eject_complete')
            gcode.respond_info("IHB: eject complete")
            gcode.run_script("_IHB_ON_EJECT_COMPLETE")

        except Exception as e:
            self._set_state('error')
            self.logger.exception("IHB: Error during ejection: %s", e)
            gcode.respond_info("IHB: !! eject failed: %s" % (e,))


def load_config(config):
    return InfiniteHeatbed(config)
