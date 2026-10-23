/* GDPNow app window: the episode map editor (systemFileMap.json). */
'use strict';

const KNOWN = ['BJ', 'M', 'PM', 'D', 'PD', 'PC', 'RC', 'CSP', 'CRP', 'CG', 'BRM', 'PBJ'];
const COLORS = {
  BJ: '#F59E0B', M: '#34D399', PM: '#10B981', D: '#60A5FA', PD: '#818CF8', PC: '#F472B6',
  RC: '#A78BFA', CSP: '#F87171', CRP: '#FB923C', CG: '#2DD4BF', BRM: '#FACC15', PBJ: '#A3E635',
};
const colorOf = (t) => COLORS[t] || '#94A3B8';
const TIME_RE = /^\d{1,3}:[0-5]\d$/;
const isTime = (v) => typeof v === 'string' && (v === '' || TIME_RE.test(v));
const toSec = (v) => (TIME_RE.test(v || '') ? +v.split(':')[0] * 60 + +v.split(':')[1] : null);

const M = { data: null, sel: -1, dirty: false, filter: 'all', q: '', ed: null };

/* meta <-> {scenes, other}: "X": start, "EX": end; everything else is an extra field */
function parseMeta(meta) {
  const keys = Object.keys(meta);
  const isEnd = (k) => k.length > 1 && k[0] === 'E' && isTime(meta[k]) && (KNOWN.includes(k.slice(1)) || k.slice(1) in meta);
  const scenes = [], other = [];
  for (const k of keys) {
    const v = meta[k];
    if (isEnd(k)) {
      if (!(k.slice(1) in meta)) scenes.push({ type: k.slice(1), start: '', end: v });
      continue;
    }
    if (isTime(v) && (KNOWN.includes(k) || ('E' + k) in meta)) scenes.push({ type: k, start: v, end: meta['E' + k] || '' });
    else other.push({ key: k, value: v });
  }
  scenes.sort((a, b) => (toSec(a.start) ?? 1e9) - (toSec(b.start) ?? 1e9));
  return { scenes, other };
}

function buildMeta(ed) {
  const meta = {};
  for (const s of ed.scenes) {
    const t = s.type.trim().toUpperCase();
    if (!t) continue;
    meta[t] = s.start;
    if (s.end) meta['E' + t] = s.end;
  }
  for (const o of ed.other) if (o.key.trim()) meta[o.key.trim()] = o.value;
  return meta;
}

const label = (key) => key.replace(/\.[^.]+$/, '');
const cur = () => M.data.episodes[M.sel];

function setDirty(v) {
  M.dirty = v;
  $('#savebar').classList.toggle('hidden', !v);
  window.pywebview.api.set_map_dirty(v).catch(() => {});
}

function commit() {
  cur().meta = buildMeta(M.ed);
  setDirty(true);
  renderTimeline();
  renderListRow(M.sel);
}

/* ------------------------------------------------------------- list */
function epBars(meta) {
  const types = [...new Set(parseMeta(meta).scenes.map((s) => s.type))];
  return el('div', { class: 'bars' }, types.slice(0, 10).map((t) => el('i', { style: `background:${colorOf(t)}`, title: t })));
}

function epRow(ep, i) {
  return el('div', { class: 'ep' + (i === M.sel ? ' active' : ''), 'data-i': i, onclick: () => selectEp(i) },
    el('span', { class: 'num' }, label(ep.key)), epBars(ep.meta),
    el('span', { class: 'dot ' + (ep.file ? 'good' : ''), title: ep.file ? ep.file : 'No file for this episode in your folder' }));
}

function visible(ep) {
  if (M.filter === 'file' && !ep.file) return false;
  if (M.filter === 'nofile' && ep.file) return false;
  return !M.q || label(ep.key).toLowerCase().includes(M.q);
}

