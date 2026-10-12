"""
سكريبت رفع ملفات PDF لـ Pinecone — لنشر الفرقة التالتة الحالي
==============================================================

يشتغل مستقل عن التطبيق الرئيسي، تشغّله من جهازك (VS Code) مش من الـ Space.
النشر ده للفرقة التالتة بس: فيه index واحد وnamespace واحد (مُهيّأين في التطبيق
بـ PINECONE_INDEX و PINECONE_NAMESPACE). السكريبت مبيعملش أي توجيه لفرق أو
indexes تانية — لو الـ index/namespace هنا مختلفين عن اللي في التطبيق، الـ vectors
هتتكتب في مكان التطبيق مش بيقراه.

الاستخدام:
    python upload_to_pinecone.py --folder ./materials/grade3_pdfs

    # لتجربة الملفات وعدد الأجزاء من غير ما ترفع فعليًا (من غير تكلفة API):
    python upload_to_pinecone.py --folder ./materials/grade3_pdfs --dry-run

المتطلبات (نفس نسخ المكتبات المستخدمة في app.py الأساسي):
    pip install PyMuPDF==1.24.5 pinecone==3.2.2 google-genai==0.7.0

متغيرات البيئة (أو مرّرها كـ --pinecone-key / --google-key):
    PINECONE_API_KEY
    GOOGLE_API_KEY   (أو GEMINI_API_KEY)
    PINECONE_INDEX       (الافتراضي نفس افتراضي app.py)
    PINECONE_NAMESPACE   (الافتراضي فاضي "" = نفس افتراضي app.py)

الأمان:
    - بيستخدم نفس موديل الـ embedding بتاع التطبيق (models/gemini-embedding-001).
    - الـ IDs ثابتة (اسم الملف + رقم الجزء) والكتابة upsert، فإعادة التشغيل على
      نفس الملفات بتحدّث بدل ما تكرّر.
    - لو الـ namespace فيه vectors بالفعل بيطلب تأكيد قبل الإضافة (--yes لتخطّيه).
    - بيتحقق إن بُعد الـ embedding بيطابق بُعد الـ index قبل أي كتابة.
    - بيرجّع exit code غير صفر لو أي جزء فشل.
"""

import argparse
import os
import sys
import time
from pathlib import Path

# ── تقسيم حتمي: أجزاء بحجم معقول مع تداخل بسيط عشان معلومة على حافة جزء
# متتقطعش تمامًا. نفس الملف ونفس الإعدادات = نفس الأجزاء بنفس الترتيب دايمًا.
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150
EMBED_MODEL = "models/gemini-embedding-001"  # نفس الموديل المستخدم في app.py بالظبط
DEFAULT_INDEX = "edtech-db"                  # نفس افتراض app.py (PINECONE_INDEX)
BATCH_SIZE = 50


def extract_text_from_pdf(path: Path) -> str:
    import fitz  # PyMuPDF
    doc = fitz.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    text = " ".join(text.split())  # توحيد المسافات
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


def stable_id(filename: str, chunk_idx: int) -> str:
    """ID ثابت لكل جزء — يخلي إعادة تشغيل السكريبت على نفس الملفات آمنة
    (upsert بيحدّث بدل ما يكرر). متغيّرش الصيغة دي: الـ vectors القديمة في
    الـ index متسجّلة بيها."""
    safe = "".join(c if c.isalnum() else "_" for c in filename)
    return f"{safe}_{chunk_idx}"


