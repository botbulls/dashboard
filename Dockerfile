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

CMD ["futuresboard", "--host", "0.0.0.0", "--port", "5000", "--disable-auto-scraper"]
