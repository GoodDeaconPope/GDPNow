/* GDPNow app window: shared helpers + the Play view. */
'use strict';

// macOS (WebKit window): Cmd shortcuts and the Terminal runner for the video
const IS_MAC = /Mac/.test(navigator.platform || navigator.userAgent);
const GRWM_RUNNER = IS_MAC ? 'run_grwm.command' : 'run_grwm.bat';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) n.append(kid);
  return n;
};
const icon = (id) => {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const u = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  u.setAttribute('href', '#' + id); s.append(u); return s;
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const mmss = (sec) => {
  sec = Math.max(0, Math.floor(sec || 0));
  return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
};

async function call(name, ...args) {
  try {
    return await window.pywebview.api[name](...args);
  } catch (e) {
    // a Python exception from an app call: a bug, so show the report
    if (e && typeof e === 'object') e._reported = true;
    reportPageError(`app call ${name}() failed\n${(e && (e.stack || e.message)) || e}`);
    throw e;
  }
}

/* ---------------------------------------------------- error reports */
let errOpen = false;

function showErrorReport(rep) {
  if (!rep) return;
  errOpen = true;
  const where = (rep.full.split('\n').find((l) => l.startsWith('Where: ')) || '').slice(7);
  $('#err-where').textContent = where ? `In the ${where}.` : '';
  $('#err-support').textContent = rep.support || 'DM the pope on Discord';
  $('#err-how').textContent = `Copy the text below (it is already selected: press ${IS_MAC ? 'Cmd' : 'Ctrl'}+C) and paste it into the chat.`;
  const box = $('#err-text');
  box.value = rep.short;
  const name = (rep.path || '').split(/[\\/]/).pop();
  $('#err-file').textContent = name ? `The full report is saved in the "error reports" folder as ${name}.` : '';
  $('#err-ok').onclick = () => { $('#err-modal').classList.remove('show'); errOpen = false; };
  $('#err-modal').classList.add('show');
  setTimeout(() => {
    box.focus(); box.select();
    requestAnimationFrame(() => { box.scrollTop = 0; });  // select() scrolls to the end
  }, 50);
}

async function reportPageError(details) {
  if (errOpen) return;  // one at a time
  try { showErrorReport(await window.pywebview.api.page_error_report(String(details))); }
  catch (e) { toast(`Something went wrong. DM the pope on Discord: ${details}`, 'err'); }
}

window.addEventListener('error', (ev) => {
  if (!window.pywebview || !window.pywebview.api) return;
  reportPageError(`${ev.message}\n${ev.error && ev.error.stack ? ev.error.stack : `${ev.filename}:${ev.lineno}:${ev.colno}`}`);
});
window.addEventListener('unhandledrejection', (ev) => {
  const r = ev.reason;
  if (r && r._reported) return;
  reportPageError(`Unhandled promise rejection\n${(r && (r.stack || r.message)) || r}`);
});

function toast(text, kind = '') {
  const t = el('div', { class: 'toast ' + kind }, el('div', {}, text));
  $('#toasts').append(t);
  setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, kind === 'err' ? 7000 : 3500);
}

function modal({ title, text = '', input = null, ok = 'OK', cancel = 'Cancel', danger = false }) {
  return new Promise((resolve) => {
    const bg = $('#modal'), inp = $('#modal-input'), okB = $('#modal-ok'), noB = $('#modal-cancel');
    $('#modal-title').textContent = title;
    $('#modal-text').textContent = text;
    inp.classList.toggle('hidden', input === null);
    inp.value = input || '';
    okB.textContent = ok;
    okB.className = danger ? 'btn stop' : 'btn primary';
    noB.classList.toggle('hidden', cancel === null);
    noB.textContent = cancel || '';
    bg.classList.add('show');
    if (input !== null) setTimeout(() => { inp.focus(); inp.select(); }, 30);
    const done = (v) => { bg.classList.remove('show'); okB.onclick = noB.onclick = inp.onkeydown = null; resolve(v); };
    okB.onclick = () => done(input !== null ? inp.value : true);
    noB.onclick = () => done(null);
    inp.onkeydown = (e) => { if (e.key === 'Enter') okB.click(); if (e.key === 'Escape') noB.click(); };
  });
}

