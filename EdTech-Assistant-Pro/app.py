"""
المعيد الذكي — كلية التربية النوعية، جامعة الزقازيق
قسم تكنولوجيا التعليم والحاسب الآلي
Engineered by: Abdelrahman Essam
"""
import os, json, uuid, logging, sqlite3, tempfile
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from groq import Groq

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("app.log",encoding="utf-8"), logging.StreamHandler()])
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

def get_groq_client():
    if not GROQ_KEYS:
        return None
    # اختيار مفتاح عشوائي في كل طلب لتوزيع الضغط (Load Balancing)
    return Groq(api_key=random.choice(GROQ_KEYS))

GOOGLE_KEY   = os.environ.get("GOOGLE_API_KEY","") or os.environ.get("GEMINI_API_KEY","")
PINECONE_KEY = os.environ.get("PINECONE_API_KEY","")
PINECONE_IDX = os.environ.get("PINECONE_INDEX","edtech-db")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET","admin_2025")

# ── Groq LLM Logic ────────────────────────────────
SYSTEM_PROMPT = """أنت "المعيد الذكي" — مساعد أكاديمي ذكي لطلاب قسم تكنولوجيا التعليم والحاسب الآلي،
كلية التربية النوعية، جامعة الزقازيق.
- أجب بالعربية الفصحى البسيطة دائماً
- اعتمد على محتوى المنهج المقدَّم أولاً
- كن دقيقاً، واضحاً، وأكاديمياً في ردودك"""

def llm(msgs, max_tokens=1500, temp=0.5):
    client = get_groq_client()
    if not client: raise RuntimeError("لا يوجد مفاتيح Groq صالحة في النظام")
    r = client.chat.completions.create(
        model="llama-3.3-70b-versatile", messages=msgs,
        max_tokens=max_tokens, temperature=temp)
    return r.choices[0].message.content.strip()

def llm_stream(msgs, max_tokens=1500):
    client = get_groq_client()
    if not client:
        yield "⚠️ عذراً، لا يوجد مفاتيح Groq صالحة للاتصال."; return
    for chunk in client.chat.completions.create(
        model="llama-3.3-70b-versatile", messages=msgs,
        max_tokens=max_tokens, temperature=0.5, stream=True):
        t = chunk.choices[0].delta.content
        if t: yield t

def sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def stream_llm_payload(msgs, sid, event, max_tokens=1500, log_data=None, save_chat=False):
    buf = []
    def gen():
        try:
            for t in llm_stream(msgs, max_tokens=max_tokens):
                buf.append(t)
                yield sse({"token": t, "session_id": sid})
        except Exception as e:
            log.error(f"Stream {event}: {e}")
            t = "⚠️ حدث خطأ في الاتصال بالذكاء الاصطناعي، حاول مرة أخرى."
            buf.append(t)
            yield sse({"token": t, "session_id": sid, "error": True})

        answer = "".join(buf)
        if save_chat and answer:
            db_msg(sid, "assistant", answer)
        payload = {"session": sid}
        if log_data:
            payload.update(log_data)
        db_log(event, payload)
        yield sse({"done": True, "session_id": sid})
    return StreamingResponse(gen(), media_type="text/event-stream")

# ── RAG (Pinecone + Gemini) ───────────────────────
_pine = None
def _fallback_rag(q):
    f = Path("data/docs.txt")
    if not f.exists(): return ""
    words = q.lower().split()
    paras = [p for p in f.read_text(encoding="utf-8").split("\n\n")
             if any(w in p.lower() for w in words)]
    return "\n\n".join(paras[:5])

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
        def rag_search(q, top_k=5, thr=0.55):
            try:
                hits = _pine.query(vector=_embed(q), top_k=top_k, include_metadata=True)
                good = [m for m in hits.matches if m.score >= thr]
                if not good: return _fallback_rag(q)
                return "\n\n".join(f"[{m.score:.0%}] {m.metadata.get('text','')}" for m in good)
            except Exception as e:
                log.warning(f"Pinecone: {e}"); return _fallback_rag(q)
        log.info(f"✅ Pinecone ({PINECONE_IDX}) جاهز")
    except Exception as e:
        log.warning(f"Pinecone error: {e}"); _pine = None

