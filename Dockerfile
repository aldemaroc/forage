FROM python:3.12-slim

LABEL org.opencontainers.image.title="Forage" \
      org.opencontainers.image.description="Self-hosted web search & extract service: one container, Hermes API plus a Firecrawl-compatible /v1/scrape" \
      org.opencontainers.image.source="https://github.com/aldemaroc/forage" \
      org.opencontainers.image.url="https://github.com/aldemaroc/forage" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

WORKDIR /srv/forage

# libatomic1 is needed by the Playwright driver's own node binary before any
# browser is downloaded: python:3.12-slim ships without it, and `--with-deps`
# only installs the browser's libraries, so the driver fails to load.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (layer caching). Playwright downloads Chromium + system deps.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && patchright install chromium \
    && scrapling install

# Application code
COPY app/ app/

# Factory-default config (users override via bind mount in compose)
COPY config.example.yaml /etc/forage/config.yaml

ENV FORAGE_CONFIG=/etc/forage/config.yaml

EXPOSE 3672

CMD ["python", "-m", "app.main"]
