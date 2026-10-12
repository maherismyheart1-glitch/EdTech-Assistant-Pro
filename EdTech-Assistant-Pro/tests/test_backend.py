"""
اختبار الباك اند — بيشتغل أوفلاين بالكامل (Groq/Pinecone/Gemini كلهم test doubles، مفيش أي
اتصال حقيقي):

    python tests/test_backend.py        # من فولدر المشروع
    node tests/sw_test.js               # سلوك الـ service worker
    (بعد pip install -r requirements.txt)

- بيشتغل على نسخة معزولة في فولدر مؤقت — مبيلمسش app_data.db بتاعك.
- المايجريشن بيتجرّب في عمليات منفصلة (subprocess) على قواعد بيانات قديمة حقيقية الشكل
  (messages من غير user_id، توكنات بالنص الصريح، إلخ) — ومنها نسخة من app_data.db الموجودة
  في الريبو لو موجودة. الأصل بيتقارن بالـ sha256 قبل وبعد عشان نتأكد إنه ما اتلمسش.
- ملفات PDF في الاختبارات بايتات PDF صالحة فعلًا (tests/pdf_fixtures.py). ملحوظة صريحة:
  قراءة الـ PDF بتتم عن طريق PyMuPDF الحقيقي لو متثبّت في بيئتك؛ لو مش متثبّت (زي بيئة
  التطوير الأوفلاين اللي اتكتب فيها الاختبار) بيتستخدم parser بديل بسيط — فالتحقق من سلوك
  PyMuPDF نفسه بيحصل بس لما تشغّل الاختبار بالحزمة الحقيقية.
- بيرجّع exit code 1 لو أي فحص فشل.
"""
import asyncio, concurrent.futures, glob, hashlib, io, json, logging, os, re, shutil, sqlite3
import subprocess, sys, tempfile, threading, time, types, uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from pdf_fixtures import make_pdf, truncated_pdf   # noqa: E402

for _k in list(os.environ):
    if _k.startswith(("GROQ_API_KEY", "PINECONE", "GOOGLE_API", "GEMINI", "APP_GRADE", "ALLOWED_ORIGINS", "LOG_FILE")):
        del os.environ[_k]
SECRET = "test-secret-xyz"
os.environ["ADMIN_SECRET"] = SECRET

PASSED, FAILED = [], []
def check(cond, label, detail=""):
    (PASSED if cond else FAILED).append(label)
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond or not detail else f"   -> {detail}"))

def section(title):
    print(f"\n── {title} ─────────────────────────────")

def make_workdir():
    d = Path(tempfile.mkdtemp(prefix="edtech-work-"))
    for name in ("app.py", "static", "frontend", "data"):
        src = ROOT / name
        (shutil.copytree if src.is_dir() else shutil.copy)(src, d / name)
    return d

_PRELUDE = """
import os, sys, json, sqlite3
sys.path.insert(0, {workdir!r}); os.chdir({workdir!r})
_ri = os.path.isdir; os.path.isdir = lambda p: False if str(p) == '/data' else _ri(p)
import app as appmod
os.path.isdir = _ri
from fastapi.testclient import TestClient
client = TestClient(appmod.app)
"""
def run_py(workdir, body, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GROQ_API_KEY", "PINECONE", "GOOGLE_API", "GEMINI", "APP_GRADE", "ALLOWED_ORIGINS", "LOG_FILE"))}
    env["ADMIN_SECRET"] = SECRET
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", _PRELUDE.format(workdir=str(workdir)) + body],
                          cwd=str(workdir), env=env, capture_output=True, text=True, timeout=240)

_SNAPSHOT = """
c = sqlite3.connect('app_data.db')
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
snap = {
 'version': c.execute('PRAGMA user_version').fetchone()[0],
 'counts': {t: c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in tables},
 'messages_cols': [r[1] for r in c.execute('PRAGMA table_info(messages)')],
 'indexes': sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")),
 'tokens': [r[0] for r in c.execute('SELECT token FROM auth_sessions')],
 'expires_null': c.execute('SELECT COUNT(*) FROM auth_sessions WHERE expires_at IS NULL').fetchone()[0],
 'users': [list(r) for r in c.execute('SELECT username, role, grade FROM users ORDER BY id')],
 'sessions': dict(c.execute('SELECT id, user_id FROM sessions').fetchall()),
 'msgs': [list(r) for r in c.execute('SELECT session_id, role, content FROM messages ORDER BY id')],
 'feedback': [list(r) for r in c.execute('SELECT rating FROM feedback ORDER BY id')],
}
print('SNAP=' + json.dumps(snap, ensure_ascii=False))
"""
def snapshot(workdir, extra_body="", extra_env=None):
    r = run_py(workdir, extra_body + _SNAPSHOT, extra_env)
    line = next((l for l in r.stdout.splitlines() if l.startswith("SNAP=")), None)
    return (json.loads(line[5:]) if line else None), r

ALL_INDEXES = sorted(["idx_messages_session", "idx_messages_user", "idx_messages_ts", "idx_analytics_event",
                      "idx_analytics_ts", "idx_quiz_answers_topic", "idx_auth_sessions_user",
                      "idx_documents_uploaded", "idx_quizzes_session", "idx_sessions_user"])

# ── قواعد بيانات قديمة بأشكالها الحقيقية ───────────────────────────────────────
_V0_DDL = """
CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, ts TEXT);
CREATE TABLE complaints(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, category TEXT DEFAULT 'عام',
    priority TEXT DEFAULT 'متوسطة', text TEXT, status TEXT DEFAULT 'مفتوحة', ts TEXT);
CREATE TABLE feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, question TEXT, answer TEXT, rating TEXT, ts TEXT);
CREATE TABLE analytics(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, data TEXT, ts TEXT);
"""
def build_legacy_v0(path):
    """شكل الداتابيز الأصلي: messages من غير user_id، ومفيش users/auth/sessions خالص."""
    c = sqlite3.connect(path); c.executescript(_V0_DDL)
    for sid, role, txt in [("s-old-1", "user", "سؤال قديم 1"), ("s-old-1", "assistant", "رد قديم 1"),
                            ("s-old-1", "user", "سؤال قديم 2"), ("s-old-2", "user", "سؤال زائر"),
                            ("s-old-2", "assistant", "رد للزائر")]:
        c.execute("INSERT INTO messages(session_id,role,content,ts) VALUES(?,?,?,?)", (sid, role, txt, "2025-03-01T10:00:00"))
    c.execute("INSERT INTO complaints(session_id,text,ts) VALUES('s-old-1','شكوى قديمة','2025-03-01T10:05:00')")
    c.execute("INSERT INTO feedback(session_id,question,answer,rating,ts) VALUES('s-old-1','q','a','good','2025-03-01T10:06:00')")
    c.execute("INSERT INTO feedback(session_id,question,answer,rating,ts) VALUES('s-old-1','q','a','ok','2025-03-01T10:07:00')")
    for ev, data in [("chat", '{"session":"s-old-1","rag":true}'), ("chat", '{"session":"s-old-2","rag":false}'),
                     ("exam", '{"topic":"ADDIE","session":"s-old-1","format":"interactive"}')]:
        c.execute("INSERT INTO analytics(event,data,ts) VALUES(?,?,?)", (ev, data, "2025-03-01T10:00:00"))
    c.commit(); c.close()

LEGACY_RAW_TOKEN, LEGACY_OLD_TOKEN = "legacy-plaintext-token-abc", "legacy-ancient-token-zzz"
def build_legacy_auth_era(path):
    """نسخة أحدث شوية: users + auth_sessions (توكن نص صريح، من غير expires_at) + messages فيها user_id،
    من غير جدول sessions ولا quizzes ولا documents."""
    c = sqlite3.connect(path)
    c.executescript(_V0_DDL.replace("ts TEXT);\nCREATE TABLE complaints", "ts TEXT, user_id INTEGER);\nCREATE TABLE complaints", 1) + """
CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student', grade INTEGER, created_at TEXT);
CREATE TABLE auth_sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE quiz_answers(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, topic TEXT, question TEXT,
    chosen_index INTEGER, correct_index INTEGER, is_correct INTEGER, ts TEXT);
""")
    c.executemany("INSERT INTO users(username,password_hash,role,grade,created_at) VALUES(?,?,?,?,?)",
                  [("legacy1", "x", "student", 1, "2025-01-01T00:00:00"),
                   ("legacy4", "x", "student", 4, "2025-01-01T00:00:00"),
                   ("legacyprof", "x", "professor", 3, "2025-01-01T00:00:00")])
    now = datetime.now().isoformat()
    c.execute("INSERT INTO auth_sessions(token,user_id,created_at) VALUES(?,?,?)", (LEGACY_RAW_TOKEN, 1, now))
    c.execute("INSERT INTO auth_sessions(token,user_id,created_at) VALUES(?,?,?)", (LEGACY_OLD_TOKEN, 2, "2020-01-01T00:00:00"))
    c.executemany("INSERT INTO messages(session_id,role,content,ts,user_id) VALUES(?,?,?,?,?)",
                  [("s-owned", "user", "سؤال طالب", "2025-03-01T10:00:00", 1), ("s-owned", "assistant", "رد", "2025-03-01T10:00:01", None),
                   ("s-guest", "user", "سؤال زائر", "2025-03-01T11:00:00", None), ("s-guest", "assistant", "رد", "2025-03-01T11:00:01", None)])
    c.execute("INSERT INTO quiz_answers(session_id,topic,question,chosen_index,correct_index,is_correct,ts) VALUES('s-owned','ADDIE','q',0,0,1,'2025-03-01T10:10:00')")
    c.execute("INSERT INTO feedback(session_id,question,answer,rating,ts) VALUES('s-owned','q','a','bad','2025-03-01T10:06:00')")
    c.commit(); c.close()

