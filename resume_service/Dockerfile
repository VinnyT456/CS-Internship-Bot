# Resume-build microservice: FastAPI + tectonic (tiny LaTeX engine).
# tectonic is a single static binary — pulled from its official GitHub release
# (Debian's apt has no `tectonic` package on slim). It downloads TeX packages
# on first use and caches them, so the image stays small vs the ~5 GB texlive.
FROM python:3.12-slim

# Runtime deps: curl to fetch the binary, CA certs + fontconfig for tectonic.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Install the tectonic x86_64 static binary from the official release.
ARG TECTONIC_VERSION=0.15.0
RUN set -eux; \
    url="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-musl.tar.gz"; \
    curl -fsSL "$url" -o /tmp/tectonic.tar.gz; \
    tar -xzf /tmp/tectonic.tar.gz -C /usr/local/bin tectonic; \
    rm /tmp/tectonic.tar.gz; \
    tectonic --version

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
