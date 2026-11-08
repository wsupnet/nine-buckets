# Debian release named explicitly so a rebuild can't silently move to a new one.
FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/networth.db

# Non-root user, created before the code is copied so a code change doesn't
# rerun this step. docker-compose.yml overrides the uid with `user:` to match the
# owner of the host data folder.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /data \
 && chown appuser:appuser /data

WORKDIR /srv

# Dependencies first: this layer is reused until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chmod: source copied to a NAS share often lands as 0600, which makes it
# unreadable to a non-root `user:`. The code stays root-owned and read-only.
COPY --chmod=a+rX app/ ./app/
COPY --chmod=a+rX templates/ ./templates/
COPY --chmod=a+rX static/ ./static/

USER appuser

# No VOLUME here on purpose: it would quietly put data in an anonymous Docker
# volume when nothing is mounted. Mount a host folder at /data instead (compose
# does); the app warns at startup if /data isn't a mount.
EXPOSE 8000

# Python's own urllib, so the image needs no curl. Fails on any non-2xx.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# No reverse proxy in front, so forwarded headers are never trusted.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers"]
