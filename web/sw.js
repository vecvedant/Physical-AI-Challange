/* Minimal service worker.
 *
 * Deliberately does NOT cache API responses or the WebSocket. A dashboard that
 * shows yesterday's power because it came out of a cache is worse than one that
 * shows nothing - an operator would act on stale numbers without knowing. Only
 * the shell is cached, so the page loads instantly on a phone that has been
 * here before and then fills itself from the live socket.
 */
const SHELL = 'udyogiq-shell-v3';
const ASSETS = ['/', '/static/manifest.webmanifest', '/static/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // Live data always goes to the network. If it fails, it fails visibly.
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;

  e.respondWith(
    fetch(e.request)
      .then(res => {
        const copy = res.clone();
        caches.open(SHELL).then(c => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
  );
});
