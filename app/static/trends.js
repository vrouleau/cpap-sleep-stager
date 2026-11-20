/* Trend dashboards (Sleep / CPAP): one date-range control scopes the tiles, every chart
   and the table. Dependency-free SVG; each night's summary is embedded by the page. */
(() => {
  const NS = 'http://www.w3.org/2000/svg';
  const root = document.getElementById('trends');
  if (!root) return;
  const PAGE = root.dataset.page;
  const ALL = JSON.parse(document.getElementById('nights-data').textContent)
    .sort((a, b) => a.d.localeCompare(b.d));

  // ------------------------------------------------------------------ dates
  const iso = dt => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (dt, n) => { const r = new Date(dt); r.setDate(r.getDate() + n); return r; };
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const short = s => { const d = parse(s); return `${MON[d.getMonth()]} ${d.getDate()}`; };
  const long = s => { const d = parse(s); return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${MON[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`; };

  const PRESETS = {
    '7d': ['Last 7 days', t => [addDays(t, -6), t]],
    '30d': ['Last 30 days', t => [addDays(t, -29), t]],
    '90d': ['Last 90 days', t => [addDays(t, -89), t]],
    '12m': ['Last 12 months', t => [addDays(new Date(t.getFullYear() - 1, t.getMonth(), t.getDate()), 1), t]],
    'month': ['This month', t => [new Date(t.getFullYear(), t.getMonth(), 1), t]],
    'lastmonth': ['Last month', t => [new Date(t.getFullYear(), t.getMonth() - 1, 1), new Date(t.getFullYear(), t.getMonth(), 0)]],
    'year': ['This year', t => [new Date(t.getFullYear(), 0, 1), t]],
    'all': ['All nights', t => [ALL.length ? parse(ALL[0].d) : t, t]],
  };

  const q = new URLSearchParams(location.search);
  let state = { range: q.get('range') || '30d', from: q.get('from'), to: q.get('to') };

  function span() {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    // nights are named after the evening they start, so last night is "yesterday"
    const lastNight = addDays(today, -1);
    if (state.range === 'custom' && state.from && state.to) return [state.from, state.to];
    const p = PRESETS[state.range] || PRESETS['30d'];
    const [a, b] = p[1](lastNight);
    return [iso(a), iso(b)];
  }

  // ------------------------------------------------------------------ range control
  const sel = document.getElementById('range');
  const cf = document.getElementById('range-from'), ct = document.getElementById('range-to');
  const custom = document.getElementById('range-custom');
  for (const [k, [label]] of Object.entries(PRESETS)) sel.add(new Option(label, k));
  sel.add(new Option('Custom…', 'custom'));
  sel.value = PRESETS[state.range] || state.range === 'custom' ? state.range : '30d';
  function syncControls() {
    const [a, b] = span();
    custom.hidden = state.range !== 'custom';
    cf.value = a; ct.value = b;
  }
  function commit() {
    const p = new URLSearchParams();
    p.set('range', state.range);
    if (state.range === 'custom') { p.set('from', state.from); p.set('to', state.to); }
    history.replaceState(null, '', '?' + p.toString());
    document.querySelectorAll('a[data-keep-range]').forEach(a => { a.search = p.toString(); });
    render();
  }
  sel.addEventListener('change', () => {
    if (sel.value === 'custom') {
      const [a, b] = span();
      state = { range: 'custom', from: a, to: b };
    } else state = { range: sel.value };
    syncControls(); commit();
  });
  for (const inp of [cf, ct]) inp.addEventListener('change', () => {
    if (!cf.value || !ct.value) return;
    state = { range: 'custom', from: cf.value <= ct.value ? cf.value : ct.value, to: cf.value <= ct.value ? ct.value : cf.value };
    commit();
  });

  // ------------------------------------------------------------------ data
  const num = v => (v === null || v === undefined || Number.isNaN(v) ? null : +v);
  const mean = xs => { const v = xs.filter(x => x !== null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
  const clockMin = t => { if (!t) return null; const d = new Date(t); let m = d.getHours() * 60 + d.getMinutes(); if (m < 12 * 60) m += 24 * 60; return m - 12 * 60; };  // minutes after noon

  const FIELDS = {
    deep: n => (n.s.deep_min != null ? n.s.deep_min / 60 : null),
    light: n => (n.s.nrem_min != null ? (n.s.nrem_min - (n.s.deep_min || 0)) / 60 : null),
    rem: n => num(n.s.rem_min) !== null ? n.s.rem_min / 60 : null,
    awake: n => num(n.s.awake_min) !== null ? n.s.awake_min / 60 : null,
    tst: n => num(n.s.tst_min) !== null ? n.s.tst_min / 60 : null,
    eff: n => num(n.s.sleep_efficiency),
    waso: n => num(n.s.waso_min),
    onset: n => clockMin(n.s.sleep_onset),
    wake: n => clockMin(n.s.final_wake),
    hr: n => num(n.s.hr_sleep_mean),
    arousal_idx: n => (n.ring ? num(n.s.arousal_index) : null),
    deep_pct: n => num(n.s.deep_pct_tst),
    rem_pct: n => num(n.s.rem_pct_tst),
    ahi: n => (n.cpap ? num(n.s.ahi_sleep) : null),
    oa: n => (n.cpap && n.s.ahi_by_kind ? num(n.s.ahi_by_kind['Obstructive apnea']) : null),
    ca: n => (n.cpap && n.s.ahi_by_kind ? num(n.s.ahi_by_kind['Central apnea']) : null),
    h: n => (n.cpap && n.s.ahi_by_kind ? num(n.s.ahi_by_kind['Hypopnea']) : null),
    ua: n => (n.cpap && n.s.ahi_by_kind ? num(n.s.ahi_by_kind['Apnea']) : null),
    rera: n => (n.cpap ? num(n.s.rera_count) : null),
    usage: n => (n.cpap && n.s.usage_min != null ? n.s.usage_min / 60 : null),
    breaks: n => (n.cpap ? num(n.s.mask_breaks) : null),
    rr: n => (n.cpap ? num(n.s.resp_rate_sleep) : null),
    odi4: n => (n.ring ? num(n.s.odi4) : null),
    spo2min: n => (n.ring ? num(n.s.spo2_min) : null),
  };

  /* One bucket per night, or per week when the range is long (values = mean of the
     nights in that week that have the measure). */
  function buckets(nights, a, b) {
    const byDate = new Map(nights.map(n => [n.d, n]));
    const days = [];
    for (let d = parse(a); iso(d) <= b; d = addDays(d, 1)) days.push(iso(d));
    const weekly = days.length > 92;
    if (!weekly) return { weekly, list: days.map(d => ({ key: d, label: short(d), title: long(d), nights: byDate.has(d) ? [byDate.get(d)] : [] })) };
    const out = [];
    for (const d of days) {
      const dt = parse(d), monday = iso(addDays(dt, -((dt.getDay() + 6) % 7)));
      if (!out.length || out[out.length - 1].key !== monday) out.push({ key: monday, label: short(monday), title: `Week of ${long(monday)}`, nights: [] });
      if (byDate.has(d)) out[out.length - 1].nights.push(byDate.get(d));
    }
    return { weekly, list: out };
  }
  const val = (bk, f) => mean(bk.nights.map(FIELDS[f]));

  // ------------------------------------------------------------------ svg helpers
  function el(tag, attrs = {}, parent) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  function ticks(lo, hi, n = 4) {
    const step = niceMax((hi - lo) / n);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
    return out;
  }
  // column with a 4px rounded data end and a square foot
  function colPath(x, y, w, h, round) {
    if (h <= 0) return '';
    const r = round ? Math.min(4, w / 2, h) : 0;
    return `M${x},${y + h}V${y + r}${r ? `Q${x},${y} ${x + r},${y}` : ''}H${x + w - r}${r ? `Q${x + w},${y} ${x + w},${y + r}` : ''}V${y + h}Z`;
  }

  const tip = document.createElement('div');
  tip.className = 'ttip'; tip.hidden = true; document.body.appendChild(tip);
  function showTip(ev, bk, rows) {
    tip.replaceChildren();
    const h = document.createElement('div'); h.className = 'ttip-h'; h.textContent = bk.title; tip.appendChild(h);
    if (!bk.nights.length) { const r = document.createElement('div'); r.className = 'muted'; r.textContent = 'No data'; tip.appendChild(r); }
    for (const [color, label, value] of rows) {
      const r = document.createElement('div'); r.className = 'ttip-r';
      if (color) { const k = document.createElement('span'); k.className = 'key'; k.style.background = color; r.appendChild(k); }
      const v = document.createElement('b'); v.textContent = value; r.appendChild(v);
      const l = document.createElement('span'); l.textContent = label; r.appendChild(l);
      tip.appendChild(r);
    }
    if (bk.nights.length > 1) { const r = document.createElement('div'); r.className = 'muted small'; r.textContent = `Average of ${bk.nights.length} nights`; tip.appendChild(r); }
    tip.hidden = false;
    const pad = 14, w = tip.offsetWidth, hgt = tip.offsetHeight;
    let x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
    if (y + hgt > innerHeight - 8) y = ev.clientY - hgt - pad;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  const hideTip = () => { tip.hidden = true; };

  /* Shared frame: y axis, hairline grid, x labels, and one hover column per bucket. */
  function frame(host, bks, yLo, yHi, yFmt, opts = {}) {
    host.replaceChildren();
    // report charts are drawn at a fixed paper width (then scaled by CSS) so the PDF
    // looks the same whatever the screen size; dashboards use the live width
    const W = +host.dataset.paperW || Math.max(host.clientWidth, 280), H = opts.h || 190, L = opts.left || 40, R = opts.right || 8, T = 10, B = 24;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'tchart', role: 'img' }, host);
    const pw = W - L - R, ph = H - T - B, n = bks.length, slot = pw / n;
    const x = i => L + slot * i + slot / 2;
    const y = v => T + ph - ((v - yLo) / (yHi - yLo)) * ph;
    for (const t of (opts.ticks || ticks(yLo, yHi))) {
      el('line', { x1: L, x2: W - R, y1: y(t), y2: y(t), class: t === yLo ? 'base' : 'grid' }, svg);
      el('text', { x: L - 6, y: y(t) + 4, class: 'ytick' }, svg).textContent = yFmt(t);
    }
    const every = Math.max(1, Math.ceil(56 / slot));
    bks.forEach((b, i) => {
      if (i % every) return;
      el('text', { x: x(i), y: H - 6, class: 'xtick' }, svg).textContent = b.label;
    });
    return { svg, x, y, slot, pw, ph, T, L, H, B, W };
  }
  function hover(fr, bks, rowsFor, onClick) {
    const g = el('g', { class: 'hits' }, fr.svg);
    bks.forEach((b, i) => {
      const r = el('rect', { x: fr.x(i) - fr.slot / 2, y: fr.T, width: fr.slot, height: fr.ph, class: 'hit' }, g);
      r.addEventListener('pointermove', ev => { fr.svg.querySelectorAll(`[data-i="${i}"]`).forEach(m => m.classList.add('lift')); showTip(ev, b, rowsFor(b)); });
      r.addEventListener('pointerleave', () => { fr.svg.querySelectorAll(`[data-i="${i}"]`).forEach(m => m.classList.remove('lift')); hideTip(); });
      if (b.nights.length === 1) { r.style.cursor = 'pointer'; r.addEventListener('click', () => onClick(b.nights[0])); }
    });
  }
  const openNight = n => { location.href = `/nights/${n.id}`; };

  function bars(host, bks, series, fmt, opts = {}) {
    const totals = bks.map(b => series.reduce((a, s) => a + (val(b, s.key) || 0), 0));
    const hi = niceMax(Math.max(opts.ref ? opts.ref.value * 1.1 : 0, ...totals, opts.min || 0));
    const fr = frame(host, bks, 0, hi, fmt.tick || fmt.v, { right: opts.ref ? 32 : 8 });
    const bw = Math.min(24, Math.max(2, fr.slot * 0.68));
    bks.forEach((b, i) => {
      let acc = 0;
      const parts = series.map(s => [s, val(b, s.key) || 0]).filter(([, v]) => v > 0);
      parts.forEach(([s, v], j) => {
        const y0 = fr.y(acc), y1 = fr.y(acc + v);
        const gapTop = j < parts.length - 1 ? 1 : 0, gapBottom = j > 0 ? 1 : 0;
        el('path', { d: colPath(fr.x(i) - bw / 2, y1 + gapTop, bw, Math.max(0, y0 - y1 - gapTop - gapBottom), j === parts.length - 1),
          fill: s.color, class: 'mark', 'data-i': i }, fr.svg);
        acc += v;
      });
    });
    if (opts.ref) {
      // hairline across the plot; its label sits in the right margin, clear of the bars
      el('line', { x1: fr.L, x2: fr.W - 32, y1: fr.y(opts.ref.value), y2: fr.y(opts.ref.value), class: 'ref' }, fr.svg);
      el('text', { x: fr.W - 28, y: fr.y(opts.ref.value) + 4, class: 'reflabel' }, fr.svg).textContent = opts.ref.label;
    }
    hover(fr, bks, b => {
      const rows = series.map(s => [s.color, s.label, fmtOr(val(b, s.key), fmt.v)]);
      if (series.length > 1 && b.nights.length) rows.push(['', opts.totalLabel || 'Total', fmt.v(series.reduce((a, s) => a + (val(b, s.key) || 0), 0))]);
      return rows;
    }, openNight);
  }

  function line(host, bks, s, fmt, opts = {}) {
    const vals = bks.map(b => val(b, s.key));
    const have = vals.filter(v => v !== null);
    let lo = opts.lo ?? Math.min(...have), hi = opts.hi ?? Math.max(...have);
    if (!have.length) { lo = 0; hi = 1; }
    if (hi - lo < (opts.minSpan || 1)) { const c = (hi + lo) / 2; lo = c - (opts.minSpan || 1) / 2; hi = c + (opts.minSpan || 1) / 2; }
    const tk = ticks(lo, hi);
    lo = Math.min(lo, tk[0]); hi = Math.max(hi, tk[tk.length - 1]);
    const fr = frame(host, bks, lo, hi, fmt.tick || fmt.v, { ticks: ticks(lo, hi) });
    let d = '', pen = false;
    vals.forEach((v, i) => { if (v === null) { pen = false; return; } d += (pen ? 'L' : 'M') + fr.x(i).toFixed(1) + ',' + fr.y(v).toFixed(1); pen = true; });
    el('path', { d, fill: 'none', stroke: s.color, class: 'lineplot' }, fr.svg);
    const dots = bks.length <= 60;
    vals.forEach((v, i) => {
      if (v === null) return;
      const isolated = (i === 0 || vals[i - 1] === null) && (i === vals.length - 1 || vals[i + 1] === null);
      if (dots || isolated) el('circle', { cx: fr.x(i), cy: fr.y(v), r: 4, fill: s.color, class: 'dotmark', 'data-i': i }, fr.svg);
    });
    hover(fr, bks, b => [[s.color, s.label, fmtOr(val(b, s.key), fmt.v)]], openNight);
  }

  /* Sleep window: a floating column from falling asleep to the final awakening, on a
     clock axis that runs from evening (top) to morning (bottom). */
  function window_(host, bks, color) {
    const on = bks.map(b => val(b, 'onset')), off = bks.map(b => val(b, 'wake'));
    const have = [...on, ...off].filter(v => v !== null);
    let lo = Math.floor((Math.min(...have, 9 * 60) - 30) / 60) * 60, hi = Math.ceil((Math.max(...have, 19 * 60) + 30) / 60) * 60;
    if (!have.length) { lo = 8 * 60; hi = 20 * 60; }
    const clock = m => { const h = Math.floor((m / 60 + 12) % 24); return `${String(h).padStart(2, '0')}:00`; };
    const tk = []; for (let t = lo; t <= hi; t += 120) tk.push(t);
    // flip: earlier clock time at the top
    const fr = frame(host, bks, -hi, -lo, v => clock(-v), { ticks: tk.map(t => -t), h: 210, left: 46 });
    const bw = Math.min(24, Math.max(2, fr.slot * 0.68));
    bks.forEach((b, i) => {
      if (on[i] === null || off[i] === null) return;
      const y1 = fr.y(-on[i]), y2 = fr.y(-off[i]);
      const r = Math.min(4, bw / 2);
      el('rect', { x: fr.x(i) - bw / 2, y: y1, width: bw, height: Math.max(2, y2 - y1), rx: r, fill: color, class: 'mark', 'data-i': i }, fr.svg);
    });
    const hm = m => { if (m === null) return '–'; const t = (m + 12 * 60) % (24 * 60); return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.round(t % 60)).padStart(2, '0')}`; };
    hover(fr, bks, b => [['', 'asleep', hm(val(b, 'onset'))], ['', 'final awakening', hm(val(b, 'wake'))], [color, 'asleep in total', fmtOr(val(b, 'tst'), hours)]], openNight);
  }

  // ------------------------------------------------------------------ formatting
  const fmtOr = (v, f) => (v === null ? '–' : f(v));
  const hours = v => { const h = Math.floor(v), m = Math.round((v - h) * 60); return m === 60 ? `${h + 1}h00` : `${h}h${String(m).padStart(2, '0')}`; };
  const F = {
    hours: { v: hours, tick: v => `${v}h` },
    pct: { v: v => `${Math.round(v)}%`, tick: v => `${v}%` },
    min: { v: v => `${Math.round(v)} min`, tick: v => `${v}` },
    rate: { v: v => v.toFixed(v < 10 ? 2 : 1), tick: v => `${+v.toFixed(2)}` },
    count: { v: v => (Number.isInteger(v) ? `${v}` : v.toFixed(1)), tick: v => `${v}` },
    bpm: { v: v => `${v.toFixed(1)} bpm`, tick: v => `${v}` },
    perMin: { v: v => `${v.toFixed(1)} /min`, tick: v => `${v}` },
    spo2: { v: v => `${Math.round(v)}%`, tick: v => `${v}%` },
  };
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ------------------------------------------------------------------ pages
  const STAGES = () => [
    { key: 'deep', label: 'Deep (est.)', color: css('--deep') },
    { key: 'light', label: 'Light NREM', color: css('--nrem') },
    { key: 'rem', label: 'REM', color: css('--rem') },
    { key: 'awake', label: 'Awake', color: css('--awake') },
  ];
  const EVENTS = () => [
    { key: 'oa', label: 'Obstructive', color: css('--ev-oa') },
    { key: 'ca', label: 'Central', color: css('--ev-ca') },
    { key: 'h', label: 'Hypopnea', color: css('--ev-h') },
    { key: 'ua', label: 'Apnea (unclassified)', color: css('--ev-ua') },
  ];

  function card(title, sub, legend, draw) {
    const c = document.createElement('section'); c.className = 'card chart-card';
    const h = document.createElement('h2'); h.textContent = title; c.appendChild(h);
    if (sub) { const p = document.createElement('p'); p.className = 'muted small chart-sub'; p.textContent = sub; c.appendChild(p); }
    if (legend && legend.length > 1) {
      const lg = document.createElement('div'); lg.className = 'legend';
      for (const s of legend) {
        const i = document.createElement('span'); i.className = 'lgi';
        const sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = s.color;
        i.append(sw, document.createTextNode(s.label)); lg.appendChild(i);
      }
      c.appendChild(lg);
    }
    const plot = document.createElement('div'); plot.className = 'plot'; c.appendChild(plot);
    c._draw = () => draw(plot);
    return c;
  }

  function tile(label, value, sub) {
    const t = document.createElement('div'); t.className = 'stat';
    const l = document.createElement('span'); l.className = 'stat-l'; l.textContent = label;
    const v = document.createElement('b'); v.textContent = value;
    t.append(l, v);
    if (sub) { const s = document.createElement('span'); s.className = 'stat-s'; s.textContent = sub; t.appendChild(s); }
    return t;
  }

  function table(nights, cols) {
    const wrap = document.getElementById('table');
    wrap.replaceChildren();
    if (!nights.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'No nights in this range.'; wrap.appendChild(p); return; }
    const t = document.createElement('table'); t.className = 'hist';
    const hr = t.createTHead().insertRow();
    for (const [h] of [['Night'], ...cols]) { const th = document.createElement('th'); th.textContent = h; hr.appendChild(th); }
    const tb = t.createTBody();
    for (const n of [...nights].reverse()) {
      const r = tb.insertRow();
      const a = document.createElement('a'); a.href = `/nights/${n.id}`; a.textContent = n.d;
      r.insertCell().appendChild(a);
      for (const [, f] of cols) r.insertCell().textContent = f(n);
      r.addEventListener('click', ev => { if (ev.target.tagName !== 'A') openNight(n); });
    }
    wrap.appendChild(t);
  }
  const cell = (f, fmt) => n => fmtOr(FIELDS[f](n), fmt);
  const avgOf = (nights, f) => mean(nights.map(FIELDS[f]));

  function sleepPage(nights, bks) {
    const stages = STAGES();
    const ring = nights.filter(n => n.ring);
    const deepAvg = avgOf(ring, 'deep_pct');
    const tiles = [
      tile('Nights', `${nights.length}`, `${ring.length} with the O2Ring`),
      tile('Asleep', fmtOr(avgOf(nights, 'tst'), hours), 'average per night'),
      tile('Sleep efficiency', fmtOr(avgOf(nights, 'eff'), F.pct.v), 'asleep / in bed'),
      tile('Awake after falling asleep', fmtOr(avgOf(nights, 'waso'), F.min.v), 'average per night'),
      tile('REM', fmtOr(avgOf(nights, 'rem_pct'), F.pct.v), 'of sleep'),
      tile('Deep (est.)', fmtOr(deepAvg, F.pct.v), ring.length ? 'of sleep, O2Ring nights' : 'needs the O2Ring'),
      tile('Arousals', fmtOr(avgOf(ring, 'arousal_idx'), v => `${v.toFixed(1)} / h`), ring.length ? 'brief, still asleep · O2Ring nights' : 'needs the O2Ring'),
    ];
    const cards = [
      card('Sleep stages', 'Hours per night. Deep sleep is an estimate and needs the O2Ring.', stages,
        p => bars(p, bks, stages, F.hours, { totalLabel: 'In bed' })),
      card('Sleep window', 'From falling asleep to the final awakening.', null,
        p => window_(p, bks, css('--nrem'))),
      card('Sleep efficiency', 'Share of the time in bed spent asleep.', null,
        p => line(p, bks, { key: 'eff', label: 'efficiency', color: css('--accent') }, F.pct, { minSpan: 10 })),
      card('Awake after falling asleep', 'Minutes awake between falling asleep and the final awakening.', null,
        p => bars(p, bks, [{ key: 'waso', label: 'awake', color: css('--awake') }], F.min)),
    ];
    if (ring.length) cards.push(card('Pulse while asleep', 'Average pulse during sleep (O2Ring nights).', null,
      p => line(p, bks, { key: 'hr', label: 'pulse', color: css('--line1') }, F.bpm, { minSpan: 4 })));
    const cols = [['Asleep', cell('tst', hours)], ['Efficiency', cell('eff', F.pct.v)], ['Awake (WASO)', cell('waso', F.min.v)],
      ['Light', cell('light', hours)], ['Deep', cell('deep', hours)], ['REM', cell('rem', hours)], ['Arousals / h', cell('arousal_idx', v => v.toFixed(1))], ['Pulse', cell('hr', F.bpm.v)]];
    return { tiles, cards, cols };
  }

  function cpapPage(nights, bks) {
    const ev = EVENTS();
    const cp = nights.filter(n => n.cpap), ring = nights.filter(n => n.ring);
    const comp = cp.filter(n => (FIELDS.usage(n) || 0) >= 4).length;
    const reras = cp.reduce((a, n) => a + (FIELDS.rera(n) || 0), 0);
    const tiles = [
      tile('AHI', fmtOr(avgOf(cp, 'ahi'), F.rate.v), 'events / hour asleep · under 5 is normal'),
      tile('Usage', fmtOr(avgOf(cp, 'usage'), hours), 'average per night'),
      tile('Nights ≥ 4 h', cp.length ? `${comp} / ${cp.length}` : '–', cp.length ? `${Math.round(100 * comp / cp.length)}% of CPAP nights` : 'no CPAP nights'),
      tile('Mask breaks', fmtOr(avgOf(cp, 'breaks'), F.count.v), 'off and back on, per night'),
      tile('RERAs', `${reras}`, 'arousals flagged by the machine'),
      tile('ODI 4%', fmtOr(avgOf(ring, 'odi4'), F.rate.v), ring.length ? 'drops / hour, O2Ring nights' : 'needs the O2Ring'),
    ];
    const cards = [
      card('AHI by event type', 'Events per hour asleep, counted only while asleep.', ev,
        p => bars(p, bks, ev, F.rate, { totalLabel: 'AHI', min: 0.5 })),
      card('Usage', 'Hours with the mask on. The line marks 4 h, the usual compliance threshold.', null,
        p => bars(p, bks, [{ key: 'usage', label: 'mask on', color: css('--accent') }], F.hours, { ref: { value: 4, label: '4 h' } })),
      card('Mask breaks', 'Times the mask came off and went back on during the night.', null,
        p => bars(p, bks, [{ key: 'breaks', label: 'breaks', color: css('--muted') }], F.count, { min: 2 })),
      card('Breathing rate while asleep', 'Median breaths per minute during sleep.', null,
        p => line(p, bks, { key: 'rr', label: 'breathing rate', color: css('--accent') }, F.perMin, { minSpan: 4 })),
    ];
    if (ring.length) {
      cards.push(card('Oxygen drops (ODI 4%)', 'SpO₂ drops of 4 points or more per hour (O2Ring nights).', null,
        p => bars(p, bks, [{ key: 'odi4', label: 'drops / hour', color: css('--line2') }], F.rate, { min: 0.5 })));
      cards.push(card('Lowest SpO₂', 'The night’s lowest oxygen saturation (O2Ring nights).', null,
        p => line(p, bks, { key: 'spo2min', label: 'lowest SpO₂', color: css('--line2') }, F.spo2, { minSpan: 6 })));
    }
    const cols = [['AHI', cell('ahi', F.rate.v)], ['Obstr.', cell('oa', F.rate.v)], ['Central', cell('ca', F.rate.v)],
      ['Hypopnea', cell('h', F.rate.v)], ['Usage', cell('usage', hours)], ['Mask breaks', cell('breaks', F.count.v)],
      ['Breaths/min', cell('rr', v => v.toFixed(1))], ['ODI 4%', cell('odi4', F.rate.v)], ['SpO₂ min', cell('spo2min', F.spo2.v)]];
    return { tiles, cards, cols };
  }

  // ------------------------------------------------------------------ report (PDF)
  /* A printable report for a clinician: who/when, three summary blocks, four charts,
     a nightly (or monthly) table, free-text notes and the method. "Download PDF" opens
     the print dialog; the page's print styles lay it out on paper. */
  const store = {
    get: k => { try { return localStorage.getItem('report.' + k) || ''; } catch { return ''; } },
    set: (k, v) => { try { localStorage.setItem('report.' + k, v); } catch { /* private mode */ } },
  };
  function kv(title, rows, note) {
    const s = document.createElement('section'); s.className = 'rp-block';
    const h = document.createElement('h3'); h.textContent = title; s.appendChild(h);
    const dl = document.createElement('dl');
    for (const [k, v] of rows) {
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      dl.append(dt, dd);
    }
    s.appendChild(dl);
    if (note) { const p = document.createElement('p'); p.className = 'rp-note'; p.textContent = note; s.appendChild(p); }
    return s;
  }
  const clockOf = m => { if (m === null) return '–'; const t = (Math.round(m) + 12 * 60) % (24 * 60); return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`; };
  const median = xs => { const v = xs.filter(x => x !== null).sort((p, q) => p - q); return v.length ? v[Math.floor(v.length / 2)] : null; };

  function renderReport(a, b, nights, bk) {
    const cp = nights.filter(n => n.cpap), ring = nights.filter(n => n.ring);
    const days = Math.round((parse(b) - parse(a)) / 864e5) + 1;
    const pct = (n, d) => (d ? `${Math.round(100 * n / d)}%` : '–');
    const comp = cp.filter(n => (FIELDS.usage(n) || 0) >= 4).length;
    const r2 = v => v.toFixed(2);

    document.getElementById('rp-period').textContent = `${long(a)} – ${long(b)}`;
    document.getElementById('rp-generated').textContent = new Date().toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
    const sources = [cp.length && `ResMed CPAP (${cp.length} night${cp.length === 1 ? '' : 's'})`,
      ring.length && `Wellue O2Ring oximeter (${ring.length} night${ring.length === 1 ? '' : 's'})`].filter(Boolean);
    document.getElementById('rp-sources').textContent = sources.join(' · ') || 'No recordings in this period';

    // lowest SpO2 of the period and the night it happened
    const low = ring.reduce((m, n) => (FIELDS.spo2min(n) !== null && (m === null || FIELDS.spo2min(n) < FIELDS.spo2min(m)) ? n : m), null);
    const blocks = [];
    if (cp.length) blocks.push(kv('CPAP therapy', [
      ['Nights used', `${cp.length} of ${days}`],
      ['Average use', fmtOr(avgOf(cp, 'usage'), hours)],
      ['Nights ≥ 4 h', `${comp} of ${cp.length} (${pct(comp, cp.length)})`],
      ['AHI (events / h asleep)', fmtOr(avgOf(cp, 'ahi'), r2)],
      ['  Obstructive apnea', fmtOr(avgOf(cp, 'oa'), r2)],
      ['  Central apnea', fmtOr(avgOf(cp, 'ca'), r2)],
      ['  Hypopnea', fmtOr(avgOf(cp, 'h'), r2)],
      ['  Unclassified apnea', fmtOr(avgOf(cp, 'ua'), r2)],
      ['RERAs (total)', `${cp.reduce((s, n) => s + (FIELDS.rera(n) || 0), 0)}`],
      ['Mask off/on per night', fmtOr(avgOf(cp, 'breaks'), v => v.toFixed(1))],
      ['Breathing rate asleep', fmtOr(avgOf(cp, 'rr'), v => `${v.toFixed(1)} /min`)],
    ], 'Events as recorded by the CPAP, counted only while asleep. Averages are per night.'));
    if (ring.length) blocks.push(kv('Oximetry', [
      ['Nights recorded', `${ring.length} of ${days}`],
      ['ODI 4% (drops / h)', fmtOr(avgOf(ring, 'odi4'), r2)],
      ['Mean SpO₂', fmtOr(mean(ring.map(n => num(n.s.spo2_mean))), v => `${v.toFixed(1)}%`)],
      ['Lowest SpO₂', low ? `${Math.round(FIELDS.spo2min(low))}% (${short(low.d)})` : '–'],
      ['Time below 90% (T90)', fmtOr(mean(ring.map(n => num(n.s.t90_min))), v => `${v.toFixed(1)} min / night`)],
      ['Pulse asleep', fmtOr(avgOf(ring, 'hr'), v => `${v.toFixed(1)} bpm`)],
    ], 'Finger oximeter, 4-second samples.'));
    if (nights.length) blocks.push(kv('Sleep (estimated)', [
      ['Time asleep', fmtOr(avgOf(nights, 'tst'), hours)],
      ['Time in bed', fmtOr(mean(nights.map(n => (num(n.s.tib_min) !== null ? n.s.tib_min / 60 : null))), hours)],
      ['Sleep efficiency', fmtOr(avgOf(nights, 'eff'), F.pct.v)],
      ['Awake after falling asleep', fmtOr(avgOf(nights, 'waso'), F.min.v)],
      ['Awakenings (≥ 1 min)', fmtOr(mean(nights.map(n => num(n.s.awakenings))), v => v.toFixed(1))],
      ['Typical sleep window', `${clockOf(median(nights.map(FIELDS.onset)))} – ${clockOf(median(nights.map(FIELDS.wake)))}`],
      ['REM', fmtOr(avgOf(nights, 'rem_pct'), v => `${Math.round(v)}% of sleep`)],
      ['Deep (est.)', fmtOr(avgOf(ring, 'deep_pct'), v => `${Math.round(v)}% of sleep`)],
      ['Arousals', fmtOr(avgOf(ring, 'arousal_idx'), v => `${v.toFixed(1)} / h`)],
    ], 'Estimated from breathing, pulse and movement, not EEG.'));
    document.getElementById('rp-summary').replaceChildren(...blocks);

    const charts = [
      card('Sleep stages per night (estimated)', null, STAGES(), p => bars(p, bk.list, STAGES(), F.hours, { totalLabel: 'In bed' })),
    ];
    if (cp.length) {
      charts.push(card('AHI by event type', null, EVENTS().map(e => ({ ...e, label: e.label.replace('Apnea (unclassified)', 'Unclassified') })),
        p => bars(p, bk.list, EVENTS(), F.rate, { totalLabel: 'AHI', min: 0.5 })));
      charts.push(card('CPAP use (hours, line at 4 h)', null, null,
        p => bars(p, bk.list, [{ key: 'usage', label: 'mask on', color: css('--accent') }], F.hours, { ref: { value: 4, label: '4 h' } })));
    }
    if (ring.length) charts.push(card('Lowest SpO₂ per night', null, null,
      p => line(p, bk.list, { key: 'spo2min', label: 'lowest SpO₂', color: css('--line2') }, F.spo2, { minSpan: 6 })));
    charts.forEach((c, i) => { c.querySelector('.plot').dataset.paperW = i === 0 ? 720 : 350; });
    document.getElementById('rp-charts').replaceChildren(...charts);
    cards = charts;
    charts.forEach(c => c._draw());

    // nightly table up to ~3 months, monthly averages beyond
    // [header, field, format]: a night's value, or a month's average of it
    const cols = [['Use', 'usage', hours], ['AHI', 'ahi', r2], ['OA', 'oa', r2], ['CA', 'ca', r2], ['H', 'h', r2],
      ['Asleep', 'tst', hours], ['Eff.', 'eff', F.pct.v], ['WASO', 'waso', v => `${Math.round(v)}`],
      ['ODI4', 'odi4', r2], ['SpO₂ min', 'spo2min', F.spo2.v]];
    const t = document.createElement('table'); t.className = 'rp-table';
    const hr = t.createTHead().insertRow();
    const monthly = nights.length > 92;
    for (const h of [monthly ? 'Month' : 'Night', ...(monthly ? ['Nights'] : []), ...cols.map(c => c[0])]) {
      const th = document.createElement('th'); th.textContent = h; hr.appendChild(th);
    }
    const tb = t.createTBody();
    if (!monthly) {
      for (const n of nights) {
        const r = tb.insertRow(); r.insertCell().textContent = short(n.d);
        for (const [, f, fmt] of cols) r.insertCell().textContent = fmtOr(FIELDS[f](n), fmt);
      }
    } else {
      const groups = new Map();
      for (const n of nights) { const k = n.d.slice(0, 7); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(n); }
      for (const [k, ns] of groups) {
        const r = tb.insertRow();
        r.insertCell().textContent = `${MON[+k.slice(5) - 1]} ${k.slice(0, 4)}`;
        r.insertCell().textContent = `${ns.length}`;
        for (const [, f, fmt] of cols) r.insertCell().textContent = fmtOr(avgOf(ns, f), fmt);
      }
    }
    document.getElementById('rp-table-title').textContent = monthly ? 'Monthly averages' : 'Night by night';
    document.getElementById('rp-table').replaceChildren(nights.length ? t : Object.assign(document.createElement('p'), { textContent: 'No nights in this period.' }));
    document.getElementById('empty').hidden = nights.length > 0;
  }

  if (PAGE === 'report') {
    const name = document.getElementById('rp-name-in'), notes = document.getElementById('rp-notes-in');
    const sync = () => {
      document.getElementById('rp-name').textContent = name.value.trim();
      const out = document.getElementById('rp-notes');
      out.textContent = notes.value.trim();
      out.closest('section').hidden = !notes.value.trim();
    };
    name.value = store.get('name'); notes.value = store.get('notes');
    name.addEventListener('input', () => { store.set('name', name.value); sync(); });
    notes.addEventListener('input', () => { store.set('notes', notes.value); sync(); });
    sync();
    // print in light colours whatever the screen theme, and name the PDF after the period
    let title = document.title;
    addEventListener('beforeprint', () => {
      document.documentElement.dataset.print = '1';
      const [a, b] = span();
      title = document.title; document.title = `Sleep report ${a} to ${b}`;
      render();
    });
    addEventListener('afterprint', () => { delete document.documentElement.dataset.print; document.title = title; render(); });
    document.getElementById('rp-print').addEventListener('click', () => window.print());
  }

  // ------------------------------------------------------------------ render
  let cards = [];
  function render() {
    const [a, b] = span();
    const nights = ALL.filter(n => n.d >= a && n.d <= b);
    const bk = buckets(nights, a, b);
    document.getElementById('range-label').textContent =
      `${long(a)} – ${long(b)}${bk.weekly ? ' · weekly averages' : ''}`;
    if (PAGE === 'report') { renderReport(a, b, nights, bk); return; }
    const page = (PAGE === 'cpap' ? cpapPage : sleepPage)(nights, bk.list);
    document.getElementById('tiles').replaceChildren(...page.tiles);
    const host = document.getElementById('charts');
    host.replaceChildren(...page.cards);
    cards = page.cards;
    const empty = document.getElementById('empty');
    empty.hidden = nights.length > 0;
    cards.forEach(c => c._draw());
    table(nights, page.cols);
  }
  let rt;
  addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => cards.forEach(c => c._draw()), 150); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', render);
  syncControls();
  render();
})();
