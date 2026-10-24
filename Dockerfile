# Tradli AI Platform -- production image.
# Flask + gunicorn on Debian slim, non-root, no build toolchain left behind.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    TRADLI_ENV=production

WORKDIR /srv/tradli

# Dependencies first: this layer is cached until requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app/ ./app/
COPY wsgi.py gunicorn.conf.py ./
COPY .env.example ./.env.example

# A dedicated unprivileged user; the data volume is the only writable path.
RUN useradd --create-home --uid 10001 tradli \
    && mkdir -p /srv/tradli/data \
    && chown -R tradli:tradli /srv/tradli
USER tradli

EXPOSE 8000

# The app creates its SQLite schema on first import, so no migration step.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
