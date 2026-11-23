/*
 * Voron Infinite Heatbed — web UI
 * Talks to Moonraker over its JSON-RPC websocket (same host as Mainsail).
 *   ?host=192.168.1.50:7125  connect to another Moonraker (development)
 *   ?demo=1                  run against a built-in fake printer
 */
'use strict';
(function () {
  const $ = (id) => document.getElementById(id);
  const qs = new URLSearchParams(location.search);
  const EMBED = qs.has('embed');

  // Theme: ?theme=fluidd|mainsail, else the ui_theme set at install
  // (sent by the Moonraker component), else the Mainsail-style default.
  function applyTheme(theme) {
    if (qs.get('theme')) theme = qs.get('theme');
    theme = theme === 'fluidd' ? 'fluidd' : 'mainsail';
    document.documentElement.dataset.theme = theme;
    const full = document.getElementById('open-full');
    if (full) full.href = './' + (theme === 'fluidd' ? '?theme=fluidd' : '');
  }
  applyTheme(null);
  if (EMBED) document.documentElement.classList.add('embed');

  // ── Small helpers ──────────────────────────────────────────────────────────
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 0) => (typeof v === 'number' && isFinite(v) ? v : d);
  const fmtNum = (v, dp = 1) => num(v).toFixed(dp).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  const fmtDur = (sec) => {
    sec = Math.max(0, Math.round(num(sec)));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h + ':' + String(m).padStart(2, '0');
  };
  const basename = (p) => String(p || '').split('/').pop();
  const store = {
    get(k, d) { try { const v = localStorage.getItem('ihb.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('ihb.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };

  let toastTimer = null;
  function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (isErr ? ' err' : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 2500);
  }
  function setConn(msg) {
    const c = $('conn');
    c.textContent = msg || '';
    c.hidden = !msg;
  }

  // ── Moonraker JSON-RPC client ──────────────────────────────────────────────
  class Moonraker {
    constructor(url) {
      this.url = url; this.id = 0; this.pending = new Map(); this.handlers = {};
      this.ws = null; this.retry = 1000;
    }
    on(method, fn) { (this.handlers[method] = this.handlers[method] || []).push(fn); }
    emit(method, params) { (this.handlers[method] || []).forEach((fn) => fn(params)); }
    connect() {
      const ws = new WebSocket(this.url);
      this.ws = ws;
      ws.onopen = () => { this.retry = 1000; this.emit('_open'); };
      ws.onclose = () => {
        this.pending.forEach((p) => p.reject(new Error('Disconnected')));
        this.pending.clear();
        this.emit('_close');
        setTimeout(() => this.connect(), this.retry);
        this.retry = Math.min(this.retry * 2, 10000);
      };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.id !== undefined && this.pending.has(msg.id)) {
          const p = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) p.reject(new Error(msg.error.message || 'Request failed'));
          else p.resolve(msg.result);
        } else if (msg.method) {
          this.emit(msg.method, msg.params || []);
        }
      };
    }
    call(method, params) {
      return new Promise((resolve, reject) => {
        if (!this.ws || this.ws.readyState !== 1) { reject(new Error('Not connected')); return; }
        const id = ++this.id;
        this.pending.set(id, { resolve, reject });
        this.ws.send(JSON.stringify({ jsonrpc: '2.0', method, params: params || {}, id }));
      });
    }
  }

  // ── State ──────────────────────────────────────────────────────────────────
  const S = {
    printer: {},            // subscribed Klipper objects
    hasIHB: false,
    tmcKeys: [],
    server: null,           // Moonraker component state (mats, settings, …)
    serverMissing: false,
    queue: { queued_jobs: [], queue_state: 'ready' },
    history: [],
    meta: {},               // filename -> metadata
    fileMats: {},           // filename -> {plate, mat}
    clearedBefore: store.get('clearedBefore', 0),
    coolStart: null,
    editingMat: null,       // index, -1 for new, null when closed
    sigs: {},               // last render signature per section
  };
  let mr = null;

  const ihb = () => S.printer.infinite_heatbed || {};
  const params = () => ihb().params || {};
  const printState = () => (S.printer.print_stats || {}).state || 'standby';
  const isPrinting = () => ['printing', 'paused'].includes(printState());
  const isEjecting = () => ['ejecting', 'door_open'].includes(ihb().state);

  async function call(method, p, quiet) {
    try {
      return await mr.call(method, p);
    } catch (e) {
      if (!quiet) toast(e.message, true);
      throw e;
    }
  }
  const gcode = (script) => call('printer.gcode.script', { script });

  // ── Loading ────────────────────────────────────────────────────────────────
  async function subscribe() {
    const list = await mr.call('printer.objects.list');
    const objs = list.objects || [];
    S.hasIHB = objs.includes('infinite_heatbed');
    S.tmcKeys = objs.filter((o) => /^tmc\w+ manual_stepper infinite_heatbed_motor\d$/.test(o));
    const objects = {
      print_stats: null,
      virtual_sdcard: ['progress', 'is_active'],
      heater_bed: ['temperature', 'target'],
    };
    if (S.hasIHB) objects.infinite_heatbed = null;
    S.tmcKeys.forEach((k) => { objects[k] = ['run_current']; });
    const res = await mr.call('printer.objects.subscribe', { objects });
    S.printer = res.status || {};
    setConn(S.hasIHB ? '' : 'The infinite_heatbed module is not loaded in Klipper.');
  }

  async function loadServer() {
    try {
      const res = await mr.call('machine.infinite_heatbed.status');
      S.server = res.server || null;
      S.serverMissing = !S.server;
      if (S.server) applyTheme(S.server.ui_theme);
    } catch (e) {
      S.server = null;
      S.serverMissing = true;
    }
  }

  async function loadQueue() {
    try {
      S.queue = await mr.call('server.job_queue.status');
    } catch (e) {
      S.queue = { queued_jobs: [], queue_state: 'unavailable' };
    }
    await loadFileInfo((S.queue.queued_jobs || []).map((j) => j.filename));
    render();
  }

  async function loadHistory() {
    try {
      const res = await mr.call('server.history.list', { limit: 15, order: 'desc' });
      S.history = res.jobs || [];
      S.history.forEach((j) => { if (j.metadata && !S.meta[j.filename]) S.meta[j.filename] = j.metadata; });
    } catch (e) {
      S.history = [];
    }
    const current = (S.printer.print_stats || {}).filename;
    await loadFileInfo(S.history.map((j) => j.filename).concat(current ? [current] : []));
    render();
  }

  async function loadFileInfo(files) {
    const need = [...new Set(files.filter(Boolean))];
    await Promise.all(need.filter((f) => !S.meta[f]).map(async (f) => {
      try { S.meta[f] = await mr.call('server.files.metadata', { filename: f }); }
      catch (e) { S.meta[f] = {}; }
    }));
    const unknown = need.filter((f) => !(f in S.fileMats));
    if (unknown.length && !S.serverMissing) {
      try {
        const res = await mr.call('machine.infinite_heatbed.file_mats', { filenames: unknown });
        Object.assign(S.fileMats, res.files || {});
      } catch (e) { /* component missing */ }
    }
  }

  async function init() {
    setConn('');
    try {
      const info = await mr.call('server.info');
      if (info.klippy_state === 'ready') await subscribe();
      else setConn('Klipper is ' + info.klippy_state + '.');
    } catch (e) {
      setConn('Klipper is not ready: ' + e.message);
    }
    await loadServer();
    await Promise.all([loadQueue(), loadHistory()]);
    render();
  }

  function mergeStatus(diff) {
    Object.keys(diff || {}).forEach((key) => {
      const cur = S.printer[key] || (S.printer[key] = {});
      Object.keys(diff[key]).forEach((f) => {
        const v = diff[key][f];
        cur[f] = (v && typeof v === 'object' && !Array.isArray(v) && cur[f] && typeof cur[f] === 'object')
          ? Object.assign({}, cur[f], v) : v;
      });
    });
  }

  function wire() {
    mr.on('_open', init);
    mr.on('_close', () => setConn('Disconnected from Moonraker. Reconnecting…'));
    mr.on('notify_status_update', (p) => {
      const before = (S.printer.print_stats || {}).filename;
      mergeStatus(p[0]);
      const after = (S.printer.print_stats || {}).filename;
      if (after && after !== before) loadFileInfo([after]).then(render);
      render();
    });
    mr.on('notify_klippy_ready', () => { subscribe().then(render).catch(() => {}); });
    mr.on('notify_klippy_shutdown', () => setConn('Klipper shut down. Check the Mainsail console.'));
    mr.on('notify_klippy_disconnected', () => setConn('Klipper disconnected.'));
    mr.on('notify_job_queue_changed', () => loadQueue());
    mr.on('notify_history_changed', () => loadHistory());
    mr.on('notify_ihb_server_update', (p) => {
      S.server = p[0]; S.serverMissing = false; applyTheme(S.server.ui_theme); render();
    });
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  let queued = false;
  function render() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      const rows = queueRows();
      renderNow(rows);
      renderBelt();
      renderQueue(rows);
      renderMats();
      renderSettings();
    });
  }
  // Rebuild a section only when its data changed (keeps hover/focus stable)
  function changed(key, data) {
    const sig = JSON.stringify(data);
    if (S.sigs[key] === sig) return false;
    S.sigs[key] = sig;
    return true;
  }

  function progress() {
    const vs = S.printer.virtual_sdcard || {};
    return num(vs.progress);
  }

  // ── Now printing ──
  function renderNow(rows) {
    const ps = S.printer.print_stats || {};
    const printing = isPrinting();
    const prog = progress();
    const done = rows.filter((r) => r.kind === 'done').length;
    const total = rows.length;
    const plateNo = done + 1;

    let eyebrow;
    if (printing) eyebrow = (ps.state === 'paused' ? 'Paused' : 'Now printing') + ' · plate ' + plateNo + ' of ' + total;
    else if (isEjecting()) eyebrow = 'Ejecting';
    else eyebrow = 'Printer ' + printState();
    $('now-eyebrow').textContent = eyebrow;
    $('now-file').textContent = (printing || isEjecting()) && ps.filename ? ps.filename : 'No active print';
    $('now-bar').style.width = (printing ? prog * 100 : 0) + '%';
    $('now-progress').textContent = printing ? Math.round(prog * 100) + '%' : '–';
    const info = ps.info || {};
    $('now-layer').textContent = printing && info.total_layer ? (info.current_layer || 0) + '/' + info.total_layer : '–';
    let remaining = 0;
    if (printing && prog > 0.01) remaining = num(ps.print_duration) / prog - num(ps.print_duration);
    $('now-eta').textContent = printing ? fmtDur(remaining) : '–';
    const queueLeft = remaining + rows.filter((r) => r.kind === 'queued')
      .reduce((a, r) => a + num((S.meta[r.filename] || {}).estimated_time), 0);
    $('now-queue').textContent = queueLeft > 0 ? fmtDur(queueLeft) : '–';

    // one-line summary for the compact embed view
    const waiting = rows.filter((r) => r.kind === 'queued').length;
    $('embed-print').textContent = printing
      ? (ps.state === 'paused' ? 'Paused ' : 'Printing ') + basename(ps.filename) + ' · ' + Math.round(prog * 100) +
        '% · plate ' + plateNo + ' of ' + total
      : (isEjecting() ? 'Ejecting' : 'Printer ' + printState()) + (waiting ? ' · ' + waiting + ' plate' + (waiting > 1 ? 's' : '') + ' queued' : '');

    const pause = $('btn-pause');
    pause.textContent = ps.state === 'paused' ? 'Resume' : 'Pause';
    pause.disabled = !printing;
    const stop = $('btn-stop-after');
    const stopOn = !!(S.server && S.server.stop_after);
    stop.classList.toggle('on', stopOn);
    stop.textContent = stopOn ? 'Stopping after this plate' : 'Stop after this plate';
    stop.disabled = !S.server;
  }

  // ── Belt bed ──
  const PLATE_X0 = 150, PLATE_X1 = 650;
  function seamPoint(frac) {
    // walk the drawn belt loop clockwise from the plate entry edge (150,150)
    const r = 48, segs = [540, Math.PI * r, 580, Math.PI * r, 40];
    const total = segs.reduce((a, b) => a + b, 0);
    let d = (((frac % 1) + 1) % 1) * total;
    if (d < segs[0]) return [150 + d, 150];
    d -= segs[0];
    if (d < segs[1]) { const a = -Math.PI / 2 + d / r; return [690 + r * Math.cos(a), 198 + r * Math.sin(a)]; }
    d -= segs[1];
    if (d < segs[2]) return [690 - d, 246];
    d -= segs[2];
    if (d < segs[3]) { const a = Math.PI / 2 + d / r; return [110 + r * Math.cos(a), 198 + r * Math.sin(a)]; }
    d -= segs[3];
    return [110 + d, 150];
  }

  function renderBelt() {
    const h = ihb(), p = params();
    const busy = isEjecting();
    const state = h.state || 'unavailable';

    const badge = $('belt-badge');
    const labels = { printing: 'Holding · printing', idle: 'Idle', error: 'Error', unavailable: 'Not loaded' };
    badge.textContent = busy ? 'Ejecting · ' + (h.phase || '') : (labels[state] || state);
    badge.className = 'badge ' + (busy ? 'ejecting' : state);

    // ruler + plate
    const plateLen = num(p.plate_length_mm) || 300;
    if (changed('ruler', plateLen)) {
      const ticks = [0, plateLen / 2, plateLen];
      $('ruler').innerHTML =
        '<line x1="' + PLATE_X0 + '" y1="34" x2="' + PLATE_X1 + '" y2="34"/>' +
        ticks.map((t, i) => {
          const x = PLATE_X0 + (PLATE_X1 - PLATE_X0) * (i / 2);
          return '<line x1="' + x + '" y1="28" x2="' + x + '" y2="40"/>' +
            '<text x="' + x + '" y="60" text-anchor="middle">' + Math.round(t) + (i === 2 ? ' mm' : '') + '</text>';
        }).join('');
    }
    const bed = S.printer.heater_bed || {};
    $('plate-zone').classList.toggle('cool', !(num(bed.target) > 0));
    $('plate-label').textContent = 'heated plate · ' + (S.server ? S.server.active_mat || 'magnetic mat zone' : 'magnetic mat zone') +
      (bed.temperature != null ? ' · ' + Math.round(bed.temperature) + '°C' : '');

    const target = busy ? num(h.eject_target_mm) : num(h.eject_distance_mm);
    const travelled = busy || h.phase === 'done' ? num(h.eject_travelled_mm) : 0;
    $('belt-travel').textContent = fmtNum(travelled, 0) + ' / ' + fmtNum(target, 0) + ' mm';
    $('belt-mini').textContent = 'Belt ' + fmtNum(travelled, 0) + ' / ' + fmtNum(target, 0) + ' mm this eject · ' +
      (S.server && S.server.active_mat ? S.server.active_mat : 'no mat') +
      (bed.temperature != null ? ' · bed ' + Math.round(bed.temperature) + '°C' : '');

    // seam
    const dot = $('seam-dot');
    const loop = num(p.belt_loop_length_mm);
    if (h.seam_travel_mm == null || !loop) {
      dot.setAttribute('cx', 150); dot.setAttribute('cy', 150);
      dot.classList.add('unknown');
    } else {
      const [x, y] = seamPoint(h.seam_travel_mm / loop);
      dot.setAttribute('cx', x.toFixed(1)); dot.setAttribute('cy', y.toFixed(1));
      dot.classList.remove('unknown');
    }

    // tiles
    $('t-pos').textContent = fmtNum(h.belt_position_mm, 1) + ' mm';
    const seam = $('t-seam');
    if (h.seam_travel_mm == null || !loop) { seam.textContent = 'not set'; seam.className = ''; }
    else {
      seam.textContent = fmtNum(h.seam_travel_mm, 0) + ' mm' + (h.seam_on_plate ? ' · on plate' : '');
      seam.className = h.seam_on_plate ? 'warn' : '';
    }
    $('t-cycles').textContent = h.eject_cycles != null ? h.eject_cycles : '–';
    $('t-last').textContent = h.last_eject_s ? fmtNum(h.last_eject_s, 1) + ' s' : '–';
    const amps = S.tmcKeys.map((k) => (S.printer[k] || {}).run_current).filter((a) => a != null);
    const motor = $('t-motor');
    motor.textContent = amps.length ? amps.map((a) => a.toFixed(2)).join(' / ') + ' A' : '–';
    motor.className = amps.length ? 'ok' : '';

    renderEject(h, p);

    // buttons
    const printing = isPrinting();
    const blocked = !S.hasIHB || printing || busy;
    if (changed('jog', blocked)) {
      $('jog-btns').innerHTML = [-50, -10, -1, 1, 10, 50].map((d) =>
        '<button class="btn" data-jog="' + d + '"' + (blocked ? ' disabled' : '') + '>' +
        (d > 0 ? '+' : '−') + Math.abs(d) + '</button>').join('');
    }
    $('btn-seam').disabled = blocked;
    $('btn-eject').disabled = blocked || state === 'error';
    $('btn-reset').hidden = !(state === 'error' || busy);
    $('btn-reset').textContent = busy ? 'Abort eject' : 'Reset error';
  }

  const PHASE_STEP = { cooling: 0, lifting: 1, door: 2, belt: 2, confirming: 2, closing: 3, done: 4 };
  function renderEject(h, p) {
    const busy = isEjecting();
    const phase = h.phase || 'idle';
    const rt = num(h.release_temp), lift = num(p.z_lift_mm);
    const dist = busy ? num(h.eject_target_mm) : num(h.eject_distance_mm);
    const bed = S.printer.heater_bed || {};

    if (phase === 'cooling') {
      if (S.coolStart == null) S.coolStart = num(bed.temperature);
    } else {
      S.coolStart = null;
    }
    const at = phase === 'failed' ? -1 : (PHASE_STEP[phase] != null ? PHASE_STEP[phase] : -1);
    const fill = [0, 0, 0, 0];
    for (let i = 0; i < 4; i++) {
      if (phase === 'done' || i < at) fill[i] = 1;
    }
    if (at === 0 && S.coolStart != null && S.coolStart > rt) {
      fill[0] = Math.min(1, Math.max(0, (S.coolStart - num(bed.temperature)) / (S.coolStart - rt)));
    }
    if (at === 1) fill[1] = 0.5;
    if (at === 2) fill[2] = num(h.eject_progress);
    if (at === 3) fill[3] = 0.5;
    if (!busy && phase !== 'done') fill.fill(0);

    const steps = [
      { label: rt > 0 ? 'Cool bed to ' + Math.round(rt) + '°C' : 'Cool bed (off)', skip: !(rt > 0), w: 2 },
      { label: lift > 0 ? 'Lift Z +' + fmtNum(lift, 0) : 'Lift Z (off)', skip: !(lift > 0), w: 1 },
      { label: 'Run belt ' + fmtNum(dist, 0) + ' mm', skip: false, w: 4 },
      { label: 'Done → next', skip: false, w: 1 },
    ];
    let wsum = 0, wdone = 0;
    steps.forEach((s, i) => { if (!s.skip || fill[i]) { wsum += s.w; wdone += s.w * fill[i]; } });
    $('eject-pct').textContent = Math.round(wsum ? (wdone / wsum) * 100 : 0) + '%';

    const html = steps.map((s, i) => {
      const cls = ['step'];
      if (s.skip && !busy) cls.push('skip');
      if (fill[i] >= 1) cls.push('done');
      if (busy && i === at) cls.push('active');
      return '<div class="' + cls.join(' ') + '"><div class="track"><div class="fill" style="width:' +
        (fill[i] * 100).toFixed(1) + '%"></div></div><label>' + esc(s.label) + '</label></div>';
    }).join('');
    if (changed('eject', html)) $('eject-steps').innerHTML = html;

    let note = '';
    if (busy) note = { cooling: 'Waiting for the bed to cool', lifting: 'Lifting the toolhead', door: 'Opening the door',
      belt: 'Running the belt', confirming: 'Checking the bed is clear', closing: 'Closing the door' }[phase] || phase;
    else if (isPrinting()) note = 'Starts when this plate finishes';
    else if (phase === 'done' && h.last_result === 'ok') note = 'Last eject complete';
    else if (h.state === 'error') note = 'Eject failed';
    $('eject-note').textContent = note;

    const err = $('eject-error');
    err.hidden = !(h.state === 'error' && h.last_error);
    err.textContent = h.last_error ? 'Eject failed: ' + h.last_error + '. Check the bed is clear, then press Reset error.' : '';
  }

  // ── Queue ──
  function queueRows() {
    const rows = [];
    const cleared = S.clearedBefore;
    S.history.slice().reverse().forEach((j) => {
      if (j.status === 'in_progress' || num(j.end_time) <= cleared) return;
      rows.push({ kind: 'done', filename: j.filename, status: j.status });
    });
    const ps = S.printer.print_stats || {};
    if (isPrinting() && ps.filename) {
      rows.push({ kind: 'current', filename: ps.filename });
    } else if (isEjecting() && rows.length && rows[rows.length - 1].kind === 'done') {
      // the plate that just finished is the one being ejected
      rows[rows.length - 1].ejecting = true;
    }
    (S.queue.queued_jobs || []).forEach((j) => rows.push({ kind: 'queued', filename: j.filename, id: j.job_id }));

    // walk the queue to find jobs that need a mat swap
    let mat = S.server ? S.server.active_mat : null;
    let firstQueued = true;
    rows.forEach((r) => {
      const fm = S.fileMats[r.filename] || {};
      r.mat = fm.mat || null;
      r.plate = fm.plate || '';
      if (r.kind === 'current' && r.mat) mat = r.mat;
      if (r.kind !== 'queued') return;
      r.swap = !!(mat && r.mat && r.mat !== mat);
      if (r.mat) mat = r.mat;
      r.next = firstQueued;
      firstQueued = false;
    });
    return rows;
  }

  function statusChip(r) {
    if (r.kind === 'done') {
      if (r.ejecting) return '<span class="status next">Ejecting</span>';
      if (r.status === 'completed') return '<span class="status done">Done ✓</span>';
      if (r.status === 'cancelled') return '<span class="status cancelled">Cancelled</span>';
      return '<span class="status failed">Failed</span>';
    }
    if (r.kind === 'current') {
      if (isEjecting()) return '<span class="status next">Ejecting</span>';
      return '<span class="status printing">' + (printState() === 'paused' ? 'Paused ' : 'Printing ') +
        Math.round(progress() * 100) + '%</span>';
    }
    if (r.swap) return '<span class="status swap">Mat swap</span>';
    if (r.next) return '<span class="status next">Next</span>';
    return '<span class="status waiting">Waiting</span>';
  }

  function thumbHtml(filename) {
    const m = S.meta[filename] || {};
    const th = (m.thumbnails || []).slice().sort((a, b) => num(a.width) - num(b.width))
      .find((t) => num(t.width) >= 32) || (m.thumbnails || [])[0];
    if (th && th.relative_path) {
      const dir = filename.includes('/') ? filename.slice(0, filename.lastIndexOf('/') + 1) : '';
      const url = '/server/files/gcodes/' + (dir + th.relative_path).split('/').map(encodeURIComponent).join('/');
      return '<img class="thumb" loading="lazy" alt="" src="' + esc(url) + '">';
    }
    const color = (m.filament_colors || [])[0] || '#2a2b31';
    return '<span class="thumb" style="background:' + esc(color) + '"></span>';
  }

  function renderQueue(rows) {
    const qstate = S.queue.queue_state;
    const queuedCount = rows.filter((r) => r.kind === 'queued').length;
    const doneCount = rows.filter((r) => r.kind === 'done').length;
    const left = rows.filter((r) => r.kind !== 'done')
      .reduce((a, r) => a + num((S.meta[r.filename] || {}).estimated_time), 0);
    const active = isPrinting() || isEjecting();

    const view = {
      rows: rows.map((r) => [r.kind, r.filename, r.id, r.status, r.mat, r.plate, r.swap, r.next, r.ejecting,
        r.kind === 'current' ? Math.round(progress() * 100) : 0, isEjecting(), printState()]),
      meta: rows.map((r) => !!S.meta[r.filename]),
      qstate, server: S.server && [S.server.mat_swap, S.server.notice, S.server.active_mat],
      missing: S.serverMissing,
    };
    if (!changed('queue', view)) return;

    $('q-summary').textContent = rows.length + ' plates · ' + doneCount + ' done' + (left ? ' · ' + fmtDur(left) + ' left' : '');
    const qbtn = $('btn-queue');
    if (active && qstate !== 'paused') { qbtn.textContent = 'Pause queue'; qbtn.dataset.act = 'pause'; qbtn.disabled = false; }
    else {
      qbtn.textContent = qstate === 'paused' ? 'Resume queue' : 'Start queue';
      qbtn.dataset.act = 'start';
      qbtn.disabled = qstate === 'unavailable' || (!queuedCount && qstate !== 'paused');
    }
    $('btn-clear').disabled = !doneCount;

    // banners
    const banners = [];
    if (S.serverMissing) {
      banners.push('<div class="banner warn">The Moonraker component is not loaded. Heat mats, eject settings and mat-swap pausing need it. See the README.</div>');
    }
    if (qstate === 'unavailable') {
      banners.push('<div class="banner warn">Moonraker\'s job queue is not enabled. Add a [job_queue] section to moonraker.conf.</div>');
    }
    const swap = S.server && S.server.mat_swap;
    if (swap) {
      banners.push('<div class="banner warn"><span>⚠ Queue paused: put the <b>' + esc(swap.mat) + '</b> mat on the bed for ' +
        esc(basename(swap.filename)) + '.</span><button class="btn" data-swap="' + esc(swap.mat) + '">Mat is on · start queue</button></div>');
    } else {
      const idx = rows.findIndex((r) => r.swap);
      if (idx >= 0) {
        banners.push('<div class="banner warn"><span>⚠ Plate ' + (idx + 1) + ' needs <b>' + esc(rows[idx].mat) +
          '</b>. The queue will pause for a mat swap before it starts.</span></div>');
      }
    }
    if (S.server && S.server.notice) {
      banners.push('<div class="banner info">' + esc(S.server.notice) + '</div>');
    }
    $('q-banners').innerHTML = banners.join('');

    const firstQueued = rows.findIndex((r) => r.kind === 'queued');
    $('q-rows').innerHTML = rows.map((r, i) => {
      const m = S.meta[r.filename] || {};
      const fil = [m.filament_type || m.filament_name, m.filament_weight_total ? Math.round(m.filament_weight_total) + ' g' : '']
        .filter(Boolean).join(' · ') || '–';
      const matLabel = r.mat || (r.plate ? r.plate : 'Any');
      let acts = '';
      if (r.kind === 'queued') {
        const qi = i - firstQueued;
        acts = '<button class="icon-btn" title="Move up" data-move="' + qi + ':-1"' + (qi === 0 ? ' disabled' : '') + '>↑</button>' +
          '<button class="icon-btn" title="Move down" data-move="' + qi + ':1"' + (qi === queuedCount - 1 ? ' disabled' : '') + '>↓</button>' +
          '<button class="icon-btn" title="Remove from queue" data-remove="' + esc(r.id) + '">✕</button>';
      }
      return '<tr class="' + r.kind + '">' +
        '<td class="num">' + (i + 1) + '</td>' +
        '<td class="c-thumb">' + thumbHtml(r.filename) + '</td>' +
        '<td class="file" title="' + esc(r.filename) + '">' + esc(basename(r.filename)) + '</td>' +
        '<td class="c-fil">' + esc(fil) + '</td>' +
        '<td><span class="chip' + (r.swap ? ' mismatch' : '') + '">' + esc(matLabel) + '</span></td>' +
        '<td class="c-time">' + (m.estimated_time ? fmtDur(m.estimated_time) : '–') + '</td>' +
        '<td>' + statusChip(r) + '</td>' +
        '<td class="acts">' + acts + '</td></tr>';
    }).join('');
    $('q-empty').hidden = rows.length > 0;
  }

  // ── Heat mats ──
  function renderMats() {
    const sv = S.server;
    const view = { mats: sv && sv.mats, active: sv && sv.active_mat, editing: S.editingMat, missing: S.serverMissing };
    if (!changed('mats', view)) return;
    $('btn-add-mat').disabled = !sv;
    if (!sv) {
      $('mat-grid').innerHTML = '<p class="muted small">Heat mats need the Moonraker component.</p>';
      $('mat-form').hidden = true;
      return;
    }
    $('mat-grid').innerHTML = (sv.mats || []).map((m, i) => {
      const z = num(m.z_offset);
      return '<div class="mat' + (m.name === sv.active_mat ? ' active' : '') + '" role="button" tabindex="0" data-mat="' + i + '">' +
        '<b>' + esc(m.name) + '</b><span>release ' + Math.round(num(m.release_temp)) + '° · Z ' +
        (z > 0 ? '+' : z < 0 ? '−' : '') + Math.abs(z).toFixed(2) + (m.mesh ? ' · mesh ' + esc(m.mesh) : '') + '</span>' +
        '<button class="icon-btn edit" title="Edit" data-edit="' + i + '">✎</button></div>';
    }).join('');
    const form = $('mat-form');
    form.hidden = S.editingMat == null;
    if (S.editingMat != null) {
      const m = S.editingMat >= 0 ? sv.mats[S.editingMat] : { name: '', release_temp: 30, z_offset: 0, mesh: '', slicer_plate: '' };
      ['name', 'release_temp', 'z_offset', 'mesh', 'slicer_plate'].forEach((k) => { form.elements[k].value = m[k] == null ? '' : m[k]; });
      $('mat-delete').hidden = S.editingMat < 0 || sv.mats.length < 2;
    }
  }

  async function saveMats(mats, active) {
    const body = { mats };
    if (active) body.active = active;
    const res = await call('machine.infinite_heatbed.post_mats', body);
    if (S.server) { S.server.mats = res.mats; S.server.active_mat = res.active; }
    render();
  }

  // ── Settings ──
  const SETTING_ROWS = [
    { key: 'release_temp', label: 'Release temperature', unit: '°C', step: 1, hint: 'from the active heat mat' },
    { key: 'z_lift_mm', label: 'Z lift before eject', unit: 'mm', step: 1 },
    { key: 'eject_distance_mm', label: 'Eject distance', unit: 'mm', step: 10, hint: '0 = print length + margin' },
    { key: 'eject_margin_mm', label: 'Eject margin', unit: 'mm', step: 5 },
    { key: 'belt_speed', label: 'Belt speed', unit: 'mm/s', step: 5 },
    { key: 'eject_retries', label: 'Retries if not clear', unit: '', step: 1 },
  ];
  const ADV_ROWS = [
    { key: 'belt_loop_length_mm', label: 'Belt loop length', unit: 'mm', step: 1, hint: 'full belt circumference' },
    { key: 'plate_length_mm', label: 'Plate length', unit: 'mm', step: 1, hint: 'heated zone along the belt' },
    { key: 'eject_timeout_s', label: 'Sensor timeout', unit: 's', step: 5 },
    { key: 'cool_timeout_s', label: 'Cooling timeout', unit: 's', step: 60 },
  ];

  function settingValue(key) {
    if (key === 'release_temp') return num(ihb().release_temp);
    return params()[key];
  }

  function renderSettings() {
    const box = $('settings');
    if (box.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
    const values = SETTING_ROWS.concat(ADV_ROWS).map((r) => settingValue(r.key));
    if (!changed('settings', [values, params().advance_past_seam, S.hasIHB, !!S.server])) return;
    const disabled = !S.hasIHB || !S.server;
    const row = (r) => {
      const v = settingValue(r.key);
      return '<div class="set-row"><label for="set-' + r.key + '">' + esc(r.label) +
        (r.hint ? '<span class="hint">' + esc(r.hint) + '</span>' : '') + '</label>' +
        '<span class="unit-input"><input id="set-' + r.key + '" data-key="' + r.key + '" type="number" min="0" step="' + r.step + '" value="' +
        (v == null ? '' : esc(+(+v).toFixed(3))) + '"' + (disabled ? ' disabled' : '') + '><span class="unit">' + esc(r.unit) + '</span></span></div>';
    };
    $('set-rows').innerHTML = SETTING_ROWS.map(row).join('');
    $('set-adv').innerHTML = ADV_ROWS.map(row).join('');
    const cb = $('set-seam');
    cb.checked = !!params().advance_past_seam;
    cb.disabled = disabled;
  }

  let savedTimer = null;
  function flashSaved() {
    const s = $('set-saved');
    s.textContent = 'Saved';
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => { s.textContent = ''; }, 1500);
  }

  async function saveSetting(key, raw) {
    const val = Number(raw);
    if (raw === '' || !isFinite(val) || val < 0) { toast('Enter a number of 0 or more', true); S.sigs.settings = null; render(); return; }
    if (key === 'release_temp') {
      const sv = S.server;
      const mats = (sv.mats || []).map((m) => (m.name === sv.active_mat ? Object.assign({}, m, { release_temp: val }) : m));
      await saveMats(mats);
    } else {
      await call('machine.infinite_heatbed.post_settings', { [key]: val });
    }
    flashSaved();
  }

  // ── Actions ────────────────────────────────────────────────────────────────
  function bindActions() {
    $('btn-pause').onclick = () => call(printState() === 'paused' ? 'printer.print.resume' : 'printer.print.pause').catch(() => {});
    $('btn-stop-after').onclick = () => {
      const on = !(S.server && S.server.stop_after);
      call('machine.infinite_heatbed.stop_after', { enable: on })
        .then((r) => { if (S.server) S.server.stop_after = r.stop_after; render(); }).catch(() => {});
    };

    $('jog-btns').onclick = (e) => {
      const b = e.target.closest('[data-jog]');
      if (!b || b.disabled) return;
      gcode('HEATBED_JOG DISTANCE=' + b.dataset.jog).catch(() => {});
    };
    $('btn-seam').onclick = () => {
      if (!confirm('Is the belt seam exactly at the reference mark (the plate\'s entry edge on the idler side)?')) return;
      gcode('HEATBED_SET_SEAM').then(() => toast('Seam position saved')).catch(() => {});
    };
    $('btn-eject').onclick = () => {
      const d = num(ihb().eject_distance_mm);
      if (!confirm('Eject now? The belt will run ' + Math.round(d) + ' mm. Make sure nothing is in the way.')) return;
      gcode('HEATBED_EJECT').catch(() => {});
    };
    $('btn-reset').onclick = () => {
      const busy = isEjecting();
      if (busy && !confirm('Abort the running eject? The belt stops after its current move.')) return;
      gcode('HEATBED_RESET' + (busy ? ' FORCE=1' : '')).catch(() => {});
    };

    // queue
    $('btn-queue').onclick = () => {
      const act = $('btn-queue').dataset.act;
      call(act === 'pause' ? 'server.job_queue.pause' : 'server.job_queue.start').then(loadQueue).catch(() => {});
    };
    $('btn-clear').onclick = () => {
      S.clearedBefore = Date.now() / 1000;
      store.set('clearedBefore', S.clearedBefore);
      render();
    };
    $('q-banners').onclick = async (e) => {
      const b = e.target.closest('[data-swap]');
      if (!b) return;
      try {
        await call('machine.infinite_heatbed.post_mats', { active: b.dataset.swap });
        await call('server.job_queue.start');
      } catch (err) { /* toast shown */ }
    };
    $('q-rows').onclick = async (e) => {
      const mv = e.target.closest('[data-move]');
      const rm = e.target.closest('[data-remove]');
      const jobs = (S.queue.queued_jobs || []).slice();
      if (mv && !mv.disabled) {
        const [i, dir] = mv.dataset.move.split(':').map(Number);
        const j = i + dir;
        if (j < 0 || j >= jobs.length) return;
        [jobs[i], jobs[j]] = [jobs[j], jobs[i]];
        await call('server.job_queue.post_job', { filenames: jobs.map((x) => x.filename), reset: true }).catch(() => {});
        loadQueue();
      } else if (rm) {
        await call('server.job_queue.delete_job', { job_ids: [rm.dataset.remove] }).catch(() => {});
        loadQueue();
      }
    };

    // add files dialog
    let allFiles = [];
    const picked = new Set();
    const listFiles = () => {
      const q = $('files-search').value.trim().toLowerCase();
      const shown = allFiles.filter((f) => !q || f.toLowerCase().includes(q)).slice(0, 300);
      $('files-list').innerHTML = shown.map((f) =>
        '<label><input type="checkbox" data-f="' + esc(f) + '"' + (picked.has(f) ? ' checked' : '') + '>' +
        '<span class="fname" title="' + esc(f) + '">' + esc(f) + '</span></label>').join('') ||
        '<p class="empty">No G-code files found.</p>';
      $('files-count').textContent = picked.size ? picked.size + ' selected' : '';
      $('files-add').disabled = !picked.size;
    };
    $('btn-add').onclick = async () => {
      picked.clear();
      $('files-search').value = '';
      allFiles = [];
      listFiles();
      $('files-dlg').showModal();
      try {
        const res = await call('server.files.list', { root: 'gcodes' });
        allFiles = (res || []).sort((a, b) => num(b.modified) - num(a.modified)).map((f) => f.path || f.filename);
      } catch (e) { allFiles = []; }
      listFiles();
    };
    $('files-search').oninput = listFiles;
    $('files-list').onchange = (e) => {
      const f = e.target.dataset.f;
      if (!f) return;
      if (e.target.checked) picked.add(f); else picked.delete(f);
      $('files-count').textContent = picked.size ? picked.size + ' selected' : '';
      $('files-add').disabled = !picked.size;
    };
    $('files-dlg').addEventListener('close', () => {
      if ($('files-dlg').returnValue !== 'add' || !picked.size) return;
      call('server.job_queue.post_job', { filenames: [...picked] }).then(() => {
        toast('Added ' + picked.size + ' file' + (picked.size > 1 ? 's' : '') + ' to the queue');
        loadQueue();
      }).catch(() => {});
    });

    // mats
    $('mat-grid').onclick = (e) => {
      const edit = e.target.closest('[data-edit]');
      if (edit) { e.stopPropagation(); S.editingMat = Number(edit.dataset.edit); render(); return; }
      const tile = e.target.closest('[data-mat]');
      if (!tile || !S.server) return;
      const m = S.server.mats[Number(tile.dataset.mat)];
      if (m && m.name !== S.server.active_mat) {
        call('machine.infinite_heatbed.post_mats', { active: m.name }).then((r) => {
          S.server.active_mat = r.active; render(); toast('Heat mat: ' + r.active);
        }).catch(() => {});
      }
    };
    $('mat-grid').onkeydown = (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.dataset.mat != null) { e.preventDefault(); e.target.click(); }
    };
    $('btn-add-mat').onclick = () => { S.editingMat = -1; render(); };
    $('mat-cancel').onclick = () => { S.editingMat = null; render(); };
    $('mat-delete').onclick = async () => {
      const sv = S.server, i = S.editingMat;
      if (i == null || i < 0 || !confirm('Delete the ' + sv.mats[i].name + ' mat?')) return;
      const mats = sv.mats.filter((_, k) => k !== i);
      S.editingMat = null;
      await saveMats(mats).catch(() => {});
    };
    $('mat-form').onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target.elements, sv = S.server;
      const entry = {
        name: f.name.value.trim(), release_temp: Number(f.release_temp.value) || 0,
        z_offset: Number(f.z_offset.value) || 0, mesh: f.mesh.value.trim(), slicer_plate: f.slicer_plate.value.trim(),
      };
      if (!entry.name) { toast('Give the mat a name', true); return; }
      const mats = sv.mats.slice();
      const oldName = S.editingMat >= 0 ? mats[S.editingMat].name : null;
      if (S.editingMat >= 0) mats[S.editingMat] = entry; else mats.push(entry);
      const active = oldName && oldName === sv.active_mat ? entry.name : null;
      try {
        await saveMats(mats, active);
        S.editingMat = null;
        S.fileMats = {};   // plate mapping may have changed
        loadQueue();
        render();
      } catch (err) { /* toast shown */ }
    };

    // settings
    $('settings').addEventListener('change', (e) => {
      const t = e.target;
      if (t.id === 'set-seam') {
        call('machine.infinite_heatbed.post_settings', { advance_past_seam: t.checked ? 1 : 0 }).then(flashSaved).catch(() => {});
      } else if (t.dataset.key) {
        saveSetting(t.dataset.key, t.value).catch(() => {});
      }
    });
    $('settings').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.dataset.key) e.target.blur();
    });
    $('settings').addEventListener('focusout', () => setTimeout(render, 0));
  }

  // ── Start ──────────────────────────────────────────────────────────────────
  function start() {
    if (qs.has('demo') && window.IHBDemo) {
      mr = new window.IHBDemo();
    } else {
      const host = qs.get('host') || location.host;
      const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      mr = new Moonraker(proto + host + '/websocket');
    }
    wire();
    bindActions();
    render();
    mr.connect();
  }

  if (qs.has('demo') && !window.IHBDemo) {
    const s = document.createElement('script');
    s.src = 'demo.js';
    s.onload = start;
    s.onerror = start;
    document.head.appendChild(s);
  } else {
    start();
  }
})();
