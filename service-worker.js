const CACHE_NAME = 'meezan-cache-v4';

self.addEventListener('install', e => {
    self.skipWaiting();
    e.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(['/meezan-tracker/manifest.json']);
        })
    );
});

self.addEventListener('activate', e => {
    e.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', e => {
    const url = new URL(e.request.url);

    // Always fetch index.html fresh — never cache it
    if (url.pathname === '/meezan-tracker/' || 
        url.pathname === '/meezan-tracker/index.html' ||
        url.pathname === '/' || 
        url.pathname === '/index.html') {
        e.respondWith(
            fetch(e.request).catch(() => caches.match(e.request))
        );
        return;
    }

    // Everything else: cache first
    e.respondWith(
        caches.match(e.request).then(cached => cached || fetch(e.request))
    );
});
