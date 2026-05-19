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
- Two issues released in the same month (e.g. a regular December issue and a
  Christmas double) won't clobber each other — the second bundle's id is
  disambiguated with the slugified `--issue` (preferred) or a content-hash
  suffix as fallback. See [Bulk ingestion](#bulk-ingestion) below.

## Development setup

Requires Python 3.11+.

```bash
git clone <your-repo> magsearch && cd magsearch
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

Expected: 151 passed, 1 skipped (CBR fixture not present by default), 1
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
pip install paddlepaddle-gpu==3.3.1 --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu118/
# CUDA 12.6:
pip install paddlepaddle-gpu==3.3.1 --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
```

(Use `--extra-index-url` rather than `-i`: that keeps PyPI as the primary
index for transitive dependencies like `httpx`/`numpy` and only falls back
to PaddlePaddle's index for the `paddlepaddle-gpu` wheel itself.)

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

## Single-issue upload via the web UI

For one-off issues, the admin web UI offers a faster path than `rsync` +
`magsearch import`. After producing a bundle on the GPU box:

```bash
cd ./bundles
zip -r byte-1985-12.zip byte-1985-12/
```

…sign in to the admin UI as an admin, go to **Issues → Upload bundle**, and
pick the zip. The server validates the manifest, verifies the per-file
checksums, stages the bundle into `bundles_dir/<id>/`, and runs the same
`import_bundle()` the CLI uses. Re-uploading the same zip is a no-op (you'll
land on the existing issue's edit page).

The bulk flow (`bulk-ingest` + `rsync` + `bulk-import`) is still the right
choice for ingesting many issues at once — the web upload is meant for a
single bundle at a time.

## Bulk ingestion

For ingesting a whole collection (hundreds or thousands of files), drive the
pipeline with `bulk-ingest` and `bulk-import`. Both are thin wrappers around the
single-file commands above and preserve the same two-machine flow.

### Sidecar metadata

Put a `metadata.csv` (or `metadata.json`) next to the source files. Only
`filename` is required; the rest mirror the single-file `ingest` flags:

```csv
filename,title,issue,date,publisher
byte-1985-12.pdf,Byte,Vol 10 No 12,1985-12-01,McGraw-Hill
byte-1985-12-xmas.pdf,Byte,Christmas Double,1985-12-15,McGraw-Hill
byte-1986-01.pdf,Byte,Vol 11 No 1,1986-01-01,McGraw-Hill
```

CSV tolerates `#` comments and blank lines. JSON form is a top-level array of
objects with the same keys.

### Running bulk ingest

```bash
magsearch bulk-ingest ./input \
  --bundles-dir ./bundles
```

`bulk-ingest` walks the directory, applies sidecar metadata per file, calls the
per-file pipeline on each, and writes progress to
`./input/.bulk-state.jsonl`. Crash and re-run — already-completed files are
skipped via the state log; partially-done files are safe to retry (the per-file
pipeline writes `manifest.json` atomically as its last step).

| flag | purpose |
|---|---|
| `--sidecar PATH` | Override sidecar location (default: `<input>/metadata.csv` or `metadata.json`) |
| `--state-file PATH` | Override state log location |
| `--recursive` | Walk subdirectories |
| `--retry-failed` | Re-attempt files previously marked `failed` |
| `--force-all` | Pass `--force` to every per-file ingest |
| `--strict-sidecar` | Error (instead of warn) when files have no sidecar row |
| `--halt-on-error` | Stop the batch on first failure |
| `--dry-run` | Print planned actions; touch nothing |
| `--fake-ocr`, `--device`, `--verbose` | Passthrough to the per-file pipeline |

The state log records, per file: `status` (`done` / `failed` / `in_progress` /
`pending`), `attempts`, `bundle_id`, `content_hash`, and the last error. Inspect
it with `jq` if you need to triage failures across a long run.

### Importing on the server

```bash
rsync -aP ./bundles/ server:/data/bundles/
ssh server
magsearch bulk-import /data/bundles
```

`bulk-import` walks every subdirectory of `/data/bundles` and imports each
bundle, with its own state log at
`/data/bundles/.bulk-import-state.jsonl`. Re-runs are idempotent — bundles
already imported are skipped, and bundles previously marked `failed` are
skipped unless `--retry-failed` is passed.

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

## Authentication & admin

The web app ships with an optional session-based login and a small admin UI
at `/admin` (dashboard, issue editor, user management, app config). Whether
visitors must log in is itself a config toggle (`require_login` in the admin
config page) — leave it off for a fully public archive, turn it on to gate the
site behind a password.
The single-issue **Upload bundle** action under `/admin/issues` requires an
admin session regardless of the public/private toggle.

Set `MAGSEARCH_SESSION_SECRET` to any non-empty random string in production.
If unset, the app falls back to a hard-coded insecure dev value — fine for
local poking, bad for anything reachable from the internet.

Create the first admin user from the CLI:

```bash
magsearch admin create alice
# password: ******
```

Related CLI commands:

| command | purpose |
|---|---|
| `magsearch admin create <user>` | Create an administrator (prompts for password) |
| `magsearch admin list` | List all users with admin/user role flag |
| `magsearch admin reset-password <user>` | Reset a user's password |

Once an admin exists, sign in at `/login` and the rest of user management
(create/edit/delete users, promote to admin, change the public/private toggle)
lives in `/admin`.

## Configuration

Both CLI and web app read settings from environment variables (or a `.env`
file in the working directory). All names are prefixed with `MAGSEARCH_`.

| variable | default | purpose |
|---|---|---|
| `MAGSEARCH_DATABASE_URL` | `sqlite:///./data/magsearch.db` | SQLAlchemy URL. Stick with SQLite. |
| `MAGSEARCH_BUNDLES_DIR` | `./data/bundles` | Where the web app looks up page images and originals. |
| `MAGSEARCH_SESSION_SECRET` | _(empty → insecure dev fallback)_ | Signs login session cookies. Set to a long random string in production. |
| `MAGSEARCH_MAX_UPLOAD_BYTES` | `2147483648` | Cap on web bundle upload size, in bytes. Applies to both multipart body and uncompressed bundle contents. |
| `MAGSEARCH_AUTH_ENABLED` | `true` | Keep auth on for `magsearch web`. The desktop launcher sets it to `false`, which skips the login gate, pins a synthesized `local` admin onto every request, and registers the multi-file `/import` route. |

Inside the Docker image the first two are pre-set to `sqlite:////data/magsearch.db`
and `/data/bundles`. Pass `MAGSEARCH_SESSION_SECRET` for any non-throwaway
deployment (e.g. `docker run -e MAGSEARCH_SESSION_SECRET=$(openssl rand -hex 32) …`).

## Project layout

```
src/magsearch/
  cli.py                 # typer: ingest, bulk-ingest, import, bulk-import, web, desktop, db, admin
  settings.py            # pydantic-settings (includes auth_enabled flag)
  db.py                  # SQLAlchemy engine + session
  desktop.py             # desktop launcher: data dir + alembic + uvicorn + pywebview
  desktop_paths.py       # per-OS user-data dir resolution
  models.py              # Magazine, Page, User, AppConfig
  manifest.py            # bundle manifest (cross-machine contract)
  importer.py            # bundle dir → DB
  bulk_import.py         # iterate bundles dir → DB, with resumable state log
  ingest/
    pipeline.py          # 6-step orchestrator
    bulk.py              # iterate input dir, sidecar parsing, state log
    formats.py           # PDF / CBZ / CBR readers
    normalize.py         # WebP encoding
    ocr.py               # OCREngine, FakeOCREngine, PaddleOCREngine
    ids.py               # slug, content hash, collision-safe id resolver
  web/
    app.py / routes.py   # FastAPI app
    search.py / deps.py
    auth.py              # password hashing, session helpers, ensure_local_admin
    middleware.py        # require-login gate (honors auth_enabled flag)
    config_service.py    # app_config table accessors
    routes_admin.py      # /admin/* (dashboard, issues, users, config)
    routes_auth.py       # /login, /logout
    routes_import.py     # /import (multi-file, only mounted in desktop mode)
    templates/           # Jinja2 (incl. admin/*)

alembic/                 # migrations
tests/                   # pytest suite
magsearch.spec           # PyInstaller spec for the desktop binary
Dockerfile
Dockerfile.ingest
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
  so this only affects the image-IO side. `bulk-ingest` is also serial for the
  same reason — parallel OCR would contend on one GPU.
- **CBR test fixture is not checked in.** Creating a `.rar` archive needs the
  proprietary `rar` binary. `tests/fixtures/cbzs.py` has the exact command;
  drop a `tests/fixtures/tiny.cbr` in place and the auto-skipped test runs.

## Desktop app

The same package also ships as a standalone desktop application for macOS,
Linux, and Windows. The desktop build wraps the FastAPI server in a
pywebview window, with no OCR pipeline. It runs the same code as
`magsearch web` but with `MAGSEARCH_AUTH_ENABLED=false`, which skips the
login gate and registers a public multi-file `/import` route at the cost
of admin pages being unauthenticated. Data lives in a per-OS user dir
(`~/Library/Application Support/magsearch` on macOS,
`%APPDATA%\magsearch` on Windows,
`~/.local/share/magsearch` on Linux).

Users import pre-computed `.magbundle` files (zips of bundle directories)
via the in-app file picker at `/import`, which accepts one or more files
in a single submit. Per-file failures don't abort the batch.

### Run from source

```bash
pip install -e ".[desktop]"
magsearch desktop
```

### Build a standalone binary

```bash
pip install -e ".[desktop]"
pyinstaller magsearch.spec --noconfirm
./dist/magsearch-desktop/magsearch-desktop
```

Build per OS on the OS itself — PyInstaller does not cross-compile.
