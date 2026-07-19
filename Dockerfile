# Backend image: FastAPI + uvicorn. Runtime deps only (see requirements.txt;
# dev tooling lives in requirements-dev.txt and is not installed here).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user; pre-create the DB volume dir it will own so a
# fresh named volume mounted at /data inherits appuser ownership.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

EXPOSE 8000

# python-based healthcheck (the slim image has no curl).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status==200 else 1)"

# Persist the SQLite DB outside the image via a mounted volume (see compose).
# --proxy-headers so request.client reflects the forwarded client behind nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