if _pine is None:
    def rag_search(q, top_k=5, thr=0.55): return _fallback_rag(q)
    log.info("ℹ️ RAG fallback → docs.txt")

# ── DB (SQLite) ───────────────────────────────────
DB = sqlite3.connect("app_data.db", check_same_thread=False)
DB.executescript("""
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, role TEXT, content TEXT, ts TEXT);
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
""")
DB.commit()

def db_msg(sid, role, content):
    DB.execute("INSERT INTO messages(session_id,role,content,ts) VALUES(?,?,?,?)",
               (sid, role, content[:3000], datetime.now().isoformat()))
    DB.commit()

def db_hist(sid, limit=20):
    rows = DB.execute(
        "SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (sid, limit)).fetchall()
    return [{"role":r,"content":c} for r,c in reversed(rows)]

def db_log(event, data={}):
    DB.execute("INSERT INTO analytics(event,data,ts) VALUES(?,?,?)",
               (event, json.dumps(data,ensure_ascii=False), datetime.now().isoformat()))
    DB.commit()

# ── PDF Extraction ────────────────────────────────
def extract_pdf(path, max_chars=8000):
    try:
        doc = fitz.open(path)
        pages = []
        for p in doc:
            t = p.get_text("text")
            if len(t.strip()) < 20:
                t = " ".join(w[4] for w in p.get_text("words"))
            pages.append(t)
        doc.close()
        return "\n".join(pages).strip()[:max_chars]
    except Exception as e:
        log.error(f"PDF: {e}"); return ""

_pdf_cache: dict = {}

# ── FastAPI App ───────────────────────────────────
app = FastAPI(title="المعيد الذكي", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
Path("static").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Models ────────────────────────────────────────
class ChatReq(BaseModel):
    message: str; session_id: Optional[str] = None

class ComplaintReq(BaseModel):
    text: str; session_id: Optional[str] = None

class FeedbackReq(BaseModel):
    session_id: str; question: str; answer: str; rating: str

class AdminReq(BaseModel):
    secret: str; action: str

class ExamReq(BaseModel):
    topic: str; session_id: Optional[str] = None
    num_mcq: int = 5; num_essay: int = 2

class GradeReq(BaseModel):
    answer: str; question: Optional[str] = None
    session_id: Optional[str] = None

class SummaryReq(BaseModel):
    session_id: str

# ── Routes ────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home():
    p = Path("frontend/index.html")
    return HTMLResponse(p.read_text(encoding="utf-8") if p.exists()
                        else "<h1>المعيد الذكي ✅</h1>")

@app.post("/chat")
def chat(req: ChatReq):
    sid = req.session_id or str(uuid.uuid4())
    msg = req.message.strip()
    if not msg: raise HTTPException(400, "الرسالة فارغة")
    db_msg(sid, "user", msg)
    rag = _pdf_cache.get(sid,"") or rag_search(msg)
    ctx = f"\n\n[من قاعدة المعرفة]:\n{rag}" if rag else ""
    hist = db_hist(sid, 18)[:-1]
    msgs = ([{"role":"system","content": SYSTEM_PROMPT+ctx}]
            + [{"role":m["role"],"content":m["content"]} for m in hist]
            + [{"role":"user","content":msg}])
    try:
        ans = llm(msgs)
    except Exception as e:
        log.error(f"LLM: {e}"); ans = "⚠️ حدث خطأ في النظام، حاول مرة أخرى."
    db_msg(sid, "assistant", ans)
    db_log("chat", {"session":sid,"rag":bool(rag)})
    return {"answer": ans, "session_id": sid, "rag_used": bool(rag)}

@app.post("/chat/stream")
async def chat_stream(req: ChatReq):
    sid = req.session_id or str(uuid.uuid4())
    msg = req.message.strip()
    if not msg: raise HTTPException(400, "الرسالة فارغة")
    db_msg(sid, "user", msg)
    rag = _pdf_cache.get(sid,"") or rag_search(msg)
    ctx = f"\n\n[من قاعدة المعرفة]:\n{rag}" if rag else ""
    hist = db_hist(sid, 18)[:-1]
    msgs = ([{"role":"system","content": SYSTEM_PROMPT+ctx}]
            + [{"role":m["role"],"content":m["content"]} for m in hist]
            + [{"role":"user","content":msg}])
    return stream_llm_payload(
        msgs, sid, "stream", max_tokens=1500,
        log_data={"rag": bool(rag)}, save_chat=True
    )

@app.post("/exam/stream")
def exam_stream(req: ExamReq):
    sid = req.session_id or str(uuid.uuid4())
    topic = req.topic.strip() or "تكنولوجيا التعليم"
    rag = rag_search(topic, top_k=6)
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
        log_data={"topic": topic, "rag": bool(rag)}
    )

@app.post("/exam")
def exam(req: ExamReq):
    sid   = req.session_id or str(uuid.uuid4())
    topic = req.topic.strip() or "تكنولوجيا التعليم"
    rag   = rag_search(topic, top_k=6)
    ctx   = f"\n\n[من المنهج]:\n{rag}" if rag else ""
    prompt = f"""أنشئ امتحاناً أكاديمياً شاملاً عن: "{topic}"{ctx}

**أولاً: اختيار من متعدد ({req.num_mcq} أسئلة)**
لكل سؤال: السؤال + 4 خيارات (أ ب ج د) + ✅ الإجابة الصحيحة

**ثانياً: أسئلة مقالية ({req.num_essay} أسئلة)**
مع نموذج إجابة مختصر لكل سؤال

**ثالثاً: سؤال تطبيقي**
سؤال عملي مع تعليمات واضحة"""
    try:
        result = llm([{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":prompt}], max_tokens=2000)
    except Exception as e: result = f"⚠️ {e}"
    db_log("exam", {"topic":topic,"session":sid})
    return {"exam":result,"session_id":sid,"topic":topic}

@app.post("/grade/stream")
def grade_stream(req: GradeReq):
    sid = req.session_id or str(uuid.uuid4())
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
def grade(req: GradeReq):
    sid  = req.session_id or str(uuid.uuid4())
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
    try:
        result = llm([{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":prompt}], max_tokens=1200)
    except Exception as e: result = f"⚠️ {e}"
    db_log("grade", {"session":sid})
    return {"result":result,"session_id":sid}

@app.post("/summary/stream")
def summary_stream(req: SummaryReq):
    ctx = _pdf_cache.get(req.session_id,"")
    if not ctx: raise HTTPException(400, "لم يتم رفع ملف لهذه الجلسة")
    prompt = f"""لخّص هذا المحتوى التعليمي تلخيصاً أكاديمياً منظماً:

{ctx}

يشمل: الموضوع، أهم المفاهيم، النقاط الجوهرية، وخلاصة عملية للمذاكرة."""
    msgs = [{"role":"system","content":SYSTEM_PROMPT},
            {"role":"user","content":prompt}]
    return stream_llm_payload(
        msgs, req.session_id, "summary", max_tokens=1500,
        log_data={"chars": len(ctx)}
    )

@app.post("/summary")
def summary(req: SummaryReq):
    ctx = _pdf_cache.get(req.session_id,"")
    if not ctx: raise HTTPException(400, "لم يتم رفع ملف لهذه الجلسة")
    prompt = f"""لخّص هذا المحتوى التعليمي تلخيصاً أكاديمياً منظماً:

{ctx}

يشمل: الموضوع + أهم المفاهيم + النقاط الجوهرية + خلاصة"""
    try:
        result = llm([{"role":"system","content":SYSTEM_PROMPT},
                      {"role":"user","content":prompt}], max_tokens=1500)
    except Exception as e: result = f"⚠️ {e}"
    return {"summary":result,"session_id":req.session_id}

@app.post("/upload-pdf")
async def upload_pdf(session_id: str, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "يجب رفع ملف PDF")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await file.read()); path = tmp.name
    text = extract_pdf(path)
    Path(path).unlink(missing_ok=True)
    if not text or len(text) < 50:
        raise HTTPException(422, "لم يُستخرج نص كافٍ من الملف")
    _pdf_cache[session_id] = text
    db_log("pdf", {"session":session_id,"chars":len(text),"file":file.filename})
    return {"message":f"✅ تم قراءة الملف ({len(text):,} حرف)",
            "session_id":session_id,"chars":len(text)}

@app.post("/complaint")
def complaint(req: ComplaintReq):
    text = req.text.strip()
    sid  = req.session_id or "anonymous"
    if not text: raise HTTPException(400, "نص الشكوى فارغ")
    cat = "عام"
    if any(w in text for w in ["درجة","امتحان","مادة","دكتور","محاضرة","تقدير"]): cat="أكاديمي"
    elif any(w in text for w in ["مكتب","شهادة","تسجيل","قيد","وثيقة"]): cat="إداري"
    elif any(w in text for w in ["موقع","منصة","إنترنت","تطبيق","نظام"]): cat="تقني"
    pri = "عالية" if any(w in text for w in ["عاجل","مهم","ظلم","خطأ"]) else "متوسطة"
    cur = DB.cursor()
    cur.execute("INSERT INTO complaints(session_id,category,priority,text,status,ts) VALUES(?,?,?,?,?,?)",
                (sid,cat,pri,text,"مفتوحة",datetime.now().isoformat()))
    DB.commit(); cid = cur.lastrowid
    db_log("complaint", {"id":cid,"cat":cat})
    return {"message":f"✅ تم حفظ شكواك برقم #{cid}","id":cid,"category":cat,"priority":pri}

@app.post("/feedback")
def feedback(req: FeedbackReq):
    DB.execute("INSERT INTO feedback(session_id,question,answer,rating,ts) VALUES(?,?,?,?,?)",
               (req.session_id,req.question[:500],req.answer[:500],req.rating,datetime.now().isoformat()))
    DB.commit()
    return {"message":"شكراً على تقييمك! 🌟"}

@app.get("/history/{session_id}")
def get_history(session_id: str):
    hist = db_hist(session_id, 40)
    return {"messages": hist}

@app.post("/admin")
def admin(req: AdminReq):
    if req.secret != ADMIN_SECRET: raise HTTPException(403,"كلمة السر غلط")
    if req.action == "stats":
        tm  = DB.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        ses = DB.execute("SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0]
        tc  = DB.execute("SELECT COUNT(*) FROM complaints").fetchone()[0]
        oc  = DB.execute("SELECT COUNT(*) FROM complaints WHERE status='مفتوحة'").fetchone()[0]
        tf  = DB.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        gf  = DB.execute("SELECT COUNT(*) FROM feedback WHERE rating='good'").fetchone()[0]
        return {"messages":tm,"sessions":ses,"complaints_total":tc,"complaints_open":oc,
                "feedback_total":tf,"satisfaction":f"{int(gf/tf*100)}%" if tf else "—",
                "pinecone":"✅ متصل" if _pine else "❌ غير متصل"}
    if req.action == "complaints":
        rows = DB.execute("SELECT id,category,priority,status,text,ts FROM complaints ORDER BY id DESC LIMIT 100").fetchall()
        return {"complaints":[{"id":r[0],"category":r[1],"priority":r[2],"status":r[3],"text":r[4],"ts":r[5]} for r in rows]}
    if req.action == "feedback":
        rows = DB.execute("SELECT session_id,rating,question,ts FROM feedback ORDER BY id DESC LIMIT 100").fetchall()
        return {"feedback":[{"session":r[0],"rating":r[1],"question":r[2],"ts":r[3]} for r in rows]}
    if req.action == "messages":
        rows = DB.execute("SELECT session_id,role,content,ts FROM messages ORDER BY id DESC LIMIT 200").fetchall()
        return {"messages":[{"session":r[0],"role":r[1],"content":r[2][:200],"ts":r[3]} for r in rows]}
    raise HTTPException(400, f"أمر غير معروف: {req.action}")

@app.get("/health")
def health():
    # التأكد من حالة اتصال Pinecone و Groq
    return {"status":"ok","ts":datetime.now().isoformat(),
            "groq":"✅" if len(GROQ_KEYS) > 0 else "❌",
            "pinecone":"✅" if _pine else "❌","version":"3.0"}
