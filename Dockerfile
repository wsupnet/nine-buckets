FROM python:3.12-slim

# curl is used by the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

# Run as a non-root user. The uid is fixed so it can be matched against the host
# directory backing the /data volume.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /srv /data \
 # COPY preserves the build context's file modes. Source copied to a NAS share
 # often lands as 0600, which makes the app unreadable to any non-root `user:`
 # override. Normalise so the image works whatever the host permissions were.
 && chmod -R a+rX /srv
USER appuser

ENV DB_PATH=/data/networth.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# No reverse proxy in front, so forwarded headers are never trusted.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers"]
