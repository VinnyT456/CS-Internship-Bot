"""Resume upload pipeline: validate a PDF, rasterize it to a single stacked
PNG (what the vision model reads), and store both in the Resumes bucket.

Rendering uses PyMuPDF (fitz) — pure-Python, no poppler system dep — to turn
each page into a pixmap, then PIL to stack the pages vertically into one image.
"""

import io
import logging

logger = logging.getLogger("cs_internship_bot")

BUCKET = "Resumes"
RENDER_DPI = 150          # crisp enough for a vision model, small enough to store
MAX_PDF_BYTES = 8 * 1024 * 1024  # 8 MB cap — resumes are small


class ResumeError(Exception):
    """User-facing problem with the upload (bad file, too big, render failed)."""


def _render_pdf_to_png(pdf_bytes: bytes) -> bytes:
    """PDF bytes -> a single PNG (all pages stacked vertically) as bytes."""
    # Imported lazily so PyMuPDF + Pillow (~50 MB resident) only load when a
    # resume is actually rendered, not at module import on the always-on box.
    import fitz  # PyMuPDF
    from PIL import Image

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — surface any fitz failure as user error
        raise ResumeError("That doesn't look like a valid PDF.") from exc

    if doc.page_count == 0:
        raise ResumeError("That PDF has no pages.")

    zoom = RENDER_DPI / 72  # fitz default is 72 dpi
    matrix = fitz.Matrix(zoom, zoom)

    pages = []
    try:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pages.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    finally:
        doc.close()

    if len(pages) == 1:
        stacked = pages[0]
    else:
        width = max(p.width for p in pages)
        height = sum(p.height for p in pages)
        stacked = Image.new("RGB", (width, height), "white")
        y = 0
        for p in pages:
            stacked.paste(p, (0, y))
            y += p.height

    out = io.BytesIO()
    # Drop optimize=True (an extra exhaustive zlib pass) but keep the default
    # compression level: level 1 is only ~10ms faster yet more than doubles the
    # PNG size, which then costs MORE on every upload + later download. Default
    # (~6) is the fast-enough / small-enough sweet spot.
    stacked.save(out, format="PNG")
    for p in pages:
        p.close()
    return out.getvalue()


# Below this many extracted chars we treat the PDF as image-only (a scan or an
# export-as-image) — get_text() found no real text layer.
_MIN_TEXT_CHARS = 60


def extract_text_from_pdf(pdf_bytes: bytes) -> tuple[str, str]:
    """Extract the resume's text MECHANICALLY from the PDF via PyMuPDF —
    deterministic, no AI, no hallucination. This is the source of truth every
    downstream AI command reads from.

    Returns (text, source):
      - ("<real text>", "pdf")   normal text-based PDF (the common case)
      - ("", "image_only")       no text layer (scan / image export) — caller
                                  should vision-OCR as a fallback and warn the user
      - ("", "error")            couldn't open the PDF at all
    """
    import fitz  # PyMuPDF (lazy — heavy import)

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        logger.exception("extract_text_from_pdf: fitz open failed")
        return "", "error"
    try:
        parts = []
        for page in doc:
            # "text" mode = reading-order plain text; no layout coords, no AI.
            parts.append(page.get_text("text"))
    finally:
        doc.close()
    text = "\n".join(parts).strip()
    if len(text) < _MIN_TEXT_CHARS:
        return "", "image_only"
    return text, "pdf"