/* ------------------------------------------------------------ state */
const S = {
  state: null, scan: null,
  master_type: 'RC', cols: 2, rows: 2, cycle: 1, interval: 15, minutes: 5, monitor: 1,
  cursor: 0, polling: null, running: false, lastAnswers: null, sessionDir: null,
  picks: [],  // episodes to play first (like `gdpnow 257`)
};

/* ---------------------------------------------------------- steppers */
function buildSteppers() {
  for (const box of $$('.stepper')) {
    const key = box.dataset.key, min = +box.dataset.min, max = +box.dataset.max;
    const input = el('input', { type: 'number', min, max });
    const set = (v) => {
      v = Math.round(Number(v));
      if (!Number.isFinite(v)) v = S[key];
      S[key] = Math.min(max, Math.max(min, v));
      render();
    };
    box.append(
      el('button', { onclick: () => set(S[key] - 1), title: 'Less' }, '−'),
      input,
      ...(box.dataset.unit ? [el('span', { class: 'unit' }, box.dataset.unit)] : []),
      el('button', { onclick: () => set(S[key] + 1), title: 'More' }, '+'),
    );
    input.addEventListener('change', () => set(input.value));
    input.addEventListener('wheel', (e) => { e.preventDefault(); set(S[key] + (e.deltaY < 0 ? 1 : -1)); }, { passive: false });
    box._input = input;
  }
}

/* --------------------------------------------------------- library */
function renderLibrary() {
  const st = S.state;
  const has = !!st.video_dir;
  $('#lib-empty').classList.toggle('hidden', has);
  $('#lib-full').classList.toggle('hidden', !has);
  $('#btn-rescan').classList.toggle('hidden', !has);
  if (!has) return;
  const parts = st.video_dir.split(/[\\/]/).filter(Boolean);
  $('#lib-name').textContent = parts[parts.length - 1] || st.video_dir;
  $('#lib-path').textContent = st.video_dir;
  const box = $('#lib-status');
  box.innerHTML = '';
  const sc = S.scan;
  if (!sc) {
    box.append(el('span', { class: 'chip' }, el('span', { class: 'dot warn' }), 'Looking for videos…'));
    return;
  }
  if (sc.error) {
    box.append(el('span', { class: 'chip' }, el('span', { class: 'dot bad' }), sc.error));
    return;
  }
  box.append(
    el('span', { class: 'chip' }, el('span', { class: 'dot ' + (sc.matched ? 'good' : 'bad') }),
      el('b', {}, String(sc.matched)), ` of ${sc.map_total} episodes found`),
  );
  if (sc.not_in_map) box.append(el('span', { class: 'chip', title: 'These files have an episode number, but the map has no entry (no scene times) for it. Add them on the Episode map page.' },
    el('span', { class: 'dot warn' }), el('b', {}, String(sc.not_in_map)), ' not in the map'));
  if (sc.no_number) box.append(el('span', { class: 'chip', title: 'No episode number found in these file names. Rename them to their number, e.g. 330.mp4.' },
    el('span', { class: 'dot warn' }), el('b', {}, String(sc.no_number)), ' without a number'));
  box.append(el('span', { class: 'chip' }, 'Map: ', el('b', {}, st.map_is_bundled ? 'bundled' : st.map_file.split(/[\\/]/).pop())));
  box.append(el('a', { class: 'link chip', onclick: () => call('open_report') }, 'Matching report'));
  const sub = el('label', { class: 'chip toggle', title: 'Also look inside the folders in this folder' },
    el('input', { type: 'checkbox', ...(sc.subfolders ? { checked: '' } : {}),
      onchange: async (e) => { await call('set_subfolders', e.target.checked); rescan(true); } }), 'Subfolders');
  box.append(sub);
}

function renderPills() {
  const box = $('#pills');
  box.innerHTML = '';
  for (const t of S.state.types) {
    const n = S.scan && S.scan.type_counts ? S.scan.type_counts[t] : null;
    box.append(el('div', {
      class: 'pill' + (S.master_type === t ? ' active' : '') + (n === 0 ? ' zero' : ''),
      onclick: () => { S.master_type = t; render(); },
    }, el('div', { class: 'code' }, t), el('div', { class: 'count' }, n === null ? '–' : `${n}`)));
  }
}