def sha256_file(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

# ═══════════════════════ 1) مايجريشن قاعدة البيانات ═══════════════════════
section("1) المايجريشن — قواعد بيانات قديمة حقيقية")

# 1-أ) الشكل الأصلي (messages من غير user_id) — ده اللي كان بيوقّع التطبيق عند الإقلاع
wd = make_workdir(); build_legacy_v0(wd / "app_data.db")
before = {"messages": 5, "complaints": 1, "feedback": 2, "analytics": 3}
snap, res = snapshot(wd)
check(snap is not None, "1أ: تطبيق بقاعدة قديمة من غير messages.user_id بيبدأ بنجاح (الباج القديم: no such column: user_id)", res.stdout[-300:] + res.stderr[-500:])
if snap:
    check("user_id" in snap["messages_cols"], "1أ: عمود messages.user_id اتضاف")
    check(snap["version"] == 2, "1أ: PRAGMA user_version اترفع لـ 2", str(snap["version"]))
    check(snap["indexes"] == ALL_INDEXES, "1أ: كل الفهارس اتعملت بعد ما أعمدتها اتضمنت", str(snap["indexes"]))
    check(all(snap["counts"][t] == n for t, n in before.items()), "1أ: صفر فقد في البيانات (messages/complaints/feedback/analytics)", str(snap["counts"]))
    check(snap["sessions"] == {"s-old-1": None, "s-old-2": None}, "1أ: sessions اتعبّت من الرسايل القديمة كجلسات مجهولة (مفيش ملكية اتخترعت)", str(snap["sessions"]))
    check([m[2] for m in snap["msgs"]][:2] == ["سؤال قديم 1", "رد قديم 1"], "1أ: محتوى الرسايل القديمة زي ما هو")
    check(snap["feedback"] == [["good"], ["ok"]], "1أ: التقييمات القديمة (حتى القيمة الغريبة) اتحفظت كما هي")
snap2, res2 = snapshot(wd)
check(snap2 == snap, "1أ: تشغيل المايجريشن مرة تانية idempotent (نفس الحالة بالظبط، من غير أخطاء)", res2.stderr[-300:])

# 1-ب) عصر الـ auth: توكنات بالنص الصريح + من غير expires_at + من غير sessions
wd = make_workdir(); build_legacy_auth_era(wd / "app_data.db")
snap, res = snapshot(wd, extra_body="""
me_ok = client.get('/auth/me', headers={'Authorization': 'Bearer %s'})
me_old = client.get('/auth/me', headers={'Authorization': 'Bearer %s'})
print('ME=' + json.dumps([me_ok.status_code, me_ok.json().get('username') if me_ok.status_code == 200 else None, me_ok.json().get('grade') if me_ok.status_code == 200 else None, me_old.status_code]))
""" % (LEGACY_RAW_TOKEN, LEGACY_OLD_TOKEN))
check(snap is not None, "1ب: قاعدة بيانات عصر الـ auth بتتحدّث بنجاح", res.stderr[-500:])
if snap:
    me_line = next(l for l in res.stdout.splitlines() if l.startswith("ME="))
    me_status, me_user, me_grade, old_status = json.loads(me_line[3:])
    check(all(len(t) == 64 for t in snap["tokens"]) and LEGACY_RAW_TOKEN not in snap["tokens"] and LEGACY_OLD_TOKEN not in snap["tokens"],
          "1ب: كل التوكنات القديمة اتهاشت — مفيش توكن خام متخزّن بعد المايجريشن", str(snap["tokens"]))
    check(snap["expires_null"] == 0, "1ب: expires_at اتعبّى لكل التوكنات القديمة")
    check(me_status == 200 and me_user == "legacy1", "1ب: توكن العميل القديم لسه صالح (سلوك تسجيل الدخول الحالي اتحافظ عليه)", str((me_status, me_user)))
    check(old_status == 401, "1ب: توكن قديم جدًا (صلاحيته حسب سياسة الجلسات انتهت) بيتعامل كتوكن غلط", str(old_status))
    check(me_grade == 3, "1ب: الطالب القديم بفرقة 1 بيتحلّل لفرقة النشر (3)", str(me_grade))
    users = {u[0]: u for u in snap["users"]}
    check(users["legacy1"][2] == 3 and users["legacy4"][2] == 3, "1ب: حسابات الطلبة (فرقة 1 و 4) اتحوّلت لـ APP_GRADE=3", str(users))
    check(users["legacyprof"][1] == "professor", "1ب: دور professor اتحافظ عليه (مفيش تغيير في الأدوار)")
    check(snap["sessions"].get("s-owned") == 1 and snap["sessions"].get("s-guest") is None and "s-guest" in snap["sessions"],
          "1ب: جلسة فيها رسايل طالب اتربطت بمالكه، وجلسة الزائر فضلت مجهولة", str(snap["sessions"]))
    check(snap["counts"]["users"] == 3 and snap["counts"]["quiz_answers"] == 1 and snap["counts"]["messages"] == 4,
          "1ب: الحسابات وتاريخ الاختبارات والرسايل كلها اتحفظت", str(snap["counts"]))
    check(snap["indexes"] == ALL_INDEXES and snap["version"] == 2, "1ب: الفهارس كاملة و user_version=2")
snap_b2, _ = snapshot(wd)
check(snap_b2 is not None and snap_b2["tokens"] == snap["tokens"], "1ب: تشغيل تاني مبيغيّرش التوكنات المتهاشة (idempotent)")

# 1-ج) نسخة من app_data.db الفعلية في الريبو (لو موجودة) — الأصل مبيتلمسش
repo_db = ROOT / "app_data.db"
if repo_db.exists():
    wd = make_workdir(); shutil.copy(repo_db, wd / "app_data.db")
    orig_hash = sha256_file(repo_db)
    pre = sqlite3.connect(wd / "app_data.db")
    pre_counts = {t: pre.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for (t,) in pre.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    pre.close()
    snap, res = snapshot(wd)
    check(snap is not None, "1ج: نسخة من app_data.db الفعلية بتبدأ بنجاح", res.stderr[-500:])
    if snap:
        check(all(snap["counts"].get(t) == n for t, n in pre_counts.items()), "1ج: كل جداول app_data.db الأصلية محتفظة بعدد صفوفها", f"{pre_counts} vs {snap['counts']}")
    check(sha256_file(repo_db) == orig_hash, "1ج: app_data.db الأصلية في الريبو ما اتلمستش (نفس sha256)")
else:
    print("SKIP  1ج: مفيش app_data.db في الريبو")

# 1-د) قاعدة جديدة تمامًا
wd = make_workdir()
snap, res = snapshot(wd)
check(snap is not None and snap["version"] == 2 and snap["indexes"] == ALL_INDEXES and "user_id" in snap["messages_cols"],
      "1د: قاعدة جديدة: كل الجداول والفهارس و user_version=2", res.stderr[-300:])

# ═══════════════════════ 2) إعدادات وتشغيل ═══════════════════════
section("2) الإعدادات: APP_GRADE، مفاتيح Groq، اللوج، CORS، سر الأدمن")
src_all = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in
                    [ROOT / "app.py", ROOT / "frontend" / "index.html", ROOT / "frontend" / "dashboard.html",
                     ROOT / "static" / "auth.js", ROOT / "static" / "sw.js"])
check("FIXED_GRADE" not in src_all, "2: مفيش أي اعتماد على FIXED_GRADE في الكود")

wd = make_workdir(); build_legacy_auth_era(wd / "app_data.db")
r = run_py(wd, """
reg = client.post('/auth/register', json={'username': 'newstudent', 'password': 'password1'}).json()
kb = client.get('/kb-status').json()
c = sqlite3.connect('app_data.db')
print('OUT=' + json.dumps({'grade': reg['grade'], 'kb': kb, 'legacy_grades': [r[0] for r in c.execute("SELECT grade FROM users WHERE role='student' ORDER BY id")]}))
""", {"APP_GRADE": "2"})
out = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("OUT="))[4:]) if "OUT=" in r.stdout else {}
check(out.get("grade") == 2 and out.get("kb", {}).get("ready_grades") == [2] and set(out.get("legacy_grades", [])) == {2},
      "2: APP_GRADE قابل للضبط: تسجيل جديد + kb-status + تحويل الحسابات القديمة كلهم على القيمة المُهيّأة", r.stderr[-400:] + str(out))

r = run_py(wd, "print('KEYS=' + json.dumps(appmod.GROQ_KEYS))",
           {"GROQ_API_KEY_3": "gsk_third", "GROQ_API_KEY_5": "gsk_fifth", "GROQ_API_KEY_2": ""})
keys_line = next((l for l in r.stdout.splitlines() if l.startswith("KEYS=")), "KEYS=[]")
check(json.loads(keys_line[5:]) == ["gsk_third", "gsk_fifth"], "2: متغيرات مفاتيح Groq الخمسة مدعومة، والفاضي/الناقص بيتخطّى بأمان", keys_line)
r = run_py(wd, "print('KEYS=' + json.dumps(appmod.GROQ_KEYS))", {"GROQ_API_KEY": "a", "GROQ_API_KEY_2": "b", "GROQ_API_KEY_3": "c", "GROQ_API_KEY_4": "d", "GROQ_API_KEY_5": "e"})
check(json.loads(next(l for l in r.stdout.splitlines() if l.startswith("KEYS="))[5:]) == list("abcde"), "2: الخمس مفاتيح كلهم متحمّلين (مفيش _6 ولا _7)")

r = run_py(wd, "client.get('/health'); client.post('/chat', json={'message': 'hi'})\nprint('LOGS=' + json.dumps(sorted(p for p in os.listdir('.') if p.endswith('.log'))))")
check(json.loads(next(l for l in r.stdout.splitlines() if l.startswith("LOGS="))[5:]) == [], "2: التطبيق مبيعملش أي ملف .log جوه المشروع افتراضيًا (اللوج على stdout/stderr)", r.stdout[-200:])
r = run_py(wd, "client.get('/health')\nprint('LOGS=' + json.dumps(sorted(p for p in os.listdir('.') if p.endswith('.log'))))", {"LOG_FILE": "opt-in.log"})
check("opt-in.log" in next(l for l in r.stdout.splitlines() if l.startswith("LOGS=")), "2: ملف اللوج بيتعمل بس لما LOG_FILE يتحدد صراحةً")

r = run_py(wd, "print('CORS_ORIGINS=' + json.dumps(appmod.ALLOWED_ORIGINS))")
check(json.loads(next(l for l in r.stdout.splitlines() if l.startswith("CORS_ORIGINS="))[13:]) == [], "2: CORS من غير إعداد = مفيش أصول مسموحة (نفس الأصل بس)")
r = run_py(wd, "print('CORS_ORIGINS=' + json.dumps(appmod.ALLOWED_ORIGINS))", {"ALLOWED_ORIGINS": "https://a.example, https://b.example"})
check(json.loads(next(l for l in r.stdout.splitlines() if l.startswith("CORS_ORIGINS="))[13:]) == ["https://a.example", "https://b.example"], "2: ALLOWED_ORIGINS بيتقرا كقايمة صريحة")
r = run_py(wd, "print('IMPORTED')", {"ALLOWED_ORIGINS": "*"})
check("IMPORTED" not in r.stdout and "RuntimeError" in r.stderr, "2: ALLOWED_ORIGINS='*' مرفوض عند الإقلاع")
check('allow_origins=["*"]' not in (ROOT / "app.py").read_text(encoding="utf-8"), "2: مفيش wildcard CORS في الكود")

for bad, label in (("admin_2025", "القيمة القديمة غير الآمنة"), ("", "فاضي")):
    r = run_py(wd, "print('IMPORTED')", {"ADMIN_SECRET": bad})
    check("IMPORTED" not in r.stdout and "RuntimeError" in r.stderr and "ADMIN_SECRET" in r.stderr, f"2: ADMIN_SECRET {label} → الإقلاع بيفشل بخطأ واضح")
r = run_py(wd, "print('IMPORTED')", {"ADMIN_SECRET": ""})
check(SECRET not in r.stderr, "2: رسالة خطأ الإقلاع مبتكشفش أي سر")

# ═══════════════════════ التطبيق الرئيسي في نفس العملية ═══════════════════════
TMP = Path(tempfile.mkdtemp(prefix="edtech-test-"))
for name in ("app.py", "static", "frontend", "data"):
    src = ROOT / name
    (shutil.copytree if src.is_dir() else shutil.copy)(src, TMP / name)
os.chdir(TMP); sys.path.insert(0, str(TMP))
_real_isdir = os.path.isdir
os.path.isdir = lambda p: False if str(p) == "/data" else _real_isdir(p)
import app as appmod                                   # noqa: E402
os.path.isdir = _real_isdir
from fastapi import HTTPException                      # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402
client = TestClient(appmod.app)

class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG); self.records = []
    def emit(self, record):
        self.records.append(self.format(record))
CAP = _Cap(); logging.getLogger().addHandler(CAP); logging.getLogger().setLevel(logging.DEBUG)

def auth(tok): return {"Authorization": f"Bearer {tok}"}
def db_one(sql, args=()): return appmod.db_query_one(sql, args)
def db_all(sql, args=()): return appmod.db_query_all(sql, args)
def db_dump():
    c = sqlite3.connect(TMP / "app_data.db"); t = "\n".join(c.iterdump()); c.close(); return t
