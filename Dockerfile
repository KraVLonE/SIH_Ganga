# Dashboard build
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# API + model + daily job
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
# Runtime only needs the model, API and ingestion of drivers; OCR and satellite tooling
# (used to build the curated data) are not installed in the image.
RUN pip install "pydantic-settings>=2.4" "psycopg[binary]>=3.2" "httpx>=0.27" "numpy>=2.1" \
    "scipy>=1.14" "fastapi>=0.115" "uvicorn>=0.30" "anthropic>=1.1" "google-genai>=1.0" "shapely>=2.0" "pyproj>=3.6"
COPY ganga/ ganga/
COPY data/curated/ data/curated/
COPY scripts/ scripts/
COPY --from=web /web/dist web/dist
EXPOSE 8010
CMD ["python", "-m", "ganga.cli", "serve", "--host", "0.0.0.0", "--port", "8010"]
