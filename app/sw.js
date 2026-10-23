// Service worker: keeps the app's own screens for when the VM is out of reach, so the app opens and says so.
// It never stores or answers data: /api, /auth and /ws always go to the server, live.
'use strict';
const CACHE = 'angelbot-shell-v7';
const SHELL = ['/', '/app.js', '/app.css', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/apple-touch-icon.png'];

self.addEventListener('install', ev => {
  ev.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', ev => {
  ev.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', ev => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== 'GET' || url.origin !== location.origin) return;
  if (/^\/(api|auth|ws|healthz)\b/.test(url.pathname)) return;          // data: never cached
  // screens: the network first (so updates arrive at once), the saved copy only when the VM can't be reached
  ev.respondWith(fetch(ev.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(ev.request, copy)); }
    return r;
  }).catch(() => caches.match(ev.request).then(r => r || caches.match('/'))));
});

// push: every bot alert arrives here encrypted; the browser decrypts it and we show it. Tapping opens the app.
self.addEventListener('push', ev => {
  let d = {};
  try { d = ev.data ? ev.data.json() : {}; } catch (_) { d = { title: 'AngelBot', body: ev.data ? ev.data.text() : '' }; }
  ev.waitUntil(self.registration.showNotification(d.title || 'AngelBot', {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-192.png', tag: 'angelbot-' + (d.ts || Date.now()),
    timestamp: d.ts ? d.ts * 1000 : Date.now(), data: { url: '/' },
  }));
});
self.addEventListener('notificationclick', ev => {
  ev.notification.close();
  ev.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) if (new URL(c.url).origin === location.origin) return c.focus();
    return self.clients.openWindow('/');
  }));
});