function orphanRow(o) {
  return el('div', { class: 'ep', title: o.path, onclick: () => addEpisode(o.label, o.path) },
    el('span', { class: 'num' }, o.label),
    el('span', { class: 'hint', style: 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, o.path.split(/[\\/]/).pop()),
    el('span', { class: 'btn icon ghost' }, icon('i-plus')));
}

function renderList() {
  const box = $('#ep-list');
  box.innerHTML = '';
  if (M.filter === 'new') {
    const taken = new Set(M.data.episodes.map((e) => label(e.key).toLowerCase()));
    const list = (M.data.orphans || []).filter((o) => !taken.has(o.label.toLowerCase()) && (!M.q || o.label.toLowerCase().includes(M.q)));
    if (!list.length) box.append(el('div', { class: 'hint', style: 'padding:14px 8px' }, 'Every numbered file in your folder already has a map entry.'));
    list.forEach((o) => box.append(orphanRow(o)));
  } else {
    M.data.episodes.forEach((ep, i) => { if (visible(ep)) box.append(epRow(ep, i)); });
  }
  const found = M.data.episodes.filter((e) => e.file).length;
  $('#map-sub').textContent = `${M.data.episodes.length} episodes · ${found} in your folder · ${M.data.is_bundled ? 'bundled map' : M.data.file}`;
  $('#btn-map-bundled').classList.toggle('hidden', M.data.is_bundled);
}

function renderListRow(i) {
  const old = $(`#ep-list .ep[data-i="${i}"]`);
  if (old) old.replaceWith(epRow(M.data.episodes[i], i));
}

/* ----------------------------------------------------------- editor */
function selectEp(i) {
  M.sel = i;
  $$('#ep-list .ep').forEach((x) => x.classList.toggle('active', +x.dataset.i === i));
  M.ed = parseMeta(cur().meta);
  $('#ed-empty').classList.add('hidden');
  $('#ed').classList.remove('hidden');
  renderEditor();
}

function renderEditor() {
  const ep = cur();
  const name = label(ep.key);
  $('#ed-title').textContent = /^\d+\+?$/.test(name) || /^D\d+$/i.test(name) ? `Episode ${name}` : name;
  const f = $('#ed-file');
  f.textContent = ep.file || 'No file for this episode was found in your video folder';
  f.classList.toggle('missing', !ep.file);
  $('#btn-ed-play').disabled = !ep.file;
  renderScenes();
  renderExtra();
  renderTimeline();
  if (ep.file && ep._dur === undefined) {
    ep._dur = null;
    call('file_duration', ep.file).then((d) => { ep._dur = d; if (cur() === ep) renderTimeline(); });
  }
}

function renderTimeline() {
  const ep = cur();
  const tl = $('#timeline');
  tl.innerHTML = '';
  tl.append(el('div', { class: 'tl-track' }));
  const ends = M.ed.scenes.flatMap((s) => [toSec(s.start), toSec(s.end)]).filter((x) => x !== null);
  const dur = ep._dur || (ends.length ? Math.max(...ends) + 60 : 3600);
  M.ed.scenes.forEach((s, idx) => {
    const a = toSec(s.start), b = toSec(s.end);
    if (a === null) return;
    const left = (a / dur) * 100, width = b !== null && b > a ? ((b - a) / dur) * 100 : 0;
    tl.append(el('div', {
      class: 'tl-seg' + (width ? '' : ' point'),
      style: `left:${left}%;width:${width}%;background:${colorOf(s.type)}`,
      title: `${s.type}  ${s.start}${s.end ? ' – ' + s.end : ''}`,
      onclick: () => { const row = $(`#scene-rows tr[data-i="${idx}"] input.tin`); if (row) row.focus(); },
    }, width > 4 ? s.type : ''));
  });
  const mins = dur / 60;
  const step = [1, 2, 5, 10, 15, 30].find((x) => mins / x <= 12) || 60;
  for (let m = 0; m <= mins; m += step) {
    tl.append(el('div', { class: 'tl-tick', style: `left:${(m * 60 / dur) * 100}%` }, `${m}m`));
  }
  const lg = $('#legend');
  lg.innerHTML = '';
  for (const t of [...new Set(M.ed.scenes.map((s) => s.type).filter(Boolean))]) {
    lg.append(el('span', {}, el('i', { class: 'swatch', style: `background:${colorOf(t)}` }), t));
  }
  if (!ep._dur) lg.append(el('span', { style: 'color:var(--faint)' }, ep.file ? 'reading length…' : 'video length unknown'));
}

function timeInput(s, field) {
  const inp = el('input', { class: 'tin', value: s[field], placeholder: 'mm:ss' });
  const check = () => inp.classList.toggle('bad', !isTime(inp.value.trim()));
  check();
  inp.addEventListener('input', () => {
    check();
    if (isTime(inp.value.trim())) { s[field] = inp.value.trim(); commit(); updateLengths(); }
  });
  return inp;
}

async function grabInto(s, field, inp) {
  const t = await call('preview_time');
  if (t === null || t === undefined) { toast('Open the episode in the player first, then pause where the scene starts or ends.', 'err'); return; }
  inp.value = mmss(t);
  inp.dispatchEvent(new Event('input'));
}

function lengthText(s) {
  const a = toSec(s.start), b = toSec(s.end);
  return a !== null && b !== null ? (b >= a ? mmss(b - a) : 'ends before start') : '';
}

function updateLengths() {
  $$('#scene-rows tr').forEach((tr) => { const s = M.ed.scenes[+tr.dataset.i]; tr.querySelector('.len').textContent = lengthText(s); });
}

function renderScenes() {
  const body = $('#scene-rows');
  body.innerHTML = '';
  M.ed.scenes.forEach((s, i) => {
    const typeIn = el('input', { class: 'tin type', value: s.type, list: 'scene-types', placeholder: 'type' });
    const sw = el('i', { class: 'swatch', style: `background:${colorOf(s.type)}` });
    typeIn.addEventListener('input', () => {
      s.type = typeIn.value.trim().toUpperCase();
      sw.style.background = colorOf(s.type);
      commit();
    });
    const st = timeInput(s, 'start'), en = timeInput(s, 'end');
    body.append(el('tr', { 'data-i': i },
      el('td', {}, el('div', { class: 'tcell' }, sw, typeIn)),
      el('td', {}, el('div', { class: 'tcell' }, st,
        el('button', { class: 'btn icon ghost', title: 'Use the time showing in the player', onclick: () => grabInto(s, 'start', st) }, icon('i-clock')))),
      el('td', {}, el('div', { class: 'tcell' }, en,
        el('button', { class: 'btn icon ghost', title: 'Use the time showing in the player', onclick: () => grabInto(s, 'end', en) }, icon('i-clock')))),
      el('td', {}, el('span', { class: 'len' }, lengthText(s))),
      el('td', {},
        el('button', { class: 'btn icon ghost', title: 'Play from this scene', disabled: !cur().file || toSec(s.start) === null ? '' : null,
          onclick: () => call('preview', cur().file, toSec(s.start) || 0) }, icon('i-play')),
        el('button', { class: 'btn icon ghost danger', title: 'Remove scene',
          onclick: () => { M.ed.scenes.splice(i, 1); commit(); renderScenes(); } }, icon('i-trash'))),
    ));
  });
  if (!M.ed.scenes.length) body.append(el('tr', {}, el('td', { colspan: 5, class: 'hint', style: 'text-align:center' }, 'No scenes yet.')));
}

function renderExtra() {
  const box = $('#kv-rows');
  box.innerHTML = '';
  $('#extra-sum').textContent = `Extra fields (${M.ed.other.length})`;
  M.ed.other.forEach((o, i) => {
    const k = el('input', { class: 'tin wide', value: o.key, placeholder: 'name' });
    const v = el('input', { class: 'tin wide', value: typeof o.value === 'string' ? o.value : JSON.stringify(o.value), placeholder: 'value' });
    k.addEventListener('input', () => { o.key = k.value; commit(); });
    v.addEventListener('input', () => {
      const t = v.value;
      o.value = /^-?\d+(\.\d+)?$/.test(t.trim()) ? Number(t) : t;
      commit();
    });
    box.append(el('div', { class: 'kv' }, k, v,
      el('button', { class: 'btn icon ghost danger', title: 'Remove field', onclick: () => { M.ed.other.splice(i, 1); commit(); renderExtra(); } }, icon('i-trash'))));
  });
}

/* ---------------------------------------------------------- actions */
async function loadMap() {
  const keep = M.sel >= 0 && M.data ? M.data.episodes[M.sel].key : null;
  M.data = await call('map_data');
  M.sel = keep ? M.data.episodes.findIndex((e) => e.key === keep) : -1;
  setDirty(false);
  renderList();
  if (M.sel >= 0) selectEp(M.sel);
  else { $('#ed').classList.add('hidden'); $('#ed-empty').classList.remove('hidden'); }
}

async function saveMap() {
  const bad = $$('#scene-rows .tin.bad');
  if (bad.length) { toast('Fix the times marked in red first (mm:ss, like 41:05).', 'err'); bad[0].focus(); return; }
  const res = await call('save_map', M.data.episodes.map((e) => ({ key: e.key, meta: e.meta })));
  if (res && res.ok) {
    setDirty(false);
    toast(`Map saved (${res.count} episodes). The previous version is kept as a .bak file.`, 'ok');
    await rescan(true);
  }
}

async function addEpisode(number = null, file = null) {
  const txt = typeof number === 'string' ? number
    : await modal({ title: 'Add episode', text: 'Episode number, like 512 or D07.', input: '', ok: 'Add' });
  if (!txt) return;
  const key = await call('normalize_key', txt);
  if (!key) return;
  let i = M.data.episodes.findIndex((e) => e.key.toLowerCase() === key.toLowerCase());
  if (i < 0) {
    M.data.episodes.push({ key, meta: {}, file });
    i = M.data.episodes.length - 1;
    setDirty(true);
    toast(file ? `Added ${label(key)}. Now add its scenes: open it in the player and use the clock buttons.`
      : `Added ${label(key)}. Its file shows up after you save (the folder is searched again).`);
  }
  M.q = ''; $('#ep-search').value = '';
  M.filter = 'all'; $$('#ep-filter button').forEach((b) => b.classList.toggle('active', b.dataset.f === 'all'));
  renderList();
  selectEp(i);
  $(`#ep-list .ep[data-i="${i}"]`).scrollIntoView({ block: 'nearest' });
}

async function deleteEpisode() {
  const ep = cur();
  const ok = await modal({ title: `Delete ${label(ep.key)}?`, text: 'It is removed from the map when you save. The video file is not touched.', ok: 'Delete', danger: true });
  if (!ok) return;
  M.data.episodes.splice(M.sel, 1);
  M.sel = -1;
  setDirty(true);
  renderList();
  $('#ed').classList.add('hidden');
  $('#ed-empty').classList.remove('hidden');
}

async function confirmDiscard() {
  if (!M.dirty) return true;
  return !!(await modal({ title: 'Unsaved changes', text: 'Your map changes are not saved yet.', ok: 'Discard them', danger: true }));
}

window.openMapView = async () => { if (!M.data) await loadMap(); };
window.selectEpisodeByLabel = (lbl) => {
  if (!M.data) return;
  const i = M.data.episodes.findIndex((e) => label(e.key).toLowerCase() === String(lbl).toLowerCase());
  if (i >= 0) { selectEp(i); const r = $(`#ep-list .ep[data-i="${i}"]`); if (r) r.scrollIntoView({ block: 'center' }); }
};
window.onScanUpdated = async () => {
  if (!M.data || M.dirty) return;
  const files = (await call('map_data')).episodes;
  const byKey = Object.fromEntries(files.map((e) => [e.key, e.file]));
  M.data.episodes.forEach((e) => { e.file = byKey[e.key] || null; });
  renderList();
  if (M.sel >= 0) renderEditor();
};
window.showUnsavedOnClose = async () => {
  const r = await modal({ title: 'Save the map before closing?', text: 'You have changes that are not saved.', ok: 'Save and close', cancel: 'Close without saving' });
  if (r) await saveMap();
  if (!M.dirty || r === null) await call('force_close');
};

window.addEventListener('pywebviewready', () => {
  $('#scene-types').append(...KNOWN.map((t) => el('option', { value: t })));
  $('#ep-search').addEventListener('input', (e) => { M.q = e.target.value.trim().toLowerCase(); renderList(); });
  $$('#ep-filter button').forEach((b) => b.onclick = () => {
    M.filter = b.dataset.f; $$('#ep-filter button').forEach((x) => x.classList.toggle('active', x === b)); renderList();
  });
  $('#btn-add-ep').onclick = () => addEpisode();
  $('#btn-del-ep').onclick = deleteEpisode;
  $('#btn-add-scene').onclick = () => {
    M.ed.scenes.push({ type: '', start: '', end: '' });
    renderScenes();
    const ins = $$('#scene-rows input.type');
    ins[ins.length - 1].focus();
  };
  $('#btn-add-kv').onclick = () => { M.ed.other.push({ key: '', value: '' }); renderExtra(); };
  $('#btn-ed-play').onclick = () => call('preview', cur().file, 0);
  $('#btn-save').onclick = saveMap;
  $('#btn-revert').onclick = async () => { if (await confirmDiscard()) await loadMap(); };
  $('#btn-map-other').onclick = async () => {
    if (!(await confirmDiscard())) return;
    const picked = await call('pick_map_file');
    if (picked && picked.error) { toast(picked.error, 'err'); return; }
    if (picked) { await refreshState(); await rescan(true); await loadMap(); }
  };
  $('#btn-map-bundled').onclick = async () => {
    if (!(await confirmDiscard())) return;
    await call('use_bundled_map'); await refreshState(); await rescan(true); await loadMap();
  };
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && M.dirty) { e.preventDefault(); saveMap(); }
  });
});
