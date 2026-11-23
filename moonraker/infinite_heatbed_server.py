"""
Voron Infinite Heatbed — Moonraker Component

Loaded by the [infinite_heatbed_server] section in moonraker.conf.

* Stores heat mats and eject settings in Moonraker's database and pushes them
  to Klipper (HEATBED_SET_MAT / HEATBED_SET_PARAMS) whenever Klipper starts.
* After each eject decides whether the job queue may start the next print:
  - eject failed            -> pause queue
  - "stop after this plate" -> pause queue
  - next job needs another heat mat -> pause queue, flag a mat swap
  - otherwise               -> start next job
  Requires [job_queue] automatic_transition: False so the queue never starts
  a job while the belt is still ejecting.
* Reads which plate a G-code file was sliced for (OrcaSlicer "curr_bed_type",
  or a "; ihb_mat = <name>" comment) so the UI can flag mat mismatches.
* Links the web UI (repo/ui) into Mainsail/Fluidd as /ihb/.
"""

from __future__ import annotations
import asyncio
import inspect
import logging
import os
import re

try:
    from ..common import RequestType
    GET, POST = RequestType.GET, RequestType.POST
    GET_POST = RequestType.GET | RequestType.POST
except Exception:  # older Moonraker
    GET, POST, GET_POST = ['GET'], ['POST'], ['GET', 'POST']

DB_NAMESPACE = 'infinite_heatbed'
NOTIFY_EVENT = 'infinite_heatbed:ihb_server_update'   # -> notify_ihb_server_update

DEFAULT_MATS = [
    {'name': 'Textured PEI', 'release_temp': 30, 'z_offset': -0.02, 'mesh': '',
     'slicer_plate': 'Textured PEI Plate'},
    {'name': 'Smooth PEI', 'release_temp': 28, 'z_offset': 0.0, 'mesh': '',
     'slicer_plate': 'High Temp Plate'},
    {'name': 'G10 / Garolite', 'release_temp': 40, 'z_offset': 0.03, 'mesh': '',
     'slicer_plate': 'Engineering Plate'},
    {'name': 'PEO powder', 'release_temp': 32, 'z_offset': -0.01, 'mesh': '',
     'slicer_plate': 'Cool Plate'},
]

# setting -> (type, min, max)
SETTINGS = {
    'belt_speed':          (float, 1., 300.),
    'eject_margin_mm':     (float, 0., 1000.),
    'eject_distance_mm':   (float, 0., 5000.),
    'eject_retries':       (int, 0, 5),
    'eject_timeout_s':     (float, 1., 3600.),
    'cool_timeout_s':      (float, 1., 7200.),
    'z_lift_mm':           (float, 0., 200.),
    'advance_past_seam':   (int, 0, 1),
    'belt_loop_length_mm': (float, 0., 10000.),
    'plate_length_mm':     (float, 0., 5000.),
}

_PLATE_RE = re.compile(
    rb'^;\s*(curr_bed_type|ihb_mat)\s*=\s*(.+?)\s*$', re.MULTILINE)
_UNSAFE = re.compile(r'["\';#\r\n]')


def _clean(text, limit=48):
    return _UNSAFE.sub('', str(text or '')).strip()[:limit]


