/* ═══════════════════════════════════════════════════════════════
   المعيد الذكي — منطق شاشة الدخول والتسجيل (منقول من index.html)

   • بيتحمّل قبل السكريبت الرئيسي (اللي بيستخدم safeStorageGet/Set و currentUser).
   • كل المتغيرات الـ top-level هنا بـ `var` (مش let/const) — مفيش temporal dead zone،
     فالدوال اللي بتتنادى من onclick تقدر تلمسها في أي توقيت من غير ReferenceError.
   • تسجيل الدخول متخزّن في sessionStorage: بيتقفل لما التاب/المتصفح يتقفل،
     مش مع كل ريفريش.
   ═══════════════════════════════════════════════════════════════ */

var currentUser = null;
var _authSwitchTimer = null;

/* ── Storage (sessionStorage) ─────────────────────────────────────────────
   التخزين ممكن يتمنع (خصوصًا جوه iframe زي معاينة Hugging Face المدمجة — المتصفحات
   الحديثة بتقفل third-party storage في السياق ده). لو اتمنع، النظام لسه يقدر يشتغل
   للجلسة الحالية (currentUser في الذاكرة)، بس مش هيفتكر الدخول بعد تحديث الصفحة. */
function safeStorageSet(key, value) {
  try { sessionStorage.setItem(key, value); return true; }
  catch (e) { console.warn('sessionStorage blocked (' + key + '):', e); return false; }
}
function safeStorageGet(key) {
  try { return sessionStorage.getItem(key); }
  catch (e) { console.warn('sessionStorage blocked (' + key + '):', e); return null; }
}
function safeStorageRemove(key) {
  try { sessionStorage.removeItem(key); }
  catch (e) { console.warn('sessionStorage blocked (' + key + '):', e); }
}

// نسخ قديمة كانت بتحفظ الدخول في localStorage (وبيفضل بعد قفل المتصفح). التوكن ده
// بيفضل صالح في السيرفر، فبنمسحه من الجهاز بدل ما يفضل قاعد. (sessionId القديم كمان،
// عشان محدش يرث محادثة يوزر تاني على نفس الجهاز.)
(function purgeLegacyLoginKeys() {
  try {
    ['auth_token', 'auth_username', 'auth_role', 'sessionId'].forEach(function (k) {
      localStorage.removeItem(k);
    });
  } catch (e) { /* localStorage ممكن يتمنع (iframe) — مش مشكلة */ }
})();

function prefersReducedMotion() {
  try {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (e) { return false; }
}

/* ── الفورم: تبديل ناعم بين الدخول والتسجيل ─────────────────────────────── */
function toggleAuthForm(which) {
  var login = document.getElementById('auth-login-form');
  var reg = document.getElementById('auth-register-form');
  var next = which === 'register' ? reg : login;
  var prev = which === 'register' ? login : reg;

  if (_authSwitchTimer) { clearTimeout(_authSwitchTimer); _authSwitchTimer = null; }
  login.classList.remove('auth-enter', 'auth-leave');
  reg.classList.remove('auth-enter', 'auth-leave');

  var nextVisible = getComputedStyle(next).display !== 'none';
  var prevVisible = getComputedStyle(prev).display !== 'none';
  if (nextVisible && !prevVisible) return; // النموذج المطلوب ظاهر بالفعل

  function swap() {
    prev.style.display = 'none';
    next.style.display = 'block';
    _authSwitchTimer = null;
  }
  if (!prevVisible || prefersReducedMotion()) { swap(); return; }

  // fade/slide-out للنموذج الحالي، وبعدين fade/slide-in للجديد
  prev.classList.add('auth-leave');
  _authSwitchTimer = setTimeout(function () {
    prev.classList.remove('auth-leave');
    swap();
    next.classList.add('auth-enter');
  }, 180);
}

function showAuthError(id, msg) {
  var el = document.getElementById(id);
  el.textContent = msg;
  el.style.display = 'block';
}
function hideAuthError(id) { document.getElementById(id).style.display = 'none'; }

// نبضة على الزرار وهو مستني رد السيرفر (+ منع الضغط المتكرر)
function setAuthBusy(btnId, busy, busyText) {
  var btn = document.getElementById(btnId);
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    btn.textContent = busyText;
    btn.disabled = true;
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
  } else {
    if (btn.dataset.label) { btn.textContent = btn.dataset.label; delete btn.dataset.label; }
    btn.disabled = false;
    btn.classList.remove('is-loading');
    btn.removeAttribute('aria-busy');
  }
}