ISSUED_TOKENS = []
def register(username, password="password1"):
    r = client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    ISSUED_TOKENS.append(r.json()["token"]); return r.json()["token"]
def sse_events(resp):
    return [json.loads(l[6:]) for l in resp.text.split("\n") if l.startswith("data: ")]
def reset_limiters():
    for lim in (appmod._login_limiter, appmod._admin_limiter, appmod._admin_global_limiter):
        with lim._lock: lim._d.clear()

# ── test doubles للـ LLM (مفيش اتصال حقيقي بـ Groq) ────────────────────────────
EXAM = {"mcq": [
    {"question": "ما هو نموذج ADDIE؟", "options": ["نموذج للتصميم التعليمي", "لغة برمجة", "قاعدة بيانات", "نظام تشغيل"], "correct_index": 0},
    {"question": "أول مرحلة في ADDIE؟", "options": ["التصميم", "التحليل", "التطوير", "التقويم"], "correct_index": 1},
    {"question": "مرحلة التقويم تأتي؟", "options": ["أولًا", "ثانيًا", "أخيرًا", "لا تأتي"], "correct_index": 2}],
    "essay": [{"question": "اشرح ADDIE.", "model_answer": "خمس مراحل."}],
    "practical": {"question": "صمّم درسًا.", "instructions": "اذكر الأهداف."}}