def _scan_plate(path):
    """Find the slicer plate type in the head or tail of a G-code file."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(64 * 1024)
            f.seek(max(0, size - 256 * 1024))
            tail = f.read()
    except OSError:
        return ''
    found = {}
    for chunk in (head, tail):
        for m in _PLATE_RE.finditer(chunk):
            found[m.group(1).decode()] = m.group(2).decode('utf-8', 'replace')
    # an explicit "; ihb_mat =" wins over the slicer plate type
    return found.get('ihb_mat') or found.get('curr_bed_type') or ''


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _is_post(web_request):
    try:
        rtype = web_request.get_request_type()
    except AttributeError:
        rtype = web_request.get_action()
    return 'POST' in str(rtype).upper()


class InfiniteHeatbedServer:
    def __init__(self, config):
        self.server = config.get_server()
        self.logger = logging.getLogger('infinite_heatbed_server')

        repo_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        self.ui_path = os.path.expanduser(
            config.get('ui_path', os.path.join(repo_dir, 'ui')))
        roots = config.get('web_roots', '~/mainsail, ~/fluidd')
        self.web_roots = [os.path.expanduser(r.strip())
                          for r in roots.split(',') if r.strip()]
        # Look of the web UI: 'mainsail' or 'fluidd' (chosen by install.sh)
        self.ui_theme = config.get('ui_theme', 'mainsail').strip().lower()
        if self.ui_theme not in ('mainsail', 'fluidd'):
            self.logger.warning("IHB: unknown ui_theme %r, using mainsail", self.ui_theme)
            self.ui_theme = 'mainsail'

        self.ihb = {}                 # latest Klipper infinite_heatbed status
        self.last_seq = None
        self.stop_after = False
        self.mat_swap = None          # {'filename': ..., 'mat': ...}
        self.last_notice = ''
        self.mats = [dict(m) for m in DEFAULT_MATS]
        self.active_mat = DEFAULT_MATS[0]['name']
        self.settings = {}
        self._plate_cache = {}        # path -> (mtime, plate)
        self._link_task = None

        for path, rtype, cb in (
                ('status', GET, self._handle_status),
                ('eject', POST, self._handle_eject),
                ('set_print_length', POST, self._handle_set_print_length),
                ('jog', POST, self._handle_jog),
                ('set_seam', POST, self._handle_set_seam),
                ('reset', POST, self._handle_reset),
                ('settings', GET_POST, self._handle_settings),
                ('mats', GET_POST, self._handle_mats),
                ('stop_after', POST, self._handle_stop_after),
                ('clear_mat_swap', POST, self._handle_clear_mat_swap),
                ('file_mats', POST, self._handle_file_mats),
        ):
            self.server.register_endpoint(
                '/machine/infinite_heatbed/' + path, rtype, cb)

        self.server.register_notification(NOTIFY_EVENT)
        self.server.register_event_handler(
            'server:klippy_ready', self._on_klippy_ready)
        self.server.register_event_handler(
            'server:status_update', self._on_status_update)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def component_init(self):
        db = self.server.lookup_component('database')
        try:
            db.register_local_namespace(DB_NAMESPACE)
        except Exception:
            pass
        mats = await _maybe_await(db.get_item(DB_NAMESPACE, 'mats', None))
        if isinstance(mats, list) and mats:
            self.mats = mats
        active = await _maybe_await(db.get_item(DB_NAMESPACE, 'active_mat', None))
        if active:
            self.active_mat = active
        settings = await _maybe_await(db.get_item(DB_NAMESPACE, 'settings', None))
        if isinstance(settings, dict):
            self.settings = settings
        self._ensure_ui_links()
        self._link_task = self.server.get_event_loop().create_task(self._relink_loop())

    async def close(self):
        if self._link_task is not None:
            self._link_task.cancel()

    async def _relink_loop(self):
        # Mainsail/Fluidd updates replace their folder; put the link back.
        while True:
            await asyncio.sleep(300.)
            self._ensure_ui_links()

    def _ensure_ui_links(self):
        if not os.path.isdir(self.ui_path):
            return
        for root in self.web_roots:
            if not os.path.isdir(root):
                continue
            dst = os.path.join(root, 'ihb')
            try:
                if os.path.islink(dst):
                    if os.readlink(dst) == self.ui_path:
                        continue
                    os.remove(dst)
                elif os.path.exists(dst):
                    continue          # a real folder: leave it alone
                os.symlink(self.ui_path, dst)
                self.logger.info("IHB: linked UI into %s", dst)
            except OSError as e:
                self.logger.warning("IHB: could not link UI into %s: %s", dst, e)

    async def _save(self, key, value):
        db = self.server.lookup_component('database')
        await _maybe_await(db.insert_item(DB_NAMESPACE, key, value))

    async def _gcode(self, script):
        kapis = self.server.lookup_component('klippy_apis')
        return await kapis.run_gcode(script)

    async def _on_klippy_ready(self):
        kapis = self.server.lookup_component('klippy_apis')
        try:
            result = await kapis.subscribe_objects({'infinite_heatbed': None})
            self.ihb = dict(result.get('infinite_heatbed', {}))
        except Exception as e:
            self.logger.warning("IHB: subscribe failed: %s", e)
            return
        self.last_seq = self.ihb.get('eject_seq')
        try:
            await self._push_settings()
            await self._push_mat()
        except Exception as e:
            self.logger.warning("IHB: could not push settings to Klipper: %s", e)

    async def _push_settings(self):
        if not self.settings:
            return
        args = " ".join("%s=%s" % (k.upper(), v) for k, v in self.settings.items()
                        if k in SETTINGS)
        if args:
            await self._gcode("HEATBED_SET_PARAMS SILENT=1 " + args)

    def _active_mat_entry(self):
        for m in self.mats:
            if m.get('name') == self.active_mat:
                return m
        return None

    async def _push_mat(self):
        m = self._active_mat_entry() or {}
        await self._gcode(
            'HEATBED_SET_MAT SILENT=1 NAME="%s" RELEASE_TEMP=%.1f Z_OFFSET=%.3f MESH="%s"'
            % (_clean(m.get('name')), float(m.get('release_temp') or 0),
               float(m.get('z_offset') or 0), _clean(m.get('mesh'))))

    def _server_state(self):
        return {
            'stop_after': self.stop_after,
            'mat_swap': self.mat_swap,
            'active_mat': self.active_mat,
            'mats': self.mats,
            'settings': self.settings,
            'notice': self.last_notice,
            'ui_theme': self.ui_theme,
        }

    def _notify(self, notice=None):
        if notice is not None:
            self.last_notice = notice
        self.server.send_event(NOTIFY_EVENT, self._server_state())

    # ── Queue gating after each eject ──────────────────────────────────────────

    async def _on_status_update(self, status, *args):
        diff = status.get('infinite_heatbed') if isinstance(status, dict) else None
        if not diff:
            return
        self.ihb.update(diff)
        seq = self.ihb.get('eject_seq')
        if seq is None:
            return
        if self.last_seq is None:
            self.last_seq = seq
            return
        if seq != self.last_seq:
            self.last_seq = seq
            try:
                await self._after_eject()
            except Exception:
                self.logger.exception("IHB: queue handling after eject failed")

    def _queued_files(self, jq):
        jobs = getattr(jq, 'queued_jobs', {}) or {}
        return [getattr(j, 'filename', None) for j in jobs.values()]

    async def _pause_queue(self, jq):
        if jq is not None:
            try:
                await jq.pause_queue()
            except Exception as e:
                self.logger.warning("IHB: could not pause job queue: %s", e)

    async def _after_eject(self):
        jq = self.server.lookup_component('job_queue', None)
        if self.ihb.get('last_result') != 'ok':
            await self._pause_queue(jq)
            self._notify("Eject failed: %s. Queue paused."
                         % (self.ihb.get('last_error') or 'unknown error'))
            return
        if self.stop_after:
            self.stop_after = False
            await self._pause_queue(jq)
            self._notify("Stopped after this plate. Queue paused.")
            return
        if jq is None:
            return
        if getattr(jq, 'queue_state', '') == 'paused':
            self._notify("Eject complete. Queue is paused.")
            return
        # Only continue the queue when this eject followed a finished print
        # (a manual "Eject now" on an idle printer must not start a job).
        try:
            kapis = self.server.lookup_component('klippy_apis')
            ps = await kapis.query_objects({'print_stats': ['state']})
            last_print = ps.get('print_stats', {}).get('state')
        except Exception:
            last_print = None
        if last_print != 'complete':
            self._notify("Eject complete.")
            return
        files = [f for f in self._queued_files(jq) if f]
        if not files:
            self._notify("Eject complete. Queue is empty.")
            return
        needed = await self._mat_for_file(files[0])
        if needed and needed['name'] != self.active_mat:
            self.mat_swap = {'filename': files[0], 'mat': needed['name']}
            await self._pause_queue(jq)
            self._notify("Swap the heat mat to %s, then start the queue."
                         % needed['name'])
            return
        self.mat_swap = None
        self._notify("Eject complete. Starting %s." % files[0])
        await jq.start_queue()

    # ── Plate / mat detection ──────────────────────────────────────────────────

    async def _file_plate(self, filename):
        fm = self.server.lookup_component('file_manager')
        try:
            path = os.path.join(fm.get_directory('gcodes'), filename)
            mtime = os.path.getmtime(path)
        except Exception:
            return ''
        cached = self._plate_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        loop = asyncio.get_running_loop()
        plate = await loop.run_in_executor(None, _scan_plate, path)
        self._plate_cache[path] = (mtime, plate)
        return plate

    def _mat_for_plate(self, plate):
        if not plate:
            return None
        p = plate.strip().lower()
        for m in self.mats:
            if p in (str(m.get('slicer_plate', '')).strip().lower(),
                     str(m.get('name', '')).strip().lower()):
                return m
        return None

    async def _mat_for_file(self, filename):
        return self._mat_for_plate(await self._file_plate(filename))

    # ── REST / JSON-RPC handlers ───────────────────────────────────────────────

    async def _handle_status(self, web_request):
        kapis = self.server.lookup_component('klippy_apis')
        try:
            result = await kapis.query_objects({'infinite_heatbed': None})
            ihb = result.get('infinite_heatbed', {})
        except Exception:
            ihb = {'state': 'unavailable'}
        jq = self.server.lookup_component('job_queue', None)
        queue_len = len(self._queued_files(jq)) if jq is not None else 0
        return dict(ihb, queue_length=queue_len, server=self._server_state())

    async def _handle_eject(self, web_request):
        await self._gcode('HEATBED_EJECT')
        return {'result': 'ok'}

    async def _handle_set_print_length(self, web_request):
        length = web_request.get_float('print_length')
        await self._gcode('HEATBED_PRINT_START PRINT_LENGTH=%.3f' % length)
        return {'result': 'ok'}

    async def _handle_jog(self, web_request):
        dist = web_request.get_float('distance')
        speed = web_request.get_float('speed', 0.)
        cmd = 'HEATBED_JOG DISTANCE=%.3f' % dist
        if speed > 0:
            cmd += ' SPEED=%.3f' % speed
        await self._gcode(cmd)
        return {'result': 'ok'}

    async def _handle_set_seam(self, web_request):
        await self._gcode('HEATBED_SET_SEAM')
        return {'result': 'ok'}

    async def _handle_reset(self, web_request):
        force = web_request.get_boolean('force', False)
        await self._gcode('HEATBED_RESET FORCE=%d' % int(force))
        return {'result': 'ok'}

    async def _handle_settings(self, web_request):
        if _is_post(web_request):
            new = {}
            for key, (conv, lo, hi) in SETTINGS.items():
                raw = web_request.get(key, None)
                if raw is None:
                    continue
                try:
                    val = conv(float(raw))
                except (TypeError, ValueError):
                    raise self.server.error("Invalid value for %s" % key, 400)
                if not lo <= val <= hi:
                    raise self.server.error(
                        "%s must be between %s and %s" % (key, lo, hi), 400)
                new[key] = val
            if new:
                self.settings.update(new)
                await self._save('settings', self.settings)
                await self._gcode("HEATBED_SET_PARAMS SILENT=1 " + " ".join(
                    "%s=%s" % (k.upper(), v) for k, v in new.items()))
                self._notify()
        return {'settings': self.settings}

    async def _handle_mats(self, web_request):
        if _is_post(web_request):
            mats = web_request.get('mats', None)
            active = web_request.get('active', None)
            if mats is not None:
                if not isinstance(mats, list) or not mats:
                    raise self.server.error("mats must be a non-empty list", 400)
                cleaned, seen = [], set()
                for m in mats:
                    name = _clean(m.get('name'))
                    if not name or name in seen:
                        raise self.server.error("Each mat needs a unique name", 400)
                    seen.add(name)
                    try:
                        cleaned.append({
                            'name': name,
                            'release_temp': max(0., min(120., float(m.get('release_temp') or 0))),
                            'z_offset': max(-2., min(2., float(m.get('z_offset') or 0))),
                            'mesh': _clean(m.get('mesh')),
                            'slicer_plate': _clean(m.get('slicer_plate'), 64),
                        })
                    except (TypeError, ValueError):
                        raise self.server.error("Invalid number for mat %s" % name, 400)
                self.mats = cleaned
                await self._save('mats', self.mats)
                if self._active_mat_entry() is None:
                    active = active or self.mats[0]['name']
            if active is not None:
                if active not in [m['name'] for m in self.mats]:
                    raise self.server.error("Unknown mat %s" % active, 400)
                self.active_mat = active
                await self._save('active_mat', active)
                if self.mat_swap and self.mat_swap.get('mat') == active:
                    self.mat_swap = None
            await self._push_mat()
            self._notify()
        return {'mats': self.mats, 'active': self.active_mat}

    async def _handle_stop_after(self, web_request):
        self.stop_after = web_request.get_boolean('enable', True)
        self._notify()
        return {'stop_after': self.stop_after}

    async def _handle_clear_mat_swap(self, web_request):
        self.mat_swap = None
        self._notify('')
        return {'result': 'ok'}

    async def _handle_file_mats(self, web_request):
        filenames = web_request.get('filenames', [])
        if not isinstance(filenames, list):
            raise self.server.error("filenames must be a list", 400)
        out = {}
        for fn in filenames[:200]:
            plate = await self._file_plate(str(fn))
            mat = self._mat_for_plate(plate)
            out[fn] = {'plate': plate, 'mat': mat['name'] if mat else None}
        return {'files': out}


def load_component(config):
    return InfiniteHeatbedServer(config)
