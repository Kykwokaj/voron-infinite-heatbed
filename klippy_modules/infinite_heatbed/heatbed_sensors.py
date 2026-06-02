"""
Voron Infinite Heatbed — Sensor Manager
Abstracts TOF (VL53L0X) and/or camera detection for post-eject confirmation.

TOF notes:
  Klipper has no native VL53L0X driver. The recommended approach is to use
  an RP2040 Pico running CircuitPython as a co-processor that reads the sensor
  and writes the reading (in mm) to /tmp/ihb_tof_reading on the Pi.
  This module reads that file. See README "Pattern 4".

Camera notes:
  Uses crowsnest's MJPEG snapshot endpoint. Requires the 'requests' and
  optionally 'PIL' (Pillow) packages installed on the Pi.
"""

import logging
import time
import os

class HeatbedSensors:
    def __init__(self, printer, sensor_type, params):
        self.printer = printer
        self.sensor_type = sensor_type  # 'tof' | 'camera' | 'both' | 'none'
        self.logger = logging.getLogger('infinite_heatbed.sensors')

        self.tof_threshold_mm = float(params.get('tof_threshold_mm', 50))
        self.camera_url = params.get('camera_snapshot_url',
                                     'http://localhost/webcam/?action=snapshot')

        self._tof_reading_path = '/tmp/ihb_tof_reading'

    def wait_for_clear(self, timeout_s):
        """
        Poll sensors until bed is confirmed clear, or timeout_s elapses.
        Returns True if bed is clear, False if timed out.

        Logic:
          TOF: wait for reading > threshold (object present), then > threshold again
               after it dips (object has passed). If sensor starts clear, skip first phase.
          Camera: fetch snapshot, check average brightness of belt ROI.
          Both: require both to confirm.
        """
        deadline = time.monotonic() + timeout_s

        if self.sensor_type in ('tof', 'both'):
            ok = self._tof_wait_for_clear(deadline)
            if not ok:
                return False

        if self.sensor_type in ('camera', 'both'):
            ok = self._camera_wait_for_clear(deadline)
            if not ok:
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

    def _tof_wait_for_clear(self, deadline):
        """
        Phase 1: wait until object is detected (distance < threshold).
        Phase 2: wait until object has cleared (distance > threshold).
        If object not detected at start, skip Phase 1.
        """
        POLL_INTERVAL = 0.1

        # Phase 1: detect object
        initial = self._read_tof()
        object_detected = initial is not None and initial < self.tof_threshold_mm

        if not object_detected:
            self.logger.info("TOF: no object at sensor start — waiting for object to appear")
            while time.monotonic() < deadline:
                reading = self._read_tof()
                if reading is not None and reading < self.tof_threshold_mm:
                    object_detected = True
                    self.logger.info("TOF: object detected (%.1fmm)", reading)
                    break
                time.sleep(POLL_INTERVAL)

        if not object_detected:
            self.logger.warning("TOF: object never detected — assuming bed already clear")
            return True

        # Phase 2: wait for object to clear
        self.logger.info("TOF: waiting for object to clear sensor...")
        while time.monotonic() < deadline:
            reading = self._read_tof()
            if reading is None or reading >= self.tof_threshold_mm:
                self.logger.info("TOF: object cleared (reading=%.1fmm)",
                                 reading if reading is not None else -1)
                return True
            time.sleep(POLL_INTERVAL)

        self.logger.error("TOF: timeout waiting for object to clear")
        return False

    # ── Camera ────────────────────────────────────────────────────────────────

    def _camera_wait_for_clear(self, deadline):
        """Fetch snapshot and check for objects via average pixel brightness."""
        POLL_INTERVAL = 1.0
        MAX_ATTEMPTS = 3

        while time.monotonic() < deadline:
            for _ in range(MAX_ATTEMPTS):
                clear = self._camera_is_clear()
                if clear is not None:
                    if clear:
                        self.logger.info("Camera: bed confirmed clear")
                        return True
                    else:
                        self.logger.info("Camera: objects still detected")
                    break
            time.sleep(POLL_INTERVAL)

        self.logger.error("Camera: timeout or repeated failures")
        return False

    def _camera_is_clear(self):
        """
        Fetch one JPEG snapshot and decide if the bed is empty.
        Returns True (clear), False (object present), or None (error).

        Simple approach: compare mean brightness of the belt ROI.
        A print will typically be lighter or darker than the bare belt.
        Tune BRIGHT_THRESHOLD for your setup.

        For robust detection, replace this with an ML classifier.
        See README "Pattern 5".
        """
        try:
            import urllib.request
            import struct
            import zlib

            with urllib.request.urlopen(self.camera_url, timeout=5) as resp:
                data = resp.read()

            # Very simple JPEG average brightness via raw byte analysis.
            # For accurate ROI: install Pillow (pip install Pillow) and use PIL.Image.
            avg = sum(data) / len(data) if data else 128
            self.logger.debug("Camera: snapshot avg brightness=%.1f", avg)

            # Calibrate this threshold for your belt surface vs. print colors.
            # Belt-only average should differ from print-present average by >10.
            # This is a placeholder — replace with Pillow ROI check or ML.
            BELT_BASELINE = 120     # tune to your bare belt brightness
            THRESHOLD = 10
            return abs(avg - BELT_BASELINE) < THRESHOLD

        except Exception as e:
            self.logger.warning("Camera snapshot failed: %s", e)
            return None
