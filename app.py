import logging
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
DRAFT_DIR = APP_DIR / "drafts"
OUTPUT_DIR.mkdir(exist_ok=True)
DRAFT_DIR.mkdir(exist_ok=True)
TECTONIC = APP_DIR / ".bin" / "tectonic"

API_KEY = os.environ.get("API_KEY", "")
MAX_SOURCE_BYTES = int(os.environ.get("MAX_SOURCE_BYTES", "1000000"))
MAX_CHUNK_BYTES = int(os.environ.get("MAX_CHUNK_BYTES", "50000"))
COMPILE_TIMEOUT = int(os.environ.get("COMPILE_TIMEOUT", "120"))
FILE_TTL_SECONDS = int(os.environ.get("FILE_TTL_SECONDS", "86400"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unigpt-latex-compiler")

app = FastAPI(title="UniGPT Free LaTeX Compiler", version="2.0.0")


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


class StartDocumentRequest(BaseModel):
    filename: str = Field(default="document", description="Base filename without extension")


class AppendRequest(BaseModel):
    content: str = Field(..., description="Next LaTeX source chunk. Keep each chunk well below 50 KB.")


class ReplaceRequest(BaseModel):
    old: str = Field(..., description="Exact text to replace")
    new: str = Field(..., description="Replacement text")
    count: int = Field(default=1, ge=1, le=20)


def require_key(x_api_key: str | None):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server API_KEY is not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def cleanup_old_files():
    cutoff = time.time() - FILE_TTL_SECONDS
    for directory in (OUTPUT_DIR, DRAFT_DIR):
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def safe_basename(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))[:80]
    return cleaned or "document"


def safe_document_id(document_id: str) -> str:
    try:
        return str(uuid.UUID(document_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document_id")


def draft_paths(document_id: str):
    doc_id = safe_document_id(document_id)
    return DRAFT_DIR / f"{doc_id}.tex", DRAFT_DIR / f"{doc_id}.name"


def compile_source(source: str, filename: str, request: Request) -> CompileResponse:
    if not (TECTONIC.exists() and os.access(TECTONIC, os.X_OK)):
        raise HTTPException(status_code=500, detail="Tectonic compiler is not installed")

    source_bytes = source.encode("utf-8")
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"LaTeX source exceeds {MAX_SOURCE_BYTES} bytes")

    job_id = str(uuid.uuid4())
    base = safe_basename(filename)

    with tempfile.TemporaryDirectory(prefix="latexjob_") as tmp:
        workdir = Path(tmp)
        tex_path = workdir / f"{base}.tex"
        tex_path.write_text(source, encoding="utf-8")

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


def run_startup_selftest() -> None:
    if not (TECTONIC.exists() and os.access(TECTONIC, os.X_OK)):
        logger.error("LATEX_SELFTEST_FAILED: tectonic binary missing or not executable")
        return
    source = r"""\documentclass{article}
\usepackage{amsmath}
\begin{document}
Hello Volkan.
\[e^{i\pi}+1=0\]
\end{document}
"""
    try:
        with tempfile.TemporaryDirectory(prefix="latex_selftest_") as tmp:
            workdir = Path(tmp)
            tex_path = workdir / "selftest.tex"
            tex_path.write_text(source, encoding="utf-8")
            proc = subprocess.run(
                [str(TECTONIC), "--untrusted", "--keep-logs", "--outdir", str(workdir), tex_path.name],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "HOME": str(APP_DIR)},
            )
            pdf_path = workdir / "selftest.pdf"
            if proc.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
                logger.info("LATEX_SELFTEST_OK: pages=%s bytes=%s", len(PdfReader(str(pdf_path)).pages), pdf_path.stat().st_size)
            else:
                logger.error("LATEX_SELFTEST_FAILED: %s", (proc.stdout + "\n" + proc.stderr)[-4000:])
    except Exception as exc:
        logger.exception("LATEX_SELFTEST_FAILED: %s", exc)


@app.on_event("startup")
def startup_selftest() -> None:
    run_startup_selftest()


@app.get("/health")
def health():
    return {
        "ok": True,
        "engine": "tectonic",
        "tectonic": TECTONIC.exists() and os.access(TECTONIC, os.X_OK),
        "chunked_documents": True,
        "max_chunk_bytes": MAX_CHUNK_BYTES,
        "max_source_bytes": MAX_SOURCE_BYTES,
    }


@app.post("/compile", response_model=CompileResponse)
def compile_latex(payload: CompileRequest, request: Request, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    cleanup_old_files()
    return compile_source(payload.source, payload.filename, request)


@app.post("/documents/start")
def start_document(payload: StartDocumentRequest, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    cleanup_old_files()
    document_id = str(uuid.uuid4())
    tex_path, name_path = draft_paths(document_id)
    tex_path.write_text("", encoding="utf-8")
    name_path.write_text(safe_basename(payload.filename), encoding="utf-8")
    return {"success": True, "document_id": document_id, "bytes_total": 0, "max_chunk_bytes": MAX_CHUNK_BYTES}


@app.post("/documents/{document_id}/append")
def append_document(document_id: str, payload: AppendRequest, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    cleanup_old_files()
    tex_path, _ = draft_paths(document_id)
    if not tex_path.exists():
        raise HTTPException(status_code=404, detail="Document not found or expired")
    chunk = payload.content.encode("utf-8")
    if len(chunk) > MAX_CHUNK_BYTES:
        raise HTTPException(status_code=413, detail=f"Chunk exceeds {MAX_CHUNK_BYTES} bytes")
    current = tex_path.stat().st_size
    if current + len(chunk) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"Document exceeds {MAX_SOURCE_BYTES} bytes")
    with tex_path.open("a", encoding="utf-8") as f:
        f.write(payload.content)
    total = tex_path.stat().st_size
    return {"success": True, "document_id": safe_document_id(document_id), "bytes_appended": len(chunk), "bytes_total": total}


@app.post("/documents/{document_id}/replace")
def replace_document_text(document_id: str, payload: ReplaceRequest, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    tex_path, _ = draft_paths(document_id)
    if not tex_path.exists():
        raise HTTPException(status_code=404, detail="Document not found or expired")
    source = tex_path.read_text(encoding="utf-8")
    occurrences = source.count(payload.old)
    if occurrences == 0:
        raise HTTPException(status_code=404, detail="Exact text to replace was not found")
    updated = source.replace(payload.old, payload.new, payload.count)
    if len(updated.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"Document exceeds {MAX_SOURCE_BYTES} bytes")
    tex_path.write_text(updated, encoding="utf-8")
    return {"success": True, "document_id": safe_document_id(document_id), "replacements": min(payload.count, occurrences), "bytes_total": tex_path.stat().st_size}


@app.post("/documents/{document_id}/compile", response_model=CompileResponse)
def compile_document(document_id: str, request: Request, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    cleanup_old_files()
    tex_path, name_path = draft_paths(document_id)
    if not tex_path.exists():
        raise HTTPException(status_code=404, detail="Document not found or expired")
    source = tex_path.read_text(encoding="utf-8")
    filename = name_path.read_text(encoding="utf-8") if name_path.exists() else "document"
    return compile_source(source, filename, request)


@app.get("/files/{filename}")
def get_file(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = OUTPUT_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired")
    media_type = "application/pdf" if path.suffix == ".pdf" else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media_type, filename=filename)
