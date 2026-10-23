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
  const box = h('div', { class: 'signin' }, h('h1', { text: 'AngelBot' }));
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
      h('p', { class: 'card small pre', text: './deploy/ab paper app enroll --name "My phone"' }),
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
function renderHome() {
  if (state.tab !== 'home') return;
  const s = state.status || {}, ctl = s.control || {}, view = $('#view');
  const alive = !!s.bot_alive, mode = (s.mode || '').toUpperCase();
  const head = h('div', { class: 'card' },
    h('div', { class: 'row' },
      h('span', { class: 'pill ' + (alive ? 'ok' : 'off'), text: alive ? 'Bot running' : 'Bot not running' }),
      h('span', { class: 'pill ' + (mode === 'LIVE' ? 'bad' : 'off'), text: mode === 'LIVE' ? 'LIVE · real money' : (mode || 'PAPER') + ' · simulated' })),
    h('h2', { class: 'mt12', text: 'Today after fees' }),
    h('div', { class: 'big ' + cls(s.day_pnl), text: inr(s.day_pnl) }),
    h('div', { class: 'muted small', text: `${s.trades_today || 0} trades · fees ${price(s.charges_today || 0)}` +
      (s.written_at ? ` · updated ${hhmm(s.written_at)}${s.age_sec != null ? ` (${Math.round(s.age_sec)} s ago)` : ''}` : '') }));
  const flags = [];
  if (s.halted_reason) flags.push(h('div', { class: 'banner bad', text: `Stopped for today: ${s.halted_reason}` }));
  if (s.cooldown_until) flags.push(h('div', { class: 'banner', text: `Resting after losses until ${hhmm(s.cooldown_until)}` }));
  if (ctl.paused) flags.push(h('div', { class: 'banner', text: 'New entries are paused' }));
  if (ctl.kill) flags.push(h('div', { class: 'banner bad', text: 'Emergency stop is set. Resume before the next session.' }));
  const feed = s.feed || {};
  if (alive && feed.connected === false) flags.push(h('div', { class: 'banner bad', text: 'Price feed is disconnected' }));
  const pos = s.positions || [];
  const posCard = h('div', { class: 'card' }, h('h2', { text: 'Position' }),
    pos.length ? pos.map(p => h('dl', { class: 'kv' },
      h('dt', { text: p.symbol }), h('dd', { text: `${p.qty} shares at ${price(p.avg)}` }),
      h('dt', { text: 'Now' }), h('dd', { class: cls(p.unrealized), text: `${price(p.ltp)} · ${inr(p.unrealized)}` }),
      h('dt', { text: 'Safety exit' }), h('dd', { text: price(p.stop) }),
      h('dt', { text: 'Goal' }), h('dd', { text: price(p.target) })))
      : h('p', { class: 'muted m0', text: 'No open position' }));
  const strat = Object.entries(s.strategy_state || {});
  const stratCard = h('div', { class: 'card' }, h('h2', { text: `Strategy · ${String(s.strategy || '').split('(')[0] || '–'}` }),
    strat.length ? h('ul', { class: 'lines' }, strat.flatMap(([sym, st]) => strategyLines(sym, st)).map(t => h('li', { text: t })))
      : h('p', { class: 'muted m0', text: (s.instruments || []).join(', ') || 'No data yet' }),
    h('div', { class: 'muted small mt8', text: Object.entries(s.ltp || {}).map(([k, v]) => `${k} ${price(v)}`).join(' · ') }));
  const btn = (text, klass, fn) => h('button', { class: 'btn ' + klass, text, onclick: async ev => {
    const b = ev.currentTarget; b.disabled = true;
    try { await fn(); } catch (e) { toast(errText(e, 'Cancelled')); } finally { b.disabled = false; }
  } });
  const controls = h('div', { class: 'card' }, h('h2', { text: 'Controls' }), h('div', { class: 'btns' },
    ctl.paused ? btn('▶ Resume', 'primary', async () => { await act('/api/control/resume'); toast('Resumed: new trades allowed'); refreshStatus(); })
               : btn('⏸ Pause', '', async () => { await act('/api/control/pause'); toast('Paused: no new trades'); refreshStatus(); }),
    btn('🔄 Restart', '', async () => {
      if (!confirm('Restart the paper bot now? Only works with no open position.')) return;
      await act('/api/control/restart'); toast('Restarting the paper bot…');
    }),
    btn('🛑 Emergency stop', 'danger wide', async () => {
      if (!confirm('Emergency stop: cancel waiting entries, close ALL positions and stop the bot?')) return;
      const ask = await act('/api/control/kill/ask');
      if (!confirm('Are you sure? This cannot be undone from here today.')) return;
      await act('/api/control/kill/confirm', { confirm: ask.confirm }); toast('Emergency stop set'); refreshStatus();
    })),
    h('p', { class: 'muted small mt8 mb0', text: 'Each action asks for Face ID if your last check is over 2 minutes old.' }));
  view.replaceChildren(head, ...flags, posCard, stratCard, controls);
}
async function refreshStatus() { try { state.status = await get('/api/status'); renderHome(); } catch (_) {} }

