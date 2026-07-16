FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends unrar-free \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Include the [ingest] extra (PyMuPDF + rarfile) so `magsearch ocr-rescale`
# can re-read each bundle's original.<ext>. unrar-free (installed above) is
# rarfile's CBR backend. No GPU / PaddleOCR here — full re-ingestion still
# belongs in Dockerfile.ingest.
RUN pip install --no-cache-dir ".[ingest]"

RUN mkdir -p /data/bundles
ENV MAGSEARCH_DATABASE_URL=sqlite:////data/magsearch.db
ENV MAGSEARCH_BUNDLES_DIR=/data/bundles

EXPOSE 8000

CMD ["sh", "-c", "magsearch db upgrade && magsearch web --host 0.0.0.0 --port 8000"]
