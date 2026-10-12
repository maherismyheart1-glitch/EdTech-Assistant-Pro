// v4: استراتيجية allowlist صريحة. الـ service worker بيكاشّي حاجتين بس: قشرة التطبيق
// ("/") والأصول الثابتة تحت "/static/". أي حاجة تانية — /auth، /history،
// /conversations، /chat، /exam، /quiz، /grade، /summary، /upload-pdf، /feedback،
// /complaint، /dash*، /dashboard، /kb-status، /health، وأي endpoint هيتضاف مستقبلًا —
// الـ SW مبيتدخلش فيها خالص (مفيش respondWith)، يعني بتروح للشبكة مباشرةً ومفيش
// أي رد خاص بيدخل Cache Storage. الأمان هنا مبني على "مسموح بالاسم" مش على
// "ممنوع بالاسم"، فمسار جديد مش متسجّل مش بيتخزّن بالغلط.
// تغيير رقم الإصدار بيخلي activate يمسح أي كاش قديم (اللي ممكن يكون فيه ردود
// خاصة اتخزّنت بالسلوك القديم) بالكامل.
const CACHE_NAME = 'edtech-shell-v4';
const CORE_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/college_logo.png.png',
  '/static/University_logo.png.jpg',
  '/static/dept_logo.png'
];

function isCacheable(url) {
  const path = new URL(url).pathname;
  return path === '/' || path.startsWith('/static/');
}

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(CORE_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  if (new URL(request.url).origin !== self.location.origin) return;
  if (!isCacheable(request.url)) return;   // مش مسموح → مفيش تدخّل، الشبكة مباشرةً

  // قشرة التطبيق + الأصول الثابتة: اطلب من الشبكة وخزّن نسخة (لو الرد سليم)،
  // ولو الشبكة فشلت (أوفلاين) ارجع للكاش أو للقشرة كحل أخير.
  event.respondWith(
    fetch(request)
      .then(response => {
        if (response && response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then(cached => cached || caches.match('/')))
  );
});
