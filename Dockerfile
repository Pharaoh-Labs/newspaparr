# Python 3.13 slim — aligned with the venv dev runtime
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Capture flow needs a real Chrome (rendered into Xvfb, streamed via x11vnc
# through websockify to the user's browser). Renewals are HTTP-only and don't
# touch the browser at all.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gosu \
        chromium \
        xvfb x11vnc \
        # Chromium runtime libs (needed by chromium even when not displayed)
        libnss3 libgbm1 libasound2 libxss1 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/chromium /usr/bin/google-chrome 2>/dev/null || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root user; UID adjusted at runtime by entrypoint
RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser -m appuser

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /app/data /app/logs
ENV HOME=/app

# Ports
#   1851 — web dashboard
#   6100 — websockify (noVNC bridge for the in-dashboard capture flow)
EXPOSE 1851 6100

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:1851/api/status || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "gunicorn", "--bind", "0.0.0.0:1851", "--workers", "1", "--timeout", "300", "wsgi:app"]
