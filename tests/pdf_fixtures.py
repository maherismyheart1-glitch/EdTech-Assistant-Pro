"""
مولّد ملفات PDF حقيقية (بايتات صالحة: catalog + pages + content streams + جدول xref
بإزاحات صحيحة + trailer) لاستخدامها في اختبارات الرفع. مفيش أي مكتبة خارجية.
النص لازم يكون ASCII/latin-1 (خط Helvetica القياسي) — كفاية لاختبار الرفع والاستخراج
وحدود الحجم/الصفحات/الحروف.
"""

def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

def make_pdf(pages, encrypted=False) -> bytes:
    """pages: قايمة نصوص، نص لكل صفحة. بيرجّع bytes لـ PDF صالح."""
    if isinstance(pages, str):
        pages = [pages]
    n = len(pages)
    objs = {}
    font_id = 3 + 2 * n            # 1=Catalog, 2=Pages, ثم (Page, Content) لكل صفحة، وآخر واحد الخط
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>"
    for i, text in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        objs[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                         f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        # نص طويل بيتقسّم لأسطر عشان كل سطر Tj منفصل (زي ما بيعمل أي مولّد PDF)
        words, lines, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 > 80:
                lines.append(cur); cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            lines.append(cur)
        body = "BT /F1 11 Tf 14 TL 50 740 Td\n" + "\n".join(f"({_escape(l)}) Tj T*" for l in lines) + "\nET"
        objs[content_id] = f"<< /Length {len(body.encode('latin-1'))} >>\nstream\n{body}\nendstream"
    objs[font_id] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    if encrypted:
        objs[font_id + 1] = "<< /Filter /Standard /V 1 /R 2 /O (x) /U (y) /P -4 >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    total = len(objs)
    for oid in range(1, total + 1):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n{objs[oid]}\nendobj\n".encode("latin-1")
    xref_at = len(out)
    out += f"xref\n0 {total + 1}\n0000000000 65535 f \n".encode("latin-1")
    for oid in range(1, total + 1):
        out += f"{offsets[oid]:010d} 00000 n \n".encode("latin-1")
    trailer = f"<< /Size {total + 1} /Root 1 0 R"
    if encrypted:
        trailer += f" /Encrypt {total} 0 R"
    trailer += " >>"
    out += f"trailer\n{trailer}\nstartxref\n{xref_at}\n%%EOF\n".encode("latin-1")
    return bytes(out)

def truncated_pdf(pages=("some text " * 30,)) -> bytes:
    """PDF حقيقي اتقطع بعد الهيدر ومقدمة أول object (من غير صفحات ولا xref ولا trailer) —
    تالف فعليًا بحيث أي parser (حتى اللي بيحاول يصلّح الملفات) مش هيطلّع منه نص."""
    data = make_pdf(list(pages))
    return data[:70]