function renderMonitors() {
  const box = $('#monitors');
  box.innerHTML = '';
  const mons = S.state.monitors;
  if (!mons.length) { box.append(el('div', { class: 'empty-state' }, 'No screen info')); return; }
  const minX = Math.min(...mons.map((m) => m.x)), minY = Math.min(...mons.map((m) => m.y));
  const maxX = Math.max(...mons.map((m) => m.x + m.w)), maxY = Math.max(...mons.map((m) => m.y + m.h));
  const W = box.clientWidth || 300, H = box.clientHeight || 132, pad = 14;
  const k = Math.min((W - pad * 2) / (maxX - minX), (H - pad * 2) / (maxY - minY));
  const offX = (W - (maxX - minX) * k) / 2, offY = (H - (maxY - minY) * k) / 2;
  for (const m of mons) {
    box.append(el('div', {
      class: 'mon' + (S.monitor === m.index ? ' active' : ''),
      style: `left:${offX + (m.x - minX) * k + 2}px;top:${offY + (m.y - minY) * k + 2}px;width:${m.w * k - 4}px;height:${m.h * k - 4}px`,
      title: `Screen ${m.index}${m.primary ? ' (main)' : ''}: ${m.w}×${m.h}`,
      onclick: () => { S.monitor = m.index; render(); },
    }, el('div', { style: 'text-align:center' }, el('div', { class: 'n' }, String(m.index)), el('div', { class: 'res' }, `${m.w}×${m.h}`))));
  }
  const cur = mons.find((m) => m.index === S.monitor) || mons[0];
  $('#mon-label').textContent = `${cur.w}×${cur.h}${cur.primary ? ' · main' : ''}`;
}

function renderGridPick() {
  const box = $('#gridpick');
  const C = 8, R = 6;
  if (!box.childElementCount) {
    box.style.gridTemplateColumns = `repeat(${C}, 22px)`;
    for (let r = 1; r <= R; r++) for (let c = 1; c <= C; c++) {
      const cell = el('div', { class: 'gp-cell', title: `${c} × ${r}` });
      cell.dataset.c = c; cell.dataset.r = r;
      cell.onmouseenter = () => $$('.gp-cell', box).forEach((x) => x.classList.toggle('hl', +x.dataset.c <= c && +x.dataset.r <= r));
      cell.onclick = () => { S.cols = c; S.rows = r; render(); };
      box.append(cell);
    }
    box.onmouseleave = () => $$('.gp-cell', box).forEach((x) => x.classList.remove('hl'));
  }
  $$('.gp-cell', box).forEach((x) => x.classList.toggle('sel', +x.dataset.c <= S.cols && +x.dataset.r <= S.rows));
  $('#grid-label').textContent = `${S.cols} × ${S.rows}`;
}

/* ------------------------------------------------- episodes to play first */
// Same matching as the player (gdpnow_support._episode_id): 257 = 0257, D6 = D06.
const episodeId = (text) => {
  const t = String(text).trim().replace(/\.[a-z0-9]+$/i, '').toLowerCase();
  let m = /^0*(\d+)(\+?)$/.exec(t);
  if (m) return (m[1] || '0') + m[2];
  m = /^d0*(\d+)$/.exec(t);
  return m ? 'd' + m[1] : t;
};
const findEpisode = (text) => ((S.scan && S.scan.episodes) || []).find((e) => episodeId(e.label) === episodeId(text));

function addPick(text) {
  const raw = String(text || '').trim();
  if (!raw) return;
  for (const part of raw.split(/[\s,;]+/).filter(Boolean)) {
    const ep = findEpisode(part);
    if (!ep) { toast(`Episode ${part} is not in your video folder (or not in the map).`, 'err'); continue; }
    if (S.picks.includes(ep.label)) continue;
    S.picks.push(ep.label);
  }
  $('#pick-input').value = '';
  render();
}

/* Where each pick lands, mirroring the player: picks without times for the
   chosen scene are skipped; the rest fill cells 1..N, then the next turns. */
function pickPlan() {
  const p = plan();
  const usable = [];
  const rows = S.picks.map((label) => {
    const ep = findEpisode(label);
    if (!ep || !ep.types.includes(S.master_type)) return { label, note: `no ${S.master_type} times, skipped`, bad: true };
    const i = usable.length;
    usable.push(label);
    if (i >= p.grid * p.cycle) return { label, note: 'does not fit (more picks than slots)', bad: true };
    const cell = (i % p.grid) + 1, turn = Math.floor(i / p.grid) + 1;
    return { label, note: p.cycling && turn > 1 ? `cell ${cell}, turn ${turn}` : `cell ${cell}`, bad: false };
  });
  return { rows, firstInCell: usable.slice(0, p.grid) };
}

