# magsearch

Self-hosted full-text search for old magazine scans. Point it at PDF, CBR, or
CBZ files; it OCRs every page and lets you search the resulting text. A search
hit takes you straight to the page where the term appears.

The design and rationale live in
[`docs/superpowers/specs/2026-05-11-magazine-search-design.md`](docs/superpowers/specs/2026-05-11-magazine-search-design.md);
the task-by-task implementation plan is in
[`docs/superpowers/plans/2026-05-11-magazine-search.md`](docs/superpowers/plans/2026-05-11-magazine-search.md).

## Architecture

Two physical machines, one Python package, one SQLite database. The two sides
communicate only through a directory of files (the "bundle").

```
┌────────────────── GPU box (workstation) ─────────────────────────┐
│                                                                  │
│   magsearch ingest <file>     CLI on the GPU box                 │
│     ├─ reads:  PDF / CBR / CBZ                                   │
│     ├─ uses:   PaddleOCR (GPU)                                   │
│     └─ writes: bundles/<magazine-id>/                            │
│                  ├─ original.<ext>                               │
│                  ├─ pages/0001.webp, 0002.webp, ...              │
│                  ├─ thumbs/0001.webp, ...                        │
│                  ├─ cover.webp                                   │
│                  ├─ ocr/0001.json, ...                           │
│                  └─ manifest.json                                │
│                                                                  │
└──────────────────────────────┬───────────────────────────────────┘
                               │  rsync -a bundles/<id>/  server:/data/bundles/
                               ▼
┌──────────────────────── Server (small VPS) ──────────────────────┐
│                                                                  │
│   magsearch import <bundle>          magsearch web               │
│     reads bundle dir,                  FastAPI + Jinja2          │
│     verifies checksums,                serves search UI,         │
│     populates SQLite + FTS5            page images, originals    │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

- Only the GPU box installs PaddleOCR (`pip install -e ".[ocr]"`).
- The server box stays lightweight: no GPU, no PaddleOCR.
- Re-running `magsearch ingest` on the same file is a no-op (content-hash
  guarded). Re-running `magsearch import` on the same bundle is idempotent.

## Development setup

Requires Python 3.11+.

```bash
git clone <your-repo> magsearch && cd magsearch
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

Expected: 62 passed, 1 skipped (CBR fixture not present by default), 1
deselected (real PaddleOCR test is opt-in).

Run the web app locally against an empty DB to sanity-check:

```bash
mkdir -p data/bundles
magsearch db upgrade
magsearch web --reload
# open http://127.0.0.1:8000
```

## Ingesting magazines

Run on the workstation that has PaddleOCR installed.

Install the OCR extra (one-time):

```bash
pip install -e ".[ocr]"
```

The `[ocr]` extra installs the **CPU-only** `paddlepaddle` build. To OCR on a
GPU, install the matching CUDA build *separately* (replace the `paddlepaddle`
package — they conflict). **Do not `pip install paddlepaddle-gpu` from PyPI**:
the PyPI package is pinned to 2.x and is incompatible with `paddleocr 3.x`
(you'll get an `AnalysisConfig.set_optimization_level` `AttributeError` at
ingest time). Install from PaddlePaddle's own index instead:

```bash
pip uninstall paddlepaddle
# CUDA 11.8:
pip install paddlepaddle-gpu==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu118/
# CUDA 12.6:
pip install paddlepaddle-gpu==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

`magsearch ingest` auto-detects the GPU at startup and prints the chosen
device (e.g. `[ocr] device: gpu (detected 1 CUDA device(s))`). Force a
specific device with `--device cpu` or `--device gpu`.

### Docker (GPU ingestion)

The repo also ships `Dockerfile.ingest`, a GPU-ready image that bundles
CUDA + cuDNN + a Python 3.12 venv + `paddlepaddle-gpu` + `paddleocr` + the
`magsearch` CLI. Useful when you don't want to set up the OCR stack on the
host. Requires the [NVIDIA Container Toolkit][nvct] on the host so Docker
can expose the GPU.

[nvct]: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

Build (defaults to CUDA 12.6):

```bash
docker build -f Dockerfile.ingest -t magsearch-ingest .
```

For CUDA 11.8, override the base image and PaddlePaddle index:

```bash
docker build -f Dockerfile.ingest \
  --build-arg CUDA_BASE=nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04 \
  --build-arg PADDLE_INDEX=https://www.paddlepaddle.org.cn/packages/stable/cu118/ \
  -t magsearch-ingest .
```

Run:

```bash
mkdir -p input output
cp /path/to/byte-1985-12.pdf input/

docker run --rm --gpus all \
  -v "$PWD/input:/input" -v "$PWD/output:/output" \
  magsearch-ingest ingest /input/byte-1985-12.pdf \
    --title "Byte" --issue "Vol 10 No 12" \
    --date 1985-12-01 --publisher "McGraw-Hill"
```

Bundles land in `./output/` on the host, ready to `rsync` to the server box.
The container's entrypoint is `magsearch`, so `docker run … magsearch-ingest
ingest …` becomes the natural invocation; `docker run … magsearch-ingest`
with no arguments shows the CLI help.

Optional persistence — mount a directory to `/root/.paddlex` so model
downloads survive container restarts:

```bash
docker run --rm --gpus all \
  -v "$PWD/input:/input" -v "$PWD/output:/output" \
  -v "$PWD/paddlex-cache:/root/.paddlex" \
  magsearch-ingest ingest /input/byte-1985-12.pdf …
```

Basic ingest:

```bash
magsearch ingest path/to/byte-1985-12.pdf \
  --title "Byte" \
  --issue "Vol 10 No 12" \
  --date 1985-12-01 \
  --publisher "McGraw-Hill" \
  --bundles-dir ./bundles
