# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# API image: FastAPI backend + full screening pipeline (OCR, tampering
# heuristics, face verification with YuNet/SFace ONNX models).
#
# Build context: repository root. No secrets are copied into this image —
# configuration arrives at runtime via environment variables.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

# System libs needed by opencv-python-headless and Pillow at runtime.
# libglib2.0-0 is essential; others cover OpenCV's dynamic dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (better layer caching). web3 is included here so the
# EVM audit-anchor client is functional in the deployed image.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Face models are downloaded at BUILD time (network available) and baked
# into the image, so the container starts without external downloads.
# If the download fails the build fails loudly — the face stage would
# otherwise silently degrade in the demo.
# The fetch script must be COPY'd before it can run; copying just this one
# file keeps the models layer cacheable until the script itself changes.
COPY scripts/get_face_models.py ./scripts/get_face_models.py
RUN python scripts/get_face_models.py

# Application code (architecture preserved: app/, scripts/, data fixtures)
COPY app ./app
COPY scripts ./scripts
COPY data/demo_fixtures ./data/demo_fixtures
COPY data/mock_registry ./data/mock_registry
COPY pytest.ini ./

# Non-root runtime user; owns all writable state.
RUN groupadd -r appuser && useradd -r -g appuser -u 10001 appuser \
    && mkdir -p /srv/app/data/uploads /srv/app/logs \
    && chown -R appuser:appuser /srv/app
USER appuser

# Healthcheck hits the public health endpoint (through uvicorn, not Caddy).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

EXPOSE 8000

# APP_MOCK_MODE / APP_ENV / secrets are injected via environment at runtime.
# Port comes from the environment: Render injects PORT automatically; local
# Docker/Compose (docker-compose.yml exposes 8000, Caddy proxies api:8000)
# falls back to 8000 when PORT is unset.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