STATE = {"llm_fail": False, "stream_fail": False, "probe": None}
def _probe_no_lock():
    """بيحاول ياخد قفل كتابة على اتصال تاني — لو فيه transaction مفتوحة أثناء انتظار الـ LLM هيفشل."""
    c = sqlite3.connect(TMP / "app_data.db", timeout=0.3, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("ROLLBACK"); return True
    except sqlite3.OperationalError:
        return False
    finally:
        c.close()
def fake_llm(msgs, max_tokens=1500, temp=0.5, json_mode=False):
    if STATE["probe"] is not None: STATE["probe"].append(_probe_no_lock())
    if STATE["llm_fail"]: raise RuntimeError("simulated provider outage")
    return json.dumps(EXAM, ensure_ascii=False) if json_mode else "إجابة تجريبية من المعيد الذكي."
def fake_llm_stream(msgs, max_tokens=1500):
    if STATE["stream_fail"]: raise RuntimeError("simulated stream outage")
    for t in ("أهلًا ", "بك "):
        if STATE["probe"] is not None: STATE["probe"].append(_probe_no_lock())
        yield t
REAL_LLM, REAL_LLM_STREAM, REAL_GROQ = appmod.llm, appmod.llm_stream, appmod.Groq
appmod.llm, appmod.llm_stream = fake_llm, fake_llm_stream

def count(sql, args=()): return db_one(sql, args)[0]
_tls = threading.local()
def tclient():
    """TestClient خاص بكل thread (آمن مع Starlette الحقيقي، ومع النسخة البديلة)."""
    if not hasattr(_tls, 'c'): _tls.c = TestClient(appmod.app)
    return _tls.c

# ═══════════════════════ 3) تزامن SQLite ═══════════════════════
section("3) تزامن SQLite (بلا 'cannot commit' وبلا فقد كتابة)")
check(not hasattr(appmod, "DB"), "3: مفيش اتصال SQLite عام مشترك — كل عملية بتاخد اتصالها")
with appmod.get_db() as _c:
    _fk, _jm, _bt = (_c.execute("PRAGMA foreign_keys").fetchone()[0], _c.execute("PRAGMA journal_mode").fetchone()[0],
                     _c.execute("PRAGMA busy_timeout").fetchone()[0])
check(_fk == 1 and str(_jm).lower() == "wal" and _bt >= 5000, "3: كل اتصال: WAL + foreign_keys=ON + busy_timeout", f"{_fk},{_jm},{_bt}")

errors = []
def _hammer(i):
    try:
        for j in range(5):
            r = tclient().post("/chat", json={"message": f"سؤال تزامن {i}-{j}", "session_id": f"conc-{i}"})
            if r.status_code != 200: errors.append(f"{i}/{j}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        errors.append(f"{i}: {type(e).__name__}: {e}")
with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex: list(ex.map(_hammer, range(20)))
check(not errors, "3: 20 thread × 5 رسايل متزامنة من غير أي خطأ (ولا 'cannot commit - no transaction is active')", " | ".join(errors[:4]))
check(count("SELECT COUNT(*) FROM messages WHERE session_id LIKE 'conc-%'") == 200 and count("SELECT COUNT(*) FROM sessions WHERE id LIKE 'conc-%'") == 20,
      "3: كل الكتابات اتسجّلت (100 سؤال + 100 رد، 20 جلسة) من غير فقد")

TOK_A = register("alice"); TOK_B = register("bob")
UID_A = count("SELECT id FROM users WHERE username='alice'"); UID_B = count("SELECT id FROM users WHERE username='bob'")
SHARED = "shared-owned-session"
client.post("/chat", json={"message": "بداية", "session_id": SHARED}, headers=auth(TOK_A))
errors = []
def _same_session(i):
    try:
        for j in range(5):
            r = tclient().post("/chat", json={"message": f"م{i}-{j}", "session_id": SHARED}, headers=auth(TOK_A))
            if r.status_code != 200: errors.append(f"{r.status_code} {r.text[:80]}")
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex: list(ex.map(_same_session, range(10)))
check(not errors and count("SELECT COUNT(*) FROM messages WHERE session_id=?", (SHARED,)) == 102, "3: تحديثات متزامنة لنفس الجلسة (10×5) من غير أخطاء أو فقد", " | ".join(errors[:3]))

before_evt = count("SELECT COUNT(*) FROM analytics WHERE event='chat'")
errors = []
def _stream_one(i):
    try:
        r = tclient().post("/chat/stream", json={"message": f"ستريم {i}", "session_id": f"cstream-{i}"})
        if r.status_code != 200 or not any(e.get("done") for e in sse_events(r)): errors.append(f"{i}: {r.status_code}")
    except Exception as e:
        errors.append(f"{i}: {type(e).__name__}: {e}")
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex: list(ex.map(_stream_one, range(10)))
check(not errors and count("SELECT COUNT(*) FROM analytics WHERE event='chat'") == before_evt + 10
      and count("SELECT COUNT(*) FROM messages WHERE session_id LIKE 'cstream-%' AND role='assistant'") == 10,
      "3: 10 ستريم متزامنين: 10 أحداث + 10 ردود بالظبط، من غير أخطاء", " | ".join(errors[:3]))

STATE["probe"] = []
client.post("/chat", json={"message": "probe", "session_id": "probe-1"})
client.post("/chat/stream", json={"message": "probe", "session_id": "probe-2"})
client.post("/exam", json={"topic": "ADDIE", "session_id": "probe-3"})
client.post("/grade", json={"answer": "a", "session_id": "probe-4"})
probes, STATE["probe"] = STATE["probe"], None
check(len(probes) >= 5 and all(probes), "3: مفيش transaction مفتوحة (ولا قفل كتابة) أثناء انتظار الـ LLM — chat/stream/exam/grade", str(probes))

# ═══════════════════════ 4) المصادقة ═══════════════════════
section("4) المصادقة وحماية الأدمن")
reset_limiters()
check(client.post("/auth/register", json={"username": "shortpw", "password": "short1"}).status_code == 400, "4: كلمة سر أقصر من 8 → 400")
check(client.post("/auth/register", json={"username": "longpw1", "password": "x" * 129}).status_code == 422, "4: كلمة سر أطول من 128 → 422")
check(client.post("/auth/register", json={"username": "longpw2", "password": "x" * 73}).status_code == 400, "4: كلمة سر أطول من حد bcrypt (72 بايت) → 400 (مش 500)")
check(client.post("/auth/register", json={"username": "longpw3", "password": "ع" * 40}).status_code == 400, "4: كلمة سر عربي > 72 بايت → 400")
check(client.post("/auth/register", json={"username": "  ab  ", "password": "password1"}).status_code == 400, "4: اسم مستخدم أقصر من 3 بعد التقليم → 400")
check(client.post("/auth/register", json={"username": "u" * 65, "password": "password1"}).status_code == 422, "4: اسم مستخدم أطول من 64 → 422")
check(client.post("/auth/register", json={"password": "password1"}).status_code == 422, "4: من غير username → 422")
check(client.post("/auth/register", json={"username": "alice", "password": "password1"}).status_code == 409, "4: اسم مستخدم مكرر → 409")
register("  spacey  ")
check(client.post("/auth/login", json={"username": "spacey", "password": "password1"}).status_code == 200, "4: اسم المستخدم بيتقلّم (بيدخل بـ 'spacey')")

results = []
def _dup(i): results.append(tclient().post("/auth/register", json={"username": "racer", "password": "password1"}).status_code)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex: list(ex.map(_dup, range(8)))
check(sorted(results) == [200] + [409] * 7 and count("SELECT COUNT(*) FROM users WHERE username='racer'") == 1, "4: 8 تسجيلات متزامنة بنفس الاسم: واحد ينجح والباقي 409 (مفيش 500)", str(results))

row = db_one("SELECT token FROM auth_sessions WHERE user_id=?", (UID_A,))
check(row[0] == hashlib.sha256(TOK_A.encode()).hexdigest() and TOK_A not in db_dump(), "4: التوكن المخزّن SHA-256 هاش، والتوكن الخام مش موجود في أي مكان في الداتابيز")
r = client.post("/auth/login", json={"username": "alice", "password": "password1"}); LOGIN_TOK = r.json()["token"]; ISSUED_TOKENS.append(LOGIN_TOK)
check(r.status_code == 200 and r.json()["grade"] == appmod.APP_GRADE, "4: تسجيل دخول ناجح والفرقة = APP_GRADE")
check(client.post("/auth/login", json={"username": "alice", "password": "wrong-pass"}).status_code == 401, "4: كلمة سر غلط → 401")
check(client.post("/auth/login", json={"username": "ghost", "password": "password1"}).status_code == 401, "4: يوزر مش موجود → 401 (نفس الرد)")
check(client.post("/auth/login", json={"username": "alice", "password": "x" * 300}).status_code == 422, "4: كلمة سر ضخمة عند الدخول → 422")

t = register("expiry_user"); h = hashlib.sha256(t.encode()).hexdigest()
check(client.get("/auth/me", headers=auth(t)).status_code == 200, "4: توكن صالح → 200")
appmod.db_execute("UPDATE auth_sessions SET expires_at=? WHERE token=?", ((datetime.now() - timedelta(minutes=1)).isoformat(), h))
check(client.get("/auth/me", headers=auth(t)).status_code == 401 and count("SELECT COUNT(*) FROM auth_sessions WHERE token=?", (h,)) == 0,
      "4: توكن منتهي = توكن غلط (401) واتمسح")
t2 = register("expiry_user2"); appmod.db_execute("UPDATE auth_sessions SET expires_at='garbage' WHERE token=?", (hashlib.sha256(t2.encode()).hexdigest(),))
check(client.get("/auth/me", headers=auth(t2)).status_code == 401, "4: تاريخ صلاحية تالف → 401 (مش 500)")
t3 = register("logout_user")
check(client.post("/auth/logout", headers=auth(t3)).status_code == 200 and client.get("/auth/me", headers=auth(t3)).status_code == 401, "4: logout بيبطّل التوكن")
check(client.get("/auth/me", headers=auth("nonsense-token")).status_code == 401 and client.get("/auth/me").status_code == 401
      and client.get("/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401, "4: توكن غلط / من غير توكن / scheme غلط → 401")

for _ in range(5): client.post("/auth/login", json={"username": "alice", "password": "wrong-pass"})
check(client.post("/auth/login", json={"username": "alice", "password": "wrong-pass"}).status_code == 429, "4: 6 محاولات غلط → 429")
check(client.post("/auth/login", json={"username": "alice", "password": "password1"}).status_code == 429, "4: حتى بكلمة السر الصح لسه متقفول لحد انتهاء المدة")
appmod._clear_login_attempts("alice")
check(client.post("/auth/login", json={"username": "alice", "password": "password1"}).status_code == 200, "4: بعد انتهاء المدة الدخول شغّال")
for i in range(appmod.RATE_LIMIT_MAX_KEYS + 2000): appmod._login_limiter.record(f"attacker-{i}")
check(appmod._login_limiter.size() <= appmod.RATE_LIMIT_MAX_KEYS, "4: حالة الـ rate limiter في الذاكرة محدودة الحجم مهما اخترع المهاجم أسماء", str(appmod._login_limiter.size()))

reset_limiters()
check(client.post("/admin", json={"secret": SECRET, "action": "stats"}).status_code == 404, "4: /admin القديم اتشال (404) — El Dashboard هي الإدارة الوحيدة")
H1, H2 = {"X-Forwarded-For": "203.0.113.5"}, {"X-Forwarded-For": "203.0.113.6"}
codes = [client.post("/dash/summary", json={"secret": "wrong-secret-value"}, headers=H1).status_code for _ in range(10)]
check(codes == [403] * 10, "4: 10 محاولات سر غلط → 403", str(codes))
check(client.post("/dash/summary", json={"secret": "wrong-secret-value"}, headers=H1).status_code == 429, "4: المحاولة الـ 11 → 429")
check(client.post("/dash/summary", json={"secret": SECRET}, headers=H1).status_code == 429, "4: حتى بالسر الصح، العميل المتقفول لسه 429")
check(client.post("/dash/summary", json={"secret": SECRET}, headers=H2).status_code == 200, "4: عميل تاني (عنوان مختلف) بالسر الصح → 200")
reset_limiters()
for i in range(appmod.ADMIN_MAX_ATTEMPTS * 5):
    client.post("/dash/summary", json={"secret": "wrong-secret-value"}, headers={"X-Forwarded-For": f"198.51.100.{i}"})
check(client.post("/dash/summary", json={"secret": SECRET}, headers={"X-Forwarded-For": "192.0.2.77"}).status_code == 429,
      "4: تدوير عناوين X-Forwarded-For مش بيتحايل على الحد (حد عام أوسع)")
reset_limiters()
check(client.post("/dash/summary", json={"secret": SECRET}).status_code == 200, "4: بعد ما الحدود تتصفّر السر الصح شغّال")

# ═══════════════════════ 5) عزل الجلسات ═══════════════════════
section("5) عزل الجلسات (release blocker)")
PDF_OK = make_pdf(["The ADDIE model has five phases: Analysis, Design, Development, Implementation and Evaluation. " * 3])
def hit_all(sid, tok):
    h = auth(tok) if tok else {}
    return {
        "history": client.get(f"/history/{sid}", headers=h).status_code,
        "chat": client.post("/chat", json={"message": "x", "session_id": sid}, headers=h).status_code,
        "chat_stream": client.post("/chat/stream", json={"message": "x", "session_id": sid}, headers=h).status_code,
        "exam": client.post("/exam", json={"topic": "ADDIE", "session_id": sid}, headers=h).status_code,
        "grade": client.post("/grade", json={"answer": "a", "session_id": sid}, headers=h).status_code,
        "feedback": client.post("/feedback", json={"session_id": sid, "question": "q", "answer": "a", "rating": "good"}, headers=h).status_code,
        "complaint": client.post("/complaint", json={"text": "t", "session_id": sid}, headers=h).status_code,
        "upload": client.post(f"/upload-pdf?session_id={sid}", files={"file": ("a.pdf", PDF_OK, "application/pdf")}, headers=h).status_code,
        "summary": client.post("/summary", json={"session_id": sid}, headers=h).status_code,
    }
r1 = client.post("/chat", json={"message": "سؤال alice"}, headers=auth(TOK_A)); SID_A = r1.json()["session_id"]
r2 = client.post("/chat", json={"message": "سؤال تاني"}, headers=auth(TOK_A))
check(len(SID_A) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", SID_A) and SID_A != r2.json()["session_id"], "5: طلب من غير session_id → id عشوائي آمن (secrets) وفريد", SID_A)
check(count("SELECT user_id FROM sessions WHERE id=?", (SID_A,)) == UID_A, "5: الجلسة الجديدة اتربطت بصاحب الطلب")
r = client.post("/chat", json={"message": "زائر"}); SID_G = r.json()["session_id"]
check(db_one("SELECT user_id FROM sessions WHERE id=?", (SID_G,))[0] is None, "5: جلسة الزائر فضلت مجهولة (user_id NULL)")

owner = hit_all(SID_A, TOK_A)
check(all(v == 200 for v in owner.values()), "5: صاحب الجلسة بيوصل لكل الـ endpoints عادي (history/chat/stream/exam/grade/feedback/complaint/upload/summary)", str(owner))
other_user = hit_all(SID_A, TOK_B)
check(all(v == 403 for v in other_user.values()), "5: يوزر تاني → 403 على كل الـ endpoints", str(other_user))
anon_on_owned = hit_all(SID_A, None)
check(all(v == 403 for v in anon_on_owned.values()), "5: زائر على جلسة مصادَق عليها → 403 على كل الـ endpoints", str(anon_on_owned))
auth_on_anon = hit_all(SID_G, TOK_A)
check(all(v == 403 for v in auth_on_anon.values()), "5: يوزر مسجّل على جلسة زائر → 403 على كل الـ endpoints (مفيش تبنّي)", str(auth_on_anon))
check(db_one("SELECT user_id FROM sessions WHERE id=?", (SID_G,))[0] is None, "5: جلسة الزائر لسه مجهولة بعد محاولات التبنّي")
anon_own = hit_all(SID_G, None)
check(all(v == 200 for v in anon_own.values()), "5: الزائر اللي معاه الـ id بتاعه بيكمّل جلسته عادي", str(anon_own))

n_sessions = count("SELECT COUNT(*) FROM sessions")
check(client.get("/history/never-created-session-1").status_code == 404 and client.get("/history/never-created-session-1", headers=auth(TOK_A)).status_code == 404,
      "5: قراءة جلسة مش موجودة → 404 (زائر ومسجّل)")
check(client.post("/summary", json={"session_id": "never-created-session-2"}).status_code == 404
      and client.post("/feedback", json={"session_id": "never-created-session-3", "question": "q", "answer": "a", "rating": "good"}).status_code == 404,
      "5: summary/feedback على جلسة مش موجودة → 404")
check(client.post("/complaint", json={"text": "شكوى من غير جلسة", "session_id": "never-created-session-4"}).status_code == 200, "5: شكوى بجلسة مش موجودة لسه بتتقبل")
check(client.get("/history/bad id!").status_code == 404, "5: معرّف جلسة بصيغة غير صالحة → 404")
check(count("SELECT COUNT(*) FROM sessions") == n_sessions and count("SELECT COUNT(*) FROM sessions WHERE id LIKE 'never-created%'") == 0,
      "5: القراءة/الشكوى على جلسة مجهولة مبتنشئش أي صف في sessions")

fresh = str(uuid.uuid4())
check(client.post("/chat", json={"message": "m", "session_id": fresh}, headers=auth(TOK_A)).status_code == 200 and count("SELECT user_id FROM sessions WHERE id=?", (fresh,)) == UID_A,
      "5: id جديد بيتنشئ ويتربط بصاحب أول كتابة")
fresh2 = str(uuid.uuid4()); client.post("/chat", json={"message": "m", "session_id": fresh2})
check(db_one("SELECT user_id FROM sessions WHERE id=?", (fresh2,))[0] is None, "5: id جديد من زائر بيتنشئ مجهول")

wins = []
for round_no in range(15):
    sid = f"race-{uuid.uuid4()}"; barrier = threading.Barrier(2); res = {}
    def _go(who, headers):
        barrier.wait(); res[who] = tclient().post("/chat", json={"message": "race", "session_id": sid}, headers=headers).status_code
    ta = threading.Thread(target=_go, args=("anon", {})); tb = threading.Thread(target=_go, args=("alice", auth(TOK_A)))
    ta.start(); tb.start(); ta.join(); tb.join()
    owner_now = db_one("SELECT user_id FROM sessions WHERE id=?", (sid,))[0]
    ok = sorted(res.values()) == [200, 403] and ((res["alice"] == 200 and owner_now == UID_A) or (res["anon"] == 200 and owner_now is None))
    wins.append(ok)
check(all(wins), "5: سباق زائر ↔ يوزر على نفس id جديد (15 جولة): واحد بس بيكسب والتاني 403، والمالك متّسق", str(wins))

check(client.get("/conversations").status_code == 401, "5: /conversations من غير تسجيل → 401")
convs_a = {c["session_id"] for c in client.get("/conversations", headers=auth(TOK_A)).json()["conversations"]}
convs_b = {c["session_id"] for c in client.get("/conversations", headers=auth(TOK_B)).json()["conversations"]}
check(SID_A in convs_a and SID_A not in convs_b and SID_G not in convs_a, "5: /conversations بترجّع جلسات صاحبها بس")

# ═══════════════════════ 6) أمان الاختبار التفاعلي ═══════════════════════
section("6) الاختبار التفاعلي — الحقيقة من السيرفر بس")
def make_quiz(tok, sid):
    r = client.post("/exam", json={"topic": "ADDIE", "session_id": sid}, headers=auth(tok) if tok else {})
    return r.json()
d = make_quiz(TOK_A, SID_A)
check(d.get("format") == "interactive" and "quiz_id" in d and len(d["mcq"]) == 3, "6: توليد امتحان تفاعلي مع quiz_id")
QID = d["quiz_id"]; ca0 = d["mcq"][0]["correct_index"]
stored = json.loads(db_one("SELECT questions_json FROM quizzes WHERE id=?", (QID,))[0])
check(stored[0]["correct_index"] == ca0, "6: مفتاح الإجابات متخزّن في السيرفر (quizzes.questions_json)")
r = client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 0, "chosen_index": ca0}, headers=auth(TOK_A))
check(r.status_code == 200 and r.json()["is_correct"] is True, "6: إجابة صحيحة → is_correct=true من السيرفر", r.text)
wrong = (d["mcq"][1]["correct_index"] + 1) % 4
r = client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 1, "chosen_index": wrong, "correct_index": wrong, "is_correct": True, "score": 100}, headers=auth(TOK_A))
check(r.status_code == 200 and r.json()["is_correct"] is False and count("SELECT is_correct FROM quiz_answers WHERE session_id=? AND question=?", (SID_A, d["mcq"][1]["question"])) == 0,
      "6: حقول التزوير (correct_index/is_correct/score) بتتجاهل والصح/الغلط بيتحسب من مفتاح السيرفر", r.text)
check(client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 0, "chosen_index": 0}, headers=auth(TOK_A)).status_code == 409, "6: إجابة مكررة → 409")
check(client.post("/quiz/answer", json={"quiz_id": "nope", "question_index": 0, "chosen_index": 0}, headers=auth(TOK_A)).status_code == 404, "6: quiz_id مش موجود → 404")
check(client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 50, "chosen_index": 0}, headers=auth(TOK_A)).status_code == 400, "6: رقم سؤال غير موجود → 400")
check(client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 2, "chosen_index": 9}, headers=auth(TOK_A)).status_code == 400, "6: اختيار غير موجود → 400")
check(client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 2, "chosen_index": -1}, headers=auth(TOK_A)).status_code == 422
      and client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 999, "chosen_index": 0}, headers=auth(TOK_A)).status_code == 422, "6: أرقام خارج الحدود المطلقة → 422")
