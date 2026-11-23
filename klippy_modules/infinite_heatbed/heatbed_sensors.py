"""
Voron Infinite Heatbed — Sensor Manager
Abstracts TOF (VL53L0X) and/or camera detection for post-eject confirmation.

TOF notes:
  Klipper has no native VL53L0X driver. The recommended approach is to use
  an RP2040 Pico running CircuitPython as a co-processor that reads the sensor
  and writes the reading (in mm) to /tmp/ihb_tof_reading on the Pi.
  This module reads that file. See README "Pattern 4".
  The TOF check must run WHILE the belt moves: the print has to be seen
  passing the sensor, so the eject sequence starts it right after the belt
  move is queued.

Camera notes:
  Uses crowsnest's MJPEG snapshot endpoint. The check runs after the belt has
  stopped. The HTTP fetch runs in a worker thread so it never blocks Klipper.

All waiting uses reactor.pause(): time.sleep() or a blocking socket call
freezes Klipper's event loop and causes MCU 'Timer too close' shutdowns.
"""

import logging
import threading


class HeatbedSensors:
    def __init__(self, printer, sensor_type, params):
        self.printer = printer
        self.sensor_type = sensor_type  # 'tof' | 'camera' | 'both' | 'none'
        self.logger = logging.getLogger('infinite_heatbed.sensors')
        self.reactor = printer.get_reactor()

        self.tof_threshold_mm = 50.
        self.camera_url = 'http://localhost/webcam/?action=snapshot'
        self.update_params(params)

        self._tof_reading_path = '/tmp/ihb_tof_reading'

    def update_params(self, params):
        """Apply tuning values (called at startup and before every eject)."""
        if 'tof_threshold_mm' in params:
            self.tof_threshold_mm = float(params['tof_threshold_mm'])
        if params.get('camera_snapshot_url'):
            self.camera_url = str(params['camera_snapshot_url'])

    @property
    def uses_tof(self):
        return self.sensor_type in ('tof', 'both')

    @property
    def uses_camera(self):
        return self.sensor_type in ('camera', 'both')

    def _sleep(self, seconds):
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def wait_for_clear(self, timeout_s, detect_deadline=None):
        """
        Convenience wrapper: run all configured checks back to back.
        Returns True if bed is clear, False if timed out.
        The eject sequence calls tof_wait_for_clear / camera_wait_for_clear
        directly so the TOF check overlaps the belt move.
        """
        deadline = self.reactor.monotonic() + timeout_s
        if self.uses_tof and not self.tof_wait_for_clear(
                detect_deadline or deadline, deadline):
            return False
        if self.uses_camera and not self.camera_wait_for_clear(deadline):
            return False
        return True

    # ── TOF ───────────────────────────────────────────────────────────────────

    def _read_tof(self):
        """Read TOF distance from co-processor file. Returns mm or None."""
        try:
            with open(self._tof_reading_path, 'r') as f:
                return float(f.read().strip())
        except Exception:
            return None

    def tof_wait_for_clear(self, detect_deadline, deadline):
        """
        Phase 1: until detect_deadline (normally the end of the belt move),
                 wait for the object to reach the sensor (distance < threshold).
        Phase 2: until deadline, wait for it to pass (distance >= threshold).
        If nothing is ever seen, assume the bed was already clear.
        """
        POLL_INTERVAL = 0.05

        object_detected = False
        while True:
            reading = self._read_tof()
            if reading is not None and reading < self.tof_threshold_mm:
                object_detected = True
                self.logger.info("TOF: object detected (%.1fmm)", reading)
                break
            if self.reactor.monotonic() >= detect_deadline:
                break
            self._sleep(POLL_INTERVAL)

        if not object_detected:
            self.logger.warning("TOF: object never detected during belt move"
                                " — assuming bed already clear")
            return True

        self.logger.info("TOF: waiting for object to clear sensor...")
        while self.reactor.monotonic() < deadline:
            reading = self._read_tof()
            if reading is None or reading >= self.tof_threshold_mm:
                self.logger.info("TOF: object cleared (reading=%.1fmm)",
                                 reading if reading is not None else -1)
                return True
            self._sleep(POLL_INTERVAL)

        self.logger.error("TOF: timeout waiting for object to clear")
        return False

    # ── Camera ────────────────────────────────────────────────────────────────

    def camera_wait_for_clear(self, deadline):
        """Fetch snapshots until one shows a clear bed, or deadline passes."""
        POLL_INTERVAL = 1.0
        MAX_ATTEMPTS = 3

        while self.reactor.monotonic() < deadline:
            for _ in range(MAX_ATTEMPTS):
                clear = self._camera_is_clear()
                if clear is not None:
                    if clear:
                        self.logger.info("Camera: bed confirmed clear")
                        return True
                    self.logger.info("Camera: objects still detected")
                    break
            self._sleep(POLL_INTERVAL)

        self.logger.error("Camera: timeout or repeated failures")
        return False

    def _fetch_snapshot(self, timeout_s=5.):
        """Download one snapshot in a worker thread while the reactor keeps
        running. Returns the bytes or None."""
        result = {}

        def worker():
            try:
                import urllib.request
                with urllib.request.urlopen(self.camera_url,
                                            timeout=timeout_s) as resp:
                    result['data'] = resp.read()
            except Exception as e:
                result['error'] = e

        t = threading.Thread(target=worker, name='ihb-camera')
        t.daemon = True
        t.start()
        give_up = self.reactor.monotonic() + timeout_s + 1.
        while t.is_alive() and self.reactor.monotonic() < give_up:
            self._sleep(0.05)
        if 'error' in result:
            self.logger.warning("Camera snapshot failed: %s", result['error'])
        elif 'data' not in result:
            self.logger.warning("Camera snapshot timed out")
        return result.get('data')

    def _camera_is_clear(self):
        """
        Fetch one JPEG snapshot and decide if the bed is empty.
        Returns True (clear), False (object present), or None (error).

        Placeholder detector: mean value of the raw JPEG bytes. This is NOT a
        real brightness measurement — replace with a Pillow ROI check or an ML
        classifier for real use. See README "Pattern 5".
        """
        data = self._fetch_snapshot()
        if not data:
            return None
        avg = float(sum(bytearray(data))) / len(data)
        self.logger.debug("Camera: snapshot avg=%.1f", avg)
        BELT_BASELINE = 120     # tune to your bare belt
        THRESHOLD = 10
        return abs(avg - BELT_BASELINE) < THRESHOLD
