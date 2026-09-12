import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pypdf import PdfReader

APP_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = APP_DIR / "generated"
OUTPUT_DIR.mkdir(exist_ok=True)
TECTONIC = APP_DIR / ".bin" / "tectonic"

API_KEY = os.environ.get("API_KEY", "")
MAX_SOURCE_BYTES = int(os.environ.get("MAX_SOURCE_BYTES", "1000000"))
COMPILE_TIMEOUT = int(os.environ.get("COMPILE_TIMEOUT", "120"))
FILE_TTL_SECONDS = int(os.environ.get("FILE_TTL_SECONDS", "86400"))

app = FastAPI(title="UniGPT Free LaTeX Compiler", version="1.1.0")


class CompileRequest(BaseModel):
    source: str = Field(..., description="Complete standalone LaTeX source")
    filename: str = Field(default="document", description="Base filename without extension")


class CompileResponse(BaseModel):
    success: bool
    job_id: str | None = None
    pdf_url: str | None = None
    tex_url: str | None = None
    pages: int | None = None
    log: str = ""
    error: str | None = None


def require_key(x_api_key: str | None):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server API_KEY is not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def cleanup_old_files():
    cutoff = time.time() - FILE_TTL_SECONDS
    for path in OUTPUT_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def safe_basename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))[:80]
    return cleaned or "document"


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "tectonic",
        "tectonic": TECTONIC.exists() and os.access(TECTONIC, os.X_OK),
    }


@app.post("/compile", response_model=CompileResponse)
def compile_latex(
    payload: CompileRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_key(x_api_key)
    cleanup_old_files()

    if not TECTONIC.exists():
        raise HTTPException(status_code=500, detail="Tectonic compiler is not installed")

    source_bytes = payload.source.encode("utf-8")
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"LaTeX source exceeds {MAX_SOURCE_BYTES} bytes")

    job_id = str(uuid.uuid4())
    base = safe_basename(payload.filename)

    with tempfile.TemporaryDirectory(prefix="latexjob_") as tmp:
        workdir = Path(tmp)
        tex_path = workdir / f"{base}.tex"
        tex_path.write_text(payload.source, encoding="utf-8")

        cmd = [
            str(TECTONIC),
            "--untrusted",
            "--keep-logs",
            "--outdir",
            str(workdir),
            tex_path.name,
        ]

        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=COMPILE_TIMEOUT,
                env={**os.environ, "HOME": str(APP_DIR)},
            )
        except subprocess.TimeoutExpired as exc:
            log = ((exc.stdout or "") + "\n" + (exc.stderr or ""))[-20000:]
            return CompileResponse(success=False, error="Compilation timed out", log=log)

        log = (proc.stdout + "\n" + proc.stderr)[-20000:]
        pdf_path = workdir / f"{base}.pdf"

        if proc.returncode != 0 or not pdf_path.exists() or pdf_path.stat().st_size == 0:
            return CompileResponse(success=False, error="LaTeX compilation failed", log=log)

        out_pdf = OUTPUT_DIR / f"{job_id}.pdf"
        out_tex = OUTPUT_DIR / f"{job_id}.tex"
        shutil.copy2(pdf_path, out_pdf)
        shutil.copy2(tex_path, out_tex)

        try:
            pages = len(PdfReader(str(out_pdf)).pages)
        except Exception:
            pages = None

    base_url = str(request.base_url).rstrip("/")
    return CompileResponse(
        success=True,
        job_id=job_id,
        pdf_url=f"{base_url}/files/{job_id}.pdf",
        tex_url=f"{base_url}/files/{job_id}.tex",
        pages=pages,
        log=log,
    )


@app.get("/files/{filename}")
def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = OUTPUT_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired")

    media_type = "application/pdf" if path.suffix == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, filename=filename)
