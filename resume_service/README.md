# Resume Build Service

A tiny microservice that turns a resume **YAML** (husayni/yaml-resume-builder
schema) into a **PDF**, using `tectonic` (a single small LaTeX engine) instead
of the ~5 GB `texlive-full`. It runs **off** the Discord bot's host, so the bot
box needs no LaTeX.

## What it does

```
POST /build   (body: raw YAML)   ->   200 application/pdf
GET  /health                     ->   {"ok": true, "tectonic": true}
```

The `/tailor` button in the bot POSTs the AI-generated YAML here and returns the
compiled PDF to the user. If this service is unreachable or `RESUME_BUILD_URL`
is unset, the bot falls back to sending the raw `.yaml` file.

## Endpoints

- `POST /build` — body is raw YAML (`Content-Type: text/plain` or
  `application/x-yaml`). Optional `?one_page=1`. Returns the PDF, or `422` on
  bad YAML/schema, `500` on a compile failure.
- `GET /health` — liveness + whether `tectonic` is on PATH.

## Auth (optional but recommended)

Set `BUILD_TOKEN` on the service. Callers must then send
`Authorization: Bearer <token>`. The bot sends it automatically when you set
`RESUME_BUILD_TOKEN` (see below).

## Run locally

```bash
cd resume_service
pip install -r requirements.txt
# needs tectonic on PATH: `brew install tectonic` (mac) or apt `tectonic`
uvicorn app:app --reload --port 8000

# test
curl -s -X POST localhost:8000/build \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @../path/to/resume.yaml -o resume.pdf
```

## Deploy with Docker

```bash
docker build -t resume-build ./resume_service
docker run -p 8000:8000 -e BUILD_TOKEN=changeme resume-build
```

### Render (separate service)

1. New **Web Service** → point at this repo, root dir `resume_service`,
   environment **Docker** (uses the `Dockerfile`).
2. Env vars: `BUILD_TOKEN=<random>`.
3. Deploy. Note the URL, e.g. `https://resume-build.onrender.com`.

### Fly.io

```bash
cd resume_service
fly launch --dockerfile Dockerfile   # accept defaults, no DB
fly secrets set BUILD_TOKEN=<random>
fly deploy
```

### Railway

New project → Deploy from repo → set root to `resume_service` → it builds the
Dockerfile → add `BUILD_TOKEN` var.

## Point the bot at it

Set these on the **bot's** host (Render), then redeploy the bot:

```
RESUME_BUILD_URL=https://resume-build.onrender.com
RESUME_BUILD_TOKEN=<same value as BUILD_TOKEN>
```

That's it — the Tailor button now returns a PDF. Without these vars the bot
keeps sending the YAML file, so it's safe to roll out the bot first.

## Env vars (service)

| Var | Default | Meaning |
|-----|---------|---------|
| `BUILD_TOKEN` | (none) | If set, require `Authorization: Bearer <token>`. |
| `TECTONIC_BIN` | `tectonic` | Path to the tectonic binary. |
| `COMPILE_TIMEOUT` | `60` | Seconds before a compile is killed. |
| `MAX_YAML_BYTES` | `262144` | Reject bodies larger than this. |
| `PORT` | `8000` | Listen port. |

## Notes

- First request after a cold start can be slow while tectonic fetches the TeX
  bundle; the Dockerfile warms this cache at build time to avoid it.
- Free-tier hosts sleep on idle — the first build after a nap pays a wake-up
  cost. The bot's timeout is 30 s (`RESUME_BUILD_TIMEOUT`).
