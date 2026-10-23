// Service worker: keeps the app's own screens for when the VM is out of reach, so the app opens and says so.
// It never stores or answers data: /api, /auth and /ws always go to the server, live.
'use strict';
const CACHE = 'angelbot-shell-v1';
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
