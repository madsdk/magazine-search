FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends unrar-free \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/

RUN pip install --no-cache-dir .

RUN mkdir -p /data/bundles
ENV MAGSEARCH_DATABASE_URL=sqlite:////data/magsearch.db
ENV MAGSEARCH_BUNDLES_DIR=/data/bundles

EXPOSE 8000

CMD ["sh", "-c", "magsearch db upgrade && magsearch web --host 0.0.0.0 --port 8000"]