check(client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 2, "chosen_index": 0}, headers=auth(TOK_B)).status_code == 403, "6: يوزر تاني على امتحان alice → 403")
check(client.post("/quiz/answer", json={"quiz_id": QID, "question_index": 2, "chosen_index": 0}).status_code == 403, "6: زائر على امتحان يوزر → 403")

dg = make_quiz(None, SID_G)
check(client.post("/quiz/answer", json={"quiz_id": dg["quiz_id"], "question_index": 0, "chosen_index": 0}, headers=auth(TOK_A)).status_code == 403,
      "6: يوزر مسجّل مش قادر يجاوب على امتحان جلسة زائر بمعرفة quiz_id بس")
check(client.post("/quiz/answer", json={"quiz_id": dg["quiz_id"], "question_index": 0, "chosen_index": 0}).status_code == 200, "6: الزائر صاحب الامتحان بيجاوب عادي")
check(client.post("/quiz/answer", json={"quiz_id": dg["quiz_id"], "question_index": 0, "chosen_index": 0}).status_code == 409, "6: ...ومرة تانية → 409")

dq = make_quiz(TOK_A, SID_A); statuses = []; barrier = threading.Barrier(10)
def _dup_answer(_):
    barrier.wait()
    statuses.append(tclient().post("/quiz/answer", json={"quiz_id": dq["quiz_id"], "question_index": 0, "chosen_index": 1}, headers=auth(TOK_A)).status_code)
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex: list(ex.map(_dup_answer, range(10)))
check(sorted(statuses) == [200] + [409] * 9, "6: 10 إجابات متزامنة على نفس السؤال: واحدة 200 والباقي 409 حتمي (مفيش 500)", str(sorted(statuses)))
check(count("SELECT COUNT(*) FROM quiz_attempts WHERE quiz_id=?", (dq["quiz_id"],)) == 1
      and count("SELECT COUNT(*) FROM quiz_answers WHERE session_id=? AND question=?", (SID_A, dq["mcq"][0]["question"])) >= 1, "6: صف محاولة واحد بس اتسجّل")

# ═══════════════════════ 7) PDF ═══════════════════════
section("7) خط أنابيب PDF (بايتات PDF صالحة فعلًا)")
PDFS_BEFORE = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.pdf")))
check(not hasattr(appmod, "_pdf_cache"), "7: مفيش cache عام في الذاكرة — التخزين في جدول documents")
SID_P = str(uuid.uuid4())
r = client.post(f"/upload-pdf?session_id={SID_P}", files={"file": ("../../etc/notes.pdf", PDF_OK, "application/pdf")}, headers=auth(TOK_A))
check(r.status_code == 200 and r.json()["chars"] > 50, "7: PDF صالح → 200", r.text)
doc = db_one("SELECT session_id,user_id,filename,chars,uploaded_at,text FROM documents WHERE session_id=?", (SID_P,))
check(doc and doc[1] == UID_A and doc[2] == "notes.pdf" and doc[3] == len(doc[5]) and doc[4], "7: بيتخزّن: الجلسة + صاحبها + اسم الملف (من غير مسار) + عدد الحروف + وقت الرفع", str(doc[:5] if doc else doc))
check("ADDIE" in appmod.get_document_text(SID_P), "7: النص المستخرج قابل للاسترجاع من الداتابيز (مش من ذاكرة عملية)")
check(client.post("/summary", json={"session_id": SID_P}, headers=auth(TOK_A)).status_code == 200, "7: الملخص لصاحب الجلسة → 200")
check(client.post("/summary", json={"session_id": SID_P}, headers=auth(TOK_B)).status_code == 403 and client.post("/summary", json={"session_id": SID_P}).status_code == 403,
      "7: الملخص ليوزر تاني/زائر → 403")
check(client.post(f"/upload-pdf?session_id={SID_P}", files={"file": ("x.pdf", PDF_OK, "application/pdf")}, headers=auth(TOK_B)).status_code == 403, "7: رفع ملف في جلسة حد تاني → 403")
rc = client.post("/chat", json={"message": "ما هي مراحل النموذج؟", "session_id": SID_P}, headers=auth(TOK_A)).json()
check(rc.get("rag_used") is True, "7: سؤال في جلسة فيها PDF بيستخدم سياق المستند")
check(client.post(f"/upload-pdf?session_id={SID_P}", files={"file": ("notes.txt", PDF_OK, "text/plain")}, headers=auth(TOK_A)).status_code == 400
      and client.post(f"/upload-pdf?session_id={SID_P}", files={"file": ("", PDF_OK, "application/pdf")}, headers=auth(TOK_A)).status_code == 400, "7: امتداد غير .pdf أو اسم فاضي → 400")
check(client.post("/upload-pdf?session_id=bad%20id", files={"file": ("a.pdf", PDF_OK, "application/pdf")}, headers=auth(TOK_A)).status_code == 400, "7: معرّف جلسة غير صالح → 400")
new_sid = lambda: str(uuid.uuid4())
def up(data, name="f.pdf", sid=None, tok=TOK_A):
    return client.post(f"/upload-pdf?session_id={sid or new_sid()}", files={"file": (name, data, "application/pdf")}, headers=auth(tok))
for label, data in (("ملف عشوائي", b"this is not a pdf at all"), ("PDF مقطوع بعد الهيدر (تالف)", truncated_pdf()),
                    ("هيدر PDF + قمامة", b"%PDF-1.4\njunk junk junk")):
    r = up(data)
    check(r.status_code == 422, f"7: {label} → 422 برسالة واضحة (مش 500)", f"{r.status_code} {r.text[:120]}")
# محمي بكلمة سر / مش PDF فعلًا: بنختبر معالجة الكود نفسه عن طريق document double، مش سلوك parser بعينه
class _FakeDoc:
    def __init__(self, **kw): self.__dict__.update({"page_count": 1, "is_pdf": True, "needs_pass": False, "closed": False}, **kw)
    def __iter__(self): return iter([])
    def close(self): self.closed = True
_real_open_ = appmod.fitz.open
for label, doc in (("PDF محمي بكلمة سر", _FakeDoc(needs_pass=True)), ("ملف مفتوح بس مش PDF (is_pdf=False)", _FakeDoc(is_pdf=False))):
    appmod.fitz.open = lambda path, _d=doc: _d
    r = up(PDF_OK)
    check(r.status_code == 422 and doc.closed, f"7: {label} → 422 والمستند اتقفل", f"{r.status_code} {r.text[:100]} closed={doc.closed}")
appmod.fitz.open = _real_open_
check(up(make_pdf(["hi"])).status_code == 422, "7: PDF من غير نص كافٍ → 422")

old_max = appmod.MAX_PDF_BYTES; appmod.MAX_PDF_BYTES = 2000
r = up(make_pdf(["word " * 2000]))
check(r.status_code == 413, "7: ملف أكبر من حد الحجم → 413", f"{r.status_code}")
appmod.MAX_PDF_BYTES = old_max

class CountingFile:
    filename = "huge.pdf"
    def __init__(self, total): self.remaining, self.bytes_read = total, 0
    async def read(self, size=-1):
        n = min(size if size and size > 0 else 1 << 20, self.remaining); self.remaining -= n; self.bytes_read += n
        return b"%PDF-1.4\n"[:n] if self.bytes_read <= 9 else b"\0" * n
appmod.MAX_PDF_BYTES = 1024 * 1024; cf = CountingFile(200 * 1024 * 1024)
try:
    asyncio.run(appmod.upload_pdf(session_id=new_sid(), file=cf, authorization=f"Bearer {TOK_A}")); status_413 = False
except HTTPException as e:
    status_413 = e.status_code == 413
check(status_413 and cf.bytes_read <= appmod.MAX_PDF_BYTES + 1024 * 1024, "7: الرفع بيقف فورًا عند تخطّي الحد (مقروء ≤ الحد + chunk واحد من 200MB)", str(cf.bytes_read))
appmod.MAX_PDF_BYTES = old_max

old_pages = appmod.MAX_PDF_PAGES; appmod.MAX_PDF_PAGES = 5
check(up(make_pdf([f"page {i} content text here " * 4 for i in range(8)])).status_code == 413, "7: عدد صفحات أكبر من الحد → 413 (قبل الاستخراج)")
check(up(make_pdf([f"page {i} content text here " * 4 for i in range(3)])).status_code == 200, "7: عدد صفحات ضمن الحد → 200")
appmod.MAX_PDF_PAGES = old_pages

old_chars = appmod.MAX_PDF_TEXT_CHARS; appmod.MAX_PDF_TEXT_CHARS = 300
_orig_open, OPENED = appmod.fitz.open, []
def _counting_open(p):
    d = _orig_open(p); OPENED.append(d); return d
appmod.fitz.open = _counting_open
sid_b = new_sid(); r = up(make_pdf([("budget text " * 40) for _ in range(10)]), sid=sid_b)
appmod.fitz.open = _orig_open; appmod.MAX_PDF_TEXT_CHARS = old_chars
check(r.status_code == 200 and count("SELECT chars FROM documents WHERE session_id=?", (sid_b,)) <= 300, "7: النص المستخرج ملتزم بحد الحروف", r.text[:100])
try:
    touched = sum(1 for pg in OPENED[0] if getattr(pg, "get_text_calls", 0) > 0)
    check(touched == 1, "7: الاستخراج وقف لما ميزانية الحروف خلصت (صفحة واحدة من 10 اتقرت)", str(touched))
except Exception:
    print("SKIP  7: عدّاد صفحات الاستخراج (بيحتاج parser البديل)")

old_ts = (datetime.now() - timedelta(hours=appmod.DOCUMENT_TTL_HOURS + 1)).isoformat()
appmod.db_execute("INSERT INTO documents(session_id,user_id,filename,text,chars,uploaded_at) VALUES(?,?,?,?,?,?)", ("ttl-old", UID_A, "old.pdf", "old text " * 10, 90, old_ts))
check(appmod.get_document_text("ttl-old") == "", "7: مستند انتهت صلاحيته (TTL) مبيتقراش")
up(PDF_OK)
check(count("SELECT COUNT(*) FROM documents WHERE session_id='ttl-old'") == 0, "7: التنضيف: المستندات المنتهية بتتمسح مع أي رفع جديد")
check(set(glob.glob(os.path.join(tempfile.gettempdir(), "*.pdf"))) - PDFS_BEFORE == set(), "7: مفيش ملفات مؤقتة متبقية بعد النجاح والفشل وتجاوز الحد")

# ═══════════════════════ 8) تدوير مفاتيح Groq ═══════════════════════
section("8) تدوير مفاتيح Groq (بلا تكرار لا نهائي، وبلا استخدامها للتحايل على الحدود)")
class FakeStatusError(Exception):
    def __init__(self, status): super().__init__(f"HTTP {status}"); self.status_code = status
class FakeNetworkError(Exception): pass
CALLS, BEHAVIOR = [], {}
def _chunk(t): return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=t))])
class FakeGroq:
    def __init__(self, api_key=None, **kw): self.api_key = api_key; self.chat = types.SimpleNamespace(completions=self)
    def create(self, **kw):
        CALLS.append(self.api_key)
        if self.api_key in BEHAVIOR: raise BEHAVIOR[self.api_key]
        if kw.get("stream"): return iter([_chunk("a "), _chunk("b")])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok-from-" + self.api_key))])