def collect_chunks(pdfs):
    """بيرجّع ([(filename, idx, text)], عدد الملفات اللي فشلت/فاضية)."""
    all_chunks, skipped = [], 0
    for pdf in pdfs:
        try:
            text = extract_text_from_pdf(pdf)
        except Exception as e:
            print(f"  ⚠️  فشل قراءة {pdf.name}: {type(e).__name__}")
            skipped += 1
            continue
        chunks = chunk_text(text)
        if not chunks:
            print(f"  ⚠️  {pdf.name}: مفيش نص قابل للاستخراج (ممكن يكون ممسوح ضوئيًا) — اتخطّى")
            skipped += 1
            continue
        print(f"  📄 {pdf.name}: {len(text)} حرف → {len(chunks)} جزء")
        all_chunks.extend((pdf.name, i, c) for i, c in enumerate(chunks))
    return all_chunks, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="رفع ملفات PDF لـ Pinecone (نشر الفرقة التالتة)")
    ap.add_argument("--folder", type=str, required=True, help="المجلد اللي فيه ملفات الـ PDF")
    ap.add_argument("--pinecone-key", type=str, default=os.environ.get("PINECONE_API_KEY"))
    ap.add_argument("--google-key", type=str,
                     default=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    ap.add_argument("--index-name", type=str, default=os.environ.get("PINECONE_INDEX", DEFAULT_INDEX),
                     help="لازم يطابق PINECONE_INDEX في التطبيق الرئيسي")
    ap.add_argument("--namespace", type=str, default=os.environ.get("PINECONE_NAMESPACE", ""),
                     help='لازم يطابق PINECONE_NAMESPACE في التطبيق (الافتراضي فاضي "")')
    ap.add_argument("--app-grade", type=int, default=int(os.environ.get("APP_GRADE", "3")),
                     help="بيتسجّل في metadata كل جزء فقط (الافتراضي APP_GRADE أو 3)")
    ap.add_argument("--dry-run", action="store_true",
                     help="يعرض الملفات وعدد الأجزاء من غير رفع فعلي أو استدعاء أي API")
    ap.add_argument("--yes", action="store_true", help="تخطّي سؤال التأكيد لو الـ namespace فيه محتوى بالفعل")
    args = ap.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"❌ المجلد مش موجود: {folder}")
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"❌ مفيش ملفات PDF في: {folder}")

    print(f"📁 لقيت {len(pdfs)} ملف PDF")
    print(f"📦 Index: {args.index_name}   |   Namespace: {args.namespace!r}   |   Embedding: {EMBED_MODEL}\n")

    # المعالجة والتقسيم بيحصلوا دايمًا (حتى في --dry-run) عشان تشوف الأرقام الحقيقية
    all_chunks, skipped = collect_chunks(pdfs)
    print(f"\n📊 الإجمالي: {len(all_chunks)} جزء من {len(pdfs) - skipped} ملف صالح ({skipped} اتخطّى)")
    if not all_chunks:
        sys.exit("❌ مفيش أي جزء صالح للرفع.")

    if args.dry_run:
        print("\n✅ (dry-run) معملناش أي استدعاء API ولا رفع فعلي. شيل --dry-run لما تكون جاهز.")
        return 0

    if not args.pinecone_key or not args.google_key:
        sys.exit("\n❌ محتاج PINECONE_API_KEY و GOOGLE_API_KEY (أو GEMINI_API_KEY) — "
                  "كمتغيرات بيئة أو --pinecone-key / --google-key")

    from pinecone import Pinecone
    from google import genai

    pc = Pinecone(api_key=args.pinecone_key)
    index = pc.Index(args.index_name)
    gcli = genai.Client(api_key=args.google_key)

    # ── تحقق قبل أي كتابة: بُعد الـ embedding = بُعد الـ index، وتأكيد لو فيه محتوى ──
    stats = index.describe_index_stats()
    index_dim = getattr(stats, "dimension", None)
    probe = gcli.models.embed_content(model=EMBED_MODEL, contents=all_chunks[0][2]).embeddings[0].values
    if index_dim and len(probe) != index_dim:
        sys.exit(f"❌ بُعد الـ embedding ({len(probe)}) مش مطابق لبُعد الـ index ({index_dim}) — "
                  f"الـ index ده مش متوافق مع {EMBED_MODEL}. مفيش حاجة اترفعت.")
    ns_stats = (getattr(stats, "namespaces", None) or {}).get(args.namespace)
    existing = getattr(ns_stats, "vector_count", 0) if ns_stats is not None else 0
    if existing and not args.yes:
        confirm = input(f"⚠️  الـ namespace {args.namespace!r} في index '{args.index_name}' فيه {existing} vector بالفعل. "
                         f"الأجزاء الجديدة هتتضاف/تتحدّث عليهم (مش هتستبدلهم كلهم). تكمّل؟ (y/n): ")
        if confirm.strip().lower() != "y":
            sys.exit("تم الإلغاء.")

    print(f"\n🚀 بدء الرفع لـ index={args.index_name!r} namespace={args.namespace!r} ...\n")
    uploaded, failed, batch = 0, 0, []
    for n, (filename, idx, chunk) in enumerate(all_chunks):
        try:
            emb = probe if n == 0 else gcli.models.embed_content(model=EMBED_MODEL, contents=chunk).embeddings[0].values
            batch.append({
                "id": stable_id(filename, idx),
                "values": emb,
                "metadata": {"text": chunk, "source": filename, "grade": args.app_grade, "chunk": idx},
            })
        except Exception as e:
            print(f"  ⚠️  فشل embedding لـ {filename} جزء {idx}: {type(e).__name__}")
            failed += 1
            continue

        if len(batch) >= BATCH_SIZE:
            index.upsert(vectors=batch, namespace=args.namespace)
            uploaded += len(batch)
            print(f"  ✅ اترفع {uploaded}/{len(all_chunks)}")
            batch = []
            time.sleep(0.3)  # مساحة بسيطة لتجنب rate limits

    if batch:
        index.upsert(vectors=batch, namespace=args.namespace)
        uploaded += len(batch)

    print(f"\n🎉 خلصنا: {uploaded} جزء اترفع بنجاح، {failed} فشل.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
