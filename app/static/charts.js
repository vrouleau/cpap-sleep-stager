/* Dependency-free SVG charts: night timeline (hypnogram + overlays) and history trend. */
const NS = 'http://www.w3.org/2000/svg';
const COLORS = { Awake: 'var(--awake)', NREM: 'var(--nrem)', REM: 'var(--rem)', Deep: 'var(--deep)' };
// deep NREM (estimate, ring nights only) gets its own row under NREM
const shown = e => (e.stage === 'NREM' && e.deep > 0 ? 'Deep' : e.stage);

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
const hhmm = t => t.toTimeString().slice(0, 5);

function renderNight(host, d) {
  const ep = d.epochs.map(e => ({ ...e, t: new Date(e.t), st: shown(e) }));
  const hasDeep = ep.some(e => e.deep != null);
  const ROWS = hasDeep ? ['Awake', 'REM', 'NREM', 'Deep'] : ['Awake', 'REM', 'NREM'];
  if (!ep.length) return;
  const epochMs = d.epoch_s * 1000;
  const t0 = ep[0].t.getTime(), t1 = ep[ep.length - 1].t.getTime() + epochMs;
  const events = d.events.map(e => ({ ...e, t: new Date(e.t) }));

  const panels = [{ key: 'hyp', h: hasDeep ? 140 : 110, label: 'Stage' }];
  const maskOff = [];
  {
    const on = ep.map(e => e.flow_present === 1);
    const first = on.indexOf(true), last = on.lastIndexOf(true);
    for (let i = first; first >= 0 && i < last; i++) {
      if (on[i]) continue;
      let j = i; while (j < last && !on[j]) j++;
      maskOff.push([i, j]);
      i = j;
    }
  }
  const lgMask = document.querySelector('.legend .lg-maskoff');
  if (lgMask) lgMask.style.display = maskOff.length ? '' : 'none';
  const has = k => ep.some(e => e[k] != null);
  if (has('hr')) panels.push({ key: 'hr', h: 70, label: 'Pulse bpm', color: 'var(--line1)' });
  if (has('spo2')) panels.push({ key: 'spo2', h: 60, label: 'SpO₂ %', color: 'var(--line2)', min: 85, max: 100 });
  if (has('resp_rate')) panels.push({ key: 'resp_rate', h: 60, label: 'Breaths/min', color: 'var(--line3)' });
  if (has('breath_cv')) panels.push({ key: 'breath_cv', h: 60, label: 'Breath irregularity', color: 'var(--rem)' });
  if (has('motion')) panels.push({ key: 'motion', h: 45, label: 'Motion', color: 'var(--awake)', bars: true });

  function draw() {
    host.innerHTML = '';
    const W = Math.max(host.clientWidth, 320), L = 56, R = 12, gap = 10;
    const H = panels.reduce((a, p) => a + p.h + gap, 0) + 24;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'tl' }, host);
    const x = t => L + (t - t0) / (t1 - t0) * (W - L - R);
    let y0 = 0;
    for (const p of panels) {
      p.y = y0;
      el('rect', { x: L, y: y0, width: W - L - R, height: p.h, class: 'pbg' }, svg);
      if (p.key !== 'hyp') el('text', { x: L + 6, y: y0 + 13, class: 'plabel' }, svg).textContent = p.label;
      if (p.key === 'hyp') {
        const rh = p.h / ROWS.length;
        ROWS.forEach((s, i) =>
          (el('text', { x: L - 6, y: y0 + rh * i + rh / 2 + 4, class: 'axis end' }, svg).textContent = s));
        let i = 0;
        while (i < ep.length) {
          let j = i; while (j < ep.length && ep[j].st === ep[i].st) j++;
          const s = ep[i].st;
          el('rect', { x: x(ep[i].t.getTime()), y: y0 + ROWS.indexOf(s) * rh + 2,
            width: Math.max(1, x(ep[i].t.getTime() + (j - i) * epochMs) - x(ep[i].t.getTime())),
            height: rh - 4, fill: COLORS[s], rx: 2 }, svg);
          i = j;
        }
        // what you logged as awake: a band along the Awake row
        for (const w of (d.wake_log?.entries || [])) {
          const a = Math.max(t0, new Date(w.start).getTime()), b = Math.min(t1, new Date(w.end).getTime());
          if (b <= a) continue;
          const r = el('rect', { x: x(a), y: y0 + 1, width: Math.max(2, x(b) - x(a)), height: 4, class: 'logged', rx: 2 }, svg);
          el('title', {}, r).textContent = `You: awake ${w.start.slice(11, 16)}–${w.end.slice(11, 16)}`;
        }
        for (const e of events) if (e.source !== 'resmed')
          el('line', { x1: x(e.t), x2: x(e.t), y1: y0 + p.h - 6, y2: y0 + p.h, class: 'desattick' }, svg);
      } else {
        const vals = ep.map(e => e[p.key]).filter(v => v != null);
        let lo = p.min ?? Math.min(...vals), hi = p.max ?? Math.max(...vals);
        if (p.bars) lo = 0;
        if (hi - lo < 1e-6) hi = lo + 1;
        const pad = p.bars ? 0 : (hi - lo) * 0.08;
        const yy = v => y0 + p.h - ((Math.min(Math.max(v, lo), hi) - lo + pad) / (hi - lo + 2 * pad)) * p.h;
        el('text', { x: L - 6, y: y0 + 10, class: 'axis end' }, svg).textContent = +hi.toFixed(hi < 2 ? 2 : 0);
        el('text', { x: L - 6, y: y0 + p.h - 2, class: 'axis end' }, svg).textContent = +lo.toFixed(lo < 2 && lo > 0 ? 2 : 0);
        if (p.bars) {
          for (const e of ep) if (e[p.key]) el('rect', { x: x(e.t.getTime()), y: yy(e[p.key]),
            width: Math.max(1, x(e.t.getTime() + epochMs) - x(e.t.getTime())), height: y0 + p.h - yy(e[p.key]),
            fill: p.color, opacity: .8 }, svg);
        } else {
          let dstr = '', pen = false;
          for (const e of ep) {
            const v = e[p.key];
            if (v == null) { pen = false; continue; }
            dstr += (pen ? 'L' : 'M') + x(e.t.getTime() + epochMs / 2).toFixed(1) + ',' + yy(v).toFixed(1);
            pen = true;
          }
          el('path', { d: dstr, fill: 'none', stroke: p.color, 'stroke-width': 1.4 }, svg);
        }
      }
      y0 += p.h + gap;
    }
    // mask off and back on: gaps without CPAP flow between the night's first and last session
    for (const [a, b] of maskOff) {
      const xa = x(ep[a].t.getTime()), xb = x(ep[b - 1].t.getTime() + epochMs);
      const g = el('g', { class: 'maskoff' }, svg);
      el('rect', { x: xa, y: 0, width: Math.max(2, xb - xa), height: y0 - gap }, g);
      const mins = (b - a) * d.epoch_s / 60;
      el('title', {}, g).textContent = `Mask off ${hhmm(ep[a].t)}–${hhmm(new Date(ep[b - 1].t.getTime() + epochMs))}` +
        ` (${mins < 1 ? '<1' : Math.round(mins)} min)`;
    }
    // time axis
    const hour = 3600e3, first = Math.ceil(t0 / hour) * hour;
    for (let t = first; t <= t1; t += hour) {
      el('line', { x1: x(t), x2: x(t), y1: 0, y2: y0 - gap, class: 'grid' }, svg);
      el('text', { x: x(t), y: y0 + 8, class: 'axis mid' }, svg).textContent = hhmm(new Date(t));
    }
    // ResMed events: full-height lines across every panel, coloured by type
    const evLayer = el('g', { class: 'evlayer' + (showEvents ? '' : ' hidden') }, svg);
    for (const e of events) {
      if (e.source !== 'resmed') continue;
      const xa = x(e.t.getTime()), xb = x(e.t.getTime() + e.duration_s * 1000);
      const g = el('g', { class: 'ev ' + evClass(e.kind) }, evLayer);
      if (xb - xa >= 2) el('rect', { x: xa, y: 0, width: xb - xa, height: y0 - gap, class: 'evband' }, g);
      el('line', { x1: xa, x2: xa, y1: 0, y2: y0 - gap, class: 'evline' }, g);
      el('title', {}, g).textContent = `${e.t.toTimeString().slice(0, 8)} ${e.kind} (${Math.round(e.duration_s)} s)`;
    }
    // crosshair + readout
    const cross = el('line', { y1: 0, y2: y0 - gap, class: 'cross', visibility: 'hidden' }, svg);
    const tip = document.createElement('div'); tip.className = 'tip'; host.appendChild(tip);
    svg.addEventListener('mousemove', ev => {
      const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
      if (px < L) { cross.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; return; }
      const t = t0 + (px - L) / (W - L - R) * (t1 - t0);
      const e = ep[Math.min(ep.length - 1, Math.max(0, Math.floor((t - t0) / epochMs)))];
      cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('visibility', 'visible');
      const f = (v, n = 0) => v == null ? '–' : (+v).toFixed(n);
      const near = events.filter(v => Math.abs(v.t - t) < 60e3).map(v => v.kind);
      tip.innerHTML = `<b>${e.t.toTimeString().slice(0, 8)}</b> <span class="pill ${e.st.toLowerCase()}">${e.st}</span><br>` +
        `HR ${f(e.hr)} · SpO₂ ${f(e.spo2)} · RR ${f(e.resp_rate, 1)}<br>irreg ${f(e.breath_cv, 2)} · motion ${f(e.motion)}` +
        (near.length ? `<br>${near.join(', ')}` : '');
      tip.style.display = 'block';
      tip.style.left = Math.min(ev.clientX - host.getBoundingClientRect().left + 12, host.clientWidth - 190) + 'px';
      tip.style.top = (ev.clientY - host.getBoundingClientRect().top + 12) + 'px';
    });
    svg.addEventListener('mouseleave', () => { cross.setAttribute('visibility', 'hidden'); tip.style.display = 'none'; });
  }
  const kinds = new Set(events.filter(e => e.source === 'resmed').map(e => evClass(e.kind)));
  document.querySelectorAll('.legend .lg[data-ev]').forEach(n => { if (!kinds.has(n.dataset.ev)) n.style.display = 'none'; });
  let showEvents = true;
  const toggle = document.getElementById('toggle-events');
  if (toggle) {
    if (!events.some(e => e.source === 'resmed')) {
      const label = toggle.closest('label');
      if (!d.has_resmed) label.style.display = 'none';  // ring-only night: no CPAP at all
      else {  // CPAP night with zero events: say so instead of hiding the control
        toggle.checked = false; toggle.disabled = true;
        label.classList.add('none');
        label.lastChild.textContent = ' No ResMed events this night';
      }
    }
    toggle.addEventListener('change', () => {
      showEvents = toggle.checked;
      host.querySelector('.evlayer')?.classList.toggle('hidden', !showEvents);
    });
  }
  draw();
  let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(draw, 150); });
}