ROTATION_KEYS = [f"gsk_rotation_key_{i}" for i in range(1, 6)]
_orig_sample = appmod.random.sample
appmod.Groq, appmod.llm, appmod.llm_stream = FakeGroq, REAL_LLM, REAL_LLM_STREAM
def scenario(keys, behavior, ordered=True):
    appmod.GROQ_KEYS = list(keys); BEHAVIOR.clear(); BEHAVIOR.update(behavior); CALLS.clear()
    appmod.random.sample = (lambda pop, k: list(pop)) if ordered else _orig_sample
def call():
    try: return appmod.llm([{"role": "user", "content": "hi"}]), None
    except Exception as e: return None, e
try:
    k1, k2, k3 = ROTATION_KEYS[:3]
    scenario([k1, k2, k3], {k1: FakeStatusError(401), k2: FakeStatusError(403)})
    out, err = call()
    check(out == f"ok-from-{k3}" and CALLS == [k1, k2, k3], "8: مفتاح مرفوض (401/403) → بينتقل للمفتاح التالي وبينجح، وكل مفتاح اتجرّب مرة واحدة", str(CALLS))
    scenario([k1, k2, k3], {k: FakeStatusError(401) for k in (k1, k2, k3)})
    out, err = call()
    check(err is not None and CALLS == [k1, k2, k3], "8: كل المفاتيح مرفوضة → بيوقف بعد تجربة كل واحد مرة (مفيش لفّ لا نهائي) ويرمي الخطأ", str(CALLS))
    scenario([k1, k2], {k1: FakeStatusError(429)})
    out, err = call()
    check(err is not None and CALLS == [k1], "8: 429 (rate limit) → احترام الحد: مفيش تدوير على مفتاح تاني كطريقة تحايل", str(CALLS))
    scenario([k1, k2], {k1: FakeStatusError(400)})
    out, err = call()
    check(err is not None and CALLS == [k1], "8: 400 (مشكلة في الطلب نفسه) → مفيش تدوير", str(CALLS))
    scenario([k1, k2], {k1: FakeStatusError(500)})
    out, err = call()
    check(out == f"ok-from-{k2}" and CALLS == [k1, k2], "8: خطأ سيرفر 5xx → بينتقل لمفتاح تاني", str(CALLS))
    scenario([k1, k2], {k1: FakeNetworkError("conn reset")})
    out, err = call()
    check(out == f"ok-from-{k2}", "8: خطأ اتصال من غير status_code → بينتقل لمفتاح تاني (مفيش كراش)")
    scenario([], {})
    out, err = call()
    check(isinstance(err, RuntimeError) and CALLS == [], "8: مفيش مفاتيح مُهيّأة → خطأ واضح من غير أي محاولة")
    ok_runs = 0
    for _ in range(40):
        scenario(ROTATION_KEYS, {k: FakeStatusError(401) for k in ROTATION_KEYS[:4]}, ordered=False)
        out, err = call()
        ok_runs += (out == f"ok-from-{ROTATION_KEYS[4]}" and len(CALLS) == len(set(CALLS)) <= 5)
    check(ok_runs == 40, "8: ترتيب عشوائي (40 مرة، 4 من 5 مفاتيح معطّلة): بينجح دايمًا ومفيش مفتاح بيتكرر في نفس الطلب", str(ok_runs))
    scenario([k1, k2, k3], {k1: FakeStatusError(401)})
    toks = list(appmod.llm_stream([{"role": "user", "content": "hi"}]))
    check(toks == ["a ", "b"] and CALLS == [k1, k2], "8: الستريم: فشل فتح الاتصال بمفتاح → بينتقل لمفتاح تاني قبل ما أي token يتبعت", str((toks, CALLS)))
    scenario([k1, k2], {k1: FakeStatusError(401), k2: FakeStatusError(401)})
    try:
        list(appmod.llm_stream([{"role": "user", "content": "hi"}])); raised = False
    except Exception:
        raised = True
    check(raised and CALLS == [k1, k2], "8: كل المفاتيح فشلت في الستريم → استثناء صريح (مبيألّفش رد بديل)")
    e_before = count("SELECT COUNT(*) FROM analytics WHERE event='chat'"); sid = new_sid()
    r = client.post("/chat/stream", json={"message": "outage", "session_id": sid})
    evs = sse_events(r); last_ev = json.loads(db_one("SELECT data FROM analytics WHERE event='chat' ORDER BY id DESC LIMIT 1")[0])
    check(any(e.get("error") for e in evs) and last_ev["status"] == "error" and count("SELECT COUNT(*) FROM analytics WHERE event='chat'") == e_before + 1
          and count("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='assistant'", (sid,)) == 0,
          "8: انهيار كل المفاتيح أثناء /chat/stream: حدث خطأ واحد، ومفيش رد وهمي محفوظ", str(last_ev))
finally:
    appmod.Groq, appmod.GROQ_KEYS, appmod.random.sample = REAL_GROQ, [], _orig_sample
    appmod.llm, appmod.llm_stream = fake_llm, fake_llm_stream

# ═══════════════════════ 9) التحليلات، الستريم، وحالة الاسترجاع ═══════════════════════
section("9) التحليلات: حدث واحد لكل عملية + حالة الاسترجاع")
def last_event(name):
    return json.loads(db_one("SELECT data FROM analytics WHERE event=? ORDER BY id DESC LIMIT 1", (name,))[0])
def n_events(name): return count("SELECT COUNT(*) FROM analytics WHERE event=?", (name,))
def n_msgs(sid, role=None):
    return count("SELECT COUNT(*) FROM messages WHERE session_id=?" + (" AND role=?" if role else ""), (sid, role) if role else (sid,))

sid = new_sid(); e0 = n_events("chat")
client.post("/chat", json={"message": "اشرح نموذج ADDIE", "session_id": sid})
ev = last_event("chat")
check(n_events("chat") == e0 + 1 and ev["stream"] is False and ev["status"] == "ok" and ev["session"] == sid and "retrieval_status" in ev and n_msgs(sid) == 2,
      "9: /chat العادي: حدث chat واحد (stream=false, status=ok, retrieval_status) + رسالتين", str(ev))
sid = new_sid(); e0 = n_events("chat")
r = client.post("/chat/stream", json={"message": "ستريم ناجح", "session_id": sid}); evs = sse_events(r); ev = last_event("chat")
check(n_events("chat") == e0 + 1 and ev["stream"] is True and ev["status"] == "ok" and n_msgs(sid, "assistant") == 1 and n_msgs(sid, "user") == 1
      and any(e.get("done") for e in evs), "9: /chat/stream الناجح: حدث chat واحد (stream=true) + رد مساعد واحد", str(ev))
check(n_events("stream") == 0, "9: مفيش حدث بالاسم 'stream' — الستريم بيتحسب كـ chat")

STATE["stream_fail"] = True; sid = new_sid(); e0 = n_events("chat")
r = client.post("/chat/stream", json={"message": "ستريم فاشل", "session_id": sid}); evs = sse_events(r); ev = last_event("chat")
check(n_events("chat") == e0 + 1 and ev["status"] == "error" and ev.get("error_type") == "RuntimeError" and n_msgs(sid, "assistant") == 0 and any(e.get("error") for e in evs),
      "9: ستريم فاشل: حدث واحد بحالة error، ورسالة الخطأ مش متحفوظة كرد مساعد", str(ev))
check("simulated" not in r.text, "9: نص استثناء المزوّد مبيوصلش للعميل")
STATE["stream_fail"] = False
STATE["llm_fail"] = True; sid = new_sid(); e0 = n_events("chat")
r = client.post("/chat", json={"message": "فشل عادي", "session_id": sid}); ev = last_event("chat")
check(r.status_code == 200 and r.json().get("error") is True and "simulated" not in r.text and n_events("chat") == e0 + 1 and ev["status"] == "error" and n_msgs(sid, "assistant") == 0,
      "9: /chat وقت فشل الـ LLM: رسالة عامة للعميل + error=true + مفيش رد محفوظ + حدث واحد بحالة error", r.text[:160])
rx = client.post("/exam", json={"topic": "موضوع", "session_id": new_sid()}); evx = last_event("exam")
check(rx.json().get("format") == "text" and "simulated" not in rx.text and evx["status"] == "error", "9: /exam وقت فشل الـ LLM: رد عام + حدث exam بحالة error")
STATE["llm_fail"] = False

sid = new_sid(); e0 = n_events("chat")
gen = appmod.stream_events([{"role": "user", "content": "x"}], sid, "chat", log_data={"rag": False, "retrieval_status": "empty", "vector_error": False}, save_chat=True, user_id=None)
next(gen); gen.close(); ev = last_event("chat")
check(n_events("chat") == e0 + 1 and ev["status"] == "cancelled" and ev["stream"] is True and ev["chars"] > 0 and n_msgs(sid, "assistant") == 0,
      "9: ستريم اتقطع في النص: حدث واحد بحالة cancelled (مش ناجح) ومفيش رد ناقص محفوظ", str(ev))
r = client.post("/exam/stream", json={"topic": "ADDIE", "session_id": new_sid()}); ev = last_event("exam")
check(ev["status"] == "ok" and ev["stream"] is True, "9: /exam/stream: حدث exam بحالة ok و stream=true")
r = client.post("/grade/stream", json={"answer": "إجابة", "session_id": new_sid()}); ev = last_event("grade")
check(ev["status"] == "ok" and ev["stream"] is True, "9: /grade/stream: حدث grade بحالة ok و stream=true")

class FakePine:
    def __init__(self, mode): self.mode = mode
    def query(self, **kw):
        if self.mode == "timeout": raise TimeoutError("pinecone timeout")
        if self.mode == "low": return types.SimpleNamespace(matches=[types.SimpleNamespace(score=0.2, metadata={"text": "x"})])
        return types.SimpleNamespace(matches=[types.SimpleNamespace(score=0.9, metadata={"text": "محتوى المنهج"})])
NOISE = "zzqqxx wwvvyy"
def rag(mode, q):
    appmod._pine, appmod._embed = (FakePine(mode) if mode else None), (lambda t: [0.1])
    return appmod.rag_lookup(q)
r_ = rag("timeout", NOISE)
check(r_.status == "error" and r_.vector_error and not r_.text, "9: Pinecone timeout ومفيش احتياط مفيد → status=error (مش empty)")
r_ = rag("timeout", "ADDIE")
check(r_.status == "fallback" and r_.vector_error and r_.text, "9: Pinecone timeout بس الاحتياط رجّع نص → fallback (ومتسجّل vector_error)")
r_ = rag("low", NOISE); check(r_.status == "empty" and not r_.vector_error, "9: Pinecone اشتغل ومفيش تطابق ولا احتياط → empty (فجوة محتوى حقيقية)")
r_ = rag("good", NOISE); check(r_.status == "vector" and "محتوى المنهج" in r_.text, "9: تطابق ناجح من Pinecone → vector")
r_ = rag(None, "ADDIE"); check(r_.status == "fallback", "9: من غير Pinecone → fallback من data/docs")
r_ = rag(None, NOISE); check(r_.status == "empty", "9: من غير Pinecone ومفيش تطابق → empty")
check(appmod.rag_lookup("x", grade=appmod.APP_GRADE + 1).status == "unavailable", "9: فرقة مش مدعومة → unavailable (مفيش استرجاع من مادة فرقة تانية)")
old_fb = appmod.FALLBACK_DOCS_PATH; appmod.FALLBACK_DOCS_PATH = "no/such/file"
check(rag(None, "ADDIE").status == "empty", "9: مسار ملف الاحتياط قابل للضبط (ملف مش موجود → empty)")
appmod.FALLBACK_DOCS_PATH = old_fb

