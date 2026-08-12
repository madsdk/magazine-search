FROM python:3.11-slim

WORKDIR /app

# unar is rarfile's CBR backend. This image used to install unrar-free, which
# works only by luck of the base image: the unrar-free 0.3.1 in the current
# python:3.11-slim drives rarfile fine, but 0.1.3 (as on ubuntu:24.04) has no
# pipe-to-stdout command and fails rarfile's probe, leaving it with no working
# tool — that is what broke Dockerfile.ingest. bsdtar and 7z pass the probe but
# raise BadRarFile on solid archives, so unar (or non-free unrar) it is.
RUN apt-get update \
 && apt-get install -y --no-install-recommends unar \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Include the [ingest] extra (PyMuPDF + rarfile) so `magsearch ocr-rescale`
# can re-read each bundle's original.<ext>. No GPU / PaddleOCR here — full
# re-ingestion still belongs in Dockerfile.ingest.
RUN pip install --no-cache-dir ".[ingest]"

RUN mkdir -p /data/bundles
ENV MAGSEARCH_DATABASE_URL=sqlite:////data/magsearch.db
ENV MAGSEARCH_BUNDLES_DIR=/data/bundles

EXPOSE 8000

CMD ["sh", "-c", "magsearch db upgrade && magsearch web --host 0.0.0.0 --port 8000"]
