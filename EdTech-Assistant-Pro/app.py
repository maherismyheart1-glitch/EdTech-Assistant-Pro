"""
المعيد الذكي — كلية التربية النوعية، جامعة الزقازيق
قسم تكنولوجيا التعليم والحاسب الآلي
Engineered by: Abdelrahman Essam
"""
import os, json, uuid, logging, sqlite3, tempfile, re
import random, secrets, hashlib, time, threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Literal

import bcrypt
import fitz
from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from groq import Groq

# اللوج على stdout/stderr افتراضيًا (المناسب لـ Docker / Hugging Face) — مفيش app.log
# بيتعمل جوه الريبو. لو عايز ملف، حدّد LOG_FILE صراحةً. مفيش أي مكان في الكود
# بيسجّل كلمات سر أو توكنات أو مفاتيح API أو سر الأدمن.
_log_handlers = [logging.StreamHandler()]
_LOG_FILE = os.environ.get("LOG_FILE", "").strip()
if _LOG_FILE:
    _log_handlers.append(logging.FileHandler(_LOG_FILE, encoding="utf-8"))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s", handlers=_log_handlers)
log = logging.getLogger(__name__)

# ── Keys & Load Balancing ─────────────────────────
# قائمة المفاتيح الـ 5 لتوزيع الأحمال واستيعاب ضغط الكلية
GROQ_KEYS = [
    os.environ.get("GROQ_API_KEY", ""),
    os.environ.get("GROQ_API_KEY_2", ""),
    os.environ.get("GROQ_API_KEY_3", ""),
    os.environ.get("GROQ_API_KEY_4", ""),
    os.environ.get("GROQ_API_KEY_5", "")
]
# تنظيف القائمة من أي مفتاح فارغ
GROQ_KEYS = [k for k in GROQ_KEYS if k]

# ── تدوير مفاتيح Groq عند الفشل ──────────────────────────────────────────
# استيراد آمن: لو أسماء الاستثناءات دي اختلفت شوية بين إصدارات الـ SDK،
# التطبيق لسه يبتدي عادي (بيرجع لـ Exception العادي، وبرضو بيصنّف صح
# بالاعتماد على status_code اللي موجود في أي استثناء HTTP من الـ SDK ده).
try:
    from groq import RateLimitError as _GroqRateLimitError
except ImportError:
    class _GroqRateLimitError(Exception): pass
try:
    from groq import APIConnectionError as _GroqConnectionError
except ImportError:
    class _GroqConnectionError(Exception): pass

def _classify_groq_error(e):
    """بيصنّف فشل طلب Groq بالاعتماد أساسًا على status_code (موجود في كل
    استثناءات الـ SDK دي بغض النظر عن اسم الكلاس بالظبط — أوثق من الاعتماد
    على اسم الكلاس وحده، اللي ممكن يختلف شوية بين إصدارات):
      'auth_or_transient' — المفتاح ده عطلان أو مشكلة مؤقتة (401/403/404/5xx/اتصال)
                             → منطقي نجرّب مفتاح تاني من المُهيّأين.
      'rate_limit'        — 429 → نحترم حد المزوّد، مش سبب نلف على مفاتيح
                             تانية كطريقة نتحايل بيها على الحد (ممنوع صراحةً).
      'other'              — مشكلة في الطلب نفسه (400 مثلاً) — مفتاح تاني
                             مش هيصلّحها، فمفيش داعي نضيّع محاولات عليه.
    """
    if isinstance(e, _GroqRateLimitError):
        return "rate_limit"
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    if status == 429:
        return "rate_limit"
    if status is not None and (status in (401, 403, 404) or status >= 500):
        return "auth_or_transient"
    if isinstance(e, _GroqConnectionError):
        return "auth_or_transient"
    if status is not None:
        return "other"
    return "auth_or_transient"  # استثناء بلا status_code (مش من الـ SDK نفسه) — الأأمن نجرّب مفتاح تاني

def _call_groq(fn):
    """بينفّذ fn(client) على المفاتيح المتاحة بالترتيب لحد ما وحدة تنجح.
    كل مفتاح بيتجرّب مرة واحدة بالظبط لكل طلب (مفيش تكرار لنفس المفتاح)،
    وأقصى عدد محاولات = عدد المفاتيح المُهيّأة فعليًا — مفيش تكرار لا نهائي
    ممكن يحصل بنيويًا. خطأ 429 (rate limit) بيتعامل معاه كإشارة نحترمها فورًا،
    مش كسبب نلف على مفاتيح تانية (ده بالظبط استخدام المفاتيح المتعددة كطريقة
    تحايل على حد المزوّد، وده ممنوع)."""
    if not GROQ_KEYS:
        raise RuntimeError("لا يوجد مفاتيح Groq صالحة في النظام")
    order = random.sample(GROQ_KEYS, k=len(GROQ_KEYS))
    last_err = None
    for key in order:
        try:
            return fn(Groq(api_key=key))
        except Exception as e:
            kind = _classify_groq_error(e)
            if kind in ("rate_limit", "other"):
                raise
            last_err = e
            log.warning(f"Groq: مفتاح فشل ({kind})، تجربة مفتاح تاني")  # الاسم مش اتسجّل، بس نوع الفشل
            continue
    raise last_err

GOOGLE_KEY   = os.environ.get("GOOGLE_API_KEY","") or os.environ.get("GEMINI_API_KEY","")
PINECONE_KEY = os.environ.get("PINECONE_API_KEY","")
PINECONE_IDX = os.environ.get("PINECONE_INDEX","edtech-db")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
# القيمة الافتراضية القديمة ("admin_2025") كانت مكتوبة في الكود ومعروفة لأي حد
# شاف المصدر — استخدامها في الإنتاج معناه إن أي حد يقدر يدخل /admin ولوحة
# التحليلات. التطبيق دلوقتي بيرفض يشتغل من غيره أو بيه، بدل ما يشتغل بصمت
# بسر معروف للعامة.
if ADMIN_SECRET.strip() in ("", "admin_2025"):
    raise RuntimeError(
        "ADMIN_SECRET غير مضبوط أو لسه بالقيمة الافتراضية القديمة غير الآمنة "
        "('admin_2025'). حدّد قيمة قوية وسرّية في متغيرات البيئة قبل التشغيل — "
        "مثلاً بتنفيذ: python -c 'import secrets; print(secrets.token_urlsafe(24))'"
    )

# التطبيق دلوقتي للفرقة التالتة بس — كل حساب جديد بيتسجّل بالفرقة دي تلقائيًا.
APP_GRADE = int(os.environ.get("APP_GRADE", "3"))  # نشر الفرقة التالتة؛ إعداد مش ثابت في الكود، ومش بيتعرض للطلبة

# ── Groq LLM Logic ────────────────────────────────
SYSTEM_PROMPT = """أنت "المعيد الذكي" — مساعد أكاديمي ذكي لطلاب قسم تكنولوجيا التعليم والحاسب الآلي،
كلية التربية النوعية، جامعة الزقازيق.
- أجب بالعربية الفصحى البسيطة دائماً
- اعتمد على محتوى المنهج المقدَّم أولاً
- كن دقيقاً، واضحاً، وأكاديمياً في ردودك
- نسّق إجاباتك بصيغة Markdown ليسهل عرضها: **ضع المصطلحات والتعريفات المهمة بين نجمتين**
  هكذا، استخدم القوائم النقطية أو المرقّمة عند التعداد، وعناوين فرعية (## أو ###) عند
  تنظيم إجابة طويلة تحتوي على عدة محاور"""

def llm(msgs, max_tokens=1500, temp=0.5, json_mode=False):
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
        # reasoning_format مش parameter معروف في نسخة SDK دي (groq==0.11.0) —
        # لازم يتبعت عن طريق extra_body، مش كـ kwarg مباشر، وإلا بيرمي TypeError.
        kwargs["extra_body"] = {"reasoning_format": "hidden"}
    def _do(client):
        r = client.chat.completions.create(
            model="openai/gpt-oss-20b", messages=msgs,
            max_tokens=max_tokens, temperature=temp, **kwargs)
        return r.choices[0].message.content.strip()
    return _call_groq(_do)

_LLM_ERROR_TEXT = "⚠️ حدث خطأ في الاتصال بالذكاء الاصطناعي، حاول مرة أخرى."

def _llm_error(e):
    """بيسجّل نوع الخطأ بس (مش نصه) ويرجّع رسالة عامة للعميل — نص استثناء المزوّد
    ممكن يكشف تفاصيل داخلية، ومفيش داعي العميل يشوفها."""
    log.error(f"LLM error: {type(e).__name__}")
    return _LLM_ERROR_TEXT

def llm_stream(msgs, max_tokens=1500):
    # التدوير بين المفاتيح بيحصل هنا بس لحظة إنشاء الستريم (قبل أي token يتبعت
    # للعميل) — لو حصل فشل بعد ما البث ابتدا فعلًا (بعض الـ tokens اتبعتت)،
    # معناها اتصال اتقطع، مش مفتاح عطلان؛ إعادة المحاولة بمفتاح تاني في اللحظة
    # دي كانت هتكرّر/تلخبط الرد اللي العميل شافه جزء منه بالفعل.
    def _do(client):
        return client.chat.completions.create(
            model="openai/gpt-oss-20b", messages=msgs,
            max_tokens=max_tokens, temperature=0.5, stream=True)
    stream = _call_groq(_do)  # لو فشل، الاستثناء بيطلع لـ stream_llm_payload (حالة error صريحة)
    for chunk in stream:
        t = chunk.choices[0].delta.content
        if t: yield t

def sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def stream_events(msgs, sid, event, max_tokens=1500, log_data=None, save_chat=False, user_id=None):
    """مولّد أحداث SSE مع حفظ/تسجيل مضبوط: رسالة مساعد واحدة وحدث تحليلات واحد لكل
    عملية، وحالتها صريحة (ok / error / cancelled). رسالة الخطأ اللي بتظهر للمستخدم
    مبتتحفظش كأنها رد حقيقي، والبث اللي اتقطع قبل ما يخلص مبيتسجّلش كناجح.
    مفيش transaction مفتوحة أثناء انتظار Groq — كل كتابة في DB عملية قصيرة لوحدها."""
    buf = []

    def _finish(status, err_type):
        answer = "".join(buf)
        try:
            if status == "ok" and save_chat and answer:
                db_msg(sid, "assistant", answer, user_id)
            payload = dict(log_data or {})
            payload.update({"session": sid, "stream": True, "status": status, "chars": len(answer)})
            if err_type:
                payload["error_type"] = err_type
            db_log(event, payload)
        except Exception as e:
            log.error(f"Stream {event}: فشل حفظ النتيجة ({type(e).__name__})")

    status, err_type = "cancelled", None  # cancelled لو العميل قفل الاتصال قبل النهاية
    try:
        try:
            for t in llm_stream(msgs, max_tokens=max_tokens):
                buf.append(t)
                yield sse({"token": t, "session_id": sid})
            status = "ok"
        except GeneratorExit:
            raise
        except Exception as e:
            status, err_type = "error", type(e).__name__
            log.error(f"Stream {event}: {err_type}")  # نوع الخطأ بس، مش نصه
            yield sse({"token": "⚠️ حدث خطأ في الاتصال بالذكاء الاصطناعي، حاول مرة أخرى.",
                       "session_id": sid, "error": True})
    finally:
        _finish(status, err_type)
    yield sse({"done": True, "session_id": sid})

def stream_llm_payload(msgs, sid, event, max_tokens=1500, log_data=None, save_chat=False, user_id=None):
    return StreamingResponse(
        stream_events(msgs, sid, event, max_tokens, log_data, save_chat, user_id),
        media_type="text/event-stream")

# ── RAG (Pinecone + Gemini) ───────────────────────
_pine = None
# مصدر بيانات الاحتياط (fallback) الحقيقي هو data/docs (من غير امتداد .txt) —
# مش data/docs.txt زي ما كان مكتوب قبل كده، فالاحتياط كان بيرجع فاضي دايمًا مهما
# كان السؤال. الاسم قابل للتغيير من متغيرات البيئة لو الملف اتنقل/اتسمّى تاني.
FALLBACK_DOCS_PATH = os.environ.get("FALLBACK_DOCS_PATH", "data/docs")

def _fallback_rag(q):
    f = Path(FALLBACK_DOCS_PATH)
    if not f.exists(): return ""
    words = q.lower().split()
    paras = [p for p in f.read_text(encoding="utf-8").split("\n\n")
             if any(w in p.lower() for w in words)]
    return "\n\n".join(paras[:5])

# النشر ده للفرقة التالتة بس (APP_GRADE). المحتوى المفهرس كله بتاعها في الـ namespace
# المُهيّأ (PINECONE_NAMESPACE — الافتراضي فاضي = الـ namespace الافتراضي اللي فيه
# المنهج الحالي). الثابت: النشر ده مبيسترجعش غير من قاعدة المعرفة المُهيّأة دي.
PINECONE_NAMESPACE = os.environ.get("PINECONE_NAMESPACE", "")
from collections import namedtuple
RagResult = namedtuple("RagResult", "text status vector_error")
RAG_READY_GRADES = {APP_GRADE}

def rag_available_for(grade):
    """زائر (None) أو طالب على فرقة النشر → متاح. أي فرقة تانية مش مدعومة."""
    return grade is None or grade == APP_GRADE

_embed = None
if PINECONE_KEY and GOOGLE_KEY:
    try:
        from pinecone import Pinecone
        from google import genai as _genai
        _pc   = Pinecone(api_key=PINECONE_KEY)
        _pine = _pc.Index(PINECONE_IDX)
        _gcli = _genai.Client(api_key=GOOGLE_KEY)
        def _embed(t):
            return _gcli.models.embed_content(
                model="models/gemini-embedding-001", contents=t).embeddings[0].values
        log.info(f"✅ Pinecone ({PINECONE_IDX}) جاهز")
    except Exception as e:
        log.warning(f"Pinecone error: {type(e).__name__}"); _pine = None

def rag_lookup(q, top_k=5, thr=0.55, grade=None):
    """بيرجّع RagResult(text, status, vector_error) — الحالة بتفرّق بين:
      vector      — Pinecone رجّع تطابقات مقبولة
      fallback    — نص من ملف الاحتياط (data/docs) مفيد
      empty       — الاسترجاع اشتغل فعلًا ومفيش سياق مناسب (ده الـ "content gap" الحقيقي)
      error       — Pinecone فشل (تايم أوت/خطأ) ومفيش احتياط مفيد — مشكلة بنية تحتية، مش نقص منهج
      unavailable — الفرقة مش مدعومة في النشر ده (مفيش استرجاع أصلًا)"""
    if not rag_available_for(grade):
        return RagResult("", "unavailable", False)
    vector_error = False
    if _pine is not None and _embed is not None:
        try:
            hits = _pine.query(vector=_embed(q), top_k=top_k, include_metadata=True,
                                namespace=PINECONE_NAMESPACE)
            good = [m for m in hits.matches if m.score >= thr]
            if good:
                return RagResult("\n\n".join(f"[{m.score:.0%}] {m.metadata.get('text','')}" for m in good),
                                 "vector", False)
        except Exception as e:
            vector_error = True
            log.warning(f"Pinecone: {type(e).__name__}")
    fb = _fallback_rag(q)
    if fb:
        return RagResult(fb, "fallback", vector_error)
    return RagResult("", "error" if vector_error else "empty", vector_error)

def rag_search(q, top_k=5, thr=0.55, grade=None):
    return rag_lookup(q, top_k, thr, grade).text

if _pine is None:
    log.info(f"ℹ️ RAG fallback → {FALLBACK_DOCS_PATH}"
             f" ({'موجود' if Path(FALLBACK_DOCS_PATH).exists() else '⚠️ غير موجود'})")

# ── DB (SQLite) ───────────────────────────────────
# لو الـ Space فيه Persistent Storage مفعّل، Hugging Face بتعمل mount له على /data —
# نستخدمه تلقائي لو موجود عشان الحسابات والجلسات متتمسحش مع كل restart.
# لو مش موجود (تطوير محلي، أو لسه ما فعلتش التخزين الدائم)، يفضل يشتغل بالمسار المحلي زي الأول.
_DB_PATH = "app_data.db"
if os.path.isdir("/data"):
    try:
        _test = os.path.join("/data", ".write_test")
        with open(_test, "w") as f: f.write("ok")
        os.remove(_test)
        _DB_PATH = "/data/app_data.db"
        log.info("✅ Persistent storage /data متاح — الداتابيز هتتخزن فيه")
    except Exception as e:
        log.warning(f"⚠️ /data موجود بس مش قابل للكتابة ({e}) — رجعنا للمسار المحلي")