```

The result is a directory `./bundles/byte-1985-12/` containing the original
file, normalized page images, thumbnails, a cover, raw OCR output per page,
and a `manifest.json` with checksums.

Flags:

| flag | purpose |
|------|---------|
| `--title TEXT` | Magazine title (falls back to filename) |
| `--issue TEXT` | Free-form issue label |
| `--date YYYY-MM-DD` | Publication date; used in the auto-generated id |
| `--publisher TEXT` | Used in the browse view as a filter |
| `--id TEXT` | Override the auto-generated id |
| `--bundles-dir PATH` | Where to write the bundle directory |
| `--force` | Re-run even if a bundle for this content-hash exists |
| `--fake-ocr` | Use the deterministic fake engine (no PaddleOCR needed) — useful for testing |
| `--device {auto,cpu,gpu}` | Override OCR device selection (default `auto`; `gpu` requires `paddlepaddle-gpu`) |
| `-v, --verbose` | Show paddle / paddlex init logs and warnings |

Ship a bundle to the server:

```bash
rsync -aP ./bundles/byte-1985-12/ server:/data/bundles/byte-1985-12/
```

On the server, import it:

```bash
magsearch import /data/bundles/byte-1985-12
```

(Or, if running the server in Docker: `docker exec magsearch magsearch import /data/bundles/byte-1985-12`.)

The web app picks up the new magazine immediately.

## Deploying the web app with Docker

The repo ships a `Dockerfile` for the server side (no GPU, no PaddleOCR).
Build and run it:

```bash
docker build -t magsearch .
docker run -d --name magsearch --restart unless-stopped \
  -p 8000:8000 -v "$(pwd)/data:/data" magsearch
```

This:

1. Builds a Python 3.11 slim image with `unrar-free` installed for CBR
   support.
2. Bind-mounts `./data` to `/data` in the container — that's where the
   SQLite file and the bundle directories live.
3. Runs `magsearch db upgrade` on start, then `magsearch web` on
   `0.0.0.0:8000`.
4. Restarts on crash (`--restart unless-stopped`).

Open `http://<server>:8000`. Put it behind a reverse proxy + VPN / Tailscale /
Cloudflare Tunnel for "personal but accessible" exposure.

To import a bundle that you rsync'd into `./data/bundles/`:

```bash
docker exec magsearch magsearch import /data/bundles/<magazine-id>
```

To inspect or back up: just copy `./data/` — that directory is the entire
deployment state.

### Two Dockerfiles

- `Dockerfile` — server only. Lightweight Python 3.11 slim image, no GPU,
  no PaddleOCR. Runs the FastAPI app and serves bundles. The section above
  covers it.
- `Dockerfile.ingest` — GPU ingestion. CUDA + cuDNN + paddlepaddle-gpu +
  paddleocr + the `magsearch` CLI. Use it on the workstation; it's
  documented under [Docker (GPU ingestion)](#docker-gpu-ingestion) above.

The ingestion image is heavier (~5 GB unpacked, mostly CUDA) but means you
don't have to set up CUDA / paddle on the host yourself.

## Configuration

Both CLI and web app read settings from environment variables (or a `.env`
file in the working directory). All names are prefixed with `MAGSEARCH_`.

| variable | default | purpose |
|---|---|---|
| `MAGSEARCH_DATABASE_URL` | `sqlite:///./data/magsearch.db` | SQLAlchemy URL. Stick with SQLite. |
| `MAGSEARCH_BUNDLES_DIR` | `./data/bundles` | Where the web app looks up page images and originals. |

Inside the Docker image these are pre-set to `sqlite:////data/magsearch.db`
and `/data/bundles`.

## Project layout

```
src/magsearch/
  cli.py                 # typer: ingest, import, web, db
  settings.py            # pydantic-settings
  db.py                  # SQLAlchemy engine + session
  models.py              # Magazine, Page
  manifest.py            # bundle manifest (cross-machine contract)
  importer.py            # bundle dir → DB
  ingest/
    pipeline.py          # 6-step orchestrator
    formats.py           # PDF / CBZ / CBR readers
    normalize.py         # WebP encoding
    ocr.py               # OCREngine, FakeOCREngine, PaddleOCREngine
    ids.py               # slug + content hash
  web/
    app.py / routes.py   # FastAPI app
    search.py / deps.py
    templates/           # Jinja2

alembic/                 # migrations
tests/                   # pytest suite
Dockerfile
```

## Tests

```bash
pytest                       # default: skips ocr-marked tests
pytest -m ocr                # run only the PaddleOCR smoke test
pytest -v --tb=short         # verbose, short tracebacks
```

The integration tests exercise the full ingest → import → search chain
against a temp SQLite using `FakeOCREngine`, so they don't need PaddleOCR or
a GPU.

## Known limitations / follow-ups

These are deliberate gaps recorded for later:

- **No structured logging or `--verbose` flag.** The CLI uses `typer.echo`
  for human output; nothing emits to `logging.*`.
- **`--workers N` flag from the spec is unimplemented.** PDF rendering and
  WebP encoding are single-process. PaddleOCR manages its own GPU batching
  so this only affects the image-IO side.
- **CBR test fixture is not checked in.** Creating a `.rar` archive needs the
  proprietary `rar` binary. `tests/fixtures/cbzs.py` has the exact command;
  drop a `tests/fixtures/tiny.cbr` in place and the auto-skipped test runs.
- **No auth.** Intended to live behind a VPN / Tailscale / private URL. If
  you need a shared password, FastAPI middleware is a small addition.
