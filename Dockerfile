FROM python:3.11-slim

LABEL maintainer="ecoppen" \
	org.opencontainers.image.url="https://github.com/ecoppen/futuresboard" \
	org.opencontainers.image.source="https://github.com/ecoppen/futuresboard" \
	org.opencontainers.image.vendor="ecoppen" \
	org.opencontainers.image.title="futuresboard" \
	org.opencontainers.image.description="Dashboard to monitor the performance of your Binance or Bybit Futures account" \
	org.opencontainers.image.licenses="GPL-3.0"

WORKDIR /usr/src/futuresboard

# Copy requirements first for better caching
COPY requirements/ requirements/
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements/base.txt

# Copy the application source code
COPY src/ src/
COPY setup.py setup.cfg pyproject.toml ./

# Install the package with a fixed version to avoid setuptools-scm issues
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_FUTURESBOARD=1.0.0 python -m pip install -e . || \
    python -m pip install -e . --no-build-isolation

# Create data directory for database persistence
RUN mkdir -p /usr/src/futuresboard/data

ENV PYTHONUNBUFFERED=1 \
    FUTURESBOARD_PORT=5000

EXPOSE 5000

# Liveness: /health no requiere login ni toca DB/Docker/exchange.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('FUTURESBOARD_PORT', '5000'), timeout=4)" || exit 1

# gunicorn con 1 worker gthread: el scraper automatico corre como hilo dentro del worker
# (ver src/futuresboard/gunicorn_conf.py). Lee ./config/config.json (o FUTURESBOARD_CONFIG_DIR).
# Para desactivar el scraper: FUTURESBOARD_DISABLE_AUTO_SCRAPE=1.
# Scrape puntual (cron): docker run ... futuresboard --scrape-only
CMD ["gunicorn", "--config", "python:futuresboard.gunicorn_conf", "futuresboard.wsgi:app"]
