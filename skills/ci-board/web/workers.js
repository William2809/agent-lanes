// Workers card: every worker of every project, problems first, finished folded.
// Uses the page's helpers (el, mono, dur, short, clock, sha, fetchedAt, drift) from index.html.
'use strict';
const W_LIVE = new Set(['running', 'stalled']);
const W_ATTENTION_ROWS = 6;   // more fold behind "show N more" so the runners stay near the top
const W_DOT = { running: 'ok', stalled: 'warn', paused: 'hollow', errored: 'bad', done: 'hollow', finished: 'past' };
const W_MQ = { landing: 'landing', queued: 'in the merge queue', bounced: 'bounced', parked: 'parked', landed: 'landed' };
const wOpen = new Map();   // row key -> expanded (sticks across polls)

const wAgo = t => t ? short(Math.max(0, nowS() - t)) : '–';
const tilde = p => (p || '').replace(/^\/Users\/[^/]+/, '~');

function wGroups(rows) {
  const attention = rows.filter(w => w.problems.length);
  const live = rows.filter(w => !w.problems.length && W_LIVE.has(w.state));
  // A pause is usually on purpose: its own folded group unless it also has a real problem.
  const paused = rows.filter(w => !w.problems.length && w.state === 'paused');
  const rest = rows.filter(w => !w.problems.length && !W_LIVE.has(w.state) && w.state !== 'paused');
  const rank = w => w.problems.some(p => p.level === 'bad') ? 0 : 1;
  attention.sort((a, b) => rank(a) - rank(b));
  return { attention, live, paused, rest };
}

function wCi(w) {
  const c = w.ci;
  if (!c) return null;
  const span = el('span', 'wci');
  const what = c.state === 'finished'
    ? [c.outcome === 'fail' ? el('span', 'bad', 'CI failed') : 'CI ' + (c.outcome || 'done'), ' ', mono(clock(c.ended), 'grey')]
    : [c.via_mq ? 'mq check ' : 'CI ', c.state, ' on ', c.runner];
  if (c.state !== 'finished' && c.run) {
    const a = el('a');
    a.href = '#run-' + c.run;
    a.append(...what);
    span.append(a);
  } else span.append(...what);
  return span;
}

function wMq(w) {
  const q = w.mq;
  if (!q) return null;
  return el('span', q.state === 'bounced' || q.state === 'parked' ? 'warn' : q.state === 'landing' ? 'ink' : null,
    'mq ' + (W_MQ[q.state] || q.state));
}

// A problem line; "last CI run failed" carries a quiet note: the board only knows runs it saw live.
function wProblem(p) {
  const span = el('span', p.level === 'bad' ? 'bad' : 'warn', p.text);
  if (p.note) span.append(el('span', 'dim', ' · ' + p.note));
  return span;
}

function wDetail(w, id) {
  const box = el('dl', 'wdetail');
  box.id = id;
  const item = (label, ...value) => { box.append(el('dt', null, label)); const d = el('dd'); d.append(...value); box.append(d); };
  for (const p of w.problems) item('Problem', wProblem(p));
  if (w.tag) item('Tag', mono(w.tag));
  if (w.state === 'done' || w.state === 'finished') item('Report', w.state === 'finished' ? 'delivered' : 'written, not yet delivered');
  if (w.resume) item('Resume', mono(w.resume));
  item('Log', mono(tilde(w.log)));
  if (w.dir) item('Worktree', mono(tilde(w.dir)), w.branch ? ' · ' : '', w.branch ? mono(w.branch) : '');
  item('Model', [w.model, w.effort].filter(Boolean).join(' · ') || '–', w.preset ? el('span', 'grey', ' · preset ' + w.preset) : '');
  item('Started', mono(w.started ? clock(w.started) : '–'), w.ended ? ' · ended ' : '', w.ended ? mono(clock(w.ended)) : '',
    w.rc != null && w.rc !== 0 ? el('span', 'bad', ' · rc ' + w.rc) : '');
  if (w.ci) {
    const run = w.ci.run ? mono(w.ci.run) : '';
    item('CI run', w.ci.state === 'finished' ? run : (() => { const a = el('a'); a.href = '#run-' + w.ci.run; a.append(run); return a; })(),
      el('span', 'grey', ' · ' + w.ci.runner + ' · ' + w.ci.state + (w.ci.outcome ? ' · ' + w.ci.outcome : '')));
  }
  if (w.mq) {
    const list = el('ul', 'wmq');
    for (const e of w.mq.history.slice().reverse()) {
      const li = el('li');
      li.append(mono(clock(e.at), 'grey'), ' ', el('span', e.event === 'bounced' || e.event === 'parked' ? 'warn' : null, e.event),
        e.reason ? el('span', 'grey', ' · ' + e.reason) : '');
      list.append(li);
    }
    item('Merge queue', 'lane ', mono(w.lane), ' · ' + (W_MQ[w.mq.state] || w.mq.state), list);
  }
  return box;
}