def dash(path): return client.post(path, json={"secret": SECRET}).json()
t0 = dash("/dash/topics")
def chat_mode(mode, q):
    appmod._pine, appmod._embed = (FakePine(mode) if mode else None), (lambda t: [0.1])
    client.post("/chat", json={"message": q, "session_id": new_sid()})
for _ in range(2): chat_mode("low", NOISE)
chat_mode("good", NOISE)
for _ in range(3): chat_mode("timeout", NOISE)
chat_mode("timeout", "ADDIE")
appmod._pine = None
for legacy in ('{"session":"lg1","rag":true}', '{"session":"lg2","rag":false}'):
    appmod.db_execute("INSERT INTO analytics(event,data,ts) VALUES('chat',?,?)", (legacy, datetime.now().isoformat()))
t1 = dash("/dash/topics"); b0, b1 = t0["retrieval_breakdown"], t1["retrieval_breakdown"]
diff = {k: b1[k] - b0[k] for k in b1}
check(diff["empty"] == 2 and diff["vector"] == 1 and diff["error"] == 3 and diff["fallback"] == 1 and diff["legacy_matched"] == 1 and diff["legacy_empty"] == 1,
      "9: تفصيل حالات الاسترجاع في الداشبورد صح (empty/vector/error/fallback + الأحداث القديمة)", str(diff))
check(t1["retrieval_errors"] - t0["retrieval_errors"] == 4, "9: أخطاء البنية التحتية بتتعدّ لوحدها (3 error + 1 fallback بعد timeout)", str(t1["retrieval_errors"] - t0["retrieval_errors"]))
eligible = b1["vector"] + b1["fallback"] + b1["empty"] + b1["legacy_matched"] + b1["legacy_empty"]
check(t1["content_gap_rate"] == f"{round(100 * (b1['empty'] + b1['legacy_empty']) / eligible)}%",
      "9: content_gap_rate = empty الحقيقية بس ÷ (vector+fallback+empty) — من غير أخطاء Pinecone", t1["content_gap_rate"])
r0 = t1["content_gap_rate"]
for _ in range(5): chat_mode("timeout", NOISE)
check(dash("/dash/topics")["content_gap_rate"] == r0, "9: إضافة 5 أخطاء Pinecone تاني مبتغيّرش نسبة فجوة المحتوى")

# ═══════════════════════ 10) El Dashboard ═══════════════════════
section("10) El Dashboard: كل رقم مطابق للمصدر الحقيقي")
T_WEAK, T_STRONG, T_FEW = "موضوع_ضعيف_اختبار", "موضوع_قوي_اختبار", "موضوع_قليل_اختبار"
def answer_topic(topic, correct, times):
    for _ in range(times):
        q = client.post("/exam", json={"topic": topic, "session_id": SID_A}, headers=auth(TOK_A)).json()
        ci = q["mcq"][0]["correct_index"]
        client.post("/quiz/answer", json={"quiz_id": q["quiz_id"], "question_index": 0, "chosen_index": ci if correct else (ci + 1) % 4}, headers=auth(TOK_A))
answer_topic(T_WEAK, False, 3); answer_topic(T_STRONG, True, 3); answer_topic(T_FEW, False, 2)
appmod.db_execute("INSERT INTO feedback(session_id,question,answer,rating,ts) VALUES(?,?,?,?,?)", (SID_A, "q", "a", "weird-legacy-value", datetime.now().isoformat()))
client.post("/feedback", json={"session_id": SID_A, "question": "q", "answer": "a", "rating": "bad"}, headers=auth(TOK_A))
s = dash("/dash/summary"); t = dash("/dash/topics")
check(s["messages"] == count("SELECT COUNT(*) FROM messages") and s["questions"] == count("SELECT COUNT(*) FROM messages WHERE role='user'") and s["questions"] < s["messages"],
      "10: إجمالي الرسايل (أسئلة+ردود) والأسئلة (role=user) منفصلين ومطابقين لجدول messages")
check(s["sessions"] == count("SELECT COUNT(*) FROM sessions"), "10: عدد الجلسات من جدول sessions")
check(s["users_total"] == count("SELECT COUNT(*) FROM users") and s["students_total"] == count("SELECT COUNT(*) FROM users WHERE role='student'"), "10: المستخدمين من جدول users")
good, bad = count("SELECT COUNT(*) FROM feedback WHERE rating='good'"), count("SELECT COUNT(*) FROM feedback WHERE rating='bad'")
total_rows = count("SELECT COUNT(*) FROM feedback")
check(s["feedback_good"] == good and s["feedback_bad"] == bad and s["feedback_total"] == good + bad and total_rows > good + bad,
      "10: good/bad بيتعدّوا صراحةً (مش bad = total − good) — قيمة تاريخية غريبة مبتتحسبش bad", f"{s['feedback_good']}/{s['feedback_bad']}/{s['feedback_total']} rows={total_rows}")
check(s["satisfaction"] == f"{int(good / (good + bad) * 100)}%", "10: نسبة الرضا = good ÷ (good+bad)")
check(s["complaints_total"] == count("SELECT COUNT(*) FROM complaints") and s["complaints_open"] == count("SELECT COUNT(*) FROM complaints WHERE status='مفتوحة'"), "10: الشكاوى من جدول complaints")
first_day = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
check(len(s["questions_by_day"]) == 14 and sum(x["count"] for x in s["questions_by_day"]) == count("SELECT COUNT(*) FROM messages WHERE role='user' AND ts >= ?", (first_day,))
      and "messages_by_day" not in s, "10: الاتجاه اليومي = أسئلة الطلاب (رسائل المستخدم بس) آخر 14 يوم، ومطابق لقاعدة البيانات")
ua = next(u for u in s["users"] if u["username"] == "alice")
check(ua["messages"] == count("SELECT COUNT(*) FROM messages WHERE role='user' AND user_id=?", (UID_A,)), "10: عدد أسئلة كل مستخدم = رسايل role=user الفعلية")
check(t["quiz_summary"]["total_attempts"] == count("SELECT COUNT(*) FROM quiz_answers"), "10: إجمالي محاولات الاختبار من quiz_answers (المحسوبة في السيرفر)")
c_ok = count("SELECT SUM(is_correct) FROM quiz_answers"); c_all = count("SELECT COUNT(*) FROM quiz_answers")
check(t["quiz_summary"]["overall_accuracy"] == f"{round(100 * c_ok / c_all)}%", "10: الدقة الكلية مطابقة للحساب من الجدول")
weak = {w["topic"]: w for w in t["weak_topics"]}
check(T_WEAK in weak and weak[T_WEAK]["accuracy"] == 0 and T_STRONG in weak and weak[T_STRONG]["accuracy"] == 100 and T_FEW not in weak,
      "10: أضعف الموضوعات: بيظهر بعد 3 محاولات على الأقل (2 مبيظهرش)", str(list(weak)))
check(t["weak_topics"][0]["accuracy"] <= t["weak_topics"][-1]["accuracy"], "10: أضعف الموضوعات مرتّبة من الأسوأ")
top_wrong = t["hardest_questions"][0]
check(top_wrong["example"] == EXAM["mcq"][0]["question"] and top_wrong["count"] == count("SELECT COUNT(*) FROM quiz_answers WHERE is_correct=0 AND question=?", (EXAM["mcq"][0]["question"],)),
      "10: الأسئلة الأكثر غلطًا من نتايج الاختبار الفعلية", str(top_wrong))
fu = dict(db_all("SELECT event, COUNT(*) FROM analytics WHERE event IN ('chat','exam','grade','pdf','complaint') GROUP BY event"))
check(t["feature_usage"] == fu, "10: استخدام الميزات مطابق لأحداث analytics", f"{t['feature_usage']} vs {fu}")
appmod.db_execute("INSERT INTO messages(session_id,role,content,ts,user_id) VALUES('span-a','user','a','2026-01-01T10:00:00',NULL),('span-a','user','b','2026-01-01T10:30:00',NULL)")
spans = [(datetime.fromisoformat(mx) - datetime.fromisoformat(mn)).total_seconds() / 60 for mn, mx in db_all("SELECT MIN(ts), MAX(ts) FROM messages GROUP BY session_id")]
spans = [x for x in spans if x > 0]
t = dash("/dash/topics")
check(t["avg_session_span_minutes"] == round(sum(spans) / len(spans), 1) and "avg_session_minutes" not in t, "10: مدة الجلسة اتسمّت 'امتداد الجلسة' (أول ← آخر رسالة) والقيمة مطابقة للحساب")
q_ = dash("/dash/questions")
check(q_["total_questions"] == count("SELECT COUNT(*) FROM messages WHERE role='user'"), "10: /dash/questions مطابق لعدد أسئلة المستخدمين")
cs = dash("/dash/complaints")["complaints"]; fb = dash("/dash/feedback")["feedback"]
check(any(c["user"] == "alice" for c in cs) and any(f["user"] == "alice" for f in fb), "10: الشكاوى والتقييمات بتنسب للمستخدم الصح (عن طريق الجلسة)")
before = dash("/dash/summary")["questions"]; client.post("/chat", json={"message": "سؤال جديد", "session_id": new_sid()})
check(dash("/dash/summary")["questions"] == before + 1, "10: الداشبورد بيعكس الأحداث الجديدة فورًا من غير إعادة بناء")
wd = make_workdir()
r = run_py(wd, """
S = lambda p: client.post(p, json={'secret': %r}).json()
print('EMPTY=' + json.dumps({'s': S('/dash/summary'), 't': S('/dash/topics'), 'q': S('/dash/questions'), 'c': S('/dash/complaints'), 'f': S('/dash/feedback')}, ensure_ascii=False))
""" % SECRET)
emp = json.loads(next(l for l in r.stdout.splitlines() if l.startswith("EMPTY="))[6:])
check(emp["s"]["messages"] == 0 and emp["s"]["sessions"] == 0 and emp["s"]["satisfaction"] == "—" and emp["s"]["feedback_total"] == 0 and emp["s"]["users"] == []
      and emp["t"]["content_gap_rate"] == "—" and emp["t"]["quiz_summary"]["overall_accuracy"] == "—" and emp["t"]["weak_topics"] == [] and emp["t"]["avg_session_span_minutes"] == 0
      and emp["q"]["total_questions"] == 0 and emp["c"]["complaints"] == [] and emp["f"]["feedback"] == [],
      "10: قاعدة فاضية → أصفار وحالات فاضية صريحة (مفيش أرقام مختلقة)", r.stderr[-300:])