function renderPicks() {
  const dl = $('#pick-options');
  const eps = ((S.scan && S.scan.episodes) || []).filter((e) => e.types.includes(S.master_type) && !S.picks.includes(e.label));
  dl.innerHTML = '';
  eps.map((e) => e.label).sort(typeof compareEpisodes === 'function' ? compareEpisodes : undefined)
    .forEach((l) => dl.append(el('option', { value: l })));
  const box = $('#picks');
  box.innerHTML = '';
  for (const r of pickPlan().rows) {
    box.append(el('span', { class: 'pick' + (r.bad ? ' bad' : '') }, r.label,
      el('span', { class: 'slot' }, r.note),
      el('button', { title: 'Remove', onclick: () => { S.picks = S.picks.filter((x) => x !== r.label); render(); } }, '×')));
  }
  $('#pick-clear').classList.toggle('hidden', !S.picks.length);
}

/* The player's own numbers (play_videos / cycle_videos), shown before starting. */
function plan() {
  const grid = S.cols * S.rows;
  const n = S.scan && S.scan.type_counts ? S.scan.type_counts[S.master_type] : null;
  let cycle = S.cycle;
  const notes = [];
  if (n !== null) {
    if (n === 0) notes.push(`No episode in your folder has ${S.master_type} times.`);
    else if (n < grid) notes.push(`Only ${n} episode${n === 1 ? '' : 's'} have ${S.master_type}: ${grid - n} cell${grid - n === 1 ? '' : 's'} will stay empty.`);
    if (cycle > 1 && n > 0 && n < grid * cycle) {
      const c2 = Math.max(1, Math.floor(n / grid));
      notes.push(`Only ${n} episodes have ${S.master_type}, so ${c2} take turns per cell instead of ${cycle}.`);
      cycle = c2;
    }
  }
  const interval = S.master_type === 'CSP' ? 28 : S.interval;
  const total = S.minutes * 60;
  const intervals = Math.ceil(total / interval);
  return { grid, n, cycle, interval, total, intervals, switches: Math.max(0, intervals - 1), notes, cycling: cycle > 1 };
}

function renderRotation() {
  const p = plan();
  const csp = S.master_type === 'CSP';
  $('#f-interval').classList.toggle('disabled', S.cycle <= 1 || csp);
  $('#f-minutes').classList.toggle('disabled', S.cycle <= 1);
  for (const box of $$('.stepper')) box._input.value = (box.dataset.key === 'interval' && csp) ? 28 : S[box.dataset.key];
  const calc = $('#calc');
  let html;
  if (S.cycle <= 1) {
    html = `One episode per cell, no switching. It plays until you press <b>Q</b>.`;
  } else {
    const per = Math.ceil(p.intervals / Math.max(1, p.cycle));
    html = `${S.minutes} min ÷ ${p.interval} s = <b>${p.intervals} scenes per cell</b> (${p.switches} switches). ` +
      `<b>${p.cycle}</b> episodes take turns in each cell, about ${per} clips each, ${p.grid * p.cycle} episodes in all.` +
      (csp ? ' CSP always switches every 28 s.' : '');
  }
  for (const n of p.notes) html += `<span class="warn">${esc(n)}</span>`;
  calc.innerHTML = html;
}