function wRow(w) {
  const row = el('div', 'wrow');
  row.dataset.key = w.key;
  const id = 'w-' + w.key.replace(/[^A-Za-z0-9_-]/g, '_');
  const open = !!wOpen.get(w.key);
  const btn = el('button', 'wtoggle');
  btn.type = 'button';
  btn.dataset.key = w.key;
  btn.setAttribute('aria-expanded', String(open));
  btn.setAttribute('aria-controls', id);
  const dot = el('i', 'wdot ' + (W_DOT[w.state] || 'past'));
  const what = el('span', 'wwhat');
  const one = el('span', 'wl1');
  one.append(el('b', null, w.name), el('span', 'wproj', w.project + (w.lane && w.lane !== w.name && w.lane !== w.project ? ' / ' + w.lane : '')));
  if (w.branch && w.branch !== w.lane && w.branch !== w.name) one.append(mono(w.branch, 'wbr'));
  const two = el('span', 'wl2');
  const p = w.problems[0];
  if (p) two.append(wProblem(p), w.problems.length > 1 ? el('span', 'grey', ' · +' + (w.problems.length - 1)) : '');
  else two.append(w.tag ? mono(w.tag) : el('span', 'dim', 'no tag'));
  what.append(one, two);
  const side = el('span', 'wside');
  const s1 = el('span', 'wl1');
  const model = mono([w.model, w.effort].filter(Boolean).join(' '), 'wmodel');
  const bits = [wCi(w), wMq(w)].filter(Boolean);
  bits.forEach((b, i) => { if (i) s1.append(' · '); s1.append(b); });
  const state = { stalled: 'stalled', running: 'running', paused: 'paused', errored: 'errored', done: 'done', finished: 'finished' }[w.state] || w.state;
  const s2 = el('span', 'wl2');
  s2.append(model, ' · ', el('span', w.state === 'errored' ? 'bad' : w.state === 'stalled' ? 'warn' : null, state), ' ', mono(wAgo(w.started), 'grey'));
  if (W_LIVE.has(w.state)) s2.append(' · log ', mono(wAgo(w.log_at), w.state === 'stalled' ? 'warn' : 'grey'));
  side.append(s1, s2);
  btn.append(dot, what, side, el('span', 'chev'));
  row.append(btn);
  const detail = wDetail(w, id);
  detail.hidden = !open;
  row.append(detail);
  return row;
}

// A folded group: a toggle button (state kept in wOpen) and its rows.
function wFold(key, label, rows, openLabel) {
  const open = !!wOpen.get(key);
  const more = el('button', 'steps wmore');
  more.type = 'button';
  more.dataset.key = key;
  more.setAttribute('aria-expanded', String(open));
  more.setAttribute('aria-controls', 'w-' + key.slice(1));
  more.append(el('span', 'chev'), open ? openLabel : label);
  const list = wList(rows);
  list.id = 'w-' + key.slice(1);
  list.hidden = !open;
  return [more, list];
}

function wList(rows) {
  const box = el('div', 'wrows');
  rows.forEach(w => box.append(wRow(w)));
  return box;
}

document.addEventListener('click', e => {
  const b = e.target.closest('button.wtoggle, button.wmore');
  if (!b) return;
  wOpen.set(b.dataset.key, !wOpen.get(b.dataset.key));
  b.focus();
  window.renderWorkers(data);
});

window.renderWorkers = function (d) {
  const card = document.getElementById('workers');
  const active = document.activeElement;
  const focused = active && active.matches('button.wtoggle, button.wmore') ? active.dataset.key : null;
  const sum = document.getElementById('wsum');
  const rows = (d && d.workers) || [];
  const g = wGroups(rows);
  const shown = g.attention.length + g.live.length + g.paused.length;
  sum.replaceChildren();
  if (rows.length) {
    const a = el('a', null);
    a.href = '#workers';
    a.append('workers ', mono(String(shown)));
    if (g.attention.length) a.append(' · ', el('span', 'warn', g.attention.length + ' need attention'));
    sum.append(a);
  }
  const head = el('div', 'head');
  head.append(el('h2', null, 'Workers'));
  const st = el('span', 'state');
  st.append(mono(String(shown)), ' · ', g.attention.length ? el('span', 'warn', g.attention.length + ' need attention') : 'none need attention');
  head.append(st);
  const at = el('span', 'machine');
  at.append('all projects · last 24 h');
  head.append(at);
  const parts = [head];
  const section = (label, n) => {
    const sec = el('div', 'section');
    const l = el('div', 'label');
    l.append(label, mono(String(n), 'dim'));
    sec.append(l);
    parts.push(sec);
    return sec;
  };
  if (g.attention.length) {
    const sec = section('Needs attention', g.attention.length);
    sec.append(wList(g.attention.slice(0, W_ATTENTION_ROWS)));
    const extra = g.attention.slice(W_ATTENTION_ROWS);
    if (extra.length) sec.append(...wFold('#attention', 'Show ' + extra.length + ' more', extra, 'Show fewer'));
  }
  const live = section('Live', g.live.length);
  live.append(g.live.length ? wList(g.live) : el('div', 'quiet', 'No other worker is running.'));
  if (g.paused.length || g.rest.length) {
    const sec = el('div', 'section wfolds');
    if (g.paused.length) sec.append(...wFold('#paused', 'Paused ' + g.paused.length, g.paused, 'Hide paused'));
    if (g.rest.length) sec.append(...wFold('#finished', g.rest.length + ' finished', g.rest, 'Hide finished'));
    parts.push(sec);
  }
  if (!rows.length) parts.push(el('div', 'note', d && d.workers_at ? 'No workers in the last 24 h.' : 'Reading worker state.'));
  card.replaceChildren(...parts);
  if (focused) {
    const again = card.querySelector(`button[data-key="${CSS.escape(focused)}"]`);
    if (again) again.focus();
  }
};