# ── اتصال SQLite منفصل لكل عملية (بدل اتصال عام واحد متشارك بين كل الـ threads) ──
# الاتصال العام القديم (check_same_thread=False) كان بيتشارك بين كل الطلبات
# المتزامنة، فكان ممكن transaction بتاع طلب يتقفل (commit/rollback) من غير
# قصد بواسطة طلب تاني شغال في نفس اللحظة على thread مختلف على نفس الاتصال —
# وده بالظبط سبب "cannot commit - no transaction is active". كل عملية دلوقتي
# بتاخد اتصالها الخاص بيها (get_db()) وtransaction خاص بيها من غير ما تتشارك
# مع حد، فمفيش تعارض ممكن يحصل من الأساس.
@contextmanager
def get_db(write=False):
    """write=True بيبدأ BEGIN IMMEDIATE: أي معاملة بتقرا وبعدين تكتب (زي "افحص
    ملكية الجلسة ثم أنشئها") لازم تاخد قفل الكتابة من الأول — وإلا في وضع WAL
    ممكن ترمي "database is locked" فورًا لو حد كتب بعد ما ابتدت (snapshot قديم)."""
    conn = sqlite3.connect(_DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")     # قراءات وكتابات متزامنة من غير ما تتقفل بعض
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")   # أي تعارض نادر يستنى بدل ما يرمي خطأ فورًا
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

def db_execute(sql, params=()):
    """عملية كتابة واحدة (INSERT/UPDATE/DELETE) في transaction خاص بيها — بترجّع lastrowid."""
    with get_db() as db:
        return db.execute(sql, params).lastrowid

def db_query_one(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchone()

def db_query_all(sql, params=()):
    with get_db() as db:
        return db.execute(sql, params).fetchall()

# ── مخطط قاعدة البيانات + المايجريشن (مُرقّم بـ PRAGMA user_version) ─────────────
# الترتيب ثابت ومحدّد (مش CREATE INDEX أعمى + ALTER TABLE في try/except متفرّق):
#   1) فتح الاتصال وضبط الـ pragmas
#   2) إنشاء الجداول الأساسية (من غير أي فهرس)
#   3) فحص الجداول الموجودة فعليًا (PRAGMA table_info) وإضافة أي عمود ناقص
#   4) مايجريشن البيانات (التوكنات، صلاحيتها، ملكية الجلسات)
#   5) إنشاء الفهارس — بعد ما نضمن إن كل أعمدتها موجودة
#   6) التحقق من المخطط، 7) رفع user_version، 8) commit
# السبب: DB قديمة (زي اللي في الريبو) messages فيها من غير user_id — إنشاء
# idx_messages_user قبل إضافة العمود كان بيوقّع التطبيق عند الإقلاع
# (OperationalError: no such column: user_id).
SCHEMA_VERSION = 2

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

SESSION_LIFETIME_HOURS = int(os.environ.get("SESSION_LIFETIME_HOURS", "720"))  # 30 يوم افتراضيًا

# الجداول بالشكل النهائي — CREATE TABLE IF NOT EXISTS بس، من غير فهارس هنا.
# DB قديمة بتعدّي على نفس الجدول من غير الأعمدة الجديدة، وخطوة 3 بتكمّلها.
_BASE_TABLES = """
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, role TEXT, content TEXT, ts TEXT, user_id INTEGER);
CREATE TABLE IF NOT EXISTS complaints(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, category TEXT DEFAULT 'عام',
    priority TEXT DEFAULT 'متوسطة', text TEXT,
    status TEXT DEFAULT 'مفتوحة', ts TEXT);
CREATE TABLE IF NOT EXISTS feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, question TEXT, answer TEXT, rating TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS analytics(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT, data TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS quiz_answers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, topic TEXT, question TEXT,
    chosen_index INTEGER, correct_index INTEGER, is_correct INTEGER, ts TEXT);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    grade INTEGER,
    created_at TEXT);
CREATE TABLE IF NOT EXISTS auth_sessions(
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    title TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS quizzes(
    id TEXT PRIMARY KEY,
    session_id TEXT,
    user_id INTEGER,
    topic TEXT,
    questions_json TEXT,
    created_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS quiz_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quiz_id TEXT, question_index INTEGER,
    chosen_index INTEGER, is_correct INTEGER, ts TEXT,
    UNIQUE(quiz_id, question_index));
CREATE TABLE IF NOT EXISTS documents(
    session_id TEXT PRIMARY KEY,
    user_id INTEGER, filename TEXT, text TEXT,
    chars INTEGER, uploaded_at TEXT);
"""

# أعمدة اتضافت بعد النسخة الأولى من كل جدول — أي جدول قديم ناقصها بيتكمّل.
_REQUIRED_COLUMNS = {
    "messages":      [("user_id", "INTEGER")],
    "auth_sessions": [("expires_at", "TEXT")],
    "sessions":      [("title", "TEXT"), ("updated_at", "TEXT")],
    "documents":     [("filename", "TEXT"), ("chars", "INTEGER"), ("uploaded_at", "TEXT")],
}

# كل فهرس هنا بيتعمل بعد ما الأعمدة بتاعته تتضمن إنها موجودة (خطوة 5).
_INDEXES = [
    ("idx_messages_session",    "messages(session_id)"),
    ("idx_messages_user",       "messages(user_id)"),
    ("idx_messages_ts",         "messages(ts)"),
    ("idx_analytics_event",     "analytics(event)"),
    ("idx_analytics_ts",        "analytics(ts)"),
    ("idx_quiz_answers_topic",  "quiz_answers(topic)"),
    ("idx_auth_sessions_user",  "auth_sessions(user_id)"),
    ("idx_documents_uploaded",  "documents(uploaded_at)"),
    ("idx_quizzes_session",     "quizzes(session_id)"),
    ("idx_sessions_user",       "sessions(user_id)"),
]

def _table_columns(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}

def _ensure_columns(db):
    for table, cols in _REQUIRED_COLUMNS.items():
        existing = _table_columns(db, table)
        for name, decl in cols:
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

def _migrate_v1_auth(db):
    """v1: هاش التوكنات القديمة (كانت نص صريح PRIMARY KEY) + صلاحية للتوكنات
    اللي من غير expires_at. التوكن الخام لسه في متصفح صاحبه، وأي طلب جاي
    بيتعمله hash قبل المقارنة، فتسجيل الدخول الحالي بيفضل شغّال من غير ما
    المستخدم يلاحظ حاجة — ومفيش توكن خام بيفضل متخزّن بعد المايجريشن."""
    for (tok,) in db.execute("SELECT token FROM auth_sessions").fetchall():
        if len(tok) != 64:  # مش SHA-256 hex → توكن قديم بالنص الصريح
            db.execute("UPDATE auth_sessions SET token=? WHERE token=?", (hash_token(tok), tok))
    now_dt = datetime.now()
    for (tok, created) in db.execute(
            "SELECT token, created_at FROM auth_sessions WHERE expires_at IS NULL").fetchall():
        try:
            base = datetime.fromisoformat(created) if created else now_dt
        except ValueError:
            base = now_dt
        exp = (base + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
        db.execute("UPDATE auth_sessions SET expires_at=? WHERE token=?", (exp, tok))

def _migrate_v2_sessions(db):
    """v2: تعبئة جدول sessions من رسايل قديمة سبقت وجوده. MIN(user_id) بيتجاهل
    NULL: جلسة كل رسايلها من زائر → user_id=NULL (بتفضل مجهولة، مفيش ملكية
    بتتخترع)، وجلسة فيها رسايل يوزر مسجّل → بتتربط بمالكها الفعلي."""
    now_iso = datetime.now().isoformat()
    for (sid_row, uid_row, first_ts, last_ts) in db.execute(
            "SELECT session_id, MIN(user_id), MIN(ts), MAX(ts) FROM messages "
            "WHERE session_id IS NOT NULL GROUP BY session_id").fetchall():
        db.execute("INSERT OR IGNORE INTO sessions(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                   (sid_row, uid_row, first_ts or now_iso, last_ts or now_iso))

_MIGRATIONS = [(1, _migrate_v1_auth), (2, _migrate_v2_sessions)]

def _validate_schema(db):
    for table, cols in _REQUIRED_COLUMNS.items():
        missing = {c for c, _ in cols} - _table_columns(db, table)
        if missing:
            raise RuntimeError(f"مخطط الداتابيز ناقص: {table} مفيهوش الأعمدة {sorted(missing)}")
    existing_idx = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    missing_idx = [n for n, _ in _INDEXES if n not in existing_idx]
    if missing_idx:
        raise RuntimeError(f"فهارس ناقصة بعد المايجريشن: {missing_idx}")

def run_migrations():
    conn = sqlite3.connect(_DB_PATH, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # executescript بيعمل commit ضمني لأي transaction مفتوحة، فبنشغّله قبل
        # ما نفتح أي BEGIN يدوي (وإلا بيرمي "cannot commit - no transaction is active").
        conn.executescript(_BASE_TABLES)
        conn.execute("BEGIN IMMEDIATE")  # لو أكتر من worker ابتدوا مع بعض، التاني بيستنى ويلاقي النسخة اتحدّثت
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            _ensure_columns(conn)
            for target, step in _MIGRATIONS:
                if version < target:
                    step(conn)
            # التطبيع دايمًا (idempotent): كل حسابات الطلبة على فرقة النشر الحالية
            # (APP_GRADE). الأدوار مبتتلمسش (professor/admin زي ما هي).
            conn.execute("UPDATE users SET grade=? WHERE role='student' AND grade IS NOT ?",
                         (APP_GRADE, APP_GRADE))
            for name, target in _INDEXES:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
            _validate_schema(conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except sqlite3.Error: pass
            raise
    finally:
        conn.close()

run_migrations()

def db_msg(sid, role, content, user_id=None):
    db_execute("INSERT INTO messages(session_id,role,content,ts,user_id) VALUES(?,?,?,?,?)",
               (sid, role, content[:3000], datetime.now().isoformat(), user_id))

def db_hist(sid, limit=20):
    rows = db_query_all(
        "SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (sid, limit))
    return [{"role":r,"content":c} for r,c in reversed(rows)]

def parse_exam_json(raw: str):
    """يحاول يفهم رد الموديل كـ JSON منظم لامتحان تفاعلي، مع تنضيف أي code fences
    زيادة، والتحقق الفعلي من شكل كل سؤال. بيرجع None لو الرد مش قابل للاستخدام
    كامتحان تفاعلي — عشان الاستدعاء يقدر يرجع للنص العادي بدل ما يكسر الميزة."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    clean_mcq = []
    for q in data.get("mcq", []) if isinstance(data.get("mcq"), list) else []:
        if not isinstance(q, dict):
            continue
        opts = q.get("options")
        idx = q.get("correct_index")
        if (isinstance(q.get("question"), str) and q["question"].strip()
                and isinstance(opts, list) and 2 <= len(opts) <= 8
                and all(isinstance(o, str) for o in opts)
                and isinstance(idx, int) and 0 <= idx < len(opts)):
            clean_mcq.append({"question": q["question"].strip(), "options": opts, "correct_index": idx})
    if not clean_mcq:
        return None

    clean_essay = []
    for e in data.get("essay", []) if isinstance(data.get("essay"), list) else []:
        if isinstance(e, dict) and isinstance(e.get("question"), str) and e["question"].strip():
            clean_essay.append({
                "question": e["question"].strip(),
                "model_answer": e.get("model_answer", "").strip() if isinstance(e.get("model_answer"), str) else ""
            })

    practical = data.get("practical")
    clean_practical = None
    if isinstance(practical, dict) and isinstance(practical.get("question"), str) and practical["question"].strip():
        clean_practical = {
            "question": practical["question"].strip(),
            "instructions": practical.get("instructions", "").strip()
                            if isinstance(practical.get("instructions"), str) else ""
        }

    return {"mcq": clean_mcq, "essay": clean_essay, "practical": clean_practical}

def db_log(event, data={}):
    db_execute("INSERT INTO analytics(event,data,ts) VALUES(?,?,?)",
               (event, json.dumps(data,ensure_ascii=False), datetime.now().isoformat()))

# ── Question normalization (for "most asked" grouping) ────
# تجميع بسيط بالتطابق شبه الحرفي بعد التطبيع — مش تجميع دلالي (embeddings)،
# فبيمسك نفس السؤال بصياغات قريبة (تشكيل/علامات ترقيم مختلفة)، لكن مش
# صياغات مختلفة لنفس الموضوع. ملاحظة موثّقة في الـ README كخطوة v1.
_AR_DIACRITICS = re.compile(r'[\u064B-\u065F\u0670\u06D6-\u06ED]')
_AR_PUNCT = re.compile(r'[؟،؛!.\?,;:"\'()\[\]{}«»_\-]')

def normalize_question(text: str) -> str:
    t = _AR_DIACRITICS.sub('', text or '')
    t = t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    t = t.replace('ى', 'ي').replace('ة', 'ه')
    t = _AR_PUNCT.sub(' ', t)
    return re.sub(r'\s+', ' ', t).strip().lower()

def top_questions_from(rows, n=10):
    groups = {}
    for (content,) in rows:
        key = normalize_question(content)
        if not key: continue
        g = groups.setdefault(key, {"count": 0, "example": content.strip()})
        g["count"] += 1
    return sorted(groups.values(), key=lambda g: -g["count"])[:n]

# ── Auth (users, sessions, passwords) ─────────────
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def create_session_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)   # ده اللي بيرجع للعميل ويتخزّن عنده بس
    expires = (datetime.now() + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
    db_execute("INSERT INTO auth_sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
               (hash_token(token), user_id, datetime.now().isoformat(), expires))  # بنخزّن الهاش بس
    return token

def get_user_by_token(token: str):
    """بيدوّر بالهاش (مش التوكن الخام) — الخام مايتخزّنش في الداتابيز خالص، فلو
    حد قرا الداتابيز مش هيقدر ينتحل شخصية حد بيه. توكن منتهي الصلاحية بيتمسح
    ويتعامل معاه بالظبط زي توكن غلط."""
    row = db_query_one(
        "SELECT u.id,u.username,u.role,u.grade,s.expires_at FROM auth_sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token=?", (hash_token(token),))
    if not row: return None
    try:
        expired = bool(row[4]) and datetime.fromisoformat(row[4]) < datetime.now()
    except ValueError:
        expired = True  # تاريخ صلاحية تالف → يتعامل معاه زي توكن غلط، مش 500
    if expired:
        db_execute("DELETE FROM auth_sessions WHERE token=?", (hash_token(token),))
        return None
    # كل مستخدم في النشر ده بيتحلّل لفرقة النشر (APP_GRADE) — مفيش اختيار فرقة من الطالب
    return {"id": row[0], "username": row[1], "role": row[2], "grade": APP_GRADE}

def require_auth(authorization: Optional[str] = Header(None)):
    """يتحقق من وجود توكن صالح — يُستخدم كحارس على أي endpoint محتاج تسجيل دخول."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "يجب تسجيل الدخول أولاً")
    token = authorization[7:]
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(401, "الجلسة غير صالحة أو منتهية، سجّل الدخول تاني")
    user["token"] = token
    return user

def resolve_user(authorization: Optional[str]):
    """بيرجع {id, role, grade} لو فيه توكن صالح، أو None من غير ما يعمل raise —
    عشان الشات والامتحانات يفضلوا شغالين للزوار من غير حساب بالظبط زي ما كانوا."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return get_user_by_token(authorization[7:])

# ── Session ownership (chat/exam/pdf sessions — جدول sessions، مش auth_sessions) ──
def get_authorized_session(session_id: Optional[str], user, *,
                           on_unknown: str = "create", touch: bool = True) -> str:
    """الحارس الوحيد لملكية الجلسة — كل endpoint بيتعامل مع session_id بيعدّي عليه.
    القواعد (المالك لازم يساوي هوية الطالب بالظبط، NULL = زائر):
      • جلسة مالكها يوزر ← ممنوعة على أي حد غير هو (زائر أو يوزر تاني) → 403
      • جلسة زائر (من غير مالك) ← ممنوعة على أي يوزر مسجّل دخول → 403 (مفيش "تبنّي"
        لجلسة زائر بمعرفة/تخمين الـ id) — والزائر اللي معاه الـ id بتاعه بيكمّل عادي
      • جلسة مش موجودة: on_unknown="create" (كتابة) بتتعمل وتتربط بصاحب الطلب،
        "404" (قراءة بس، زي /history) بترجع 404 من غير ما تنشئ أي حاجة، و"ignore"
        (زي /complaint) بترجّع الـ id من غير إنشاء
      • مفيش session_id أصلًا (وon_unknown="create") → id عشوائي آمن (secrets)
    الفحص والإنشاء في معاملة واحدة IMMEDIATE، فمفيش سباق بين طلبين على نفس id جديد.
    الأمان مش معتمد على سرية الـ UUID — الفحص بالمالك هو الحاجز."""
    uid = user["id"] if user else None
    now = datetime.now().isoformat()
    if not session_id:
        if on_unknown != "create":
            raise HTTPException(404, "الجلسة دي مش موجودة")
        sid = secrets.token_urlsafe(24)
        with get_db(write=True) as db:
            db.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                       (sid, uid, now, now))
        return sid
    sid = session_id
    with get_db(write=(on_unknown == "create" or touch)) as db:
        row = db.execute("SELECT user_id FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            if on_unknown == "create":
                db.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
                           (sid, uid, now, now))
                return sid
            if on_unknown == "ignore":
                return sid
            raise HTTPException(404, "الجلسة دي مش موجودة")
        if row[0] != uid:
            raise HTTPException(403, "مفيش صلاحية توصل للجلسة دي")
        if touch:
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
        return sid

# ── محدّد محاولات الفشل (تسجيل الدخول + سر الأدمن) ─────────────────────────────────
# بسيط ومحلي للعملية الواحدة (مفيش Redis): قاموس في الذاكرة بحجم محدود — مهما
# اخترع المهاجم أسماء/عناوين جديدة، الحجم مش هيعدّي RATE_LIMIT_MAX_KEYS (بيتشال
# الأقدم). مش موزّع: لو التطبيق اشتغل بأكتر من worker، كل واحد له عدّاده.
class _FailureLimiter:
    def __init__(self, max_attempts, window_seconds, max_keys=5000, message="محاولات كتير غلط — حاول تاني بعد شوية"):
        self.max_attempts, self.window, self.max_keys, self.message = max_attempts, window_seconds, max_keys, message
        self._d = {}
        self._lock = threading.Lock()
    def _fresh(self, key, now):
        lst = [t for t in self._d.get(key, ()) if now - t < self.window]
        if lst: self._d[key] = lst
        else: self._d.pop(key, None)
        return lst
    def check(self, key):
        with self._lock:
            if len(self._fresh(key, time.time())) >= self.max_attempts:
                raise HTTPException(429, self.message)
    def record(self, key):
        now = time.time()
        with self._lock:
            lst = self._fresh(key, now)
            lst.append(now)
            self._d[key] = lst
            excess = len(self._d) - self.max_keys
            if excess > 0:
                for k in list(self._d)[:excess]:
                    self._d.pop(k, None)
    def clear(self, key):
        with self._lock:
            self._d.pop(key, None)
    def size(self):
        with self._lock:
            return len(self._d)

RATE_LIMIT_MAX_KEYS  = int(os.environ.get("RATE_LIMIT_MAX_KEYS", "5000"))
LOGIN_MAX_ATTEMPTS   = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOGIN_WINDOW_SECONDS", "300"))
ADMIN_MAX_ATTEMPTS   = int(os.environ.get("ADMIN_MAX_ATTEMPTS", "10"))
ADMIN_WINDOW_SECONDS = int(os.environ.get("ADMIN_WINDOW_SECONDS", "300"))
_login_limiter = _FailureLimiter(LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS, RATE_LIMIT_MAX_KEYS,
                                 "محاولات دخول كتير غلط — حاول تاني بعد شوية")
_admin_limiter = _FailureLimiter(ADMIN_MAX_ATTEMPTS, ADMIN_WINDOW_SECONDS, RATE_LIMIT_MAX_KEYS,
                                 "محاولات كتير غلط — حاول تاني بعد شوية")

def _check_login_rate_limit(username: str):  _login_limiter.check(username)
def _record_login_failure(username: str):    _login_limiter.record(username)
def _clear_login_attempts(username: str):    _login_limiter.clear(username)

# ── PDF Extraction ────────────────────────────────
# حدود قابلة للتحكم فيها من متغيرات البيئة — بترفض الملفات الضخمة قبل أي
# معالجة مكلفة (فتح PDF، استخراج نص) بدل ما تستهلك وقت/ذاكرة على ملف هيترفض
# في الآخر أصلًا.
MAX_PDF_BYTES = int(os.environ.get("MAX_PDF_MB", "10")) * 1024 * 1024
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "60"))
MAX_PDF_TEXT_CHARS = int(os.environ.get("MAX_PDF_TEXT_CHARS", "8000"))
DOCUMENT_TTL_HOURS = int(os.environ.get("DOCUMENT_TTL_HOURS", "24"))

class PDFTooLargeError(Exception):
    """عدد الصفحات تجاوز الحد المسموح — نميّزه عن أي خطأ استخراج تاني عشان
    الـ endpoint يرجّع رسالة واضحة بدل رسالة عامة."""

class PDFRejectedError(Exception):
    """ملف اتفتح بس مش مقبول (مش PDF فعلًا، أو محمي بكلمة سر)."""

def extract_pdf(path, max_chars=None, max_pages=None):
    max_chars = MAX_PDF_TEXT_CHARS if max_chars is None else max_chars
    max_pages = MAX_PDF_PAGES if max_pages is None else max_pages
    doc = fitz.open(path)   # PyMuPDF بيتحقق من البنية الفعلية للملف، مش الامتداد بس
    try:
        if not getattr(doc, "is_pdf", True):
            raise PDFRejectedError("الملف ده مش PDF")
        if getattr(doc, "needs_pass", False):
            raise PDFRejectedError("الملف محمي بكلمة سر")
        if doc.page_count > max_pages:   # قبل أي استخراج مكلف
            raise PDFTooLargeError(f"الملف فيه {doc.page_count} صفحة — الحد الأقصى {max_pages}")
        parts, total = [], 0
        for page in doc:
            t = page.get_text("text")
            if len(t.strip()) < 20:
                t = " ".join(w[4] for w in page.get_text("words"))
            parts.append(t)
            total += len(t) + 1
            if total >= max_chars:   # الميزانية خلصت — منكمّلش استخراج باقي الصفحات
                break
        return "\n".join(parts).strip()[:max_chars]
    finally:
        doc.close()

# ── تخزين نص PDF المستخرَج ─────────────────────────
# بدل قاموس عام في الذاكرة (بيتصفّر مع كل إعادة تشغيل، ومش موجود لو التطبيق
# اشتغل بأكتر من worker process): بنخزّن في جدول documents العادي، بنفس مسار
# قاعدة البيانات الدائم (/data لو متاح). كل رفع جديد لنفس الجلسة بيستبدل اللي
# قبله (مستند واحد لكل جلسة، زي السلوك الأصلي)، ومعاه مالك الجلسة واسم الملف
# ووقت الرفع، وبيتنضّف تلقائيًا بعد TTL محدّد بدل ما يتراكم للأبد.
def store_document(session_id, user_id, filename, text):
    now = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(hours=DOCUMENT_TTL_HOURS)).isoformat()
    with get_db() as db:
        db.execute("DELETE FROM documents WHERE uploaded_at < ?", (cutoff,))  # تنضيف انتهازي
        db.execute(
            "INSERT INTO documents(session_id,user_id,filename,text,chars,uploaded_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "user_id=excluded.user_id, filename=excluded.filename, text=excluded.text, "
            "chars=excluded.chars, uploaded_at=excluded.uploaded_at",
            (session_id, user_id, filename, text, len(text), now))

def get_document_text(session_id):
    cutoff = (datetime.now() - timedelta(hours=DOCUMENT_TTL_HOURS)).isoformat()
    row = db_query_one(
        "SELECT text FROM documents WHERE session_id=? AND uploaded_at >= ?",
        (session_id, cutoff))
    return row[0] if row else ""

# ── FastAPI App ───────────────────────────────────
# CORS: الواجهة بتتقدّم من نفس الأصل (التطبيق نفسه) فمش محتاجة CORS خالص. لو فيه
# أصل تاني مسموح له فعلًا، حدّده صراحةً في ALLOWED_ORIGINS (مفصولين بفاصلة).
# "*" مرفوض عمدًا.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if "*" in ALLOWED_ORIGINS:
    raise RuntimeError("ALLOWED_ORIGINS لازم تحدّد أصول صريحة — '*' مش مسموح.")
app = FastAPI(title="المعيد الذكي", version="3.0")
if ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
Path("static").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Models ────────────────────────────────────────
# كل الحدود دي مفروضة هنا في الباك اند — مش معتمدين على تحقق الفرونت.
_SESSION_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"

class ChatReq(BaseModel):
    message: str = Field(..., max_length=4000)
    session_id: Optional[str] = Field(None, pattern=_SESSION_ID_PATTERN)

class ComplaintReq(BaseModel):
    text: str = Field(..., max_length=3000)
    session_id: Optional[str] = Field(None, pattern=_SESSION_ID_PATTERN)

class FeedbackReq(BaseModel):
    session_id: str = Field(..., pattern=_SESSION_ID_PATTERN)
    question: str = Field(..., max_length=2000)
    answer: str = Field(..., max_length=20000)   # بيتخزّن مقطوع 500 حرف، بس الرد الأصلي ممكن يبقى طويل
    rating: Literal["good", "bad"]

class RegisterReq(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=128)

class LoginReq(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=128)

class DashReq(BaseModel):
    secret: str = Field(..., max_length=256)

class QuizAnswerReq(BaseModel):
    quiz_id: str = Field(..., max_length=64)
    question_index: int = Field(..., ge=0, le=100)
    chosen_index: int = Field(..., ge=0, le=20)

class ExamReq(BaseModel):
    topic: str = Field(..., max_length=200)
    session_id: Optional[str] = Field(None, pattern=_SESSION_ID_PATTERN)
    num_mcq: int = Field(5, ge=1, le=20)
    num_essay: int = Field(2, ge=0, le=10)

class GradeReq(BaseModel):
    answer: str = Field(..., max_length=8000)
    question: Optional[str] = Field(None, max_length=2000)
    session_id: Optional[str] = Field(None, pattern=_SESSION_ID_PATTERN)

class SummaryReq(BaseModel):
    session_id: str = Field(..., pattern=_SESSION_ID_PATTERN)

# ── Routes ────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home():
    p = Path("frontend/index.html")
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<h1>المعيد الذكي ✅</h1>")

# ── Auth Routes ───────────────────────────────────
MIN_PASSWORD_LEN = int(os.environ.get("MIN_PASSWORD_LEN", "8"))
MAX_PASSWORD_BYTES = 72  # حد bcrypt نفسه — أطول من كده بيتقطع أو بيرمي خطأ حسب النسخة

@app.post("/auth/register")
def register(req: RegisterReq):
    username = req.username.strip()
    if len(username) < 3:
        raise HTTPException(400, "اسم المستخدم قصير جداً (3 أحرف على الأقل)")
    if len(req.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"كلمة السر قصيرة جداً ({MIN_PASSWORD_LEN} أحرف على الأقل)")
    if len(req.password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise HTTPException(400, "كلمة السر طويلة جداً")
    if db_query_one("SELECT id FROM users WHERE username=?", (username,)):
        raise HTTPException(409, "اسم المستخدم ده مستخدم بالفعل")
    try:
        user_id = db_execute(
            "INSERT INTO users(username,password_hash,role,grade,created_at) VALUES(?,?,?,?,?)",
            (username, hash_password(req.password), "student", APP_GRADE, datetime.now().isoformat()))
    except sqlite3.IntegrityError:  # طلبين متزامنين بنفس الاسم — الـ UNIQUE هو الحكم
        raise HTTPException(409, "اسم المستخدم ده مستخدم بالفعل")
    token = create_session_token(user_id)
    db_log("register", {"username": username, "grade": APP_GRADE})
    return {"token": token, "username": username, "role": "student", "grade": APP_GRADE}

@app.post("/auth/login")
def login(req: LoginReq):
    username = req.username.strip()
    _check_login_rate_limit(username)
    row = db_query_one("SELECT id,password_hash,role FROM users WHERE username=?", (username,))
    if not row or not verify_password(req.password, row[1]):
        _record_login_failure(username)
        raise HTTPException(401, "اسم المستخدم أو كلمة السر غلط")
    _clear_login_attempts(username)
    token = create_session_token(row[0])
    db_log("login", {"username": username})
    return {"token": token, "username": username, "role": row[2], "grade": APP_GRADE}

@app.post("/auth/logout")
def logout(user = Depends(require_auth)):
    db_execute("DELETE FROM auth_sessions WHERE token=?", (hash_token(user["token"]),))
    return {"ok": True}

@app.get("/auth/me")
def me(user = Depends(require_auth)):
    return {"username": user["username"], "role": user["role"], "grade": user["grade"]}

# ── El Dashboard (الإدارة الوحيدة) ────────────────────────────────────────────────
# صفحة مستقلة على /dashboard + endpoints /dash/* — كلهم بنفس ADMIN_SECRET من غير
# أدوار ولا حسابات. مفيش لوحة إدارة تانية (اللي كانت في تطبيق الطلاب و/admin اتشالوا).
# المقارنة constant-time، وفيه حد لمحاولات السر الغلط: لكل عميل (أول عنوان في
# X-Forwarded-For لو فيه) + حد عام أوسع لو حد بدّل العنوان — بسيط ومحلي للعملية.
_admin_global_limiter = _FailureLimiter(ADMIN_MAX_ATTEMPTS * 5, ADMIN_WINDOW_SECONDS, 10,
                                        "محاولات كتير غلط — حاول تاني بعد شوية")

def _client_key(xff: Optional[str]) -> str:
    first = (xff or "").split(",")[0].strip()
    return first[:64] or "direct"

def _check_admin_secret(secret: str, client_key: str = "direct"):
    _admin_limiter.check(client_key)
    _admin_global_limiter.check("*")
    if not secrets.compare_digest(secret.encode("utf-8"), ADMIN_SECRET.encode("utf-8")):
        _admin_limiter.record(client_key)
        _admin_global_limiter.record("*")
        raise HTTPException(403, "كلمة السر غلط")
    _admin_limiter.clear(client_key)

def _session_users(db):
    """session_id ← أسماء اليوزرز اللي بعتوا رسايل في الجلسة دي وهم مسجّلين دخول.
    جدولا feedback و complaints مفيهمش عمود user_id، فالربط لازم يعدّي على
    session_id ← messages.user_id ← users.username (مش JOIN مباشر على user_id)."""
    names = {}
    for sid, uname in db.execute(
            "SELECT DISTINCT m.session_id, u.username FROM messages m "
            "JOIN users u ON u.id = m.user_id ORDER BY u.username"):
        names.setdefault(sid, []).append(uname)
    return {sid: ", ".join(v) for sid, v in names.items()}

@app.post("/dash/summary")
def dash_summary(req: DashReq, x_forwarded_for: Optional[str] = Header(None)):
    _check_admin_secret(req.secret, _client_key(x_forwarded_for))
    with get_db() as db:
        # مصدر كل رقم: messages (سؤال + رد)، sessions (المصدر الرسمي للجلسات)،
        # users، complaints، feedback — كلهم من قاعدة البيانات الحالية في كل طلب.
        total_messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]           # أسئلة + ردود
        questions      = db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
        ses    = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        linked = db.execute("SELECT COUNT(*) FROM messages WHERE user_id IS NOT NULL").fetchone()[0]
        tc  = db.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
        oc  = db.execute("SELECT COUNT(*) FROM complaints WHERE status='مفتوحة'").fetchone()[0]
        # التقييمات: good و bad بيتعدّوا صراحةً (مش bad = total - good، لأن أي قيمة
        # تاريخية غريبة كانت هتتحسب "bad" غلط)
        good = db.execute("SELECT COUNT(*) FROM feedback WHERE rating='good'").fetchone()[0]
        bad  = db.execute("SELECT COUNT(*) FROM feedback WHERE rating='bad'").fetchone()[0]
        total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        students    = db.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]

        # أسئلة الطلبة يوميًا آخر 14 يوم (النهارده + 13 قبله) — رسائل المستخدم بس
        # (مش ردود المساعد)، والأيام الفاضية بتتملى بصفر عشان الرسم يفضل متصل.
        first_day = (datetime.now() - timedelta(days=13)).strftime('%Y-%m-%d')
        daily = dict(db.execute(
            "SELECT substr(ts,1,10) AS d, COUNT(*) FROM messages "
            "WHERE role='user' AND ts >= ? GROUP BY d", (first_day,)).fetchall())
        questions_by_day = []
        for i in range(13, -1, -1):
            day = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            questions_by_day.append({"date": day, "count": daily.get(day, 0)})

        # قايمة اليوزرز: عدد الأسئلة اللي بعتها اليوزر نفسه (role='user') وآخر رسالة ليه.
        rows = db.execute(
            "SELECT u.username, u.created_at, COALESCE(s.cnt, 0), s.last_ts "
            "FROM users u LEFT JOIN ("
            "  SELECT user_id, SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS cnt, MAX(ts) AS last_ts "
            "  FROM messages WHERE user_id IS NOT NULL GROUP BY user_id"
            ") s ON s.user_id = u.id ORDER BY u.id DESC").fetchall()
        users = [{"username": r[0], "joined": r[1], "messages": r[2], "last_active": r[3]}
                 for r in rows]

    fb_total = good + bad
    return {
        "messages": total_messages, "questions": questions, "sessions": ses, "messages_linked": linked,
        "questions_by_day": questions_by_day,
        "complaints_total": tc, "complaints_open": oc,
        "feedback_total": fb_total, "feedback_good": good, "feedback_bad": bad,
        "satisfaction": f"{int(good/fb_total*100)}%" if fb_total else "—",
        "pinecone": "✅ متصل" if _pine else "❌ غير متصل",
        "users_total": total_users, "students_total": students,
        "users": users,
    }

@app.post("/dash/questions")
def dash_questions(req: DashReq, x_forwarded_for: Optional[str] = Header(None)):
    _check_admin_secret(req.secret, _client_key(x_forwarded_for))
    with get_db() as db:
        all_rows = db.execute("SELECT content FROM messages WHERE role='user'").fetchall()
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        recent_rows = db.execute(
            "SELECT content FROM messages WHERE role='user' AND ts >= ?", (cutoff,)).fetchall()
    return {
        "total_questions": len(all_rows),
        "top_all_time": top_questions_from(all_rows, 20),
        "top_recent_7d": top_questions_from(recent_rows, 20),
    }

@app.post("/dash/complaints")
def dash_complaints(req: DashReq, x_forwarded_for: Optional[str] = Header(None)):
    _check_admin_secret(req.secret, _client_key(x_forwarded_for))
    with get_db() as db:
        users = _session_users(db)
        rows = db.execute(
            "SELECT id,category,priority,status,text,ts,session_id FROM complaints ORDER BY id DESC"
        ).fetchall()
    return {"complaints": [
        {"id": r[0], "category": r[1], "priority": r[2], "status": r[3],
         "text": r[4], "ts": r[5], "session_id": r[6], "user": users.get(r[6])}
        for r in rows]}

@app.post("/dash/feedback")
def dash_feedback(req: DashReq, x_forwarded_for: Optional[str] = Header(None)):
    _check_admin_secret(req.secret, _client_key(x_forwarded_for))
    with get_db() as db:
        users = _session_users(db)
        rows = db.execute(
            "SELECT id,session_id,rating,question,answer,ts FROM feedback ORDER BY id DESC"
        ).fetchall()
    return {"feedback": [
        {"id": r[0], "session_id": r[1], "rating": r[2], "question": r[3],
         "answer": r[4], "ts": r[5], "user": users.get(r[1])}
        for r in rows]}

@app.post("/dash/topics")
def dash_topics(req: DashReq, x_forwarded_for: Optional[str] = Header(None)):
    _check_admin_secret(req.secret, _client_key(x_forwarded_for))

    with get_db() as db:
        # الموضوعات الأكثر طلبًا = مرات طلب توليد امتحان على الموضوع (أحداث "exam"
        # في analytics) — مش أسئلة الشات.
        exam_rows = db.execute("SELECT data FROM analytics WHERE event='exam'").fetchall()
        topic_groups = {}
        for (data,) in exam_rows:
            try:
                topic = (json.loads(data).get("topic") or "").strip()
            except Exception:
                topic = ""
            if not topic:
                continue
            key = normalize_question(topic)
            g = topic_groups.setdefault(key, {"count": 0, "topic": topic})
            g["count"] += 1
        most_requested_topics = sorted(topic_groups.values(), key=lambda g: -g["count"])[:15]

        # أداء الاختبارات حسب الموضوع — من إجابات محسوبة في السيرفر بس (quiz_answers)
        topic_perf = {}
        for topic, is_correct in db.execute("SELECT topic, is_correct FROM quiz_answers"):
            key = normalize_question(topic)
            g = topic_perf.setdefault(key, {"topic": topic, "attempts": 0, "correct": 0})
            g["attempts"] += 1
            g["correct"] += is_correct
        MIN_ATTEMPTS = 3  # عشان موضوع بمحاولة واحدة غلط مايتصنّفش "ضعيف" غلط من عيّنة صغيرة جدًا
        weak_topics = [
            {"topic": g["topic"], "attempts": g["attempts"], "correct": g["correct"],
             "accuracy": round(100 * g["correct"] / g["attempts"])}
            for g in topic_perf.values() if g["attempts"] >= MIN_ATTEMPTS
        ]
        weak_topics.sort(key=lambda g: g["accuracy"])
        weak_topics = weak_topics[:15]

        total_attempts = sum(g["attempts"] for g in topic_perf.values())
        total_correct = sum(g["correct"] for g in topic_perf.values())
        quiz_summary = {
            "total_exams_generated": len(exam_rows),
            "total_attempts": total_attempts,
            "overall_accuracy": f"{round(100 * total_correct / total_attempts)}%" if total_attempts else "—",
        }

        wrong_rows = db.execute("SELECT question FROM quiz_answers WHERE is_correct=0").fetchall()
        hardest_questions = top_questions_from(wrong_rows, 15)

        # ── حالة الاسترجاع (RAG) ────────────────────────────────────────────
        # فجوة المحتوى الحقيقية = الاسترجاع اشتغل ومفيش سياق مناسب ("empty") بس.
        # خطأ بنية تحتية (Pinecone تايم أوت مثلًا) مش "المنهج مفيهوش الإجابة" —
        # بيتعدّ لوحده في retrieval_errors ومبيدخلش في نسبة الفجوة. أحداث الشات
        # القديمة (قبل ما retrieval_status يتسجّل) بتتصنّف من علامة rag القديمة.
        chat_rows = db.execute("SELECT data FROM analytics WHERE event='chat'").fetchall()
        counts = {"vector": 0, "fallback": 0, "empty": 0, "error": 0, "document": 0,
                  "unavailable": 0, "legacy_matched": 0, "legacy_empty": 0}
        retrieval_errors = chat_errors = chat_cancelled = 0
        for (data,) in chat_rows:
            try:
                d = json.loads(data)
            except Exception:
                continue
            st = d.get("status")
            if st == "error":
                chat_errors += 1
            elif st == "cancelled":
                chat_cancelled += 1
            rs = d.get("retrieval_status")
            if rs in counts:
                counts[rs] += 1
            else:
                counts["legacy_matched" if d.get("rag") else "legacy_empty"] += 1
            if rs == "error" or d.get("vector_error"):
                retrieval_errors += 1
        eligible = (counts["vector"] + counts["fallback"] + counts["empty"]
                    + counts["legacy_matched"] + counts["legacy_empty"])
        gaps = counts["empty"] + counts["legacy_empty"]
        content_gap_rate = f"{round(100 * gaps / eligible)}%" if eligible else "—"

        # امتداد الجلسة (مش "وقت نشط" فعلي): من أول رسالة لآخر رسالة في الجلسة، بالدقايق
        durations = []
        for (mn, mx) in db.execute("SELECT MIN(ts), MAX(ts) FROM messages GROUP BY session_id"):
            try:
                mins = (datetime.fromisoformat(mx) - datetime.fromisoformat(mn)).total_seconds() / 60
                if mins > 0:
                    durations.append(mins)
            except Exception:
                pass
        avg_session_span_minutes = round(sum(durations) / len(durations), 1) if durations else 0

        feature_usage = dict(db.execute(
            "SELECT event, COUNT(*) FROM analytics WHERE event IN "
            "('chat','exam','grade','pdf','complaint') GROUP BY event"))

    return {
        "most_requested_topics": most_requested_topics,
        "weak_topics": weak_topics,
        "quiz_summary": quiz_summary,
        "hardest_questions": hardest_questions,
        "content_gap_rate": content_gap_rate,
        "retrieval_breakdown": counts,
        "retrieval_errors": retrieval_errors,
        "chat_errors": chat_errors,
        "chat_cancelled": chat_cancelled,
        "avg_session_span_minutes": avg_session_span_minutes,
        "feature_usage": feature_usage,
    }

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    p = Path("frontend/dashboard.html")
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists() else "<h1>Coming soon</h1>")

@app.post("/chat")
def chat(req: ChatReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    uid, grade = (u["id"], u["grade"]) if u else (None, None)
    sid = get_authorized_session(req.session_id, u)
    msg = req.message.strip()
    if not msg: raise HTTPException(400, "الرسالة فارغة")
    db_msg(sid, "user", msg, uid)
    doc = get_document_text(sid)
    res = RagResult(doc, "document", False) if doc else rag_lookup(msg, grade=grade)
    rag = res.text
    ctx = f"\n\n[من قاعدة المعرفة]:\n{rag}" if rag else ""
    hist = db_hist(sid, 18)[:-1]
    msgs = ([{"role":"system","content": SYSTEM_PROMPT+ctx}]
            + [{"role":m["role"],"content":m["content"]} for m in hist]
            + [{"role":"user","content":msg}])
    status = "ok"
    try:
        ans = llm(msgs)
    except Exception as e:
        ans, status = _llm_error(e), "error"
    if status == "ok":  # رسالة الخطأ مبتتحفظش كإنها رد حقيقي من المساعد
        db_msg(sid, "assistant", ans, uid)
    db_log("chat", {"session": sid, "rag": bool(rag), "stream": False, "status": status,
                    "retrieval_status": res.status, "vector_error": res.vector_error})
    out = {"answer": ans, "session_id": sid, "rag_used": bool(rag),
           "kb_available": rag_available_for(grade)}
    if status == "error":
        out["error"] = True
    return out

@app.post("/chat/stream")
async def chat_stream(req: ChatReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    uid, grade = (u["id"], u["grade"]) if u else (None, None)
    sid = get_authorized_session(req.session_id, u)
    msg = req.message.strip()
    if not msg: raise HTTPException(400, "الرسالة فارغة")
    db_msg(sid, "user", msg, uid)
    doc = get_document_text(sid)
    res = RagResult(doc, "document", False) if doc else rag_lookup(msg, grade=grade)
    rag = res.text
    ctx = f"\n\n[من قاعدة المعرفة]:\n{rag}" if rag else ""
    hist = db_hist(sid, 18)[:-1]
    msgs = ([{"role":"system","content": SYSTEM_PROMPT+ctx}]
            + [{"role":m["role"],"content":m["content"]} for m in hist]
            + [{"role":"user","content":msg}])
    # event الاسم "chat" هنا (مش "stream" زي قبل كده) عشان يتحسب مع نفس فئة
    # الشات العادي في لوحة التحليلات (feature_usage، content_gap_rate) —
    # stream:true بيفضل موجود في البيانات لو حد عايز يميّز بينهم لاحقًا.
    return stream_llm_payload(
        msgs, sid, "chat", max_tokens=1500,
        log_data={"rag": bool(rag), "retrieval_status": res.status, "vector_error": res.vector_error},
        save_chat=True, user_id=uid
    )


@app.post("/exam/stream")
def exam_stream(req: ExamReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    grade = u["grade"] if u else None
    sid = get_authorized_session(req.session_id, u)
    topic = req.topic.strip() or "تكنولوجيا التعليم"
    res = rag_lookup(topic, top_k=6, grade=grade)
    rag = res.text
    ctx = f"\n\n[من المنهج]:\n{rag}" if rag else ""
    prompt = f"""أنشئ امتحاناً أكاديمياً منظماً عن: "{topic}"{ctx}

**أولاً: اختيار من متعدد ({req.num_mcq} أسئلة)**
لكل سؤال: السؤال + 4 اختيارات (أ ب ج د) + الإجابة الصحيحة.

**ثانياً: أسئلة مقالية ({req.num_essay} أسئلة)**
مع نموذج إجابة مختصر لكل سؤال.

**ثالثاً: سؤال تطبيقي**
سؤال عملي مع تعليمات واضحة."""
    msgs = [{"role":"system","content":SYSTEM_PROMPT},
            {"role":"user","content":prompt}]
    return stream_llm_payload(
        msgs, sid, "exam", max_tokens=2000,
        log_data={"topic": topic, "rag": bool(rag), "retrieval_status": res.status, "vector_error": res.vector_error}
    )

@app.post("/exam")
def exam(req: ExamReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    uid   = u["id"] if u else None
    grade = u["grade"] if u else None
    sid   = get_authorized_session(req.session_id, u)
    topic = req.topic.strip() or "تكنولوجيا التعليم"
    res   = rag_lookup(topic, top_k=6, grade=grade)
    rag   = res.text
    ctx   = f"\n\n[من المنهج]:\n{rag}" if rag else ""

    json_prompt = f"""أنشئ امتحاناً أكاديمياً عن: "{topic}"{ctx}

أرجع النتيجة بصيغة JSON فقط بالشكل ده بالظبط، من غير أي نص قبله أو بعده:
{{
  "mcq": [{{"question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0}}],
  "essay": [{{"question": "...", "model_answer": "..."}}],
  "practical": {{"question": "...", "instructions": "..."}}
}}

اعمل بالظبط {req.num_mcq} سؤال اختيار من متعدد (4 اختيارات لكل سؤال)، و{req.num_essay} سؤال مقالي مع نموذج إجابة.
correct_index هو رقم الاختيار الصحيح (0 = الأول، 1 = الثاني...)."""

    parsed = None
    try:
        raw = llm([{"role":"system","content":SYSTEM_PROMPT},
                   {"role":"user","content":json_prompt}], max_tokens=2500, json_mode=True)
        parsed = parse_exam_json(raw)
    except Exception as e:
        log.warning(f"exam json generation: {type(e).__name__}")

    base_log = {"topic": topic, "session": sid, "stream": False,
                "retrieval_status": res.status, "vector_error": res.vector_error}

    if parsed:
        db_log("exam", {**base_log, "format": "interactive", "status": "ok"})
        # بنخزّن مفتاح الإجابات في السيرفر (quizzes) — /quiz/answer هيرجع
        # يقرا منه بدل ما يصدّق أي correct_index جاي من العميل. الفرونت لسه
        # بيستقبل correct_index مع كل سؤال (عادي، مطلوب للتلوين الفوري
        # للإجابة في المتصفح)، بس مش هو مصدر الحقيقة وقت التسجيل/الإحصاء.
        quiz_id = str(uuid.uuid4())
        db_execute(
            "INSERT INTO quizzes(id,session_id,user_id,topic,questions_json,created_at) VALUES(?,?,?,?,?,?)",
            (quiz_id, sid, uid, topic, json.dumps(parsed["mcq"], ensure_ascii=False), datetime.now().isoformat()))
        return {"format": "interactive", "session_id": sid, "topic": topic, "quiz_id": quiz_id,
                "kb_available": rag_available_for(grade), **parsed}

    # فشل الـ JSON لأي سبب — نرجع لصيغة النص القديمة بدل ما نكسر الميزة كلها
    text_prompt = f"""أنشئ امتحاناً أكاديمياً شاملاً عن: "{topic}"{ctx}

**أولاً: اختيار من متعدد ({req.num_mcq} أسئلة)**
لكل سؤال: السؤال + 4 خيارات (أ ب ج د) + ✅ الإجابة الصحيحة

**ثانياً: أسئلة مقالية ({req.num_essay} أسئلة)**
مع نموذج إجابة مختصر لكل سؤال

**ثالثاً: سؤال تطبيقي**
سؤال عملي مع تعليمات واضحة"""
    status = "ok"
    try:
        result = llm([{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":text_prompt}], max_tokens=2000)
    except Exception as e:
        result, status = _llm_error(e), "error"
    db_log("exam", {**base_log, "format": "text", "status": status})
    return {"format": "text", "exam": result, "session_id": sid, "topic": topic,
            "kb_available": rag_available_for(grade)}

@app.post("/quiz/answer")
def quiz_answer(req: QuizAnswerReq, authorization: Optional[str] = Header(None)):
    """بيسجّل إجابة سؤال اختيار من متعدد. العميل بيبعت quiz_id + رقم السؤال +
    اختياره بس — الصح/الغلط بيتحسب هنا من مفتاح الإجابات المخزّن وقت توليد
    الامتحان (جدول quizzes)، وأي حقول تانية في الطلب بتتجاهل. ملكية الامتحان:
    لازم تطابق هوية صاحب الطلب بالظبط (امتحان زائر مش ممكن يجاوب عليه يوزر مسجّل
    بمعرفة الـ quiz_id بس، والعكس) + نفس فحص ملكية الجلسة الموحّد."""
    u = resolve_user(authorization)
    uid = u["id"] if u else None
    row = db_query_one(
        "SELECT session_id, user_id, topic, questions_json FROM quizzes WHERE id=?",
        (req.quiz_id,))
    if not row:
        raise HTTPException(404, "الامتحان ده مش موجود")
    qsid, qowner, topic, qjson = row
    if qowner != uid:
        raise HTTPException(403, "مفيش صلاحية على الامتحان ده")
    get_authorized_session(qsid, u, on_unknown="404", touch=False)

    questions = json.loads(qjson)
    if not (req.question_index < len(questions)):
        raise HTTPException(400, "رقم سؤال غير صالح")
    q = questions[req.question_index]
    if not (req.chosen_index < len(q["options"])):
        raise HTTPException(400, "اختيار غير صالح")

    correct_index = q["correct_index"]
    is_correct = 1 if req.chosen_index == correct_index else 0
    now = datetime.now().isoformat()
    try:
        # الـ UNIQUE(quiz_id, question_index) هو الحكم النهائي: لو طلبين متزامنين
        # جاوبوا نفس السؤال، واحد بس بينجح والتاني بياخد 409 (مش 500).
        with get_db(write=True) as db:
            db.execute(
                "INSERT INTO quiz_attempts(quiz_id,question_index,chosen_index,is_correct,ts) VALUES(?,?,?,?,?)",
                (req.quiz_id, req.question_index, req.chosen_index, is_correct, now))
            # نفس جدول quiz_answers (لوحة التحليلات بتقرا منه) — بس بقيمة اتحسبت في السيرفر
            db.execute(
                "INSERT INTO quiz_answers(session_id,topic,question,chosen_index,correct_index,is_correct,ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (qsid, topic, q["question"], req.chosen_index, correct_index, is_correct, now))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "الإجابة على السؤال ده اتسجّلت قبل كده")
    return {"ok": True, "is_correct": bool(is_correct)}

@app.post("/grade/stream")
def grade_stream(req: GradeReq, authorization: Optional[str] = Header(None)):
    sid = get_authorized_session(req.session_id, resolve_user(authorization))
    text = req.answer.strip()
    if not text: raise HTTPException(400, "الإجابة فارغة")
    qctx = f"\nالسؤال: {req.question}" if req.question else ""
    prompt = f"""قيّم إجابة الطالب:{qctx}

[إجابة الطالب]: {text}

قدّم:
1. **الدرجة** من 10 مع مبرر واضح.
2. **نقاط القوة**.
3. **نقاط الضعف**.
4. **الإجابة النموذجية**.
5. **توصيات للتحسين**."""
    msgs = [{"role":"system","content":SYSTEM_PROMPT},
            {"role":"user","content":prompt}]
    return stream_llm_payload(msgs, sid, "grade", max_tokens=1200)

@app.post("/grade")
def grade(req: GradeReq, authorization: Optional[str] = Header(None)):
    sid  = get_authorized_session(req.session_id, resolve_user(authorization))
    text = req.answer.strip()
    if not text: raise HTTPException(400, "الإجابة فارغة")
    qctx = f"\nالسؤال: {req.question}" if req.question else ""
    prompt = f"""قيّم إجابة الطالب:{qctx}

[إجابة الطالب]: {text}

قدّم:
1. **الدرجة** (من 10) مع مبرر
2. **نقاط القوة** ✅
3. **نقاط الضعف** ❌
4. **الإجابة النموذجية** 📚
5. **توصيات** 💡"""
    status = "ok"
    try:
        result = llm([{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":prompt}], max_tokens=1200)
    except Exception as e:
        result, status = _llm_error(e), "error"
    db_log("grade", {"session": sid, "stream": False, "status": status})
    return {"result":result,"session_id":sid}

@app.post("/summary/stream")
def summary_stream(req: SummaryReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    sid = get_authorized_session(req.session_id, u, on_unknown="404", touch=False)
    ctx = get_document_text(sid)
    if not ctx: raise HTTPException(400, "لم يتم رفع ملف لهذه الجلسة")
    prompt = f"""لخّص هذا المحتوى التعليمي تلخيصاً أكاديمياً منظماً:

{ctx}

يشمل: الموضوع، أهم المفاهيم، النقاط الجوهرية، وخلاصة عملية للمذاكرة."""
    msgs = [{"role":"system","content":SYSTEM_PROMPT},
            {"role":"user","content":prompt}]
    return stream_llm_payload(
        msgs, sid, "summary", max_tokens=1500,
        log_data={"chars": len(ctx)}
    )

@app.post("/summary")
def summary(req: SummaryReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    sid = get_authorized_session(req.session_id, u, on_unknown="404", touch=False)
    ctx = get_document_text(sid)
    if not ctx: raise HTTPException(400, "لم يتم رفع ملف لهذه الجلسة")
    prompt = f"""لخّص هذا المحتوى التعليمي تلخيصاً أكاديمياً منظماً:

{ctx}

يشمل: الموضوع + أهم المفاهيم + النقاط الجوهرية + خلاصة"""
    try:
        result = llm([{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":prompt}], max_tokens=1500)
    except Exception as e: result = _llm_error(e)
    return {"summary":result,"session_id":sid}

@app.post("/upload-pdf")
async def upload_pdf(session_id: str, file: UploadFile = File(...),
                      authorization: Optional[str] = Header(None)):
    if not re.fullmatch(_SESSION_ID_PATTERN, session_id):
        raise HTTPException(400, "معرّف الجلسة غير صالح")
    u = resolve_user(authorization)
    uid = u["id"] if u else None
    sid = get_authorized_session(session_id, u)
    # اسم الملف metadata بس (مبيتستخدمش أبدًا كمسار ملفات): بنشيل أي مسار ونقصّه
    filename = os.path.basename((file.filename or "").replace("\\", "/"))[:255]
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "يجب رفع ملف PDF")

    path = None
    try:
        # الرفع بيتكتب chunk بـ chunk مباشرة على ملف مؤقت آمن، وبيقف فورًا لو
        # الحجم عدّى الحد — مفيش تجميع للملف كله في قايمة/ذاكرة.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            path = tmp.name
            total = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise HTTPException(413, f"حجم الملف تجاوز الحد المسموح ({MAX_PDF_BYTES // (1024*1024)} ميجابايت)")
                tmp.write(chunk)
        try:
            text = extract_pdf(path)
        except PDFTooLargeError as e:
            raise HTTPException(413, str(e))
        except PDFRejectedError as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            log.error(f"PDF: {type(e).__name__}")
            raise HTTPException(422, "تعذّر قراءة الملف — تأكد إنه PDF سليم غير تالف")
    finally:
        if path:
            Path(path).unlink(missing_ok=True)

    if not text or len(text) < 50:
        raise HTTPException(422, "لم يُستخرج نص كافٍ من الملف")
    store_document(sid, uid, filename, text)
    db_log("pdf", {"session": sid, "chars": len(text), "file": filename})
    return {"message": f"✅ تم قراءة الملف ({len(text):,} حرف)",
            "session_id": sid, "chars": len(text)}

@app.post("/complaint")
def complaint(req: ComplaintReq, authorization: Optional[str] = Header(None)):
    text = req.text.strip()
    if not text: raise HTTPException(400, "نص الشكوى فارغ")
    # الشكوى ممكن تتقدّم من غير جلسة خالص (sentinel "anonymous" مشترك، مش جلسة
    # حقيقية) — فحص الملكية بيتطبّق بس لما يكون فيه session_id حقيقي مبعوت.
    if req.session_id:
        u = resolve_user(authorization)
        sid = get_authorized_session(req.session_id, u, on_unknown="ignore", touch=False)
    else:
        sid = "anonymous"
    cat = "عام"
    if any(w in text for w in ["درجة","امتحان","مادة","دكتور","محاضرة","تقدير"]): cat="أكاديمي"
    elif any(w in text for w in ["مكتب","شهادة","تسجيل","قيد","وثيقة"]): cat="إداري"
    elif any(w in text for w in ["موقع","منصة","إنترنت","تطبيق","نظام"]): cat="تقني"
    pri = "عالية" if any(w in text for w in ["عاجل","مهم","ظلم","خطأ"]) else "متوسطة"
    cid = db_execute(
        "INSERT INTO complaints(session_id,category,priority,text,status,ts) VALUES(?,?,?,?,?,?)",
        (sid,cat,pri,text,"مفتوحة",datetime.now().isoformat()))
    db_log("complaint", {"id":cid,"cat":cat})
    return {"message":f"✅ تم حفظ شكواك برقم #{cid}","id":cid,"category":cat,"priority":pri}

@app.post("/feedback")
def feedback(req: FeedbackReq, authorization: Optional[str] = Header(None)):
    u = resolve_user(authorization)
    sid = get_authorized_session(req.session_id, u, on_unknown="404", touch=False)
    db_execute("INSERT INTO feedback(session_id,question,answer,rating,ts) VALUES(?,?,?,?,?)",
               (sid,req.question[:500],req.answer[:500],req.rating,datetime.now().isoformat()))
    return {"message":"شكراً على تقييمك! 🌟"}

@app.get("/conversations")
def list_conversations(user = Depends(require_auth)):
    rows = db_query_all("""
        SELECT session_id,
               MIN(CASE WHEN role='user' THEN content END) AS first_msg,
               MAX(ts) AS last_ts,
               COUNT(*) AS msg_count
        FROM messages
        WHERE user_id = ?
        GROUP BY session_id
        ORDER BY last_ts DESC
        LIMIT 50
    """, (user["id"],))
    return {"conversations": [
        {"session_id": r[0], "title": (r[1] or "محادثة جديدة")[:60], "last_ts": r[2], "message_count": r[3]}
        for r in rows
    ]}

@app.get("/history/{session_id}")
def get_history(session_id: str, authorization: Optional[str] = Header(None)):
    if not re.fullmatch(_SESSION_ID_PATTERN, session_id):
        raise HTTPException(404, "الجلسة دي مش موجودة")
    u = resolve_user(authorization)
    sid = get_authorized_session(session_id, u, on_unknown="404", touch=False)
    hist = db_hist(sid, 40)
    return {"messages": hist}

@app.get("/kb-status")
def kb_status():
    # حالة مفيدة فعليًا: هل فيه مفاتيح Pinecone/Gemini مضبوطة أصلًا (configured)،
    # هل Pinecone فعليًا متصل دلوقتي (connected)، ملف الاحتياط موجود ولا لأ،
    # وأي فرق عندها محتوى مفهرس جاهز فعلًا (ready_grades).
    return {
        "configured": bool(PINECONE_KEY and GOOGLE_KEY),
        "connected": _pine is not None,
        "fallback_available": Path(FALLBACK_DOCS_PATH).exists(),
        "ready_grades": sorted(RAG_READY_GRADES),
    }

@app.get("/health")
def health():
    # التأكد من حالة اتصال Pinecone و Groq
    return {"status":"ok","ts":datetime.now().isoformat(),
            "groq":"✅" if len(GROQ_KEYS) > 0 else "❌",
            "pinecone":"✅" if _pine else "❌","version":"3.0"}
