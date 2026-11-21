/* Upload: drop or pick whole folders (ResMed SD card, O2 Insight Pro DATA folder), loose
   files or zips. Files are sorted in the browser so only what the analysis uses is sent,
   nights already imported are skipped unless "Replace" is ticked, and the rest goes up
   in batches, oldest night first (CPAP-only nights reuse the clock correction of the
   closest earlier night with both devices). */
(() => {
  const form = document.getElementById('upform');
  if (!form) return;
  const drop = document.getElementById('drop');
  const pickFiles = document.getElementById('pick-files');
  const pickFolder = document.getElementById('pick-folder');
  const summary = document.getElementById('summary');
  const go = document.getElementById('go');
  const clear = document.getElementById('clear');
  const progress = document.getElementById('progress');
  const results = document.getElementById('results');
  const BATCH_BYTES = 40 * 1024 * 1024;

  let found = [];  // {file, kind, night}
  // night date -> the files it already has, so an existing night can still take a
  // device it is missing (e.g. the O2Ring folder imported after the SD card)
  let existing = new Map();
  fetch('/api/nights').then(r => r.json()).then(ns => {
    existing = new Map(ns.map(n => [n.night_date, new Set(n.files || [])])); render();
  }).catch(() => {});
  const isNew = f => !f.night || !existing.has(f.night) || !existing.get(f.night).has(f.file.name);

  // nights run noon to noon and are named after the evening they start
  const nightOf = (y, mo, d, h) => {
    const t = new Date(Date.UTC(+y, +mo - 1, +d, +h) - 12 * 3600e3);
    return t.toISOString().slice(0, 10);
  };

  async function isRingRaw(file) {
    if (file.size < 45) return false;
    const b = new DataView(await file.slice(0, 40).arrayBuffer());
    return b.getUint8(0) === 3 && b.getUint8(1) === 0 && b.getUint32(9, true) === file.size;
  }

  async function classify(file) {
    const name = file.name;
    let m = name.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})\d{4}_(BRP|EVE|PLD)\.edf$/i);
    if (m) return { kind: m[5].toUpperCase(), night: nightOf(m[1], m[2], m[3], m[4]) };
    if (/\.edf$/i.test(name)) return { kind: 'ignored' };  // STR, SAD, CSL, ...
    if (/\.zip$/i.test(name)) return { kind: 'zip' };
    if (/\.(csv|txt)$/i.test(name)) return { kind: 'csv' };
    m = name.match(/^(\d{4})(\d{2})(\d{2})(\d{2})\d{4}$/);
    if (m && await isRingRaw(file)) return { kind: 'ring', night: nightOf(m[1], m[2], m[3], m[4]) };
    if (!name.includes('.') && await isRingRaw(file)) return { kind: 'ring' };
    return { kind: 'ignored' };
  }

  // ---- collecting files from drops (folders are walked recursively) and pickers ----
  function readEntries(dir) {
    const reader = dir.createReader();
    return new Promise((resolve, reject) => {
      const all = [];
      const next = () => reader.readEntries(batch => {
        if (!batch.length) return resolve(all);
        all.push(...batch); next();
      }, reject);
      next();
    });
  }
  async function walk(entry, out) {
    if (entry.isFile) out.push(await new Promise((res, rej) => entry.file(res, rej)));
    else if (entry.isDirectory) for (const e of await readEntries(entry)) await walk(e, out);
  }
  async function add(files) {
    summary.innerHTML = '<p class="muted">Reading files…</p>';
    const seen = new Set(found.map(f => f.file.name + '|' + f.file.size));
    for (const file of files) {
      const key = file.name + '|' + file.size;
      if (seen.has(key)) continue;
      seen.add(key);
      found.push({ file, ...(await classify(file)) });
    }
    render();
  }

  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', async e => {
    e.preventDefault();
    drop.classList.remove('over');
    const entries = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    const files = [];
    if (entries.length) for (const en of entries) await walk(en, files);
    else files.push(...e.dataTransfer.files);
    add(files);
  });
  pickFiles.addEventListener('change', () => { add([...pickFiles.files]); pickFiles.value = ''; });
  pickFolder.addEventListener('change', () => { add([...pickFolder.files]); pickFolder.value = ''; });
  clear.addEventListener('click', () => { found = []; results.innerHTML = ''; render(); });
  form.querySelector('[name=replace]').addEventListener('change', render);

  // ---- summary ----
  const replace = () => form.querySelector('[name=replace]').checked;
  const usable = () => found.filter(f => f.kind !== 'ignored');
  const toSend = () => usable().filter(f => replace() || isNew(f));
  const fmtMB = b => b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1e3)) + ' kB';

  function render() {
    if (!found.length) {
      summary.innerHTML = '';
      go.disabled = true; clear.hidden = true;
      return;
    }
    const n = k => found.filter(f => f.kind === k).length;
    const nights = [...new Set(usable().filter(f => f.night).map(f => f.night))].sort();
    const old = nights.filter(d => existing.has(d));
    const fresh = nights.filter(d => !existing.has(d));
    // existing nights this upload adds files to (a second device, a missing session)
    const grow = old.filter(d => usable().some(f => f.night === d && isNew(f)));
    const done = old.filter(d => !grow.includes(d));
    const send = toSend();
    const row = (ok, text) => `<li class="${ok ? 'ok' : 'no'}">${ok ? '✓' : '–'} ${text}</li>`;
    const range = ds => ds.length ? (ds.length > 1 ? `${ds[0]} → ${ds[ds.length - 1]}` : ds[0]) : '';
    summary.innerHTML = `<ul class="found">
      ${row(n('BRP'), `<b>${n('BRP')}</b> CPAP flow sessions (<code>*_BRP.edf</code>)`)}
      ${row(n('EVE'), `<b>${n('EVE')}</b> CPAP event files (<code>*_EVE.edf</code>)`)}
      ${row(n('PLD'), `<b>${n('PLD')}</b> CPAP machine-data files: pressure, leak, flow limitation (<code>*_PLD.edf</code>)`)}
      ${row(n('ring') + n('csv'), `<b>${n('ring')}</b> O2Ring recordings${n('csv') ? ` + <b>${n('csv')}</b> CSV exports` : ''}`)}
      ${n('zip') ? row(true, `<b>${n('zip')}</b> zip file${n('zip') > 1 ? 's' : ''} (unpacked on the server)`) : ''}
      ${row(nights.length, `<b>${nights.length}</b> night${nights.length === 1 ? '' : 's'} ${range(nights)}`)}
    </ul>
    ${old.length && replace() ? `<p class="muted small">${old.length} of them already imported: they will be replaced (your wake-log entries are kept).</p>` : ''}
    ${grow.length && !replace() ? `<p class="muted small">${grow.length} already-imported night${grow.length === 1 ? '' : 's'} get${grow.length === 1 ? 's' : ''} new files and will be re-analysed: ${range(grow)}.</p>` : ''}
    ${done.length && !replace() ? `<p class="muted small">${done.length} night${done.length === 1 ? ' is' : 's are'} already fully imported and will be skipped. Tick <i>Replace nights that already exist</i> in Options to re-import ${done.length === 1 ? 'it' : 'them'}.</p>` : ''}
    ${fresh.length && old.length ? `<p class="muted small">New: ${range(fresh)}</p>` : ''}
    <p class="muted small">${n('ignored')} other file${n('ignored') === 1 ? '' : 's'} not needed (settings, STR.edf, SAD/CSL, …).
      To upload: ${send.length} file${send.length === 1 ? '' : 's'}, ${fmtMB(send.reduce((a, f) => a + f.file.size, 0))}.</p>`;
    go.disabled = !send.length;
    go.textContent = send.length ? `Upload & analyse` : (usable().length ? 'Nothing new to upload' : 'No usable files');
    clear.hidden = false;
  }

  // ---- upload in night-ordered batches ----
  function batches() {
    const send = toSend();
    const byNight = new Map();
    for (const f of send.filter(f => f.night)) {
      if (!byNight.has(f.night)) byNight.set(f.night, []);
      byNight.get(f.night).push(f.file);
    }
    const out = [];
    let cur = [], size = 0;
    for (const night of [...byNight.keys()].sort()) {
      const fs = byNight.get(night), s = fs.reduce((a, f) => a + f.size, 0);
      if (cur.length && size + s > BATCH_BYTES) { out.push(cur); cur = []; size = 0; }
      cur.push(...fs); size += s;
    }
    if (cur.length) out.push(cur);
    // files whose night is only known after parsing (CSV exports, zips) go last, together
    const rest = send.filter(f => !f.night).map(f => f.file);
    if (rest.length) out.push(rest);
    return out;
  }

  function post(files, done, total) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const [k, v] of new FormData(form).entries()) if (k !== 'files') fd.append(k, v);
      if (!fd.has('replace')) fd.append('replace', 'false');
      if (!fd.has('auto_sync')) fd.append('auto_sync', 'false');
      for (const f of files) fd.append('files', f, f.name);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/nights');
      xhr.upload.onprogress = e => {
        if (e.lengthComputable) setProgress((done + e.loaded) / total, 'Uploading');
      };
      xhr.upload.onload = () => setProgress((done + files.reduce((a, f) => a + f.size, 0)) / total, 'Analysing');
      xhr.onload = () => xhr.status === 200 ? resolve(JSON.parse(xhr.responseText))
        : reject(new Error(xhr.responseText || xhr.statusText));
      xhr.onerror = () => reject(new Error('network error'));
      xhr.send(fd);
    });
  }
  function setProgress(frac, label) {
    progress.hidden = false;
    progress.querySelector('.bar').style.width = (100 * Math.min(1, frac)).toFixed(1) + '%';
    progress.querySelector('.label').textContent = `${label}… ${Math.round(100 * Math.min(1, frac))}%`;
  }
  function addResult(r) {
    const li = document.createElement('li');
    const err = r.status.startsWith('error');
    li.className = err ? 'err' : r.id ? 'ok' : 'skip';
    li.innerHTML = r.id ? `<a href="/nights/${r.id}">${r.night_date}</a> <span class="muted">${r.status}</span>`
      : `${r.night_date} <span class="muted">${r.status}</span>`;
    results.appendChild(li);
  }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const bs = batches();
    if (!bs.length) return;
    const total = bs.flat().reduce((a, f) => a + f.size, 0) || 1;
    go.disabled = true; clear.hidden = true;
    results.innerHTML = '';
    let done = 0, ok = 0;
    for (const b of bs) {
      try {
        const res = await post(b, done, total);
        res.forEach(addResult);
        ok += res.filter(r => r.id).length;
      } catch (err) {
        addResult({ night_date: 'Batch', status: 'error: ' + err.message.slice(0, 200) });
      }
      done += b.reduce((a, f) => a + f.size, 0);
    }
    setProgress(1, `Done: ${ok} night${ok === 1 ? '' : 's'} imported`);
    progress.querySelector('.label').textContent = `Done: ${ok} night${ok === 1 ? '' : 's'} imported.`;
    found = [];
    if (!document.getElementById('again')) {
      const again = document.createElement('a');
      again.id = 'again'; again.href = '/'; again.className = 'btn'; again.textContent = 'Show history';
      results.after(again);
    }
    render();
  });

  render();
})();
