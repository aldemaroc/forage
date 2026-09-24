# Setting up SearXNG for Forage (optional)

Forage can delegate search to [SearXNG](https://github.com/searxng/searxng), a privacy-respecting metasearch engine that aggregates Google, Bing, Brave, etc. without API keys.

> **SearXNG is optional.** Since 1.0.0 the default is `search.provider: forage`: Forage renders the SERPs itself, in its own browser, in the same container, and the stack in `docker-compose.yml` runs alone with no extra network. Use this guide only if you would rather keep a SearXNG instance in front of the engines.

To switch, set both keys in `config.yaml`:

```yaml
search:
  provider: searxng
  searxng_url: http://searxng:8080
```

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

Forage's `docker-compose.yml` does **not** join any external network by default. Create a `docker-compose.override.yml` next to it (Compose merges it automatically) to attach Forage to the SearXNG network:

```yaml
# docker-compose.override.yml - only needed with search.provider: searxng
services:
  forage:
    networks:
      - default
      - searxng_default

networks:
  searxng_default:
    external: true
```

Then `docker compose up -d` recreates Forage joined to both networks. Keep `search.searxng_url: http://searxng:8080` in `config.yaml`.

The SearXNG compose above creates a network named `searxng_default`. If your SearXNG compose uses a different project name, the network will be `<project>_default`: adjust the `networks:` block above (and `search.searxng_url` if you renamed the service).

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
