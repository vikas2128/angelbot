// AngelBot app: talks only to this server's API (same origin). Everything shown is built with textContent, never
// innerHTML, so nothing the server or an alert contains can run as code. No outside scripts, fonts or images.
'use strict';

// ───────────────────────────── small helpers ─────────────────────────────
const $ = (sel, el = document) => el.querySelector(sel);
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
// line icons, drawn here (24x24, stroke): no icon font, no outside files
const ICONS = {
  home: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z',
  trades: 'M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01',
  screener: 'M3 5h18l-7 8.5V19l-4 2v-7.5z',
  report: 'M4 20V11M10 20V5M16 20v-6M21 20H3',
  alerts: 'M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15zM10 21h4',
  settings: 'M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4',
  pause: 'M9 5v14M15 5v14',
  play: 'M8 5.5v13l10-6.5z',
  restart: 'M20 12a8 8 0 1 1-2.34-5.66M20 4v4.5h-4.5',
  stop: 'M8.5 3h7L21 8.5v7L15.5 21h-7L3 15.5v-7zM9 12h6',
  refresh: 'M20 12a8 8 0 1 1-2.34-5.66M20 4v4.5h-4.5',
  chevron: 'M9 6l6 6-6 6',
  down: 'M6 9l6 6 6-6',
  left: 'M15 6l-6 6 6 6',
  right: 'M9 6l6 6-6 6',
  bell: 'M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15zM10 21h4',
  logout: 'M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 8l-4 4 4 4M6 12h10',
  trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  key: 'M14.5 9.5a4.5 4.5 0 1 1-1.3-3.2M13.5 10.5 21 18v3h-3v-2h-2v-2h-2l-1.7-1.7',
};
function icon(name, cls = 'i') {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('class', cls); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(NS, 'path'); path.setAttribute('d', ICONS[name] || '');
  svg.append(path);
  return svg;
}
function setAction(el) { $('#hdr-action').replaceChildren(...(el ? [el] : [])); }
const inr = (x, sign = true) => {
  if (x == null || isNaN(x)) return '–';
  const v = Number(x), s = v > 0 && sign ? '+' : v < 0 ? '−' : '';
  return s + '₹' + Math.abs(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const price = x => (x == null || isNaN(x)) ? '–' : '₹' + Number(x).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = x => (x == null || isNaN(x)) ? '–' : (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(2) + '%';
const cls = x => x > 0 ? 'up' : x < 0 ? 'dn' : '';
const hhmm = iso => iso ? String(iso).slice(11, 16) : '–';
// a passkey prompt that was cancelled or timed out raises NotAllowedError with a long technical message
function errText(e, fallback) {
  if (e && (e.name === 'NotAllowedError' || e.name === 'AbortError')) return 'Face ID / passkey was cancelled or timed out. Try again.';
  if (e && e.name === 'InvalidStateError') return 'This device is already registered. Sign in instead.';
  return (e && e.message) || fallback;
}
let toastTimer;
function toast(msg, ms = 3500) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

// ───────────────────────────── API ─────────────────────────────
class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function call(method, path, body) {
  const opts = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (method === 'POST') {
    opts.headers['Content-Type'] = 'application/json';
    opts.headers['X-Requested-With'] = 'angelbot';
    opts.body = JSON.stringify(body || {});
  }
  let r;
  try { r = await fetch(path, opts); } catch (e) { throw new ApiError("Can't reach the bot. Is Tailscale on?", 0); }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith('/auth/')) { showSignIn(); throw new ApiError('Signed out', 401); }
  if (!r.ok) throw new ApiError(data.error || `Error ${r.status}`, r.status);
  return data;
}
const get = p => call('GET', p);
const post = (p, b) => call('POST', p, b);

// ───────────────────────────── passkeys (WebAuthn) ─────────────────────────────
const b64u = {
  dec(s) { s = s.replace(/-/g, '+').replace(/_/g, '/'); s += '='.repeat((4 - s.length % 4) % 4); return Uint8Array.from(atob(s), c => c.charCodeAt(0)).buffer; },
  enc(buf) { let s = ''; for (const b of new Uint8Array(buf)) s += String.fromCharCode(b); return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); },
};
function creationOptions(o) {
  return { ...o, challenge: b64u.dec(o.challenge), user: { ...o.user, id: b64u.dec(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map(c => ({ ...c, id: b64u.dec(c.id) })) };
}
function requestOptions(o) {
  return { ...o, challenge: b64u.dec(o.challenge), allowCredentials: (o.allowCredentials || []).map(c => ({ ...c, id: b64u.dec(c.id) })) };
}
function credJSON(c) {
  const r = c.response, out = { id: c.id, rawId: b64u.enc(c.rawId), type: c.type, response: { clientDataJSON: b64u.enc(r.clientDataJSON) } };
  if (r.attestationObject) out.response.attestationObject = b64u.enc(r.attestationObject);
  if (r.authenticatorData) {
    out.response.authenticatorData = b64u.enc(r.authenticatorData);
    out.response.signature = b64u.enc(r.signature);
    out.response.userHandle = r.userHandle ? b64u.enc(r.userHandle) : null;
  }
  return out;
}
function passkeySupported() { return !!(window.PublicKeyCredential && navigator.credentials); }
async function registerDevice(code) {
  const o = await post('/auth/register/options', { code });
  const cred = await navigator.credentials.create({ publicKey: creationOptions(o.options) });
  await post('/auth/register/verify', { id: o.id, credential: credJSON(cred) });
}
async function signIn() {
  const o = await post('/auth/login/options');
  const cred = await navigator.credentials.get({ publicKey: requestOptions(o.options) });
  await post('/auth/login/verify', { id: o.id, credential: credJSON(cred) });
}
async function stepUp() {
  const o = await post('/auth/stepup/options');
  const cred = await navigator.credentials.get({ publicKey: requestOptions(o.options) });
  await post('/auth/stepup/verify', { id: o.id, credential: credJSON(cred) });
}
// an action: if the passkey check is too old, ask for Face ID once and try again
async function act(path, body) {
  try { return await post(path, body); } catch (e) {
    if (e.status === 403 && /passkey/i.test(e.message)) { await stepUp(); return await post(path, body); }
    throw e;
  }
}

// ───────────────────────────── state and live updates ─────────────────────────────
const state = { tab: 'home', status: null, me: null, ws: null, wsTry: 0, poll: null };
function setConn(kind, text) { const c = $('#conn'); c.className = 'pill ' + kind; c.textContent = text; }
function connectLive() {
  if (state.ws) return;
  const ws = new WebSocket(location.origin.replace(/^http/, 'ws') + '/ws');
  state.ws = ws;
  ws.onopen = () => { state.wsTry = 0; setConn('ok', 'live'); stopPoll(); };
  ws.onmessage = ev => {
    try { const m = JSON.parse(ev.data); if (m.type === 'status') { state.status = m.status; if (state.tab === 'home') renderHome(); } } catch (_) {}
  };
  ws.onclose = () => {
    state.ws = null; setConn('off', 'reconnecting'); startPoll();
    if (state.me && state.me.signed_in) setTimeout(connectLive, Math.min(30000, 1000 * 2 ** state.wsTry++));
  };
}
function startPoll() {
  if (state.poll) return;
  state.poll = setInterval(async () => {
    try { state.status = await get('/api/status'); if (state.tab === 'home') renderHome(); } catch (_) {}
  }, 5000);
}
function stopPoll() { clearInterval(state.poll); state.poll = null; }

// ───────────────────────────── sign-in screen ─────────────────────────────
async function showSignIn() {
  $('#tabs').hidden = true; setConn('off', 'signed out');
  if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; }
  stopPoll();
  let me = {};
  try { me = await get('/auth/me'); } catch (_) {}
  const view = $('#view'); view.replaceChildren();
  const box = h('div', { class: 'signin' }, h('img', { class: 'logo', src: '/icon.svg', alt: '' }), h('h1', { text: 'AngelBot' }));
  if (!passkeySupported()) {
    box.append(h('p', { class: 'banner bad', text: 'This browser has no passkey support. Use Safari or Chrome on a recent phone.' }));
  } else if (!me.registered) {
    const input = h('input', { class: 'field', placeholder: 'XXXXX-XXXXX', autocomplete: 'one-time-code', autocapitalize: 'characters', inputmode: 'text', 'aria-label': 'Enrolment code' });
    const go = h('button', { class: 'btn primary', text: 'Register this device', onclick: async () => {
      go.disabled = true;
      try { await registerDevice(input.value); toast('Device registered. Now sign in.'); await signIn(); await boot(); }
      catch (e) { toast(errText(e, 'Registration was cancelled')); go.disabled = false; }
    } });
    box.append(h('p', { class: 'muted', text: 'Register this phone once. On the VM run:' }),
      h('p', { class: 'code', text: './deploy/ab paper app enroll --name "My phone"' }),
      h('p', { class: 'muted', text: 'and type the code here (valid 10 minutes). Then confirm with Face ID or your fingerprint.' }), input, go);
  } else {
    const go = h('button', { class: 'btn primary', text: 'Sign in with passkey', onclick: async () => {
      go.disabled = true;
      try { await signIn(); await boot(); } catch (e) { toast(errText(e, 'Sign-in was cancelled')); go.disabled = false; }
    } });
    const other = h('button', { class: 'btn', text: 'Register another device', onclick: () => { me.registered = false; renderReg(); } });
    const renderReg = () => {
      view.replaceChildren(box); box.replaceChildren(h('h1', { text: 'AngelBot' }));
      const input = h('input', { class: 'field', placeholder: 'XXXXX-XXXXX', autocomplete: 'one-time-code', 'aria-label': 'Enrolment code' });
      const reg = h('button', { class: 'btn primary', text: 'Register this device', onclick: async () => {
        reg.disabled = true;
        try { await registerDevice(input.value); toast('Device registered. Now sign in.'); await signIn(); await boot(); }
        catch (e) { toast(errText(e, 'Registration was cancelled')); reg.disabled = false; }
      } });
      box.append(h('p', { class: 'muted', text: 'Enter a code from: ./deploy/ab paper app enroll --name "..."' }), input, reg);
    };
    box.append(h('p', { class: 'muted', text: 'Sign in with Face ID, your fingerprint or the phone PIN.' }), go, other);
  }
  view.append(box);
}

// ───────────────────────────── home ─────────────────────────────
function strategyLines(sym, st) {
  if (!st) return [];
  if (st.strategy && Object.keys((state.status || {}).strategies || {}).length > 1) sym = `${sym} (${STRATEGY_NAMES[st.strategy] || st.strategy})`;
  if (st.error) return [`${sym}: strategy state unavailable`];
  const bars = st.bars || 0, need = st.needed || 0;
  if (need && bars < need) return [`${sym}: warming up, ${bars} of ${need} one-minute bars`];
  if ('fast_ema' in st) {
    const rel = st.fast_above ? 'above' : 'below';
    const out = [`${sym} · ${bars} bars · fast avg ${price(st.fast_ema)} ${rel} slow avg ${price(st.slow_ema)} (${pct(st.gap_pct)})`];
    if (st.cross_move_pct != null) out.push(`${st.next_signal === 'buy' ? 'Buy signal' : 'Sell/exit signal'} if a minute closes ` +
      `${st.next_signal === 'buy' ? 'above' : 'below'} ${price(st.cross_price)} (${pct(st.cross_move_pct)} from now)`);
    return out;
  }
  if ('range_high' in st) {
    if (!st.range_done) return [`${sym}: building the opening range (${price(st.range_low)} to ${price(st.range_high)} so far)`];
    const f = st.fired || [];
    return [`${sym} · opening range ${price(st.range_low)} to ${price(st.range_high)}`,
      [f.includes(1) ? 'buy already taken today' : `buy above ${price(st.buy_above)}`,
       f.includes(-1) ? 'sell already taken today' : `sell below ${price(st.sell_below)}`].join(' · ')];
  }
  if ('vwap' in st) return [`${sym} · VWAP ${price(st.vwap)}, price ${pct(st.dev_pct)} from it`,
    `buy below ${price(st.buy_below)} · sell above ${price(st.sell_above)}`];
  return [`${sym} · ${bars} bars`];
}
const STRATEGY_NAMES = { ema_cross: 'EMA crossover', orb: 'Opening range breakout', vwap_revert: 'VWAP reversion' };
function perStockTitle(s) {
  const names = [...new Set(Object.values(s.strategies || {}).map(v => String(v).split('(')[0]))];
  if (names.length > 1) return 'Strategy per stock';
  return STRATEGY_NAMES[names[0] || String(s.strategy || '').split('(')[0]] || 'Strategy';
}
function stat(label, value, klass = '') { return h('div', { class: 'stat' }, h('div', { class: 'l', text: label }), h('div', { class: 'v ' + klass, text: value })); }
function renderHome() {
  if (state.tab !== 'home') return;
  const s = state.status || {}, ctl = s.control || {}, view = $('#view');
  const alive = !!s.bot_alive, mode = (s.mode || 'paper').toUpperCase();
  const hero = h('div', { class: 'card hero' },
    h('div', { class: 'row' },
      h('span', { class: 'pill ' + (alive ? 'ok' : 'off'), text: alive ? 'Running' : 'Not running' }),
      h('span', { class: 'pill plain ' + (mode === 'LIVE' ? 'bad' : 'off'), text: mode === 'LIVE' ? 'LIVE · real money' : `${mode} · simulated` })),
    h('div', { class: 'pnl ' + cls(s.day_pnl), text: inr(s.day_pnl) }),
    h('div', { class: 'meta', text: `Today after fees · ${s.trades_today || 0} trade${s.trades_today === 1 ? '' : 's'} · fees ${price(s.charges_today || 0)}` +
      (s.written_at ? ` · ${hhmm(s.written_at)}` : '') }));
  const flags = [];
  if (s.halted_reason) flags.push(h('div', { class: 'banner bad', text: `Stopped for today: ${s.halted_reason}` }));
  if (s.cooldown_until) flags.push(h('div', { class: 'banner', text: `Resting after losses until ${hhmm(s.cooldown_until)}` }));
  if (ctl.paused) flags.push(h('div', { class: 'banner', text: 'New entries are paused' }));
  if (ctl.kill) flags.push(h('div', { class: 'banner bad', text: 'Emergency stop is set. Resume before the next session.' }));
  const feed = s.feed || {};
  if (alive && feed.connected === false) flags.push(h('div', { class: 'banner bad', text: 'Price feed is disconnected' }));
  const pos = s.positions || [];
  const posCards = pos.length ? pos.map(p => positionCard(p, s)) : [h('div', { class: 'card' },
    h('h2', { class: 'm0', text: 'Position' }), h('p', { class: 'muted m0 mt8 small', text: 'No open position' }))];
  const strat = Object.entries(s.strategy_state || {});
  const ltp = Object.entries(s.ltp || {}).map(([k, v]) => `${k.replace(/-EQ$/, '')} ${price(v)}`).join(' · ');
  const stratCard = h('div', { class: 'card' },
    h('div', { class: 'row' }, h('h2', { class: 'm0', text: perStockTitle(s) }), ltp ? h('span', { class: 'small muted', text: ltp }) : null),
    strat.length ? h('ul', { class: 'lines mt8' }, strat.flatMap(([sym, st]) => strategyLines(sym, st)).map((t, i) => h('li', { class: i % 2 ? 'sub' : '', text: t })))
      : h('p', { class: 'muted m0 mt8 small', text: (s.instruments || []).join(', ') || 'No data yet' }));
  const btn = (name, text, klass, fn) => h('button', { class: 'btn ' + klass, onclick: async ev => {
    const b = ev.currentTarget; b.disabled = true;
    try { await fn(); } catch (e) { toast(errText(e, 'Cancelled')); } finally { b.disabled = false; }
  } }, icon(name), text);
  const controls = h('div', { class: 'card' }, h('h2', { text: 'Controls' }), h('div', { class: 'ctl' },
    ctl.paused ? btn('play', 'Resume', 'primary', async () => { await act('/api/control/resume'); toast('Resumed: new trades allowed'); refreshStatus(); })
               : btn('pause', 'Pause', '', async () => { await act('/api/control/pause'); toast('Paused: no new trades'); refreshStatus(); }),
    btn('restart', 'Restart', '', async () => {
      if (!confirm('Restart the paper bot now? Only works with no open position.')) return;
      await act('/api/control/restart'); toast('Restarting the paper bot…');
    }),
    btn('stop', 'Stop', 'danger', async () => {
      if (!confirm('Emergency stop: cancel waiting entries, close ALL positions and stop the bot?')) return;
      const ask = await act('/api/control/kill/ask');
      if (!confirm('Are you sure? This cannot be undone from here today.')) return;
      await act('/api/control/kill/confirm', { confirm: ask.confirm }); toast('Emergency stop set'); refreshStatus();
    })),
    h('p', { class: 'hint', text: 'Actions ask for Face ID if your last check is over 2 minutes old. Stop is the emergency stop.' }));
  view.replaceChildren(hero, ...flags, ...posCards, stratCard, controls);
}
// ───────────────────────────── position card with its candle chart ─────────────────────────────
const chart = { open: new Set(), bars: {}, minute: {}, range: '1h', pick: {}, loading: new Set() };
const RANGES = [['30m', 30], ['1h', 60], ['Day', 1e9]];
function svgEl(tag, attrs = {}, ...kids) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, String(v));
  el.append(...kids);
  return el;
}
function emaSeries(vals, n) {                     // same as the bot: seeded with the average of the first n closes
  const k = 2 / (n + 1), out = []; let e = null;
  vals.forEach((v, i) => {
    if (i < n - 1) { out.push(null); return; }
    e = e === null ? vals.slice(0, n).reduce((a, b) => a + b, 0) / n : v * k + e * (1 - k);
    out.push(e);
  });
  return out;
}
function strategyParams(desc) {                   // "ema_cross(fast=9, slow=21)" -> {name, fast: 9, slow: 21}
  const m = String(desc || '').match(/^(\w+)\((.*)\)$/), out = { name: m ? m[1] : String(desc || '') };
  if (m) for (const kv of m[2].split(',')) { const [k, v] = kv.split('=').map(x => x.trim()); if (k) out[k] = isNaN(v) ? v : Number(v); }
  return out;
}
async function loadBars(sym, s) {
  const minute = String(s.now || '').slice(0, 16);
  if (chart.loading.has(sym) || (chart.bars[sym] && chart.minute[sym] === minute)) return;
  chart.loading.add(sym);
  try {
    const d = await get('/api/bars?symbol=' + encodeURIComponent(sym));
    chart.bars[sym] = d.bars.map(([t, o, hi, lo, c]) => ({ t, o, h: hi, l: lo, c }));
  } catch (_) { chart.bars[sym] = chart.bars[sym] || []; }
  chart.minute[sym] = minute;                     // success or not: try again only when the next minute starts
  chart.loading.delete(sym);
  if (state.tab === 'home') renderHome();
}
function liveBars(bars, ltp, nowIso) {            // add the forming candle from the latest price
  if (!bars.length || ltp == null) return bars;
  const out = bars.slice(), last = out[out.length - 1], m = String(nowIso || '').slice(0, 16);
  if (m && m > String(last.t).slice(0, 16)) out.push({ t: m + ':00', o: last.c, h: Math.max(last.c, ltp), l: Math.min(last.c, ltp), c: ltp, live: true });
  return out;
}
function drawChart(p, s) {
  const sym = p.symbol, all = liveBars(chart.bars[sym] || [], p.ltp, s.now);
  const box = h('div', { class: 'chartbox' });
  const chips = h('div', { class: 'choices tight' }, RANGES.map(([name]) => h('button', { class: 'chip' + (chart.range === name ? ' on' : ''), text: name,
    onclick: ev => { ev.stopPropagation(); chart.range = name; chart.pick = {}; renderHome(); } })));
  if (!all.length) { box.append(chips, h('p', { class: 'muted small m0', text: chart.loading.has(sym) ? 'Loading candles…' : 'No candles yet today.' })); return box; }
  const n = RANGES.find(r => r[0] === chart.range)[1], start = Math.max(0, all.length - n), bars = all.slice(start);
  const st = (s.strategy_state || {})[sym] || {}, sp = strategyParams((s.strategies || {})[sym] || s.strategy);
  const closesAll = all.map(b => b.c);
  const series = [];
  if (sp.name === 'ema_cross' && sp.fast && sp.slow) {
    series.push(['ema-f', emaSeries(closesAll, sp.fast).slice(start), `${sp.fast}-min avg`], ['ema-s', emaSeries(closesAll, sp.slow).slice(start), `${sp.slow}-min avg`]);
  }
  const levels = [['ln-entry', p.avg, 'Entry'], ['ln-stop', p.stop, 'Stop'], ['ln-tgt', p.target, 'Target']];
  if ('range_high' in st) levels.push(['ln-lvl', st.range_high, 'Range high'], ['ln-lvl', st.range_low, 'Range low']);
  if ('vwap' in st) levels.push(['ln-lvl', st.vwap, 'VWAP'], ['ln-lvl', st.buy_below, 'Buy below'], ['ln-lvl', st.sell_above, 'Sell above']);
  const vals = bars.flatMap(b => [b.h, b.l]).concat(levels.map(l => l[1]).filter(v => v != null), series.flatMap(x => x[1]).filter(v => v != null));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo || hi * 0.002) * 0.08; lo -= pad; hi += pad;
  const W = Math.max(280, Math.round(($('#view').clientWidth || 360) - 28)), H = 220, R = 54, B = 18, T = 6, pw = W - R, ph = H - T - B;
  const x = i => (i + 0.5) * (pw / bars.length), y = v => T + (hi - v) / (hi - lo) * ph, cw = Math.max(1, pw / bars.length * 0.66);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': `${sym} 1-minute candles` });
  const last = bars[bars.length - 1];
  const shown = levels.filter(([, v]) => v != null && v >= lo && v <= hi);
  const taken = shown.map(([, v]) => y(v)).concat([y(last.c)]);   // right-axis spots used by lines and the last price
  for (let g = 0; g <= 3; g++) {                  // price grid (its label only where nothing else is printed)
    const v = lo + (hi - lo) * (g + 0.5) / 4.0, yy = y(v);
    svg.append(svgEl('line', { x1: 0, x2: pw, y1: yy, y2: yy, class: 'grid' }));
    if (taken.every(t => Math.abs(t - yy) > 12)) svg.append(svgEl('text', { x: pw + 4, y: yy + 3, class: 'ax' }, v.toFixed(2)));
  }
  bars.forEach((b, i) => {                        // candles
    const up = b.c >= b.o, k = (up ? 'c-up' : 'c-dn') + (b.live ? ' live' : '');
    svg.append(svgEl('line', { x1: x(i), x2: x(i), y1: y(b.h), y2: y(b.l), class: 'wick ' + k }),
      svgEl('rect', { x: x(i) - cw / 2, y: y(Math.max(b.o, b.c)), width: cw, height: Math.max(1, Math.abs(y(b.o) - y(b.c))), class: 'body ' + k }));
  });
  for (const [k, vs] of series) {                 // strategy averages
    const pts = vs.map((v, i) => v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`).filter(Boolean).join(' ');
    if (pts) svg.append(svgEl('polyline', { points: pts, class: k }));
  }
  for (const [k, v, label] of shown) {            // entry / stop / target and strategy levels: name inside, price on the axis
    const yy = y(v), above = yy > T + 12;
    svg.append(svgEl('line', { x1: 0, x2: pw, y1: yy, y2: yy, class: k }),
      svgEl('text', { x: 4, y: above ? yy - 3 : yy + 11, class: 'tag ' + k }, label));
    if (Math.abs(yy - y(last.c)) > 12) svg.append(svgEl('text', { x: pw + 4, y: yy + 3, class: 'tag ' + k }, v.toFixed(2)));
  }
  // last price marker
  svg.append(svgEl('rect', { x: pw + 1, y: y(last.c) - 7, width: R - 2, height: 14, rx: 3, class: 'pricetag ' + (last.c >= last.o ? 'c-up' : 'c-dn') }),
    svgEl('text', { x: pw + 5, y: y(last.c) + 3.5, class: 'pricetxt' }, last.c.toFixed(2)));
  const ticks = [0, Math.floor((bars.length - 1) / 2), bars.length - 1];
  for (const i of [...new Set(ticks)]) svg.append(svgEl('text', { x: Math.min(Math.max(x(i), 14), pw - 14), y: H - 4, class: 'ax mid' }, String(bars[i].t).slice(11, 16)));
  const sel = chart.pick[sym];
  if (sel != null && bars[sel]) svg.append(svgEl('line', { x1: x(sel), x2: x(sel), y1: T, y2: T + ph, class: 'cross' }));
  const hit = svgEl('rect', { x: 0, y: 0, width: pw, height: H, class: 'hit' });
  const pickAt = ev => { ev.stopPropagation(); const r = svg.getBoundingClientRect(); const i = Math.floor((ev.clientX - r.left) / r.width * W / (pw / bars.length));
    chart.pick[sym] = Math.max(0, Math.min(bars.length - 1, i)); renderHome(); };
  hit.addEventListener('click', pickAt);
  svg.append(hit);
  const b = bars[sel != null && bars[sel] ? sel : bars.length - 1];
  const info = h('div', { class: 'chartinfo' }, h('span', { class: 'muted', text: String(b.t).slice(11, 16) + (b.live ? ' (forming)' : '') }),
    ` O ${b.o.toFixed(2)}  H ${b.h.toFixed(2)}  L ${b.l.toFixed(2)}  C `, h('b', { class: b.c >= b.o ? 'up' : 'dn', text: b.c.toFixed(2) }));
  const legend = h('div', { class: 'legend' }, h('span', { class: 'k ln-entry', text: 'Entry' }), h('span', { class: 'k ln-stop', text: 'Stop' }),
    h('span', { class: 'k ln-tgt', text: 'Target' }), series.map(([k, , label]) => h('span', { class: 'k ' + k, text: label })));
  box.append(chips, info, svg, legend);
  return box;
}
function positionCard(p, s) {
  const open = chart.open.has(p.symbol);
  if (open) loadBars(p.symbol, s);
  const head = h('button', { class: 'cardhead', 'aria-expanded': open ? 'true' : 'false', onclick: () => {
    open ? chart.open.delete(p.symbol) : chart.open.add(p.symbol); chart.pick = {}; renderHome(); } },
    h('h2', { class: 'm0', text: 'Position' }), h('span', { class: 'row' }, h('span', { class: 'pill plain', text: p.symbol }), icon(open ? 'down' : 'chevron')));
  return h('div', { class: 'card' }, head,
    h('div', { class: 'grid2 mt8' },
      stat('Shares', `${p.qty} @ ${price(p.avg)}`), stat('Now', `${price(p.ltp)}`),
      stat('Open P&L', inr(p.unrealized), cls(p.unrealized)), stat('Stop / target', `${price(p.stop)} / ${price(p.target)}`)),
    open ? drawChart(p, s) : h('p', { class: 'hint', text: 'Tap for the candle chart' }));
}
async function refreshStatus() { try { state.status = await get('/api/status'); renderHome(); } catch (_) {} }

// ───────────────────────────── trades ─────────────────────────────
async function renderTrades(day) {
  const view = $('#view');
  const today = new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10);    // IST date
  day = day || today;
  const shift = n => { const d = new Date(day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + n); const x = d.toISOString().slice(0, 10); return x > today ? today : x; };
  const picker = h('input', { type: 'date', class: 'field', value: day, max: today, onchange: e => renderTrades(e.target.value) });
  const bar = h('div', { class: 'daybar' },
    h('button', { class: 'iconbtn', 'aria-label': 'Previous day', onclick: () => renderTrades(shift(-1)) }, icon('left')), picker,
    h('button', { class: 'iconbtn', 'aria-label': 'Next day', disabled: day >= today, onclick: () => renderTrades(shift(1)) }, icon('right')));
  view.replaceChildren(bar, h('p', { class: 'muted center small', text: 'Loading…' }));
  let d;
  try { d = await get('/api/trades?date=' + encodeURIComponent(day)); } catch (e) { view.lastChild.textContent = e.message; return; }
  const t = d.totals;
  const sum = h('div', { class: 'card hero' },
    h('div', { class: 'row' }, h('h2', { class: 'm0', text: day === today ? 'Today' : day }), h('span', { class: 'pill plain', text: `${t.trades} trade${t.trades === 1 ? '' : 's'}` })),
    h('div', { class: 'pnl ' + cls(t.net_pnl), text: inr(t.net_pnl) }),
    h('div', { class: 'grid2 mt8' }, stat('Won', `${t.win_rate_pct}%`), stat('Gross', inr(t.gross_pnl), cls(t.gross_pnl)), stat('Fees', price(t.charges)), stat('After fees', inr(t.net_pnl), cls(t.net_pnl))));
  const rows = d.trades.map(r => h('tr', {},
    h('td', {}, h('div', { text: r.symbol.replace(/-EQ$/, '') }), h('div', { class: 'small muted', text: `${hhmm(r.entry_ts)}–${hhmm(r.exit_ts)}` })),
    h('td', { class: 'r' }, h('div', { text: `${r.qty} × ${Number(r.entry_price).toFixed(2)}` }), h('div', { class: 'small muted', text: `→ ${Number(r.exit_price).toFixed(2)}` })),
    h('td', { class: 'r nw ' + cls(r.net_pnl), text: inr(r.net_pnl) })));
  const table = d.trades.length ? h('div', { class: 'card scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', { text: 'Stock' }), h('th', { class: 'r', text: 'In → out' }), h('th', { class: 'r', text: 'Net' }))),
      h('tbody', {}, rows)))
    : h('p', { class: 'muted center small', text: 'No closed trades on this day.' });
  view.replaceChildren(bar, sum, table);
}

// ───────────────────────────── report and screener (server-built pages, shown framed) ─────────────────
async function renderFrame(path, emptyHint) {
  const view = $('#view');
  view.replaceChildren(h('p', { class: 'muted center', text: 'Loading…' }));
  try {
    const r = await fetch(path, { credentials: 'same-origin' });
    if (r.status === 401) return showSignIn();
    if (!r.ok) { const e = await r.json().catch(() => ({})); view.replaceChildren(h('div', { class: 'banner', text: e.error || emptyHint })); return; }
  } catch (_) { view.replaceChildren(h('div', { class: 'banner bad', text: "Can't reach the bot." })); return; }
  // no scripts can run in it: the server sends these pages with a policy that allows no scripts at all.
  // embed=1 asks for the compact phone layout.
  const url = path + (path.includes('?') ? '&' : '?') + 'embed=1';
  const frame = h('iframe', { class: 'frame', src: url, title: path, referrerpolicy: 'no-referrer' });
  setAction(h('button', { class: 'iconbtn', 'aria-label': 'Refresh', onclick: () => { frame.src = url + '&t=' + Date.now(); } }, icon('refresh')));
  view.replaceChildren(frame);
}

// ───────────────────────────── alerts ─────────────────────────────
async function renderAlerts() {
  const view = $('#view');
  view.replaceChildren(h('p', { class: 'muted center', text: 'Loading…' }));
  let d;
  try { d = await get('/api/alerts?limit=100'); } catch (e) { view.replaceChildren(h('div', { class: 'banner', text: e.message })); return; }
  const items = d.alerts.slice().reverse();
  setAction(h('button', { class: 'iconbtn', 'aria-label': 'Refresh', onclick: () => renderAlerts() }, icon('refresh')));
  view.replaceChildren(h('div', { class: 'card' }, h('h2', { text: 'Recent alerts' }),
    items.length ? h('div', { class: 'list' }, items.map(a => h('div', { class: 'item' },
      h('div', { class: 't', text: `${String(a.ts).slice(5, 16).replace('T', ' ')} · ${a.label || ''}` }),
      h('div', { class: 'x pre', text: a.text }))))
      : h('p', { class: 'muted', text: 'No alerts yet.' })));
}

// ───────────────────────────── push notifications ─────────────────────────────
const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
function pushSupported() { return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window; }
// serviceWorker.ready never settles if the worker could not register: give up after a few seconds
function swReady(ms = 4000) {
  return Promise.race([navigator.serviceWorker.ready,
    new Promise((_, no) => setTimeout(() => no(new Error('The app could not start its background worker. Reload and try again.')), ms))]);
}
async function currentSub() {
  const reg = await swReady();
  return reg.pushManager.getSubscription();
}
async function pushOn() {
  if (Notification.permission === 'denied') throw new Error('Notifications are blocked for this app in the phone settings.');
  if (await Notification.requestPermission() !== 'granted') throw new Error('Notifications were not allowed.');
  const { key } = await get('/api/push/key');
  const reg = await swReady();
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u.dec(key) });
  await post('/api/push/subscribe', { subscription: sub.toJSON() });
}
async function pushOff() {
  const sub = await currentSub();
  if (sub) { await post('/api/push/unsubscribe', { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
}
async function notifyCard() {
  const card = h('div', { class: 'card' }, h('h2', { text: 'Notifications' }));
  if (!pushSupported()) {
    card.append(h('p', { class: 'muted m0', text: isIOS && !standalone
      ? 'On iPhone: tap Share, then “Add to Home Screen”, open AngelBot from there, and turn notifications on here.'
      : 'This browser cannot show push notifications. Telegram still sends every alert.' }));
    return card;
  }
  const sub = await currentSub().catch(() => null);
  const on = !!sub && Notification.permission === 'granted';
  card.append(h('p', { class: 'muted m0', text: on ? 'On for this device: every bot alert arrives as a notification.'
    : 'Off for this device. Telegram still sends every alert.' }));
  const btn = (text, klass, fn) => h('button', { class: 'btn ' + klass, text, onclick: async ev => {
    ev.currentTarget.disabled = true;
    try { await fn(); } catch (e) { toast(errText(e, 'Could not change notifications')); }
    renderSettings();
  } });
  card.append(h('div', { class: 'btns mt8' }, on
    ? [btn('Send a test', 'primary', async () => { const r = await post('/api/push/test'); toast(r.sent ? 'Test sent' : 'Nothing was sent: try turning notifications off and on'); }),
       btn('Turn off', '', async () => { await pushOff(); toast('Notifications off for this device'); })]
    : [btn('Turn on notifications', 'primary wide', async () => { await pushOn(); toast('Notifications on'); })]));
  return card;
}

// ───────────────────────────── settings and devices ─────────────────────────────
async function renderSettings(openId) {
  const view = $('#view');
  let d, devs, me;
  try { [d, devs, me] = await Promise.all([get('/api/settings'), get('/auth/devices'), get('/auth/me')]); }
  catch (e) { view.replaceChildren(h('div', { class: 'banner', text: e.message })); return; }
  const card = h('div', { class: 'card' }, h('h2', { text: `Settings · ${d.area}` }));
  for (const p of d.problems || []) card.append(h('div', { class: 'banner', text: p }));
  for (const s of d.settings) {
    const row = h('button', { class: 'setting', 'aria-expanded': openId === s.id ? 'true' : 'false', onclick: () => renderSettings(openId === s.id ? null : s.id) },
      h('span', { text: s.label + (s.changed ? ' ✏️' : '') }), h('span', { class: 'v' }, s.value, icon('chevron')));
    card.append(row);
    if (openId === s.id) {
      const wrap = h('div', { class: 'choices' }, s.choices.map(c => h('button', { class: 'chip' + (c.current ? ' on' : ''), text: c.label,
        onclick: async () => { try { const r = await act('/api/settings/set', { id: s.id, choice: c.index }); toast(`Saved: ${r.new}. Applies from the next start.`); renderSettings(); } catch (e) { toast(errText(e, 'Something went wrong')); } } })));
      card.append(wrap);
      if (s.typed) {
        const amt = h('input', { class: 'field mt8', type: 'number', inputmode: 'numeric', min: '0', step: '100', placeholder: 'Any amount, e.g. 7500' });
        const save = h('button', { class: 'btn primary mt8', text: 'Save amount', onclick: async () => {
          try { const r = await act('/api/settings/set', { id: s.id, amount: amt.value }); toast(`Saved: ${r.new}`); renderSettings(); } catch (e) { toast(errText(e, 'Something went wrong')); } } });
        card.append(amt, save);
      }
    }
  }
  card.append(h('p', { class: 'hint', text: '✏️ changed here (wins over config.yaml). Applies from the next start; not backtested.' }),
    h('button', { class: 'btn wide mt8', text: 'Reset all to config.yaml', onclick: async () => {
      if (!confirm('Reset every changed setting to config.yaml?')) return;
      try { const r = await act('/api/settings/reset'); toast(`Reset ${r.reset} setting(s)`); renderSettings(); } catch (e) { toast(errText(e, 'Something went wrong')); } } }));
  const devCard = h('div', { class: 'card' }, h('h2', { text: 'Devices' }),
    h('div', { class: 'list' }, devs.devices.map(x => h('div', { class: 'item row' },
      h('span', {}, h('div', { text: x.name + (me.device === x.name ? ' (this device)' : '') }),
        h('div', { class: 't', text: `added ${String(x.created).slice(0, 10)} · last used ${x.last_used ? String(x.last_used).slice(0, 16).replace('T', ' ') : 'never'}` })),
      h('button', { class: 'chip', text: 'Remove', onclick: async () => {
        if (!confirm(`Remove ${x.name}? It is signed out at once.`)) return;
        try { await act('/auth/devices/remove', { id: x.id }); toast(`Removed ${x.name}`); renderSettings(); } catch (e) { toast(errText(e, 'Something went wrong')); } } })))),
    h('p', { class: 'muted small', text: 'Add a device: on the VM run ./deploy/ab paper app enroll --name "..." and register it from that device.' }),
    h('button', { class: 'btn wide mt8', onclick: async () => { try { await post('/auth/logout'); } catch (_) {} showSignIn(); } }, icon('logout'), 'Sign out'));
  view.replaceChildren(await notifyCard(), card, devCard);
}

// ───────────────────────────── tabs and start ─────────────────────────────
function show(tab) {
  state.tab = tab;
  setAction(null);
  for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('on', b.dataset.tab === tab);
  window.scrollTo(0, 0);
  if (tab === 'home') { renderHome(); refreshStatus(); }
  else if (tab === 'trades') renderTrades();
  else if (tab === 'screener') renderFrame('/api/screener', 'No screener snapshot yet.');
  else if (tab === 'report') renderFrame('/api/report', 'No trades to report yet.');
  else if (tab === 'alerts') renderAlerts();
  else if (tab === 'settings') renderSettings();
}
async function boot() {
  try { state.me = await get('/auth/me'); } catch (e) { $('#view').replaceChildren(h('div', { class: 'banner bad', text: e.message })); return; }
  if (!state.me.signed_in) return showSignIn();
  $('#tabs').hidden = false;
  connectLive();
  show(state.tab);
}
document.querySelectorAll('#tabs button').forEach(b => { b.prepend(icon(b.dataset.icon)); b.addEventListener('click', () => show(b.dataset.tab)); });
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.me && state.me.signed_in) { connectLive(); if (state.tab === 'home') refreshStatus(); } });
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
boot();