function evClass(kind) {
  const k = kind.toLowerCase();
  if (k.includes('obstructive')) return 'oa';
  if (k.includes('central')) return 'ca';
  if (k.includes('hypopnea')) return 'h';
  if (k.includes('apnea')) return 'ua';
  return 'other';
}

function renderTrend(host, rows) {
  function draw() {
    host.innerHTML = '';
    const W = Math.max(host.clientWidth, 280), H = 200, L = 34, B = 22, T = 6;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H }, host);
    const maxMin = Math.max(480, ...rows.map(r => r.s.tib_min || 0));
    const bw = (W - L - 4) / rows.length;
    const y = m => H - B - (m / maxMin) * (H - B - T);
    for (let h = 0; h * 60 <= maxMin; h += 2) {
      el('line', { x1: L, x2: W, y1: y(h * 60), y2: y(h * 60), class: 'grid' }, svg);
      el('text', { x: L - 4, y: y(h * 60) + 3, class: 'axis end' }, svg).textContent = h + 'h';
    }
    rows.forEach((r, i) => {
      let base = 0;
      const w = Math.max(2, Math.min(bw * 0.75, 40)), xx = L + 2 + i * bw + (bw - w) / 2;
      const part = { deep: r.s.deep_min || 0, light: (r.s.nrem_min || 0) - (r.s.deep_min || 0),
        rem: r.s.rem_min || 0, awake: r.s.awake_min || 0 };
      for (const [k, s] of [['deep', 'Deep'], ['light', 'NREM'], ['rem', 'REM'], ['awake', 'Awake']]) {
        const v = part[k];
        const rect = el('rect', { x: xx, y: y(base + v), width: w, height: y(base) - y(base + v), fill: COLORS[s] }, svg);
        el('title', {}, rect).textContent = `${r.d} · ${s === 'NREM' && r.s.deep_min != null ? 'Light NREM' : s} ${Math.round(v)} min`;
        base += v;
      }
      const a = el('a', { href: `/nights/${r.id}` }, svg);
      el('rect', { x: xx, y: T, width: w, height: H - B - T, fill: 'transparent' }, a);
      if (rows.length <= 14 || i % Math.ceil(rows.length / 10) === 0)
        el('text', { x: xx + w / 2, y: H - 6, class: 'axis mid' }, svg).textContent = r.d.slice(5);
    });
  }
  draw();
  let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(draw, 150); });
}
