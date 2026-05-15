const CACHE_NAME = 'meezan-cache-v3'; // ← bump this number every time you update

self.addEventListener('install', e => {
    self.skipWaiting(); // activate immediately, don't wait
    e.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(['/manifest.json']); // don't cache index.html
        })
    );
});

self.addEventListener('activate', e => {
    // delete all old caches
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);

    // Always fetch index.html fresh from network — never serve from cache
    if (url.pathname === '/' || url.pathname === '/index.html') {
        e.respondWith(
            fetch(e.request).catch(() => caches.match('/index.html'))
        );
        return;
    }

    // Everything else: cache first
    e.respondWith(
        caches.match(e.request).then(cached => cached || fetch(e.request))
    );
});