/* ── تسجيل الدخول / إنشاء حساب ───────────────────────────────────────────── */
async function doLogin() {
  var btn = document.getElementById('login-btn');
  if (btn && btn.disabled) return; // طلب شغال بالفعل (مثلًا Enter اتداس مرتين)
  hideAuthError('login-error');
  var username = document.getElementById('login-username').value.trim();
  var password = document.getElementById('login-password').value;
  if (!username || !password) return showAuthError('login-error', 'اكتب اسم المستخدم وكلمة السر');

  setAuthBusy('login-btn', true, 'جاري الدخول...');
  var r, d;
  try {
    r = await fetch('/auth/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: username, password: password})
    });
    d = await r.json();
  } catch (e) {
    console.error('login network error:', e);
    setAuthBusy('login-btn', false);
    return showAuthError('login-error', '⚠️ فشل الاتصال بالسيرفر');
  }
  setAuthBusy('login-btn', false);
  if (!r.ok) return showAuthError('login-error', d.detail || 'حصل خطأ، حاول تاني');

  try {
    saveAuthAndEnter(d);
  } catch (e) {
    console.error('post-login error:', e);
    var stack = (e.stack || '').split('\n').slice(0, 4).join(' | ');
    showAuthError('login-error',
      '⚠️ تم تسجيل الدخول بس حصلت مشكلة: ' + e.message + '\n' + stack);
  }
}

async function doRegister() {
  var btn = document.getElementById('register-btn');
  if (btn && btn.disabled) return;
  hideAuthError('register-error');
  var username  = document.getElementById('reg-username').value.trim();
  var password  = document.getElementById('reg-password').value;
  var password2 = document.getElementById('reg-password2').value;
  if (!username || !password) return showAuthError('register-error', 'اكتب اسم المستخدم وكلمة السر');
  if (password !== password2) return showAuthError('register-error', 'كلمة السر وتأكيدها مش متطابقين');

  setAuthBusy('register-btn', true, 'جاري إنشاء الحساب...');
  var r, d;
  try {
    // الفرقة ثابتة (التالتة) — السيرفر بيحطها تلقائي، مفيش grade في الطلب
    r = await fetch('/auth/register', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: username, password: password})
    });
    d = await r.json();
  } catch (e) {
    console.error('register network error:', e);
    setAuthBusy('register-btn', false);
    return showAuthError('register-error', '⚠️ فشل الاتصال بالسيرفر');
  }
  setAuthBusy('register-btn', false);
  if (!r.ok) return showAuthError('register-error', d.detail || 'حصل خطأ، حاول تاني');

  try {
    saveAuthAndEnter(d);
  } catch (e) {
    console.error('post-register error:', e);
    var stack = (e.stack || '').split('\n').slice(0, 4).join(' | ');
    showAuthError('register-error',
      '⚠️ تم إنشاء الحساب بس حصلت مشكلة: ' + e.message + '\n' + stack);
  }
}