# ═══════════════════════ 11) التحقق من المدخلات ═══════════════════════
section("11) التحقق من المدخلات (الباك اند هو المرجع)")
def post(path, body, tok=None): return client.post(path, json=body, headers=auth(tok) if tok else {}).status_code
check(post("/chat", {"message": "س" * 4001}) == 422 and post("/chat", {"message": "س" * 4000, "session_id": new_sid()}) == 200, "11: رسالة الشات: 4000 مقبولة و 4001 مرفوضة")
check(post("/chat", {"message": "x", "session_id": "has space"}) == 422 and post("/chat", {"message": "x", "session_id": "a" * 129}) == 422, "11: session_id بصيغة غير صالحة أو أطول من 128 → 422")
check(all(post("/exam", {"topic": "t", "num_mcq": n}) == 422 for n in (0, 21, 1000, -3)), "11: num_mcq خارج 1–20 → 422")
check(all(post("/exam", {"topic": "t", "num_essay": n}) == 422 for n in (-1, 11, 500)), "11: num_essay خارج 0–10 → 422")
check(post("/exam", {"topic": "t", "num_mcq": 1, "num_essay": 0, "session_id": new_sid()}) == 200 and post("/exam", {"topic": "t", "num_mcq": 20, "num_essay": 10, "session_id": new_sid()}) == 200, "11: حدود الامتحان المقبولة (1/0 و 20/10)")
check(post("/exam", {"topic": "م" * 201}) == 422, "11: موضوع أطول من 200 → 422")
check(post("/grade", {"answer": "ا" * 8001}) == 422 and post("/grade", {"answer": "ا" * 8000, "session_id": new_sid()}) == 200, "11: إجابة التقييم: 8000 مقبولة و 8001 مرفوضة")
check(post("/complaint", {"text": "ش" * 3001}) == 422, "11: شكوى أطول من 3000 → 422")
fbase = {"session_id": SID_A, "question": "q", "answer": "a"}
check(post("/feedback", {**fbase, "rating": "meh"}, TOK_A) == 422 and post("/feedback", fbase, TOK_A) == 422 and post("/feedback", {**fbase, "rating": "good"}, TOK_A) == 200
      and post("/feedback", {**fbase, "rating": "bad"}, TOK_A) == 200, "11: rating مقيّد بـ good/bad بس (أي قيمة تانية → 422، مش بتتحسب bad)")
check(post("/feedback", {**fbase, "question": "س" * 2001, "rating": "good"}, TOK_A) == 422 and post("/feedback", {**fbase, "answer": "ج" * 20001, "rating": "good"}, TOK_A) == 422, "11: حدود نص التقييم")
check(post("/dash/summary", {"secret": "x" * 257}) == 422, "11: سر الأدمن أطول من 256 → 422")

# ═══════════════════════ 12) الواجهة والتنضيف ═══════════════════════
section("12) الواجهة، الأدمن الواحد، والريبو")
h = client.get("/").text; dsh = client.get("/dashboard").text
check(all(x not in h for x in ("panel-admin", "adminLogin", "loadAdminData", "الإدارة</div>", "profdash", "grade-grid", "LLaMA")), "12: تطبيق الطلبة نضيف من لوحة الأدمن القديمة ومن أي أثر للوحة الأستاذ")
check("/static/auth.js" in h and "/static/auth.css" in h and h.count("authHeaders(") >= 3, "12: الواجهة بتبعت هوية المستخدم في history/feedback/complaint")
check("El Dashboard" in dsh and "questions_by_day" in dsh and "avg_session_span_minutes" in dsh, "12: El Dashboard: الاسم والمفاتيح الجديدة مستخدمة")
check(not re.search(r"<script[^>]+src=", dsh) and not any(x in dsh for x in ("cdnjs", "chart.min.js", "new Chart(", "window.Chart")),
      "12: El Dashboard من غير أي اعتماد خارجي (مفيش Chart.js ولا أي script من CDN) — الرسوم SVG مدمجة")
check("\\uFEFF" in dsh and "s = \"'\" + s" in dsh, "12: تصدير CSV: BOM + حماية formula-injection موجودة")
check(client.get("/health").json()["status"] == "ok" and client.get("/static/sw.js").status_code == 200, "12: /health والملفات الثابتة شغّالة")
if (ROOT / ".git").exists() and shutil.which("git"):
    tracked = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True).stdout.splitlines()
    bad = [f for f in tracked if re.search(r"(^|/)(app\.log|app_data\.db|uvicorn\.[a-z]+\.log|__pycache__/.*|.*\.pyc)$", f)]
    check(not bad, "12: الريبو مبيتتبّعش أي ملفات runtime (DB / logs / pycache)", str(bad))
gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
check(all(p in gi for p in ("__pycache__", "*.py[cod]", "app.log", "app_data.db", "uvicorn.*.log")), "12: .gitignore بيغطّي ملفات runtime")

# ═══════════════════════ 13) مفيش أسرار في اللوج ولا الداتابيز ═══════════════════════
section("13) الأسرار")
log_text, dump = "\n".join(CAP.records), db_dump()
seen = [t for t in ISSUED_TOKENS if t] + ["password1", SECRET, "wrong-secret-value"] + ROTATION_KEYS
leaks_log = [x for x in seen if x in log_text]
leaks_db = [x for x in seen if x in dump]
check(len(CAP.records) > 50 and not leaks_log, "13: اللوج (فيه أخطاء دخول/أدمن/مفاتيح Groq اتسبّبت فعلًا) مفيهوش أي توكن/كلمة سر/سر أدمن/مفتاح", str(leaks_log))
check(not leaks_db, "13: الداتابيز مفيهاش توكن خام ولا كلمة سر ولا سر أدمن ولا مفتاح Groq", str(leaks_db))


# ═══════════════════════ 14) سكريبت الرفع لـ Pinecone ═══════════════════════
section("14) scripts/upload_to_pinecone.py (بمكتبات pinecone/genai مزيّفة — مفيش اتصال حقيقي)")
INGEST = ROOT / "scripts" / "upload_to_pinecone.py"
ing_dir = Path(tempfile.mkdtemp(prefix="edtech-ingest-")); fake_mods = ing_dir / "fake_mods"
(fake_mods / "pinecone").mkdir(parents=True); (fake_mods / "google" / "genai").mkdir(parents=True)
(fake_mods / "pinecone" / "__init__.py").write_text("""
import json, os
def _log(e):
    with open(os.environ['FAKE_LOG'], 'a') as f: f.write(json.dumps(e) + '\\n')
class _NS:
    def __init__(self, n): self.vector_count = n
class _Stats:
    def __init__(self):
        self.dimension = int(os.environ.get('FAKE_INDEX_DIM', '8'))
        ex = int(os.environ.get('FAKE_EXISTING', '0'))
        self.namespaces = {'': _NS(ex)} if ex else {}
class _Index:
    def __init__(self, name): self.name = name
    def describe_index_stats(self): return _Stats()
    def upsert(self, vectors, namespace):
        _log({'op': 'upsert', 'index': self.name, 'namespace': namespace, 'ids': [v['id'] for v in vectors],
              'meta': [v['metadata'] for v in vectors]})
class Pinecone:
    def __init__(self, api_key): pass
    def Index(self, name): return _Index(name)
""")
(fake_mods / "google" / "__init__.py").write_text("")
(fake_mods / "google" / "genai" / "__init__.py").write_text("""
import json, os
class _E:
    def __init__(self, v): self.values = v
class _R:
    def __init__(self, v): self.embeddings = [_E(v)]
class _Models:
    def embed_content(self, model, contents):
        with open(os.environ['FAKE_LOG'], 'a') as f: f.write(json.dumps({'op': 'embed', 'model': model}) + '\\n')
        return _R([0.1] * int(os.environ.get('FAKE_EMBED_DIM', '8')))
class Client:
    def __init__(self, api_key): self.models = _Models()
""")
pdf_dir = ing_dir / "pdfs"; pdf_dir.mkdir()
(pdf_dir / "منهج.pdf".replace("منهج", "lesson one")).write_bytes(make_pdf([("ADDIE analysis design development implementation evaluation " * 40)]))
(pdf_dir / "scanned.pdf").write_bytes(make_pdf(["   "]))
(pdf_dir / "broken.pdf").write_bytes(b"not a pdf")
def ingest(args, env_extra=None, stdin=""):
    log = ing_dir / f"log-{uuid.uuid4().hex}.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PINECONE", "GOOGLE", "GEMINI", "APP_GRADE"))}
    env.update({"FAKE_LOG": str(log), "PINECONE_API_KEY": "pk-test", "GOOGLE_API_KEY": "gk-test"})
    env["PYTHONPATH"] = os.pathsep.join([str(fake_mods)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.update(env_extra or {})
    r = subprocess.run([sys.executable, str(INGEST)] + args, capture_output=True, text=True, input=stdin, env=env, timeout=120)
    ops = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return r, ops
expected_ids = None
r, ops = ingest(["--folder", str(pdf_dir), "--dry-run"])
check(r.returncode == 0 and "dry-run" in r.stdout and ops == [] and "scanned.pdf" in r.stdout and "broken.pdf" in r.stdout,
      "14: --dry-run: بيعرض الأجزاء ويتخطّى الملفات الفاضية/التالفة ومبيستدعيش أي API ولا بيرفع", r.stdout[-300:] + r.stderr[-300:])
r, ops = ingest(["--folder", str(pdf_dir)])
ups = [o for o in ops if o["op"] == "upsert"]; ids1 = [i for o in ups for i in o["ids"]]
check(r.returncode == 0 and ups and all(o["index"] == "edtech-db" and o["namespace"] == "" for o in ups), "14: الرفع الافتراضي على نفس index/namespace بتوع التطبيق (edtech-db / الافتراضي الفاضي)", r.stderr[-300:] + str(ups[:1]))
check(ids1 and all(i.startswith("lesson_one_pdf_") for i in ids1) and len(ids1) == len(set(ids1)), "14: IDs ثابتة (اسم الملف + رقم الجزء) وفريدة", str(ids1[:3]))
check({o["model"] for o in ops if o["op"] == "embed"} == {"models/gemini-embedding-001"}, "14: نفس موديل الـ embedding بتاع التطبيق")
check(all(m["grade"] == 3 for o in ups for m in o["meta"]), "14: metadata.grade = APP_GRADE (الافتراضي 3)")
r2, ops2 = ingest(["--folder", str(pdf_dir)])
check([i for o in ops2 if o["op"] == "upsert" for i in o["ids"]] == ids1, "14: إعادة التشغيل بتنتج نفس الـ IDs بالظبط (upsert آمن، من غير تكرار)")
r, ops = ingest(["--folder", str(pdf_dir)], {"FAKE_EMBED_DIM": "4"})
check(r.returncode != 0 and not any(o["op"] == "upsert" for o in ops) and "بُعد" in r.stdout + r.stderr, "14: بُعد embedding مش مطابق لبُعد الـ index → إلغاء من غير أي كتابة")
r, ops = ingest(["--folder", str(pdf_dir)], {"FAKE_EXISTING": "91"}, stdin="n\n")
check(r.returncode != 0 and not any(o["op"] == "upsert" for o in ops), "14: الـ namespace فيه محتوى + رد 'n' → إلغاء من غير كتابة")
r, ops = ingest(["--folder", str(pdf_dir)], {"FAKE_EXISTING": "91"}, stdin="y\n")
check(r.returncode == 0 and any(o["op"] == "upsert" for o in ops), "14: الـ namespace فيه محتوى + رد 'y' → بيكمّل")
r, ops = ingest(["--folder", str(pdf_dir), "--yes"], {"FAKE_EXISTING": "91"})
check(r.returncode == 0 and any(o["op"] == "upsert" for o in ops), "14: --yes بيتخطّى التأكيد")
r, ops = ingest(["--folder", str(pdf_dir), "--index-name", "other-index", "--namespace", "custom-ns"])
check(all(o["index"] == "other-index" and o["namespace"] == "custom-ns" for o in ops if o["op"] == "upsert") and any(o["op"] == "upsert" for o in ops), "14: --index-name و --namespace بيتحترموا صراحةً")
r, ops = ingest(["--folder", str(pdf_dir), "--grade", "1"])
check(r.returncode == 2 and ops == [], "14: --grade اتشال: مفيش توجيه لفرق تانية (argparse بيرفضه)")
check("grade_namespace" not in INGEST.read_text(encoding="utf-8") and "grade_{" not in INGEST.read_text(encoding="utf-8"), "14: مفيش أي منطق namespaces متعددة الفرق في السكريبت")
shutil.rmtree(ing_dir, ignore_errors=True)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    print("FAILED CHECKS:")
    for f in FAILED: print("  -", f)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