function renderPreview() {
  const p = plan();
  const mons = S.state.monitors;
  const m = mons.find((x) => x.index === S.monitor) || mons[0] || { w: 16, h: 9 };
  const screen = $('#screen');
  screen.style.aspectRatio = `${m.w} / ${m.h}`;
  screen.style.maxHeight = '300px';
  if (m.h > m.w) { screen.style.width = 'auto'; screen.style.height = '300px'; screen.style.margin = '0 auto'; }
  else { screen.style.width = '100%'; screen.style.height = 'auto'; screen.style.margin = '0'; }
  const g = $('#screen-grid');
  g.style.gridTemplateColumns = `repeat(${S.cols}, 1fr)`;
  g.style.gridTemplateRows = `repeat(${S.rows}, 1fr)`;
  g.innerHTML = '';
  const filled = p.n === null ? p.grid : Math.min(p.grid, p.n);
  const firstInCell = pickPlan().firstInCell;
  for (let i = 0; i < p.grid; i++) {
    const on = i < filled;
    const dots = on && p.cycling && p.grid <= 36 ? Array.from({ length: Math.min(p.cycle, 6) }, () => el('i')) : [];
    g.append(el('div', { class: 'scell', style: on ? '' : 'background:rgba(255,255,255,.03)' },
      firstInCell[i] && p.grid <= 36 ? el('span', { class: 'pickname', title: 'Picked: plays first in this cell' }, firstInCell[i]) : null,
      p.grid <= 64 ? el('span', { class: 'num' }, on ? String(i + 1) : '') : null,
      dots.length ? el('div', { class: 'stack' }, dots) : null));
  }
  const sum = $('#summary');
  sum.innerHTML = '';
  sum.append(
    el('span', { class: 'chip' }, el('b', {}, S.master_type)),
    el('span', { class: 'chip' }, el('b', {}, `${S.cols}×${S.rows}`), ` · ${p.grid} cell${p.grid === 1 ? '' : 's'}`),
    el('span', { class: 'chip' }, p.cycling ? `${p.intervals} scenes · ${S.minutes} min` : 'no rotation'),
    ...(S.picks.length ? [el('span', { class: 'chip' }, el('b', {}, String(S.picks.length)), ` picked`)] : []),
  );
  const ready = S.state.video_dir && S.scan && !S.scan.error && p.n > 0;
  $('#btn-start').disabled = !ready || S.running;
  $('#start-note').textContent = !S.state.video_dir ? 'Choose your video folder first.'
    : !S.scan ? 'Looking for videos…'
    : p.n === 0 ? `No ${S.master_type} episodes in your folder. Pick another scene.`
    : 'The grid opens on the chosen screen. Q or Esc stops it.';
}

function render() {
  if (!S.state) return;
  renderLibrary();
  renderPills();
  renderPicks();
  renderMonitors();
  renderGridPick();
  renderRotation();
  renderPreview();
}

/* --------------------------------------------------------- session */
async function startSession() {
  const answers = { master_type: S.master_type, cols: S.cols, rows: S.rows, cycle: S.cycle,
    interval: S.interval, minutes: S.minutes, monitor: S.monitor, episodes: S.picks };
  const res = await call('start', answers);
  if (res && res.error) { toast(res.error, 'err'); return; }
  S.running = true; S.cursor = 0; S.sessionDir = null; S.lastAnswers = answers;
  $('#log').innerHTML = '';
  $('#card-start').classList.add('hidden');
  $('#card-done').classList.add('hidden');
  $('#card-live').classList.remove('hidden');
  const p = plan();
  $('#live-what').textContent = `${S.master_type} · ${S.cols}×${S.rows}`;
  $('#progress-wrap').classList.toggle('hidden', !p.cycling);
  $('#btn-stop').disabled = false;
  render();
  S.polling = setInterval(poll, 400);
}

function lineClass(t) {
  if (/error|traceback|failed/i.test(t)) return 'e';
  if (/^\[GRWM\]|^\[NutCounter\]|SESSION_DIR/.test(t)) return 'g';
  if (/Cycle Switch|\[E\]/.test(t)) return 'c';
  if (/Successfully|\[OK\]|Session saved/.test(t)) return 's';
  return '';
}

async function poll() {
  let r;
  try { r = await window.pywebview.api.poll(S.cursor); } catch (e) { return; }
  const log = $('#log');
  const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  for (const line of r.lines) log.append(el('div', { class: lineClass(line) }, line || ' '));
  while (log.childElementCount > 3000) log.firstChild.remove();
  if (stick) log.scrollTop = log.scrollHeight;
  S.cursor = r.cursor;
  const p = plan();
  const el_ = r.elapsed || 0;
  $('#timer').innerHTML = p.cycling ? `${mmss(el_)} <small>/ ${mmss(p.total)}</small>` : mmss(el_);
  $('#progress').style.width = `${Math.min(100, (el_ / p.total) * 100)}%`;
  if (p.cycling && el_ > p.total) $('#live-what').textContent = 'Rotation done · still playing until you stop';
  if (r.session_dir) S.sessionDir = r.session_dir;
  if (!r.running) finishSession(r);
}

