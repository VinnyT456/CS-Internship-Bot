"""Resume-build microservice.

Takes a resume YAML in the husayni/yaml-resume-builder schema, renders it to
LaTeX with that library's public renderer, then compiles the LaTeX to PDF using
tectonic (a single ~50 MB binary, no 5 GB texlive). Returns the PDF bytes.

Runs OFF the bot's small Render box so the bot host needs no LaTeX at all.

Endpoints:
  GET  /health           -> {"ok": true}
  POST /build            -> body: raw YAML (text/plain or application/x-yaml)
                            optional ?one_page=1
                            200: application/pdf (the compiled resume)
                            422: invalid YAML / schema
                            500: compile failure

Auth: if BUILD_TOKEN is set, requests must send  Authorization: Bearer <token>.
"""

import os
import shutil
import subprocess
import tempfile

import yaml
from fastapi import FastAPI, Header, HTTPException, Request, Response

from yaml_resume_builder.template_renderer import render_template, validate_data

app = FastAPI(title="Resume Build Service")

BUILD_TOKEN = os.getenv("BUILD_TOKEN")
TECTONIC = os.getenv("TECTONIC_BIN", "tectonic")
COMPILE_TIMEOUT = int(os.getenv("COMPILE_TIMEOUT", "60"))
MAX_YAML_BYTES = int(os.getenv("MAX_YAML_BYTES", str(256 * 1024)))


def _check_auth(authorization: str | None) -> None:
    if not BUILD_TOKEN:
        return
    expected = f"Bearer {BUILD_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# Progressive one-page optimization levels, mirroring the upstream CLI: try
# each in order, keep the first PDF that fits on a single page.
_ONE_PAGE_LEVELS = [
    {"font_size": "11pt", "margin_reduction": 0.0, "spacing_factor": 1.0},
    {"font_size": "10pt", "margin_reduction": 0.0, "spacing_factor": 0.9},
    {"font_size": "10pt", "margin_reduction": 0.0, "spacing_factor": 0.8},
    {"font_size": "10pt", "margin_reduction": 0.1, "spacing_factor": 0.7},
    {"font_size": "10pt", "margin_reduction": 0.15, "spacing_factor": 0.6},
]


def _pdf_page_count(pdf_bytes: bytes) -> int:
    """Count pages in a PDF without extra deps (count /Type /Page objects)."""
    import re

    # Reliable enough for tectonic output: count non-Pages page objects.
    n = len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes))
    return n or 1


def _compile_with_tectonic(latex: str) -> bytes:
    """LaTeX string -> PDF bytes via tectonic. Raises HTTPException on failure."""
    if shutil.which(TECTONIC) is None:
        raise HTTPException(
            status_code=500, detail="tectonic not installed on the build host"
        )
    with tempfile.TemporaryDirectory() as tmp:
        tex_path = os.path.join(tmp, "resume.tex")
        with open(tex_path, "w", encoding="utf-8") as fh:
            fh.write(latex)
        try:
            proc = subprocess.run(
                [TECTONIC, "--outdir", tmp, "--chatter", "minimal", tex_path],
                capture_output=True,
                timeout=COMPILE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=500, detail="LaTeX compile timed out")
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace")[-800:]
            raise HTTPException(status_code=500, detail=f"LaTeX compile failed:\n{tail}")
        pdf_path = os.path.join(tmp, "resume.pdf")
        if not os.path.exists(pdf_path):
            raise HTTPException(status_code=500, detail="No PDF produced")
        with open(pdf_path, "rb") as fh:
            return fh.read()


def _build_one_page_pdf(data: dict) -> bytes:
    """Render + compile progressively tighter until the resume fits one page.
    Returns the first single-page PDF, else the tightest attempt."""
    last_pdf = None
    for params in _ONE_PAGE_LEVELS:
        try:
            latex = _sanitize_latex(render_template(data, params))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Render error: {exc}")
        pdf = _compile_with_tectonic(latex)
        last_pdf = pdf
        if _pdf_page_count(pdf) <= 1:
            return pdf
    # Nothing fit a single page — return the most compact attempt.
    return last_pdf


# pdfTeX-only primitives the template emits for ATS glyph tagging. tectonic
# (XeTeX-based) doesn't implement them and halts — strip them; they only affect
# PDF text-extraction metadata, not the visible resume.
_INCOMPATIBLE_LATEX = (
    r"\input{glyphtounicode}",
    r"\pdfgentounicode=1",
)


def _sanitize_latex(latex: str) -> str:
    """Remove pdfTeX-only lines that tectonic can't compile."""
    out = []
    for line in latex.splitlines():
        stripped = line.strip()
        if any(stripped == bad or stripped.startswith(bad) for bad in _INCOMPATIBLE_LATEX):
            continue
        out.append(line)
    return "\n".join(out)


@app.api_route("/", methods=["GET", "HEAD"])
def root():
    # Root exists so uptime monitors pinging "/" get 200, not 404.
    return {"service": "resume-build", "endpoints": ["/health", "/build"]}


# GET and HEAD both allowed — UptimeRobot defaults to HEAD, which a GET-only
# route rejects with 405.
@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True, "tectonic": shutil.which(TECTONIC) is not None}


def _scrub_placeholders(data):
    """Recursively remove any '[ADD METRIC]' placeholder from all string values,
    cleaning the surrounding 'as measured by …' phrasing so the résumé still
    reads naturally. A placeholder in the final PDF would flag as unfinished."""
    import re as _re

    token = "[ADD METRIC]"

    def clean(s):
        if token not in s:
            return s
        s = _re.sub(r"(?i)\s*,?\s*as measured by\s*\[ADD METRIC\]", "", s)
        return s.replace(token, "").replace("  ", " ").strip()

    def walk(obj):
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(v) for v in obj]
        if isinstance(obj, str):
            return clean(obj)
        return obj

    cleaned = walk(data)
    if isinstance(cleaned, dict):
        data.clear()
        data.update(cleaned)


@app.post("/build")
async def build(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=422, detail="Empty body")
    if len(raw) > MAX_YAML_BYTES:
        raise HTTPException(status_code=422, detail="YAML too large")

    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="YAML must be a mapping")

    # Safety: a "[ADD METRIC]" placeholder must NEVER reach a recruiter/ATS.
    # Scrub any that slipped through, collapsing the "as measured by …" clause.
    _scrub_placeholders(data)

    # Schema validation (logs warnings for unknown fields; raises on hard errors).
    try:
        validate_data(data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Schema error: {exc}")

    # Always fit to one page via progressive optimization.
    pdf = _build_one_page_pdf(data)
    name = str(data.get("name") or "resume").strip().replace(" ", "_")[:60] or "resume"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}.pdf"'},
    )
