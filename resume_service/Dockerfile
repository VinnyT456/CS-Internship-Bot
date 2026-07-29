# Resume-build microservice: FastAPI + tectonic (tiny LaTeX engine).
# tectonic downloads TeX packages on first use and caches them, so the image
# stays small (~a few hundred MB) instead of the ~5 GB texlive-full.
FROM python:3.12-slim

# tectonic runtime deps. Debian ships a `tectonic` package on bookworm.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tectonic ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Warm the tectonic package cache at build time so the first real request isn't
# slow. A tiny doc pulls the common LaTeX bundle into the image.
RUN printf '\\documentclass{article}\\begin{document}warm\\end{document}' > /tmp/w.tex \
    && (tectonic --outdir /tmp /tmp/w.tex || true) \
    && rm -f /tmp/w.*

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