// ───────────────────────────── trades ─────────────────────────────
async function renderTrades(day) {
  const view = $('#view');
  const today = new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10);    // IST date
  day = day || today;
  const picker = h('input', { type: 'date', class: 'field', value: day, max: today, onchange: e => renderTrades(e.target.value) });
  view.replaceChildren(h('div', { class: 'card' }, h('h2', { text: 'Day' }), picker), h('p', { class: 'muted center', text: 'Loading…' }));
  let d;
  try { d = await get('/api/trades?date=' + encodeURIComponent(day)); } catch (e) { view.lastChild.textContent = e.message; return; }
  const t = d.totals;
  const sum = h('div', { class: 'card' }, h('h2', { text: day === today ? 'Today' : day }),
    h('div', { class: 'big ' + cls(t.net_pnl), text: inr(t.net_pnl) }),
    h('div', { class: 'muted small', text: `${t.trades} trades · ${t.win_rate_pct}% won · gross ${inr(t.gross_pnl)} · fees ${price(t.charges)}` }));
  const rows = d.trades.map(r => h('tr', {},
    h('td', { text: `${hhmm(r.entry_ts)}–${hhmm(r.exit_ts)}` }), h('td', { text: r.symbol.replace(/-EQ$/, '') }),
    h('td', { class: 'r', text: `${r.qty} @ ${Number(r.entry_price).toFixed(2)}→${Number(r.exit_price).toFixed(2)}` }),
    h('td', { class: 'r nw ' + cls(r.net_pnl), text: inr(r.net_pnl) })));
  const table = d.trades.length ? h('div', { class: 'card scroll' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', { text: 'Time' }), h('th', { text: 'Stock' }), h('th', { class: 'r', text: 'Shares, in→out' }), h('th', { class: 'r', text: 'Net' }))),
      h('tbody', {}, rows)))
    : h('p', { class: 'muted center', text: 'No closed trades on this day.' });
  view.replaceChildren(h('div', { class: 'card' }, h('h2', { text: 'Day' }), picker), sum, table);
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
  // no scripts can run in it: the server sends these pages with a policy that allows no scripts at all
  const frame = h('iframe', { class: 'frame', src: path, title: path, referrerpolicy: 'no-referrer' });
  const reload = h('button', { class: 'btn', text: '↻ Refresh', onclick: () => { frame.src = path + (path.includes('?') ? '&' : '?') + 't=' + Date.now(); } });
  view.replaceChildren(frame, reload);
}

// ───────────────────────────── alerts ─────────────────────────────
async function renderAlerts() {
  const view = $('#view');
  view.replaceChildren(h('p', { class: 'muted center', text: 'Loading…' }));
  let d;
  try { d = await get('/api/alerts?limit=100'); } catch (e) { view.replaceChildren(h('div', { class: 'banner', text: e.message })); return; }
  const items = d.alerts.slice().reverse();
  view.replaceChildren(h('div', { class: 'card' }, h('h2', { text: 'Recent alerts' }),
    items.length ? h('div', { class: 'list' }, items.map(a => h('div', { class: 'item' },
      h('div', { class: 't', text: `${String(a.ts).slice(0, 16).replace('T', ' ')} · ${a.label || ''}` }),
      h('div', { class: 'pre', text: a.text }))))
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
    const row = h('button', { class: 'setting', onclick: () => renderSettings(openId === s.id ? null : s.id) },
      h('div', { class: 'row' }, h('span', { text: s.label + (s.changed ? ' ✏️' : '') }), h('span', { class: 'v', text: s.value })));
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
  card.append(h('p', { class: 'muted small', text: '✏️ = changed here (wins over config.yaml). Changes apply from the next start and are not backtested.' }),
    h('button', { class: 'btn', text: '↩ Reset all to config.yaml', onclick: async () => {
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
    h('button', { class: 'btn', text: 'Sign out', onclick: async () => { try { await post('/auth/logout'); } catch (_) {} showSignIn(); } }));
  view.replaceChildren(await notifyCard(), card, devCard);
}

// ───────────────────────────── tabs and start ─────────────────────────────
function show(tab) {
  state.tab = tab;
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
document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => show(b.dataset.tab)));
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.me && state.me.signed_in) { connectLive(); if (state.tab === 'home') refreshStatus(); } });
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
boot();
