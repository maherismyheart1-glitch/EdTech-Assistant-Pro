// اختبار سلوك الـ service worker فعليًا (مش مراجعة نص): بنحمّل static/sw.js في sandbox
// بـ mocks لـ self/caches/fetch، ونبعت أحداث fetch/activate حقيقية، ونتأكد من
// اللي بيدخل Cache Storage واللي لأ.
//   node tests/sw_test.js
const fs = require('fs'), path = require('path'), vm = require('vm');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'static', 'sw.js'), 'utf8');
const ORIGIN = 'https://app.example';
let passed = 0, failed = 0;
function check(cond, label, detail) {
  (cond ? passed++ : failed++);
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label + (!cond && detail ? '   -> ' + detail : ''));
}

function makeWorld(existingCaches = {}) {
  const listeners = {}, store = {}, puts = [], deleted = [];
  Object.keys(existingCaches).forEach(k => store[k] = existingCaches[k]);
  const caches = {
    async open(name) {
      store[name] = store[name] || {};
      return {
        async put(req, res) { puts.push({ cache: name, url: req.url }); store[name][req.url] = res; },
        async addAll(urls) { urls.forEach(u => { store[name][ORIGIN + u] = { ok: true }; }); },
        async match(req) { return store[name][req.url || req]; },
      };
    },
    async keys() { return Object.keys(store); },
    async delete(name) { deleted.push(name); delete store[name]; return true; },
    async match(req) {
      for (const n of Object.keys(store)) { const hit = store[n][req.url || (ORIGIN + req)]; if (hit) return hit; }
      return undefined;
    },
  };
  const fetchLog = [];
  const ctx = {
    URL, Promise, console,
    self: {
      location: { origin: ORIGIN },
      addEventListener(type, fn) { listeners[type] = fn; },
      skipWaiting() { return Promise.resolve(); },
      clients: { claim() { return Promise.resolve(); } },
    },
    caches,
    fetch: async (req) => { fetchLog.push(req.url); return world.networkResponse(req); },
  };
  const world = { ctx, listeners, store, puts, deleted, fetchLog,
    networkResponse: () => ({ ok: true, type: 'basic', clone() { return this; } }) };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return world;
}

async function fire(world, url, method = 'GET') {
  let responded = null;
  const event = { request: { url: ORIGIN + url, method },
                  respondWith(p) { responded = Promise.resolve(p); },
                  waitUntil(p) { responded = Promise.resolve(p); } };
  world.listeners.fetch(event);
  if (responded) await responded;
  await new Promise(r => setTimeout(r, 5));   // نسيب cache.put (غير متزامن) يخلص
  return { intercepted: !!responded };
}

(async () => {
  // ── مسموح: قشرة التطبيق والأصول الثابتة بتتخزّن ──
  let w = makeWorld();
  for (const url of ['/', '/static/auth.js', '/static/auth.css', '/static/dept_logo.png']) {
    const r = await fire(w, url);
    check(r.intercepted && w.puts.some(p => p.url === ORIGIN + url), `allowlist: ${url} بيتخزّن في الكاش`, JSON.stringify(w.puts));
  }

  // ── ممنوع: أي endpoint خاص أو API — مفيش تدخّل ولا كتابة كاش ──
  const priv = ['/auth/me', '/history/abc123', '/conversations', '/chat', '/chat/stream', '/exam', '/quiz/answer',
                '/grade', '/summary', '/upload-pdf', '/feedback', '/complaint', '/dash/summary', '/dashboard',
                '/kb-status', '/health'];
  w = makeWorld();
  for (const url of priv) {
    const r = await fire(w, url);
    check(!r.intercepted, `network-only: ${url} مبيتدخلش فيه الـ SW أصلًا`);
  }
  check(w.puts.length === 0, 'مفيش أي رد خاص دخل Cache Storage', JSON.stringify(w.puts));

  // ── endpoints مستقبلية غير معروفة: مش بتتخزّن بالغلط (allowlist مش blocklist) ──
  w = makeWorld();
  for (const url of ['/api/v2/private', '/new-feature/data', '/students/42/grades.json', '/static']) {
    const r = await fire(w, url);
    check(!r.intercepted && w.puts.length === 0, `مسار مستقبلي غير مسجّل ${url}: مفيش كاش`);
  }

  // ── طلبات POST وأصول من أصل تاني مبتتلمسش ──
  w = makeWorld();
  check(!(await fire(w, '/static/auth.js', 'POST')).intercepted, 'POST على مسار static مبيتدخلش فيه');
  const ev = { request: { url: 'https://evil.example/static/x.js', method: 'GET' }, respondWith() { throw new Error('intercepted'); } };
  let crossOrigin = true; try { w.listeners.fetch(ev); } catch (e) { crossOrigin = false; }
  check(crossOrigin, 'طلب من أصل تاني مبيتدخلش فيه');

  // ── ردود فاشلة أو غير basic مبتتخزّنش ──
  w = makeWorld();
  w.networkResponse = () => ({ ok: false, type: 'basic', clone() { return this; } });
  await fire(w, '/static/missing.js');
  check(w.puts.length === 0, 'رد 404 على static مبيتخزّنش');
  w.networkResponse = () => ({ ok: true, type: 'opaque', clone() { return this; } });
  await fire(w, '/static/other.js');
  check(w.puts.length === 0, 'رد opaque مبيتخزّنش');

  // ── activate: الكاش القديم كله بيتمسح ──
  w = makeWorld({ 'edtech-shell-v2': { [ORIGIN + '/auth/me']: { leaked: true } }, 'edtech-shell-v3': {}, 'edtech-shell-v4': {} });
  await new Promise((resolve) => { w.listeners.activate({ waitUntil(p) { p.then(resolve); } }); });
  check(w.deleted.includes('edtech-shell-v2') && w.deleted.includes('edtech-shell-v3') && !w.deleted.includes('edtech-shell-v4'),
        'activate: كاش الإصدارات القديمة (بما فيها اللي فيه رد خاص) اتمسح والحالي فضل', JSON.stringify(w.deleted));
  check(!('edtech-shell-v2' in w.store), 'الكاش القديم مبقاش موجود بعد activate');

  // ── install: بيعمل precache للأصول المسموحة بس ──
  w = makeWorld();
  await new Promise((resolve) => { w.listeners.install({ waitUntil(p) { p.then(resolve); } }); });
  const cached = Object.keys(w.store['edtech-shell-v4'] || {});
  check(cached.length > 0 && cached.every(u => { const p = new URL(u).pathname; return p === '/' || p.startsWith('/static/'); }),
        'install: الـ precache كله من / و /static/ بس', JSON.stringify(cached));

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
