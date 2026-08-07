FROM python:3.12-slim

WORKDIR /srv/forage

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