function saveAuthAndEnter(d) {
  var stored = safeStorageSet('auth_token', d.token);
  safeStorageSet('auth_username', d.username);
  try {
    if (!stored && window.self !== window.top) {
      console.warn('يبدو إن الصفحة متفتحة جوه iframe والمتصفح بيمنع التخزين — ' +
                   'الدخول هيشتغل للجلسة دي بس، افتح الرابط في تاب منفصل عشان يفضل محفوظ.');
    }
  } catch (e) { /* حتى قراءة window.top ممكن تترفض جوه iframe مقفول جدًا — تجاهلها بأمان */ }
  currentUser = {username: d.username, grade: d.grade};
  // تسجيل دخول/إنشاء حساب جديد = محادثة جديدة. جلسة الشات مرتبطة بصاحبها على
  // السيرفر؛ لو فضل sessionId قديم في التاب (مثلًا لتوكن انتهى، وجهاز مشترك في
  // معمل) وبعدين دخل حد تاني، الجلسة القديمة كانت هتتقفل في وشه (403).
  try {
    sessionId = crypto.randomUUID();
    safeStorageSet('sessionId', sessionId);
  } catch (e) { console.warn('could not rotate session id:', e); }
  enterApp();
}

function enterApp() {
  document.getElementById('auth-screen').style.display = 'none';
  document.getElementById('sidebar-username').textContent = '👤 ' + currentUser.username;
  document.getElementById('sidebar-grade').textContent = 'الفرقة الثالثة';
}

async function doLogout() {
  var token = safeStorageGet('auth_token');
  try {
    if (token) await fetch('/auth/logout', {method: 'POST', headers: {'Authorization': 'Bearer ' + token}});
  } catch (e) { /* logging out anyway */ }
  try { sessionStorage.clear(); } catch (e) { console.warn('sessionStorage blocked (clear):', e); }
  location.reload();
}

async function checkAuth() {
  var token = safeStorageGet('auth_token');
  if (!token) return; // شاشة الدخول ظاهرة تلقائيًا، مفيش حاجة تانية نعملها
  try {
    var r = await fetch('/auth/me', {headers: {'Authorization': 'Bearer ' + token}});
    if (!r.ok) { safeStorageRemove('auth_token'); return; }
    currentUser = await r.json();
    enterApp();
  } catch (e) {
    console.warn('checkAuth failed (server waking up, or a real network issue):', e);
    // نسيب التوكن زي ما هو — ممكن يكون السيرفر لسه بيصحى، مش لازم نمسحه
  }
}

/* ── تأثير "الطباعة" على عنوان شاشة الدخول ────────────────────────────────
   الـ h1 بيفضل فيه النص كامل في الـ HTML (للـ SEO ومن غير JS)، وهنا بنحوّله لـ:
   sizer مخفي (بيحجز العرض) + طبقة بتتكتب حرف حرف. القارئ الصوتي بيقرا aria-label. */
function initAuthTitleTyping() {
  var h1 = document.getElementById('auth-title');
  if (!h1 || h1.dataset.typed) return;
  h1.dataset.typed = '1';
  var text = (h1.textContent || '').trim();
  if (!text || prefersReducedMotion()) return; // من غير أنيميشن: النص الثابت زي ما هو

  h1.setAttribute('aria-label', text);
  h1.textContent = '';
  var wrap = document.createElement('span');
  wrap.className = 'type-wrap';
  wrap.setAttribute('aria-hidden', 'true');
  var sizer = document.createElement('span');
  sizer.className = 'type-sizer';
  sizer.textContent = text;
  var live = document.createElement('span');
  live.className = 'type-live typing';
  wrap.appendChild(sizer);
  wrap.appendChild(live);
  h1.appendChild(wrap);

  var i = 0;
  (function step() {
    i++;
    live.textContent = text.slice(0, i);
    if (i < text.length) {
      setTimeout(step, 90);
    } else {
      live.classList.remove('typing');
      live.classList.add('typed');
    }
  })();
}

/* ── Boot ─────────────────────────────────────────────────────────────── */
setTimeout(initAuthTitleTyping, 350); // بعد ما أنيميشن ظهور الشاشة (.5s) يبدأ يهدى
checkAuth();
