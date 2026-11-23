/*
 * Demo transport for the belt bed UI: a fake Moonraker + Klipper.
 * Loaded only with index.html?demo=1. It simulates printing, the eject
 * sequence (cool -> lift -> belt -> done), the job queue and mat swaps.
 */
'use strict';
(function () {
  const MATS = [
    { name: 'Textured PEI', release_temp: 30, z_offset: -0.02, mesh: '', slicer_plate: 'Textured PEI Plate' },
    { name: 'Smooth PEI', release_temp: 28, z_offset: 0, mesh: '', slicer_plate: 'High Temp Plate' },
    { name: 'G10 / Garolite', release_temp: 40, z_offset: 0.03, mesh: '', slicer_plate: 'Engineering Plate' },
    { name: 'PEO powder', release_temp: 32, z_offset: -0.01, mesh: '', slicer_plate: 'Cool Plate' },
  ];
  const FILES = {
    'clip_set.gcode':           { t: 38 * 60, fil: 'PLA', g: 18, c: '#3a3b40', plate: 'Textured PEI Plate' },
    'cable_guide.gcode':        { t: 65 * 60, fil: 'PETG', g: 31, c: '#55575e', plate: 'Textured PEI Plate' },
    'bracket_v3.gcode':         { t: 108 * 60, fil: 'PETG', g: 42, c: '#7cb8ff', plate: 'Textured PEI Plate' },
    'knob_m6.gcode':            { t: 52 * 60, fil: 'PLA', g: 24, c: '#d23a3a', plate: 'Textured PEI Plate' },
    'lens_cap_glossy.gcode':    { t: 34 * 60, fil: 'PLA', g: 12, c: '#2b2c30', plate: 'High Temp Plate' },
    'enclosure_hinge.gcode':    { t: 136 * 60, fil: 'ASA', g: 66, c: '#e2e2e2', plate: 'Textured PEI Plate' },
    'test_cube.gcode':          { t: 19 * 60, fil: 'PLA', g: 8, c: '#d23a3a', plate: '' },
  };

  class IHBDemo {
    constructor() {
      this.handlers = {};
      const now = Date.now() / 1000;
      this.history = [
        // newest first, like Moonraker's history.list order=desc
        { job_id: 'h2', filename: 'cable_guide.gcode', status: 'completed', end_time: now - 1800 },
        { job_id: 'h1', filename: 'clip_set.gcode', status: 'completed', end_time: now - 4000 },
      ];
      this.queue = ['knob_m6.gcode', 'lens_cap_glossy.gcode', 'enclosure_hinge.gcode', 'test_cube.gcode']
        .map((f, i) => ({ job_id: 'q' + i, filename: f }));
      this.qid = 10;
      this.queueState = 'ready';
      this.server = { stop_after: false, mat_swap: null, active_mat: 'Textured PEI', mats: MATS, settings: {}, notice: '' };
      this.params = {
        belt_speed: 40, eject_margin_mm: 20, eject_distance_mm: 420, eject_timeout_s: 60, eject_retries: 2,
        release_temp_c: 0, cool_timeout_s: 1800, z_lift_mm: 20, advance_past_seam: 1,
        belt_loop_length_mm: 1500, plate_length_mm: 300,
      };
      this.ihb = {
        state: 'printing', phase: 'idle', print_length_mm: 180, eject_distance_mm: 420, eject_target_mm: 0,
        eject_travelled_mm: 0, eject_progress: 0, belt_odometer_mm: 61234, belt_position_mm: 1284.6,
        seam_travel_mm: 1288, seam_on_plate: false, eject_cycles: 148, eject_seq: 148, last_result: 'ok',
        last_error: '', last_eject_s: 18.2, release_temp: 30, mat: MATS[0], params: this.params,
      };
      this.ps = { state: 'printing', filename: 'bracket_v3.gcode', print_duration: 4000, info: { current_layer: 84, total_layer: 136 } };
      this.progress = 0.62;
      this.bed = { temperature: 60, target: 60 };
      this.ejectT = 0;
    }

    on(m, fn) { (this.handlers[m] = this.handlers[m] || []).push(fn); }
    emit(m, p) { (this.handlers[m] || []).forEach((fn) => fn(p)); }
    connect() {
      setTimeout(() => this.emit('_open'), 50);
      setInterval(() => this.tick(), 500);
    }

    status() {
      this.ihb.seam_travel_mm = (((this.ihb.belt_position_mm - 0) % 1500) + 1500) % 1500;
      this.ihb.seam_on_plate = this.ihb.seam_travel_mm < this.params.plate_length_mm;
      this.ihb.eject_distance_mm = this.params.eject_distance_mm || this.ihb.print_length_mm + this.params.eject_margin_mm;
      const mat = MATS.concat(this.server.mats).find((m) => m.name === this.server.active_mat) || {};
      this.ihb.release_temp = mat.release_temp || 0;
      return JSON.parse(JSON.stringify({
        infinite_heatbed: this.ihb, print_stats: this.ps,
        virtual_sdcard: { progress: this.progress, is_active: this.ps.state === 'printing' },
        heater_bed: this.bed, 'tmc2209 manual_stepper infinite_heatbed_motor1': { run_current: 0.8 },
      }));
    }
    push() { this.emit('notify_status_update', [this.status(), Date.now() / 1000]); }
    pushServer() { this.emit('notify_ihb_server_update', [JSON.parse(JSON.stringify(this.server))]); }

    plateMat(f) {
      const p = (FILES[f] || {}).plate;
      const m = this.server.mats.find((x) => x.slicer_plate && x.slicer_plate.toLowerCase() === String(p).toLowerCase());
      return { plate: p || '', mat: m ? m.name : null };
    }

    tick() {
      if (this.ps.state === 'printing') {
        this.progress = Math.min(1, this.progress + 0.006);
        this.ps.print_duration += 30;
        this.ps.info.current_layer = Math.round(this.progress * this.ps.info.total_layer);
        if (this.progress >= 1) this.finishPrint();
      } else if (this.ihb.state === 'ejecting') {
        this.stepEject();
      } else if (this.bed.target === 0 && this.bed.temperature > 24) {
        this.bed.temperature -= 0.3;
      }
      this.push();
    }

    finishPrint() {
      this.ps.state = 'complete';
      this.history.unshift({ job_id: 'h' + (++this.qid), filename: this.ps.filename, status: 'completed', end_time: Date.now() / 1000 });
      this.emit('notify_history_changed', [{}]);
      this.startEject();
    }

    startEject() {
      this.ihb.state = 'ejecting';
      this.ihb.phase = this.ihb.release_temp > 0 ? 'cooling' : 'lifting';
      this.ihb.eject_target_mm = this.ihb.eject_distance_mm;
      this.ihb.eject_travelled_mm = 0;
      this.ihb.eject_progress = 0;
      this.bed.target = 0;
      this.ejectT = 0;
    }

    stepEject() {
      const h = this.ihb;
      this.ejectT += 0.5;
      if (h.phase === 'cooling') {
        this.bed.temperature = Math.max(h.release_temp - 0.5, this.bed.temperature - 3);
        if (this.bed.temperature <= h.release_temp) { h.phase = 'lifting'; this.ejectT = 0; }
      } else if (h.phase === 'lifting') {
        if (this.ejectT >= 1) { h.phase = 'belt'; this.ejectT = 0; }
      } else if (h.phase === 'belt') {
        const step = this.params.belt_speed * 0.5 * 3;   // 3x speed for the demo
        h.eject_travelled_mm = Math.min(h.eject_target_mm, h.eject_travelled_mm + step);
        h.belt_position_mm += step;
        h.belt_odometer_mm += step;
        h.eject_progress = h.eject_travelled_mm / h.eject_target_mm;
        if (h.eject_progress >= 1) {
          h.phase = 'done'; h.state = 'idle'; h.eject_cycles += 1; h.eject_seq += 1;
          h.last_result = 'ok'; h.last_eject_s = 18.4;
          this.afterEject();
        }
      }
    }

    afterEject() {
      const s = this.server;
      if (s.stop_after) { s.stop_after = false; this.queueState = 'paused'; s.notice = 'Stopped after this plate. Queue paused.'; this.pushServer(); this.emit('notify_job_queue_changed', [{}]); return; }
      if (this.queueState === 'paused' || !this.queue.length) { s.notice = 'Eject complete.'; this.pushServer(); return; }
      const next = this.queue[0].filename;
      const need = this.plateMat(next).mat;
      if (need && need !== s.active_mat) {
        s.mat_swap = { filename: next, mat: need };
        this.queueState = 'paused';
        s.notice = 'Swap the heat mat to ' + need + ', then start the queue.';
        this.pushServer();
        this.emit('notify_job_queue_changed', [{}]);
        return;
      }
      s.notice = 'Eject complete. Starting ' + next + '.';
      this.pushServer();
      this.startNext();
    }

    startNext() {
      if (!this.queue.length || this.ps.state === 'printing' || this.ihb.state === 'ejecting') return;
      const job = this.queue.shift();
      this.ps = { state: 'printing', filename: job.filename, print_duration: 0, info: { current_layer: 0, total_layer: 120 } };
      this.progress = 0;
      this.bed.target = 60; this.bed.temperature = 60;
      this.ihb.state = 'printing'; this.ihb.phase = 'idle';
      this.queueState = 'ready';
      this.emit('notify_job_queue_changed', [{}]);
    }

    async call(method, p) {
      p = p || {};
      await new Promise((r) => setTimeout(r, 30));
      const fail = (msg) => { throw new Error(msg); };
      switch (method) {
        case 'server.info': return { klippy_state: 'ready' };
        case 'printer.objects.list':
          return { objects: ['infinite_heatbed', 'print_stats', 'virtual_sdcard', 'heater_bed', 'tmc2209 manual_stepper infinite_heatbed_motor1'] };
        case 'printer.objects.subscribe': return { status: this.status() };
        case 'machine.infinite_heatbed.status': return Object.assign(this.status().infinite_heatbed, { server: this.server });
        case 'server.job_queue.status': return { queued_jobs: this.queue.slice(), queue_state: this.queueState };
        case 'server.history.list': return { jobs: this.history.slice() };
        case 'server.files.metadata': {
          const f = FILES[p.filename] || {};
          return { estimated_time: f.t, filament_type: f.fil, filament_weight_total: f.g, filament_colors: f.c ? [f.c] : [] };
        }
        case 'server.files.list':
          return Object.keys(FILES).map((f, i) => ({ path: f, modified: 1700000000 + i }));
        case 'machine.infinite_heatbed.file_mats': {
          const out = {};
          (p.filenames || []).forEach((f) => { out[f] = this.plateMat(f); });
          return { files: out };
        }
        case 'machine.infinite_heatbed.stop_after':
          this.server.stop_after = !!p.enable; this.pushServer(); return { stop_after: this.server.stop_after };
        case 'machine.infinite_heatbed.post_mats':
          if (p.mats) this.server.mats = p.mats;
          if (p.active) {
            this.server.active_mat = p.active;
            if (this.server.mat_swap && this.server.mat_swap.mat === p.active) { this.server.mat_swap = null; this.server.notice = ''; }
          }
          this.pushServer(); this.push();
          return { mats: this.server.mats, active: this.server.active_mat };
        case 'machine.infinite_heatbed.post_settings':
          Object.assign(this.params, p); Object.assign(this.server.settings, p); this.push();
          return { settings: this.server.settings };
        case 'server.job_queue.start':
          this.queueState = 'ready'; this.startNext(); this.emit('notify_job_queue_changed', [{}]); return 'ok';
        case 'server.job_queue.pause':
          this.queueState = 'paused'; this.emit('notify_job_queue_changed', [{}]); return 'ok';
        case 'server.job_queue.post_job':
          if (p.reset) this.queue = [];
          (p.filenames || []).forEach((f) => this.queue.push({ job_id: 'q' + (++this.qid), filename: f }));
          this.emit('notify_job_queue_changed', [{}]); return 'ok';
        case 'server.job_queue.delete_job':
          this.queue = this.queue.filter((j) => !(p.job_ids || []).includes(j.job_id));
          this.emit('notify_job_queue_changed', [{}]); return 'ok';
        case 'printer.print.pause': this.ps.state = 'paused'; this.push(); return 'ok';
        case 'printer.print.resume': this.ps.state = 'printing'; this.push(); return 'ok';
        case 'printer.gcode.script': {
          const s = p.script || '';
          const busy = this.ihb.state === 'ejecting';
          const printing = ['printing', 'paused'].includes(this.ps.state);
          let m;
          if ((m = /^HEATBED_JOG DISTANCE=(-?[\d.]+)/.exec(s))) {
            if (busy || printing) fail('IHB: refusing to move the belt during a print');
            this.ihb.belt_position_mm += Number(m[1]); this.ihb.belt_odometer_mm += Math.abs(Number(m[1]));
          } else if (s.startsWith('HEATBED_SET_SEAM')) {
            this.ihb.belt_position_mm = 0;
          } else if (s.startsWith('HEATBED_EJECT')) {
            if (busy) fail("IHB: Cannot eject in state 'ejecting'");
            this.startEject();
          } else if (s.startsWith('HEATBED_RESET')) {
            this.ihb.state = 'idle'; this.ihb.phase = 'idle';
          }
          this.push();
          return 'ok';
        }
        default: return fail('Demo: unsupported method ' + method);
      }
    }
  }

  window.IHBDemo = IHBDemo;
})();
