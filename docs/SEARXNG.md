# Setting up SearXNG for Forage

Forage delegates all search to [SearXNG](https://github.com/searxng/searxng), a privacy-respecting metasearch engine that aggregates Google, Bing, Brave, etc. without API keys.

> **Why SearXNG?** It is a single lightweight container, self-hosted, and it does the hard part of talking to multiple search engines (including dealing with their anti-bot quirks). Forage caches results for 5 minutes by default, which further protects the engines.

## 1. Create the SearXNG compose

```yaml
# searxng/docker-compose.yml
services:
  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    restart: always
    ports:
      - "127.0.0.1:8080:8080"   # host-only; Forage reaches it via the docker network
    volumes:
      - ./settings.yml:/etc/searxng/settings.yml:ro
    environment:
      - TZ=America/Recife
      - SEARXNG_BASE_URL=http://localhost:8080/
```

Start it:

```bash
cd searxng
docker compose up -d
```

## 2. settings.yml: the important parts

Create the file **before** running the compose. If the file does not exist, Docker will create a *directory* named `settings.yml` and SearXNG will fail to start.

SearXNG needs **JSON output enabled**, or Forage gets a 403 when calling `/search?format=json`:

```yaml
search:
  formats:
    - html
    - json

server:
  secret_key: "change-me-to-a-long-random-string"   # required
  limiter: false
```

Only enable the engines you want (defaults in the image already work):

```yaml
engines:
  - name: google
    engine: google
    shortcut: g
  - name: bing
    engine: bing
    shortcut: b
  - name: brave
    engine: brave
    shortcut: br
  - name: startpage
    engine: startpage
    shortcut: sp
```

> **Pitfall**: the `wikidata` engine fails on startup in some versions. If the container logs show a wikidata error, disable it (`enabled: false`).

## 3. Network layout: how Forage reaches SearXNG

Forage and SearXNG must be on the **same Docker network** so Forage can call SearXNG by service name (`http://searxng:8080`).

The SearXNG compose above creates a network named `searxng_default`. Forage's compose joins it as an external network:

```yaml
networks:
  searxng_default:
    external: true
```

If your SearXNG compose uses a different project name, the network will be `<project>_default`. Adjust Forage's `networks:` section and `search.searxng_url` accordingly (e.g. `http://searxng:8080`).

> **Why not `host.docker.internal`?** In a custom Compose network, `host.docker.internal` is **not** automatically resolved. Using the shared docker network + service name is the reliable pattern.

## 4. Verify

```bash
# From inside the Forage container network namespace (or via the API):
curl -s -X POST http://localhost:3672/search -H 'Content-Type: application/json' \
  -d '{"query":"hello world","limit":3}'
```

Expect `"success": true` with results.

## Tuning

- **Engine filtering**: set `search.engines` in Forage's config to limit which engines SearXNG uses (`[google, bing]`).
- **Language**: `search.default_lang: pt-BR` is passed through to SearXNG.
- **Anti-bot protection**: Forage's search cache (TTL 300s by default) means identical queries don't hit the engines repeatedly.
