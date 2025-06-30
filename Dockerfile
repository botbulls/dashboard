FROM python:3.11-slim

LABEL maintainer="ecoppen" \
	org.opencontainers.image.url="https://github.com/ecoppen/futuresboard" \
	org.opencontainers.image.source="https://github.com/ecoppen/futuresboard" \
	org.opencontainers.image.vendor="ecoppen" \
	org.opencontainers.image.title="futuresboard" \
	org.opencontainers.image.description="Dashboard to monitor the performance of your Binance or Bybit Futures account" \
	org.opencontainers.image.licenses="GPL-3.0"

# Install build dependencies only when needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/futuresboard
COPY . .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Remove build tools to slim final image
RUN apt-get purge -y build-essential gcc && apt-get autoremove -y && rm -rf /root/.cache

CMD ["futuresboard"]