function finishSession(r) {
  clearInterval(S.polling);
  S.polling = null;
  S.running = false;
  $('#card-live').classList.add('hidden');
  if (S.sessionDir) {
    $('#done-path').textContent = S.sessionDir.split(/[\\/]/).filter(Boolean).pop();
    $('#done-path').title = S.sessionDir;
    $('#card-done').classList.remove('hidden');
  } else {
    $('#card-start').classList.remove('hidden');
    const log = $('#log').cloneNode(true);
    log.id = 'last-log';
    const old = $('#last-log'); if (old) old.remove();
    log.style.marginTop = '14px';
    $('#card-start').append(log);
  }
  render();
  if (r.error_report) {
    window.pywebview.api.open_error_report(r.error_report).then(showErrorReport)
      .catch(() => reportPageError(`player error report could not be read: ${r.error_report}`));
  } else if (!S.sessionDir && r.exit_code) {
    window.pywebview.api.player_crash_report().then(showErrorReport);
  } else if (!S.sessionDir) {
    toast('The player closed without saving a session. Its log is below the Start button.', 'err');
  }
}

/* ------------------------------------------------------------ init */
async function refreshState() {
  S.state = await call('state');
  render();
}

async function rescan(force = true) {
  S.scan = null;
  render();
  S.scan = await call('scan', force);
  render();
  if (window.onScanUpdated) window.onScanUpdated();
}

async function pickFolder() {
  const p = await call('pick_video_dir');
  if (!p) return;
  await refreshState();
  await rescan(true);
}

function wireNav() {
  for (const b of $$('.nav-btn')) {
    b.onclick = () => {
      $$('.nav-btn').forEach((x) => x.classList.toggle('active', x === b));
      $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + b.dataset.view));
      if (b.dataset.view === 'map' && window.openMapView) window.openMapView();
      if (b.dataset.view === 'play') render();
    };
  }
}

async function init() {
  buildSteppers();
  wireNav();
  $('#btn-pick').onclick = pickFolder;
  $('#btn-pick-empty').onclick = pickFolder;
  $('#btn-rescan').onclick = () => rescan(true);
  $('#btn-start').onclick = startSession;
  $$('.grwm-runner').forEach((n) => { n.textContent = GRWM_RUNNER; });
  if (IS_MAC) $$('.mac-only').forEach((n) => n.classList.remove('hidden'));
  const pickInput = $('#pick-input');
  pickInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addPick(pickInput.value); } });
  // clicking a suggestion adds it right away (typing waits for Enter)
  pickInput.addEventListener('input', (e) => {
    const picked = !e.inputType || e.inputType === 'insertReplacementText';
    if (picked && [...$('#pick-options').options].some((o) => o.value === pickInput.value)) addPick(pickInput.value);
  });
  $('#pick-clear').onclick = () => { S.picks = []; render(); };
  $('#btn-stop').onclick = async () => { $('#btn-stop').disabled = true; await call('stop'); toast('Stopping and saving…'); };
  $('#btn-open-session').onclick = () => call('open_path', S.sessionDir);
  $('#btn-make-video').onclick = async () => {
    if (await call('make_video', S.sessionDir)) toast('Building grwm.mp4 in a new window. It closes when done.', 'ok');
    else toast(`${GRWM_RUNNER} was not found in the session folder.`, 'err');
  };
  $('#btn-new').onclick = () => { $('#card-done').classList.add('hidden'); $('#card-start').classList.remove('hidden'); render(); };
  window.addEventListener('resize', () => render());

  S.state = await call('state');
  const last = S.state.last || {};
  for (const k of ['master_type', 'cols', 'rows', 'cycle', 'interval', 'minutes', 'monitor']) if (last[k] !== undefined) S[k] = last[k];
  if (!S.state.monitors.some((m) => m.index === S.monitor)) {
    const prim = S.state.monitors.find((m) => m.primary);
    S.monitor = prim ? prim.index : 1;
  }
  render();
  if (S.state.video_dir) rescan(false);
  if (S.state.running) { S.running = true; S.polling = setInterval(poll, 400); }
  if (S.state.start_view === 'map') {
    $('.nav-btn[data-view="map"]').click();
    if (S.state.start_episode && window.selectEpisodeByLabel) setTimeout(() => window.selectEpisodeByLabel(S.state.start_episode), 1500);
  }
}

window.addEventListener('pywebviewready', init);
