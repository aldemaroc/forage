<div align="center">

# 🐔 Forage

**Self-hosted web search & extract service: a lightweight, drop-in replacement for self-hosted Firecrawl.**

Built specifically for [Hermes Agent](https://hermes-agent.nousresearch.com), but fully usable standalone via its REST API.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](#quick-start)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)](app/)

</div>

---

## Why Forage?

Self-hosted Firecrawl works, but it is a heavy stack: the community edition spins up **six containers** (API, Playwright service, Redis, RabbitMQ, Postgres…). Forage replaces it with a **single container** that does both jobs:

- **`web_search`**: via [SearXNG](https://github.com/searxng/searxng) (a separate lightweight container)
- **`web_extract`**: hybrid static + browser extraction with three switchable browser engines and anti-bot coverage

It was developed as the extract/search backend for Hermes Agent and ships with a ready-made Hermes plugin (`WebSearchProvider`), but the REST API is generic: any application that can speak HTTP can use it.

## Features

- **Single Docker container**: FastAPI + Chromium (via Playwright, Patchright or Scrapling) + httpx/trafilatura
- **Hybrid extraction**: static HTTP first (fast, cheap), automatic browser fallback when the page needs JS or is anti-bot protected
- **Three browser engines** (`browser.engine`): `playwright` (default), `patchright` (anti-detection fork) and `scrapling` (fingerprint impersonation + Cloudflare Turnstile bypass)
- **Anti-bot fallback** (`browser.fallback_solver`): if any engine hits a challenge, Forage retries the page with the Scrapling built-in solver as a last resort
- **Structured markdown output**: extraction is returned as real markdown (headings, bold, lists, code blocks) via trafilatura's markdown format
- **Basic stealth**: hides automation signals from Cloudflare-class protections (configurable, on by default)
- **In-memory TTL cache** with a master switch and per-operation toggles (search 5 min, extract off by default)
- **Optional Bearer API-key auth** (constant-time comparison, keys via env)
- **Config-driven**: `config.yaml` bind-mounted read-only, secrets in env vars, reload via container restart
- **Hermes integration**: bundled plugin, one-line backend switch (`web.search_backend` / `web.extract_backend`)
- **GPL v3**

## Architecture

```
Hermes Agent (web_search / web_extract)
   │  local HTTP
   ▼
Forage plugin (WebSearchProvider)        plugins/web/forage/
   │  POST /search, POST /extract
   ▼
FORAGE (single container, :3672)
   ├── FastAPI
   ├── httpx + trafilatura  → static extraction (markdown output)
   ├── Chromium             → JS rendering via playwright | patchright | scrapling
   │                          (in-process pool, stealth, anti-bot solver fallback)
   └── search               → SearXNG (shared docker network)
```

The container runs its own Chromium as a subprocess. It never touches any external browser or CDP endpoint.

## Requirements

- Docker Engine 24+ with Docker Compose v2
- A SearXNG instance (see [docs/SEARXNG.md](docs/SEARXNG.md))
- ~1 GB free disk for the image (Chromium included)

## Quick start

1. **Set up SearXNG first**: follow [docs/SEARXNG.md](docs/SEARXNG.md). You need a SearXNG instance running on a shared Docker network named `searxng_default` (the default network name from the SearXNG compose). **Start SearXNG before Forage**: Forage's compose joins that network as external and will not start without it.

2. **Clone and configure**

```bash
git clone https://github.com/aldemaroc/forage.git
cd forage
cp .env.example .env         # secrets: FORAGE_API_KEYS, TZ
cp config.example.yaml config.yaml   # behavior: port, cache, browser, ...
```

3. **Build and run**

```bash
docker compose up -d --build
curl http://localhost:3672/health
# → {"status":"ok","service":"forage","version":"0.7.0",...}
```

4. **Try it**

```bash
# Search
curl -s -X POST http://localhost:3672/search -H 'Content-Type: application/json' \
  -d '{"query":"proxmox server","limit":3}'

# Extract (static)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://en.wikipedia.org/wiki/Guineafowl"],"formats":["markdown"]}'

# Extract (browser-forced; x.com is in force_render_domains by default)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://x.com/OpenAI"]}'

# Extract a PDF / office document (detected by extension or Content-Type)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com/paper.pdf"],"formats":["markdown"]}'
```

## Integrating with Hermes Agent

Forage ships with a Hermes plugin. See **[docs/HERMES.md](docs/HERMES.md)** for full instructions (plugin install, env vars, backend switch, auth).

There are two supported setups:

| Setup | `web.search_backend` | `web.extract_backend` |
|---|---|---|
| **Everything through Forage** | `forage` | `forage` |
| **Forage extract + direct SearXNG search** | `searxng` | `forage` |

The second option skips one hop for search (Hermes → SearXNG directly), at the cost of losing Forage's search cache.

## API Reference

### `GET /health`

Liveness probe. Always open (used by the container healthcheck).

### `POST /search`

```json
{ "query": "proxmox server", "limit": 5, "language": "pt-BR", "engines": ["google", "bing"] }
```

Response (Hermes web-search envelope):

```json
{
  "success": true,
  "data": { "web": [ { "title": "...", "url": "...", "description": "...", "position": 1 } ] }
}
```

Response header `X-Forage-Cache: hit|miss|bypass|disabled`.

### `POST /extract`

```json
{
  "urls": ["https://..."],
  "formats": ["markdown"],      // "markdown" (default) | "html"
  "force_render": false,
  "wait_for": null,
  "only_main_content": true,
  "timeout": 30
}
```

Response (per URL):

```json
{
  "success": true,
  "data": [
    {
      "url": "https://...",
      "title": "...",
      "content": "clean markdown text...",
      "raw_content": "clean markdown (or raw HTML; see raw_content_markdown)",
      "method": "static"        // "static" | "browser" | "browser+solver" | "pdf" | "docx" | ...
    }
  ]
}
```

`method` tells you how the page was fetched: `static` (HTTP), `browser`
(configured engine), `browser+solver` (engine hit an anti-bot challenge and the
Scrapling solver retry succeeded), or a document type (`pdf`, `docx`, `xlsx`,
`pptx`, `rtf`).

If a page is behind an anti-bot challenge (Cloudflare etc.), Forage returns a clear error instead of challenge-page garbage:

```json
{ "url": "...", "error": "Blocked by anti-bot challenge (Cloudflare or similar)", "method": "browser" }
```

### `POST /admin/cache/purge`

Clears the in-memory caches (auth-gated). Returns `{ "cleared": N }`.

## Configuration

All behavior is driven by `config.yaml` (bind-mounted read-only into the container) and env vars for secrets. Every option is documented in **[docs/CONFIG.md](docs/CONFIG.md)**. Here is the shape:

```yaml
server:   { host, port, workers, log_level }
cache:    { enabled, max_entries, search: {enabled, ttl}, extract: {enabled, ttl} }
search:   { searxng_url, default_lang, engines, timeout }
extract:  { timeout, max_content_chars, only_main_content, user_agent,
            browser_user_agent, respect_robots, force_render, wait_for,
            min_content_chars, raw_content_markdown, force_render_domains,
            url_rewrites, full_text_domains }
browser:  { engine, min_idle, max_instances, idle_timeout, headless, launch_timeout,
            stealth, network_idle_timeout, scroll_steps, challenge_timeout,
            solve_cloudflare, fallback_solver }
auth:     { enabled }
```

| Environment variable | Where | Purpose |
|---|---|---|
| `FORAGE_API_KEYS` | service `.env` | Comma-separated Bearer keys (auth.enabled) |
| `FORAGE_CONFIG` | service `.env` | Config path inside container (default `/etc/forage/config.yaml`) |
| `TZ` | service `.env` | Container timezone |
| `FORAGE_URL` | Hermes `.env` | Base URL the plugin calls |
| `FORAGE_API_KEY` | Hermes `.env` | Key the plugin sends when auth is on |
| `FORAGE_BYPASS_CACHE` | Hermes `.env` | `true` = plugin always sends `Cache-Control: no-cache` |

## How extraction decides: static vs browser

```
domain in force_render_domains | force_render | wait_for  → browser
fetch statically (httpx)
status 401/403/429                                        → browser
looks like SPA (#root, __NEXT_DATA__, ...)                 → browser
trafilatura text < min_content_chars                      → browser
else                                                      → return static result
```

Browser results also run through the challenge detector, so blocked pages
report an error rather than junk content. When a challenge is detected and
`browser.fallback_solver` is enabled (default), Forage retries the page with
the Scrapling built-in solver as a last resort; the final `method` is
`browser+solver` when that retry succeeds.

## Benchmark

50 real sites (top-30 web + 20 agent-relevant: docs, tools, reference) tested
against all three engines on the same machine, same criteria. See
[docs/BENCHMARK.md](docs/BENCHMARK.md) for the full table.

| Engine | Accessible Websites | Inaccessible | Mean scrape time |
|---|---|---|---|
| playwright | 48 | 2 | 3.3s |
| patchright | 47 | 3 | 3.2s |
| scrapling | **48** | **2** | 3.6s |

scrapling is the only engine that passes every Cloudflare-protected site it
encounters (dailymail). Sites behind intermittent anti-bot (stackoverflow,
tiktok) vary between runs on every engine. Mean time includes only successful
scrapes.

## Development

```bash
# Run tests / smoke checks against a running instance (see docs for details)
curl -s -X POST http://localhost:3672/search -H 'Content-Type: application/json' -d '{"query":"test","limit":3}'
```

The app lives in `app/` (FastAPI). Configuration loading, caching, the SearXNG client, the hybrid extractor, the browser pool and auth are each in their own module.

## License

[GPL v3](LICENSE). © 2026 Aldemaro Campos.

## Credits

Developed by **Aldemaro Campos and Chico** 🐔
