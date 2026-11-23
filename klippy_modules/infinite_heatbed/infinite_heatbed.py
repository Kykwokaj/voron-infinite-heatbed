"""
Voron Infinite Heatbed — Main Klipper Extra
Registers [infinite_heatbed] config section, state machine, and GCode commands.

Parameter precedence: values in [infinite_heatbed] are startup defaults; the
[gcode_macro _IHB_PARAMS] variables override them and are re-read before
every eject. HEATBED_SET_PARAMS (used by the web UI) writes into
_IHB_PARAMS, so all three stay consistent.

Eject sequence:  cool bed -> lift Z -> open door -> run belt (+ sensor
confirm, with retries) -> close door -> done.
"""

import logging
import os

STATES = ['idle', 'printing', 'ejecting', 'door_open', 'error']
BUSY_STATES = ('ejecting', 'door_open')
PHASES = ('idle', 'cooling', 'lifting', 'door', 'belt', 'confirming',
          'closing', 'done', 'failed')

# _IHB_PARAMS variable -> (attribute, converter, minimum)
_PARAM_MAP = {
    'belt_speed':           ('belt_speed', float, 0.1),
    'eject_margin_mm':      ('eject_margin_mm', float, 0.),
    'eject_distance_mm':    ('eject_distance_mm', float, 0.),
    'eject_timeout_s':      ('eject_timeout_s', float, 1.),
    'eject_retries':        ('eject_retries', int, 0),
    'release_temp_c':       ('release_temp_c', float, 0.),
    'cool_timeout_s':       ('cool_timeout_s', float, 1.),
    'z_lift_mm':            ('z_lift_mm', float, 0.),
    'advance_past_seam':    ('advance_past_seam', int, 0),
    'belt_loop_length_mm':  ('belt_loop_length_mm', float, 0.),
    'plate_length_mm':      ('plate_length_mm', float, 0.),
    'tof_threshold_mm':     ('tof_threshold_mm', float, 0.),
    'camera_snapshot_url':  ('camera_snapshot_url', str, None),
    'door_open_delay_ms':   ('door_open_delay_ms', float, 0.),
    'door_close_delay_ms':  ('door_close_delay_ms', float, 0.),
}

# Extra belt travel (mm) when moving the seam out of the plate zone
SEAM_CLEARANCE_MM = 10.
MAX_JOG_MM = 1000.


class _EjectAborted(Exception):
    pass


