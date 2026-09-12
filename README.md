# UniGPT Free LaTeX Compiler

A small FastAPI + TeX Live service that lets a UniGPT Action compile complete LaTeX documents into real PDFs.

## What it does

- `POST /compile` accepts standalone LaTeX source.
- Compiles with `latexmk` + pdfLaTeX.
- Returns JSON with temporary `pdf_url`, `tex_url`, page count, and compiler log.
- `GET /health` reports whether the compiler is available.
- Generated files expire automatically.

## Safety defaults

- `-no-shell-escape` is always enabled.
- TeX file reads/writes use paranoid restrictions.
- Each compilation runs in a fresh temporary directory.
- Default compilation timeout: 120 seconds.
- Default maximum LaTeX source size: 1 MB.
- Compilation requires an API key.

## Deploy on Render for $0

This repository includes `render.yaml` and a Dockerfile. In Render, create a new **Blueprint** from this repository. The Blueprint requests Render's `free` web-service plan and generates an API key automatically.

After deployment, open:

```text
https://YOUR-SERVICE.onrender.com/health
```

You should receive JSON containing `"ok": true` and `"latexmk": true`.

Then copy the service URL and the generated `API_KEY` into your UniGPT Action configuration.

## API

### `POST /compile`

Header:

```text
X-API-Key: YOUR_SECRET
```

JSON body:

```json
{
  "source": "\\documentclass{article}\\begin{document}Hello\\end{document}",
  "filename": "hello"
}
```

Successful response includes:

```json
{
  "success": true,
  "job_id": "...",
  "pdf_url": "https://.../files/...pdf",
  "tex_url": "https://.../files/...tex",
  "pages": 1,
  "log": "..."
}
```

Generated files expire after 24 hours by default. Render's free filesystem is ephemeral, so a service restart can remove them earlier.
