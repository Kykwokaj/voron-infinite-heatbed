"""
Voron Infinite Heatbed — Moonraker Component
Exposes REST API for status queries and control.
Pushes state changes to Mainsail/Fluidd via websocket.
Triggers next queued print after ejection completes.
"""

from __future__ import annotations
import logging
import asyncio

class InfiniteHeatbedServer:
    def __init__(self, config):
        self.server = config.get_server()
        self.logger = logging.getLogger('infinite_heatbed_server')

        # Register REST endpoints
        self.server.register_endpoint(
            '/machine/infinite_heatbed/status',
            ['GET'],
            self._handle_status,
        )
        self.server.register_endpoint(
            '/machine/infinite_heatbed/eject',
            ['POST'],
            self._handle_eject,
        )
        self.server.register_endpoint(
            '/machine/infinite_heatbed/set_print_length',
            ['POST'],
            self._handle_set_print_length,
        )

        # Listen for Klipper events forwarded by Moonraker
        self.server.register_event_handler(
            'server:klippy_ready', self._on_klippy_ready
        )

    async def _on_klippy_ready(self):
        kapis = self.server.lookup_component('klippy_apis')
        # Subscribe to IHB state change events from Klipper
        await kapis.subscribe_from_transport(
            {'infinite_heatbed': None}, self._on_klipper_update
        )
        self.logger.info("InfiniteHeatbedServer: subscribed to Klipper updates")

    async def _on_klipper_update(self, status):
        """Receive status dict from Klipper and push to UI via websocket."""
        if 'infinite_heatbed' not in status:
            return
        ihb_status = status['infinite_heatbed']
        self.server.send_event('infinite_heatbed:status_update', ihb_status)

        # When ejection completes (state returns to idle), start next queued print
        if ihb_status.get('state') == 'idle':
            await self._maybe_start_next_job()

    # ── REST Handlers ──────────────────────────────────────────────────────────

    async def _handle_status(self, web_request):
        kapis = self.server.lookup_component('klippy_apis')
        try:
            result = await kapis.query_objects({'infinite_heatbed': None})
            ihb = result.get('infinite_heatbed', {})
        except Exception:
            ihb = {'state': 'unavailable'}

        job_queue = self.server.lookup_component('job_queue', None)
        queue_len = 0
        if job_queue is not None:
            try:
                queue_len = len(job_queue.queue_state.get('queued_jobs', []))
            except Exception:
                pass

        return dict(ihb, queue_length=queue_len)

    async def _handle_eject(self, web_request):
        kapis = self.server.lookup_component('klippy_apis')
        await kapis.run_gcode('HEATBED_EJECT')
        return {'result': 'ok'}

    async def _handle_set_print_length(self, web_request):
        length = web_request.get_float('print_length')
        kapis = self.server.lookup_component('klippy_apis')
        await kapis.run_gcode('HEATBED_PRINT_START PRINT_LENGTH=%.3f' % length)
        return {'result': 'ok'}

    # ── Job Queue ──────────────────────────────────────────────────────────────

    async def _maybe_start_next_job(self):
        """If there are queued jobs, start the next one."""
        job_queue = self.server.lookup_component('job_queue', None)
        if job_queue is None:
            return
        try:
            queue_state = job_queue.queue_state
            if queue_state.get('queue_state') == 'paused':
                self.logger.info("IHB: Resuming job queue for next print")
                await job_queue.pause_queue(False)
        except Exception as e:
            self.logger.warning("IHB: Could not resume job queue: %s", e)


def load_component(config):
    return InfiniteHeatbedServer(config)