class _EjectFailed(Exception):
    pass


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
        # 0 = automatic (print length + margin)
        self.eject_distance_mm = config.getfloat('eject_distance_mm', 0., minval=0.)
        self.eject_timeout_s = config.getfloat('eject_timeout_s', 60., above=0.)
        self.eject_retries = config.getint('eject_retries', 0, minval=0, maxval=5)
        # Fallback release temperature when no heat mat is selected (0 = don't wait)
        self.release_temp_c = config.getfloat('release_temp_c', 0., minval=0.)
        self.cool_timeout_s = config.getfloat('cool_timeout_s', 1800., above=0.)
        self.z_lift_mm = config.getfloat('z_lift_mm', 0., minval=0.)
        self.advance_past_seam = config.getint('advance_past_seam', 0, minval=0, maxval=1)
        # Belt geometry, needed for seam tracking (0 = unknown)
        self.belt_loop_length_mm = config.getfloat('belt_loop_length_mm', 0., minval=0.)
        self.plate_length_mm = config.getfloat('plate_length_mm', 0., minval=0.)
        # Must be read here: Klipper rejects options that a module never reads
        self.tof_threshold_mm = config.getfloat('tof_threshold_mm', 50., above=0.)
        self.camera_snapshot_url = config.get(
            'camera_snapshot_url', 'http://localhost/webcam/?action=snapshot')
        self.door_open_delay_ms = config.getfloat('door_open_delay_ms', 500., minval=0.)
        self.door_close_delay_ms = config.getfloat('door_close_delay_ms', 500., minval=0.)

        # State
        self._state = 'idle'
        self._phase = 'idle'
        self._print_length_mm = 0.
        self._belt_odometer_mm = 0.     # total distance ever driven (maintenance)
        self._belt_pos_mm = 0.          # signed belt position (for seam tracking)
        self._seam_ref_mm = None        # belt position when seam was at the mark
        self._eject_cycles = 0
        self._eject_seq = 0             # increments after every eject attempt
        self._last_result = ''          # 'ok' | 'failed' | ''
        self._last_error = ''
        self._last_eject_s = 0.
        self._abort = False
        self._eject_start = 0.
        # Current eject progress
        self._eject_target_mm = 0.
        self._eject_done_mm = 0.
        self._move_window = None        # (start_time, end_time, distance)
        # Active heat mat (pushed by the Moonraker component / web UI)
        self._mat = {'name': '', 'release_temp': 0., 'z_offset': 0., 'mesh': ''}

        # Sub-modules (loaded after printer ready)
        self._motion = None
        self._sensors = None
        self._door = None

        # Register events and GCode commands
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        for name, desc in (
                ('HEATBED_STATUS', "Print infinite heatbed status."),
                ('HEATBED_EJECT', "Manually trigger belt ejection."),
                ('HEATBED_RESET', "Clear IHB error state back to idle."),
                ('HEATBED_JOG', "Move the belt. DISTANCE=<mm> [SPEED=<mm/s>]"),
                ('HEATBED_SET_SEAM', "Mark that the belt seam is at the reference mark now."),
                ('HEATBED_PREPARE', "Called by HEATBED_PRINT_START: moves the seam off the plate."),
                ('HEATBED_SET_PARAMS', "Set eject parameters, e.g. BELT_SPEED=40 Z_LIFT_MM=20"),
                ('HEATBED_SET_MAT', "Select heat mat. NAME= RELEASE_TEMP= Z_OFFSET= MESH="),
                ('HEATBED_APPLY_MAT', "Apply the heat mat Z offset / mesh (call in start G-code)."),
        ):
            gcode.register_command(name, getattr(self, 'cmd_' + name), desc=desc)

    def _handle_ready(self):
        from .heatbed_motion import HeatbedMotion
        from .heatbed_sensors import HeatbedSensors
        from .door_controller import DoorController

        self._load_params()
        self._motion = HeatbedMotion(self.printer, self.motor_count, self.belt_speed)
        self._sensors = HeatbedSensors(self.printer, self.ejection_sensor,
                                       self._sensor_params())
        self._door = DoorController(self.printer, self.door_type)

        # Restore persisted values
        saved = self._saved_vars()
        self._print_length_mm = self._as_float(saved.get('ihb_print_length'), 0.)
        self._belt_pos_mm = self._as_float(saved.get('ihb_belt_pos'), 0.)
        self._belt_odometer_mm = self._as_float(saved.get('ihb_odometer'), 0.)
        self._eject_cycles = int(self._as_float(saved.get('ihb_eject_cycles'), 0.))
        seam = saved.get('ihb_seam_ref')
        self._seam_ref_mm = None if seam is None else self._as_float(seam, 0.)

        self.logger.info("InfiniteHeatbed ready. motor_count=%d sensor=%s door=%s",
                         self.motor_count, self.ejection_sensor, self.door_type)
        self._set_state('idle')

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _as_float(val, default):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def _macro_vars(self, name):
        macro = self.printer.lookup_object('gcode_macro %s' % name, None)
        return macro.variables if macro is not None else None

    def _saved_vars(self):
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is None:
            return {}
        return save_vars.allVariables

    def _persist(self, **values):
        """Store values in [save_variables] (if configured). Klipper's
        save_variables has no public save() method, so write the file in the
        same format its SAVE_VARIABLE command uses."""
        save_vars = self.printer.lookup_object('save_variables', None)
        if save_vars is None:
            return
        save_vars.allVariables.update(values)
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
            self.logger.exception("IHB: Unable to write save_variables")

    def _load_params(self):
        """Pull tuning values from [gcode_macro _IHB_PARAMS]."""
        pvars = self._macro_vars('_IHB_PARAMS')
        if not pvars:
            return
        for var, (attr, conv, minval) in _PARAM_MAP.items():
            if var in pvars and pvars[var] is not None:
                try:
                    val = conv(pvars[var])
                except (TypeError, ValueError):
                    self.logger.warning("IHB: bad _IHB_PARAMS value %s=%r",
                                        var, pvars[var])
                    continue
                if minval is not None and val < minval:
                    self.logger.warning("IHB: _IHB_PARAMS %s=%r below %r, ignored",
                                        var, val, minval)
                    continue
                setattr(self, attr, val)

    def _params_dict(self):
        return {var: getattr(self, attr) for var, (attr, _, _) in _PARAM_MAP.items()}

    def _sensor_params(self):
        return {'tof_threshold_mm': self.tof_threshold_mm,
                'camera_snapshot_url': self.camera_snapshot_url}

    def _resolve_print_length(self):
        """Live value from HEATBED_PRINT_START (_IHB_STATE) first, then the
        value saved across restarts, then whatever is in memory."""
        svars = self._macro_vars('_IHB_STATE')
        if svars:
            live = self._as_float(svars.get('print_length', 0.) or 0., 0.)
            if live > 0:
                return live
        saved = self._as_float(self._saved_vars().get('ihb_print_length', 0.), 0.)
        if saved > 0:
            return saved
        return self._print_length_mm

    def _eject_distance(self):
        if self.eject_distance_mm > 0:
            return self.eject_distance_mm
        return self._print_length_mm + self.eject_margin_mm

    def _release_temp(self):
        if self._mat.get('name'):
            return float(self._mat.get('release_temp') or 0.)
        return self.release_temp_c

    def _print_active(self, eventtime=None):
        ps = self.printer.lookup_object('print_stats', None)
        if ps is None:
            return False
        if eventtime is None:
            eventtime = self.printer.get_reactor().monotonic()
        return ps.get_status(eventtime).get('state') in ('printing', 'paused')

    # ── Seam tracking ──────────────────────────────────────────────────────────

    def _seam_travel(self):
        """mm the seam has moved past the reference mark, wrapped to one belt
        loop. None if the seam was never marked or the loop length is unknown."""
        if self._seam_ref_mm is None or self.belt_loop_length_mm <= 0:
            return None
        return (self._belt_pos_mm - self._seam_ref_mm) % self.belt_loop_length_mm

    def _seam_on_plate(self):
        travel = self._seam_travel()
        if travel is None or self.plate_length_mm <= 0:
            return False
        return travel < self.plate_length_mm

    def _note_belt_move(self, distance_mm):
        self._belt_pos_mm += distance_mm
        self._belt_odometer_mm += abs(distance_mm)

    def _persist_belt(self):
        self._persist(ihb_belt_pos=round(self._belt_pos_mm, 3),
                      ihb_odometer=round(self._belt_odometer_mm, 1))

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

    def _set_phase(self, phase):
        assert phase in PHASES, "Unknown phase: %s" % phase
        self._phase = phase

    def _reported_state(self, eventtime):
        # 'printing' is set by HEATBED_PREPARE; drop it once the print is gone
        # (e.g. cancelled), so the UI doesn't show a stale "printing".
        if self._state == 'printing' and not self._print_active(eventtime):
            return 'idle'
        return self._state

    def _belt_travelled(self, eventtime):
        done = self._eject_done_mm
        if self._move_window is not None:
            start, end, dist = self._move_window
            if end > start:
                frac = min(1., max(0., (eventtime - start) / (end - start)))
                done += dist * frac
        return done

    def get_status(self, eventtime=None):
        if eventtime is None:
            eventtime = self.printer.get_reactor().monotonic()
        travelled = self._belt_travelled(eventtime)
        target = self._eject_target_mm
        travel = self._seam_travel()
        return {
            'state': self._reported_state(eventtime),
            'phase': self._phase,
            'print_length_mm': self._print_length_mm,
            'eject_distance_mm': self._eject_distance(),
            'eject_target_mm': target,
            'eject_travelled_mm': round(travelled, 1),
            'eject_progress': round(min(1., travelled / target), 3) if target > 0 else 0.,
            'belt_odometer_mm': round(self._belt_odometer_mm, 1),
            'belt_position_mm': round(self._belt_pos_mm, 1),
            'seam_travel_mm': None if travel is None else round(travel, 1),
            'seam_on_plate': self._seam_on_plate(),
            'eject_cycles': self._eject_cycles,
            'eject_seq': self._eject_seq,
            'last_result': self._last_result,
            'last_error': self._last_error,
            'last_eject_s': round(self._last_eject_s, 1),
            'release_temp': self._release_temp(),
            'mat': dict(self._mat),
            'params': self._params_dict(),
            'door_state': self._door.state if self._door else 'unknown',
            'door_type': self.door_type,
            'motor_count': self.motor_count,
            'ejection_sensor': self.ejection_sensor,
        }

    # ── GCode commands ─────────────────────────────────────────────────────────

    def cmd_HEATBED_STATUS(self, gcmd):
        s = self.get_status()
        seam = s['seam_travel_mm']
        gcmd.respond_info(
            "IHB Status\n"
            "  state:         %s (%s)\n"
            "  print_length:  %.1f mm\n"
            "  eject dist:    %.1f mm\n"
            "  belt position: %.1f mm\n"
            "  seam travel:   %s\n"
            "  belt_odometer: %.1f mm\n"
            "  eject cycles:  %d\n"
            "  heat mat:      %s\n"
            "  door:          %s\n"
            "  motors:        %d\n"
            "  sensor:        %s" % (
                s['state'], s['phase'], s['print_length_mm'],
                s['eject_distance_mm'], s['belt_position_mm'],
                'not set' if seam is None else '%.1f mm' % seam,
                s['belt_odometer_mm'], s['eject_cycles'],
                s['mat']['name'] or 'none',
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
        self._load_params()
        # A fixed eject distance works without a print length
        if self.eject_distance_mm <= 0 and self._print_length_mm <= 0:
            gcmd.respond_info("IHB: No print length set. Use HEATBED_PRINT_START first.")
            return
        # Claim the state now, so a second HEATBED_EJECT sent before the
        # callback runs is rejected instead of starting a second eject.
        self._set_state('ejecting')
        reactor = self.printer.get_reactor()
        reactor.register_callback(lambda _: self._run_eject_sequence())

    def cmd_HEATBED_RESET(self, gcmd):
        force = gcmd.get_int('FORCE', 0, minval=0, maxval=1)
        if self._state in BUSY_STATES:
            if not force:
                gcmd.respond_info(
                    "IHB: Eject in progress (state '%s'). Use HEATBED_RESET FORCE=1"
                    " only if it is stuck." % self._state)
                return
            # Ask the running sequence to stop at its next check point
            self._abort = True
        self._set_state('idle')
        self._set_phase('idle')
        self._move_window = None
        gcmd.respond_info("IHB: state reset to idle")

    def cmd_HEATBED_JOG(self, gcmd):
        if self._motion is None:
            raise gcmd.error("IHB: Not ready yet.")
        dist = gcmd.get_float('DISTANCE')
        speed = gcmd.get_float('SPEED', min(self.belt_speed, 30.), above=0.)
        if abs(dist) > MAX_JOG_MM:
            raise gcmd.error("IHB: jog limited to +/-%d mm" % MAX_JOG_MM)
        if self._state in BUSY_STATES:
            raise gcmd.error("IHB: belt is busy ejecting")
        if self._print_active():
            raise gcmd.error("IHB: refusing to move the belt during a print")
        self._motion.move_from_command(dist, speed)
        self._note_belt_move(dist)
        self._persist_belt()
        gcmd.respond_info("IHB: belt jogged %+.1f mm" % dist)

    def cmd_HEATBED_SET_SEAM(self, gcmd):
        self._seam_ref_mm = self._belt_pos_mm
        self._persist(ihb_seam_ref=round(self._seam_ref_mm, 3))
        if self.belt_loop_length_mm <= 0:
            gcmd.respond_info("IHB: seam marked. Set belt_loop_length_mm so the"
                              " seam can be tracked.")
        else:
            gcmd.respond_info("IHB: seam marked at the reference point")

    def cmd_HEATBED_PREPARE(self, gcmd):
        """Runs at print start (from HEATBED_PRINT_START)."""
        if self._state in BUSY_STATES:
            raise gcmd.error("IHB: previous eject still running")
        self._load_params()
        if self.advance_past_seam and self._motion is not None and self._seam_on_plate():
            travel = self._seam_travel()
            adv = self.plate_length_mm - travel + SEAM_CLEARANCE_MM
            adv = min(adv, self.belt_loop_length_mm)
            gcmd.respond_info("IHB: seam is on the plate, advancing belt %.0f mm" % adv)
            self._motion.move_from_command(adv, self.belt_speed)
            self._note_belt_move(adv)
            self._persist_belt()
        if self._state in ('idle', 'error'):
            self._set_state('printing')
            self._set_phase('idle')

    def cmd_HEATBED_SET_PARAMS(self, gcmd):
        pvars = self._macro_vars('_IHB_PARAMS')
        changed = []
        for var, (attr, conv, minval) in _PARAM_MAP.items():
            raw = gcmd.get(var.upper(), None)
            if raw is None:
                continue
            try:
                val = conv(float(raw)) if conv is int else conv(raw)
            except ValueError:
                raise gcmd.error("IHB: bad value %s=%s" % (var.upper(), raw))
            if minval is not None and val < minval:
                raise gcmd.error("IHB: %s must be >= %s" % (var.upper(), minval))
            setattr(self, attr, val)
            if pvars is not None:
                pvars[var] = val
            changed.append("%s=%s" % (var, val))
        if self._sensors is not None:
            self._sensors.update_params(self._sensor_params())
        if gcmd.get_int('SILENT', 0):
            return
        if changed:
            gcmd.respond_info("IHB: " + " ".join(changed))
        else:
            gcmd.respond_info("IHB params: " + " ".join(
                "%s=%s" % kv for kv in sorted(self._params_dict().items())))

    def cmd_HEATBED_SET_MAT(self, gcmd):
        name = gcmd.get('NAME', '')
        self._mat = {
            'name': name,
            'release_temp': gcmd.get_float('RELEASE_TEMP', 0., minval=0., maxval=120.),
            'z_offset': gcmd.get_float('Z_OFFSET', 0., minval=-2., maxval=2.),
            'mesh': gcmd.get('MESH', ''),
        }
        if gcmd.get_int('SILENT', 0):
            return
        gcmd.respond_info("IHB: heat mat '%s' (release %.0fC, Z %+.3f%s)" % (
            name or 'none', self._mat['release_temp'], self._mat['z_offset'],
            ", mesh %s" % self._mat['mesh'] if self._mat['mesh'] else ""))

    def cmd_HEATBED_APPLY_MAT(self, gcmd):
        """Apply the active mat's Z offset (relative) and bed mesh. Call once
        per print, after CALIBRATE_Z / anything that sets the Z offset."""
        mat = self._mat
        if not mat.get('name'):
            gcmd.respond_info("IHB: no heat mat selected, nothing applied")
            return
        gcode = self.printer.lookup_object('gcode')
        if mat.get('mesh'):
            gcode.run_script_from_command('BED_MESH_PROFILE LOAD="%s"' % mat['mesh'])
        if mat.get('z_offset'):
            gcode.run_script_from_command(
                "SET_GCODE_OFFSET Z_ADJUST=%.3f MOVE=1" % mat['z_offset'])
        gcmd.respond_info("IHB: applied heat mat '%s'" % mat['name'])

    # ── Ejection sequence ──────────────────────────────────────────────────────

    def set_print_length(self, length_mm):
        """Set the print length and persist it (same effect as
        HEATBED_PRINT_START, usable from Python)."""
        length_mm = float(length_mm)
        self._print_length_mm = length_mm
        svars = self._macro_vars('_IHB_STATE')
        if svars is not None:
            svars['print_length'] = length_mm
        self._persist(ihb_print_length=length_mm)

    def _pause(self, seconds):
        """Non-blocking delay (yields to the reactor); honours HEATBED_RESET FORCE=1."""
        if self._abort:
            raise _EjectAborted()
        if seconds > 0:
            reactor = self.printer.get_reactor()
            reactor.pause(reactor.monotonic() + seconds)
        if self._abort:
            raise _EjectAborted()

    def _cool_bed(self, release_temp):
        heater = self.printer.lookup_object('heater_bed', None)
        if heater is None or release_temp <= 0:
            return
        reactor = self.printer.get_reactor()
        gcode = self.printer.lookup_object('gcode')
        status = heater.get_status(reactor.monotonic())
        if status['temperature'] <= release_temp:
            return
        self._set_phase('cooling')
        if status['target'] > 0:
            # The print is finished; stop heating so the bed can cool
            gcode.run_script("M140 S0")
        gcode.respond_info("IHB: cooling bed to %.0fC for release" % release_temp)
        deadline = reactor.monotonic() + self.cool_timeout_s
        while heater.get_status(reactor.monotonic())['temperature'] > release_temp:
            if reactor.monotonic() > deadline:
                raise _EjectFailed("bed did not cool to %.0fC within %.0fs"
                                   % (release_temp, self.cool_timeout_s))
            self._pause(1.)

    def _lift_z(self):
        if self.z_lift_mm <= 0:
            return
        reactor = self.printer.get_reactor()
        toolhead = self.printer.lookup_object('toolhead')
        status = toolhead.get_status(reactor.monotonic())
        if 'z' not in status['homed_axes']:
            self.logger.info("IHB: Z not homed, skipping Z lift")
            return
        z = status['position'][2]
        z_max = status['axis_maximum'][2]
        lift = min(self.z_lift_mm, z_max - 1. - z)
        if lift <= 0:
            return
        self._set_phase('lifting')
        self.printer.lookup_object('gcode').run_script(
            "SAVE_GCODE_STATE NAME=_ihb_lift\n"
            "G91\n"
            "G1 Z%.3f F600\n"
            "RESTORE_GCODE_STATE NAME=_ihb_lift\n"
            "M400" % lift)

    def _belt_pass(self, eject_dist, deadline_from):
        """Run the belt once and return True if the bed is confirmed clear."""
        reactor = self.printer.get_reactor()
        self._set_phase('belt')
        start = reactor.monotonic()
        move_end = self._motion.start_move(eject_dist, self.belt_speed)
        self._move_window = (start, move_end, eject_dist)
        deadline = max(deadline_from + self.eject_timeout_s, move_end + 1.)
        confirmed = True
        # TOF watches the print pass WHILE the belt moves
        if self._sensors.uses_tof:
            confirmed = self._sensors.tof_wait_for_clear(move_end, deadline)
        self._motion.wait_for_move(move_end)
        self._move_window = None
        self._eject_done_mm += eject_dist
        self._note_belt_move(eject_dist)
        self._persist_belt()
        if self._abort:
            raise _EjectAborted()
        # Camera looks at the bed once the belt has stopped
        if confirmed and self._sensors.uses_camera:
            self._set_phase('confirming')
            confirmed = self._sensors.camera_wait_for_clear(deadline)
        return confirmed

    def _finish_eject(self, result, error=''):
        reactor = self.printer.get_reactor()
        self._last_result = result
        self._last_error = error
        self._last_eject_s = reactor.monotonic() - self._eject_start
        self._eject_seq += 1
        self._move_window = None

    def _run_eject_sequence(self):
        """Full print ejection sequence. Runs in reactor callback."""
        gcode = self.printer.lookup_object('gcode')
        reactor = self.printer.get_reactor()
        self._abort = False
        self._eject_start = reactor.monotonic()
        self._eject_done_mm = 0.
        self._last_error = ''
        try:
            self._set_state('ejecting')
            self._sensors.update_params(self._sensor_params())
            eject_dist = self._eject_distance()
            self._eject_target_mm = eject_dist
            gcode.respond_info("IHB: ejecting %.0f mm at %.0f mm/s"
                               % (eject_dist, self.belt_speed))

            # 1. Let the bed cool so the print releases
            self._cool_bed(self._release_temp())

            # 2. Lift the toolhead clear of the print
            self._lift_z()

            # 3. Open door and let it finish moving before the belt starts
            if self.door_type != 'none':
                self._set_phase('door')
                self._set_state('door_open')
                self._door.open()
                gcode.run_script("M400")
                self._pause(self.door_open_delay_ms / 1000.)

            # 4. Run the belt; retry if the sensors say it's not clear
            attempts = 1 + self.eject_retries
            confirmed = False
            for attempt in range(attempts):
                if attempt:
                    gcode.respond_info("IHB: bed not clear, retry %d of %d"
                                       % (attempt, self.eject_retries))
                    self._eject_target_mm += eject_dist
                confirmed = self._belt_pass(eject_dist, reactor.monotonic())
                if confirmed:
                    break
            if not confirmed:
                raise _EjectFailed("bed not confirmed clear after %d attempt(s)"
                                   % attempts)

            # 5. Close door
            if self.door_type != 'none':
                self._set_phase('closing')
                self._pause(self.door_close_delay_ms / 1000.)
                self._door.close()
                gcode.run_script("M400")

            self._eject_cycles += 1
            self._persist(ihb_eject_cycles=self._eject_cycles)
            self._finish_eject('ok')
            self._set_phase('done')
            self._set_state('idle')

            # 6. Notify Moonraker (the component starts the next queued job)
            self.printer.send_event('infinite_heatbed:eject_complete')
            gcode.respond_info("IHB: eject complete")
            gcode.run_script("_IHB_ON_EJECT_COMPLETE")

        except _EjectAborted:
            self._finish_eject('failed', 'aborted by HEATBED_RESET')
            self._set_phase('idle')
            gcode.respond_info("IHB: eject aborted")
        except _EjectFailed as e:
            self._finish_eject('failed', str(e))
            self._set_phase('failed')
            self._set_state('error')
            self.logger.error("IHB: Eject failed: %s", e)
            gcode.respond_info("IHB: !! eject failed: %s - check the bed is clear,"
                               " then HEATBED_RESET" % (e,))
        except Exception as e:
            self._finish_eject('failed', str(e))
            self._set_phase('failed')
            self._set_state('error')
            self.logger.exception("IHB: Error during ejection: %s", e)
            gcode.respond_info("IHB: !! eject failed: %s" % (e,))


def load_config(config):
    return InfiniteHeatbed(config)
