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
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response

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


@app.get("/health")
def health():
    return {"ok": True, "tectonic": shutil.which(TECTONIC) is not None}


@app.post("/build")
async def build(
    request: Request,
    one_page: int = Query(0),
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

    # Schema validation (logs warnings for unknown fields; raises on hard errors).
    try:
        validate_data(data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Schema error: {exc}")

    params = {"one_page": True} if one_page else None
    try:
        latex = render_template(data, params)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Render error: {exc}")

    pdf = _compile_with_tectonic(latex)
    name = str(data.get("name") or "resume").strip().replace(" ", "_")[:60] or "resume"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}.pdf"'},
    )
