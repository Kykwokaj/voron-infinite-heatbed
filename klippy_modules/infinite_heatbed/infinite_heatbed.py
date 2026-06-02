"""
Voron Infinite Heatbed — Main Klipper Extra
Registers [infinite_heatbed] config section, state machine, and GCode commands.
"""

import logging

STATES = ['idle', 'printing', 'ejecting', 'door_open', 'error']

class InfiniteHeatbed:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.logger = logging.getLogger('infinite_heatbed')

        # Config
        self.motor_count = config.getint('motor_count', 1, minval=1, maxval=2)
        self.ejection_sensor = config.get('ejection_sensor', 'none')
        self.door_type = config.get('door_type', 'none')
        self.belt_speed = config.getfloat('belt_speed', 50., above=0.)
        self.eject_margin_mm = config.getfloat('eject_margin_mm', 20., minval=0.)
        self.eject_timeout_s = config.getfloat('eject_timeout_s', 60., above=0.)

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

    def _handle_ready(self):
        from .heatbed_motion import HeatbedMotion
        from .heatbed_sensors import HeatbedSensors
        from .door_controller import DoorController

        self._motion = HeatbedMotion(self.printer, self.motor_count, self.belt_speed)
        self._sensors = HeatbedSensors(self.printer, self.ejection_sensor,
                                       self.printer.lookup_object('gcode')
                                           .get_macro_params('_IHB_PARAMS', {}))
        self._door = DoorController(self.printer, self.door_type)

        # Restore saved print length if any
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is not None:
            self._print_length_mm = save_vars.allVariables.get('ihb_print_length', 0.)

        self.logger.info("InfiniteHeatbed ready. motor_count=%d sensor=%s door=%s",
                         self.motor_count, self.ejection_sensor, self.door_type)
        self._set_state('idle')

    # ── State machine ──────────────────────────────────────────────────────────

    def _set_state(self, new_state):
        assert new_state in STATES, "Unknown state: %s" % new_state
        old = self._state
        self._state = new_state
        self.logger.info("IHB state: %s -> %s", old, new_state)
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
        if self._state not in ('idle', 'printing'):
            gcmd.respond_info("IHB: Cannot eject in state '%s'" % self._state)
            return
        if self._print_length_mm <= 0:
            gcmd.respond_info("IHB: No print length set. Use HEATBED_PRINT_START first.")
            return
        reactor = self.printer.get_reactor()
        reactor.register_callback(lambda _: self._run_eject_sequence())

    # ── Ejection sequence ──────────────────────────────────────────────────────

    def set_print_length(self, length_mm):
        self._print_length_mm = length_mm
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is not None:
            save_vars.allVariables['ihb_print_length'] = length_mm
            save_vars.save()

    def _run_eject_sequence(self):
        """Full print ejection sequence. Runs in reactor callback."""
        try:
            self._set_state('ejecting')
            eject_dist = self._print_length_mm + self.eject_margin_mm

            # 1. Open door
            self._set_state('door_open')
            self._door.open()

            # 2. Advance belt; sensor confirms clear
            self._motion.move(eject_dist, self.belt_speed)
            if self.ejection_sensor != 'none':
                confirmed = self._sensors.wait_for_clear(self.eject_timeout_s)
                if not confirmed:
                    self._set_state('error')
                    self.logger.error("IHB: Eject timeout — bed may not be clear.")
                    return

            # 3. Update odometer
            self._belt_odometer_mm += eject_dist

            # 4. Close door
            self._door.close()
            self._set_state('idle')

            # 5. Notify Moonraker to start next job
            self.printer.send_event('infinite_heatbed:eject_complete')

        except Exception as e:
            self._set_state('error')
            self.logger.exception("IHB: Error during ejection: %s", e)


def load_config(config):
    return InfiniteHeatbed(config)