def process_and_store(db, user_uuid, pdf_bytes: bytes, original_filename: str) -> dict:
    """Validate + render + upload, then upsert the resumes row. Returns the row.
    Raises ResumeError for anything the user can fix."""
    if not pdf_bytes:
        raise ResumeError("The file was empty.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ResumeError("That PDF is over 8 MB — please upload a smaller file.")
    if not pdf_bytes[:5].startswith(b"%PDF"):
        raise ResumeError("Only PDF files are accepted.")

    image_bytes = _render_pdf_to_png(pdf_bytes)

    # One file pair per user; a stable path means re-upload overwrites.
    pdf_path = f"{user_uuid}/resume.pdf"
    image_path = f"{user_uuid}/resume.png"

    storage = db.supabase.storage.from_(BUCKET)
    # Upload the PDF and the rendered PNG concurrently — they're independent
    # network round-trips, so running them in parallel roughly halves the
    # storage wait on the upload path.
    from concurrent.futures import ThreadPoolExecutor

    def _up(path, data, ctype):
        storage.upload(path, data, {"content-type": ctype, "upsert": "true"})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [
                pool.submit(_up, pdf_path, pdf_bytes, "application/pdf"),
                pool.submit(_up, image_path, image_bytes, "image/png"),
            ]
            for f in futs:
                f.result()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Resume upload to storage failed")
        raise ResumeError("Couldn't store your resume — try again in a moment.") from exc

    # Extract text MECHANICALLY at upload (no AI) — the source of truth for every
    # downstream analysis. Empty => image-only PDF; the analyze flow will vision-OCR
    # lazily and warn the user to upload a text-based PDF.
    text, source = extract_text_from_pdf(pdf_bytes)

    row = {
        "user_id": user_uuid,
        "pdf_path": pdf_path,
        "image_path": image_path,
        "original_filename": original_filename[:255],
        "extracted_text": text or None,
    }
    db.supabase.table("resumes").upsert(row, on_conflict="user_id").execute()
    row["_text_source"] = source  # transient hint for the caller (not persisted)
    return row


def get_resume(db, user_uuid) -> dict | None:
    """The user's current resume row, or None."""
    try:
        data = (
            db.supabase.table("resumes")
            .select("*")
            .eq("user_id", user_uuid)
            .limit(1)
            .execute()
            .data
        )
        return data[0] if data else None
    except Exception:
        logger.exception("Failed fetching resume for %s", user_uuid)
        return None


def delete_resume(db, user_uuid) -> bool:
    """Remove the user's resume row + its stored files. True if something was
    deleted."""
    row = get_resume(db, user_uuid)
    if not row:
        return False
    try:
        db.supabase.storage.from_(BUCKET).remove(
            [row["pdf_path"], row["image_path"]]
        )
    except Exception:
        logger.exception("Failed removing resume files for %s", user_uuid)
    try:
        db.supabase.table("resumes").delete().eq("user_id", user_uuid).execute()
    except Exception:
        logger.exception("Failed deleting resume row for %s", user_uuid)
        return False
    return True


def image_bytes(db, user_uuid) -> bytes | None:
    """Download the rendered resume PNG — this is what the vision model reads."""
    row = get_resume(db, user_uuid)
    if not row:
        return None
    try:
        return db.supabase.storage.from_(BUCKET).download(row["image_path"])
    except Exception:
        logger.exception("Failed downloading resume image for %s", user_uuid)
        return None


# --- One-time text extraction (so AI commands can run text-only) -----------

_EXTRACT_PROMPT = (
    "Transcribe this resume into clean, complete plain text, VERBATIM. Preserve "
    "every section (contact, education, experience, projects, skills, "
    "achievements, publications, certifications) with all bullets, dates, "
    "numbers, links, and details exactly as written, in reading order. Do NOT "
    "summarize, rephrase, add, or omit anything — copy the real content only. "
    "Output the resume text only, no commentary, no markdown fences."
)


def extract_text(image_png: bytes) -> str | None:
    """FALLBACK ONLY: vision-transcribe the rendered PNG to text. Used just for
    image-only PDFs (no text layer) where mechanical extraction returns nothing.
    Vision can misread, so text-based PDFs must go through extract_text_from_pdf
    instead — that's the no-hallucination path."""
    from commands import gemma_client

    text = gemma_client.ask_with_image(
        image_png, _EXTRACT_PROMPT, chain=gemma_client.FAST_CHAIN
    )
    return (text or "").strip() or None


def resolve_resume_text(db, user_uuid):
    """The authoritative resume text for a user, plus how it was obtained.
    Returns (text, source):
      - ("<text>", "pdf")        mechanical extraction (trusted, no AI)
      - ("<text>", "ocr")        image-only PDF, vision-OCR fallback (may misread)
      - (None, "none")           no resume / couldn't read it
    Prefers the stored mechanical text; only OCRs when there's no text layer."""
    stored = get_resume_text(db, user_uuid)
    if stored:
        return stored, "pdf"
    # No mechanical text — likely an image-only PDF. OCR the PNG as a fallback.
    img = image_bytes(db, user_uuid)
    if not img:
        return None, "none"
    ocr = extract_text(img)
    if ocr:
        store_text(db, user_uuid, ocr)  # cache it so we OCR once
        return ocr, "ocr"
    return None, "none"


def get_resume_text(db, user_uuid) -> str | None:
    """The stored resume text (from the one-time extraction), or None."""
    row = get_resume(db, user_uuid)
    if not row:
        return None
    return (row.get("extracted_text") or "").strip() or None


def store_text(db, user_uuid, text: str) -> None:
    """Persist the extracted resume text. Best-effort."""
    try:
        db.supabase.table("resumes").update({"extracted_text": text}).eq(
            "user_id", user_uuid
        ).execute()
    except Exception:
        logger.exception("Failed storing resume text for %s", user_uuid)


# --- Structured resume (builder schema) for fast bullet-only tailoring -------

_STRUCTURE_PROMPT = (
    "Parse this resume into ONE JSON OBJECT with the exact keys below, using "
    "ONLY what the resume actually shows — copy every value verbatim, invent "
    "nothing, leave unknown fields as empty strings/lists.\n"
    "The top-level output MUST be a single JSON object that starts with '{' and "
    "ends with '}'. Do NOT return a JSON array/list at the top level.\n"
    "{\n"
    '  "name": "", "contact": {"phone": "", "email": "", "linkedin": "", "github": ""},\n'
    '  "education": [{"school": "", "location": "", "degree": "", "dates": "", "gpa": "", "coursework": [""]}],\n'
    '  "experience": [{"company": "", "role": "", "location": "", "dates": "", "description": [""]}],\n'
    '  "projects": [{"name": "", "technologies": [""], "date": "", "link": "", "description": [""]}],\n'
    '  "skills": [{"category": "", "list": [""]}],\n'
    '  "achievements": [""], "publications": [""],\n'
    '  "certifications": [{"name": "", "link": ""}]\n'
    "}\n"
    "Preserve bullet wording exactly. Return ONLY the JSON object."
)


def parse_structured(text=None, img=None) -> dict | None:
    """Parse a resume into the builder's structured schema (JSON) from text
    (preferred) or image. Returns a dict with at least a name/experience shape,
    or None. Safe to call off-thread."""
    from commands import gemma_client

    # Mechanical parse, big output — FAST tier (Flash-lite), generous headroom.
    if text:
        data = gemma_client.ask_json_text(
            f"{_STRUCTURE_PROMPT}\n\n<resume>\n{text}\n</resume>", 6000,
            chain=gemma_client.FAST_CHAIN,
        )
    elif img:
        data = gemma_client.ask_json_with_image(
            img, _STRUCTURE_PROMPT, 6000, chain=gemma_client.FAST_CHAIN
        )
    else:
        return None

    data = _coerce_resume_object(data)
    if not isinstance(data, dict):
        logger.warning("parse_structured: could not coerce to object")
        return None
    if not any(k in data for k in ("name", "experience", "projects", "skills")):
        logger.warning("parse_structured: JSON missing expected resume keys")
        return None
    return data


_RESUME_KEYS = {
    "name", "contact", "education", "experience", "projects",
    "skills", "achievements", "publications", "certifications",
}


def _coerce_resume_object(data):
    """Gemma sometimes wraps the resume object in a list, or returns the sections
    as separate single-key dicts in a list. Normalize those back to one object."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        # Case 1: [ {full resume object} ]
        dicts = [d for d in data if isinstance(d, dict)]
        for d in dicts:
            if len(set(d) & _RESUME_KEYS) >= 2:
                return d
        # Case 2: [ {"experience":[...]}, {"skills":[...]}, ... ] -> merge.
        merged = {}
        for d in dicts:
            for k, v in d.items():
                if k in _RESUME_KEYS:
                    merged[k] = v
        if merged:
            return merged
    return None


def get_structured(db, user_uuid) -> dict | None:
    """The stored structured resume (builder schema), or None."""
    row = get_resume(db, user_uuid)
    if not row:
        return None
    s = row.get("structured_json")
    return s if isinstance(s, dict) else None


def store_structured(db, user_uuid, structured: dict) -> None:
    """Persist the structured resume. Best-effort."""
    try:
        db.supabase.table("resumes").update({"structured_json": structured}).eq(
            "user_id", user_uuid
        ).execute()
    except Exception:
        logger.exception("Failed storing structured resume for %s", user_uuid)
