<div align="center">

# 🐔 Forage

**Self-hosted web search & extract service: one container that does what self-hosted Firecrawl does, speaking both the Hermes API and a Firecrawl-compatible `/v1/scrape`.**

Built specifically for [Hermes Agent](https://hermes-agent.nousresearch.com), but fully usable standalone via its REST API, and usable as a drop-in for a Firecrawl client that only needs `POST /v1/scrape`.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](#quick-start)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-aldemaroc%2Fforage-2496ED.svg)](#releases)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)](app/)

</div>

---

## Why Forage?

Self-hosted Firecrawl works, but it is a heavy stack: the community edition spins up **six containers** (API, Playwright service, Redis, RabbitMQ, Postgres…). Forage replaces it with a **single container** that does both jobs:

- **`web_search`**: via Forage's own SERP engines (browser render + per-engine DOM parse of Google/Bing/Yahoo/DuckDuckGo, `search.provider: forage`, the default, no SearXNG needed) **or** via [SearXNG](https://github.com/searxng/searxng) (a separate lightweight container, opt-in)
- **`web_extract`**: hybrid static + browser extraction with three switchable browser engines, two switchable extract engines and anti-bot coverage

It was developed as the extract/search backend for Hermes Agent and ships with a ready-made Hermes plugin (`WebSearchProvider`), but the REST API is generic: any application that can speak HTTP can use it. For Firecrawl clients there is a compatibility layer: `POST /v1/scrape` answers in Firecrawl's envelope, so switching is a base-URL change (see [docs/FIRECRAWL.md](docs/FIRECRAWL.md)).

## Features

- **Firecrawl-compatible API**: `POST /v1/scrape` (Firecrawl request fields, Firecrawl response envelope and error codes) and `POST /v1/search` (v1 shape), served from the same container and port as the native API. Formats `markdown`/`html`/`rawHtml`; unsupported Firecrawl options are ignored and reported instead of failing the request
- **Single Docker container**: FastAPI + Chromium (via Playwright, Patchright or Scrapling) + httpx/trafilatura
- **Hybrid extraction**: static HTTP first (fast, cheap), automatic browser fallback when the page needs JS or is anti-bot protected
- **Four browser engines** (`browser.engine`): `scrapling` (default, fingerprint impersonation + Cloudflare Turnstile bypass), `playwright` (vanilla), `patchright` (anti-detection fork) and `chrome-local` (the host's real desktop Chrome over CDP, for anti-bot that blocks every headless browser - see [docs/CHROME_LOCAL.md](docs/CHROME_LOCAL.md))
- **Own SERP search backend** (`search.provider: forage`): renders the search engine pages through the browser pool and parses each engine's DOM (BeautifulSoup), with ordered engines, automatic fallback on failure (never mistake a CAPTCHA for an empty answer), and parallel fan-out when more results than one engine offers are needed. Search engines can map to per-engine browser chains, e.g. `google: [scrapling, chrome-local]`
- **Two extract engines** (`extract.engine`, per-domain or per request): `readability` (default, Mozilla Readability.js in the browser + markdownify, keeps buyboxes/comments that trafilatura drops as non-main) and `trafilatura` (lighter plain-HTTP main content). Neither forces a render: the engine only matters once a page already needs the browser. Amazon product pages use `readability` by default
- **Anti-bot fallback** (`browser.fallback_solver`): if any engine hits a challenge, Forage retries the page with the Scrapling built-in solver as a last resort
- **Structured markdown output**: extraction is returned as real markdown (headings, bold, lists, code blocks) via trafilatura's markdown format or the Readability.js + markdownify engine
- **Basic stealth**: hides automation signals from Cloudflare-class protections (configurable, on by default)
- **In-memory TTL cache** with a master switch and per-operation toggles (search 5 min, extract off by default)
- **Optional Bearer API-key auth** (constant-time comparison, keys via env)
- **Config-driven**: `config.yaml` bind-mounted read-only, secrets in env vars, reload via container restart
- **Hermes integration**: bundled plugin, one-line backend switch (`web.search_backend` / `web.extract_backend`)
- **GPL v3**

## Architecture

```
Hermes Agent (web_search / web_extract)     Firecrawl client (base URL swap)
   │  local HTTP                                   │  POST /v1/scrape
   ▼                                               │
Forage plugin (WebSearchProvider)                  │
   │  POST /search, POST /extract                  │
   ▼                                               ▼
FORAGE (single container, :3672)
   ├── FastAPI
   │     ├── native API        POST /search, POST /extract
   │     └── Firecrawl compat  POST /v1/scrape, POST /v1/search
   ├── httpx + trafilatura  → static extraction (markdown output)
   ├── Chromium             → JS rendering via playwright | patchright | scrapling
   │                          (in-process pool, stealth, anti-bot solver fallback,
   │                           in-browser Readability.js for the "readability" engine)
   └── search               → own SERP engines (Google/Bing/Yahoo/DuckDuckGo) or SearXNG
```

The container runs its own Chromium as a subprocess. It never touches any external browser or CDP endpoint, unless a CDP endpoint is configured on purpose (`browser.cdp_url` / `browser.search.cdp_url`).

## Requirements

- Docker Engine 24+ with Docker Compose v2
- Nothing extra for search: the default `search.provider: forage` renders the SERPs inside Forage's own container, so the stack runs alone. SearXNG is only needed if you opt into it (see [docs/SEARXNG.md](docs/SEARXNG.md))
- ~3 GB free disk for the image (Chromium included)

## Quick start

The image ships its own default config, so the first run needs nothing but Docker.

1. **Run the published image** (no clone, no config file):

```bash
docker run --rm -p 127.0.0.1:3672:3672 ghcr.io/aldemaroc/forage:1.0.0
curl http://localhost:3672/health
# → {"status":"ok","service":"forage","version":"1.0.0",...}
```

2. **For a persistent setup**, clone and configure:

```bash
git clone https://github.com/aldemaroc/forage.git
cd forage
cp config.example.yaml config.yaml   # behavior: port, cache, search, browser, ...
cp .env.example .env                 # secrets: FORAGE_API_KEYS, TZ
```

Both files are required before `up`: the compose mounts `config.yaml` (without it Docker creates a directory in its place) and reads `.env`.

3. **Start it with the published image** (nothing to build):

```bash
docker compose pull                  # ghcr.io/aldemaroc/forage:1.0.0, also tagged 1.0, 1, latest
docker compose up -d
```

To build from source instead, run `docker compose up -d --build`: it skips the pull and tags the local build with the published name, so both paths are interchangeable.

4. **Search works out of the box.** With the default `search.provider: forage` the SERPs are rendered in Forage's own browser, in the same container: no SearXNG, no shared network, nothing else to start. Only if you would rather delegate search to SearXNG, set `search.provider: searxng` and follow [docs/SEARXNG.md](docs/SEARXNG.md) (it includes the `docker-compose.override.yml` that joins Forage to the SearXNG network).

5. **Try it**

```bash
# Search
curl -s -X POST http://localhost:3672/search -H 'Content-Type: application/json' \
  -d '{"query":"proxmox server","limit":3}'

# Extract (static)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://en.wikipedia.org/wiki/Guineafowl"],"formats":["markdown"]}'

# Extract (browser-forced; x.com has a force_render domain override by default)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://x.com/OpenAI"]}'

# Extract a PDF / office document (detected by extension or Content-Type)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com/paper.pdf"],"formats":["markdown"]}'

# Firecrawl-compatible (same port, Firecrawl's contract)
curl -s -X POST http://localhost:3672/v1/scrape -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}'
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

### Firecrawl compatibility: `POST /v1/scrape`, `POST /v1/search`

The same port also speaks Firecrawl's contract, so a client written against
Firecrawl works by changing its base URL:

```bash
curl -s -X POST http://localhost:3672/v1/scrape -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"],"onlyMainContent":true}'
```

```json
{
  "success": true,
  "data": {
    "markdown": "# Example Domain\n...",
    "metadata": {
      "title": "Example Domain",
      "sourceURL": "https://example.com",
      "url": "https://example.com",
      "statusCode": 200,
      "cacheState": "miss"
    }
  }
}
```

Errors use Firecrawl's shape and codes (`{"success": false, "code":
"SCRAPE_ALL_ENGINES_FAILED", "error": "..."}`), including HTTP 500 +
`SCRAPE_ALL_ENGINES_FAILED` for a page no engine could extract, which is what
Firecrawl reports and what clients key on to fall back to their own renderer.
Formats served: `markdown`, `html`, `rawHtml`; every other Firecrawl option is
accepted, ignored and listed in `data.warning` rather than rejected.
`POST /v1/search` speaks the v1 shape (`data` as an array). Full matrix,
mappings and non-goals: **[docs/FIRECRAWL.md](docs/FIRECRAWL.md)**.

## Configuration

All behavior is driven by `config.yaml` (bind-mounted read-only into the container) and env vars for secrets. Every option is documented in **[docs/CONFIG.md](docs/CONFIG.md)**. Here is the shape:

```yaml
server:   { host, port, workers, log_level }
cache:    { enabled, max_entries, search: {enabled, ttl}, extract: {enabled, ttl} }
search:   { provider (searxng|forage), searxng_url, default_lang, engines, timeout,
            serp_timeout }
extract:  { timeout, max_content_chars, only_main_content, engine, user_agent,
            browser_user_agent, respect_robots, force_render, wait_for,
            min_content_chars, raw_content_markdown, prefer_markdown,
            domain_overrides }
browser:  { engine, cdp_url, min_idle, max_instances, idle_timeout, headless,
            launch_timeout, stealth, network_idle_timeout, scroll_steps,
            challenge_timeout, solve_cloudflare, fallback_solver,
            search_engine_overrides,
            search: { headless, local, cdp_url, engine, humanize, min_interval,
                      retries, retry_backoff, settle_ms, user_agent } }
auth:     { enabled }
```

`browser.search.*` configures the browser that renders the SERP pages when
`search.provider: forage`, independently from the extract browser: headful by
default (the app starts its own Xvfb display), CDP endpoint or the container
browser, humanization, process-wide pacing and in-place retries.

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
domain override force_render | request force_render | wait_for → browser
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

## Releases

Container images are published to GitHub Container Registry and every tag has
a GitHub Release. Available tags: the exact version (`1.0.0`), `1.0`, `1`, and
`latest` (the latter not for pre-releases).

```bash
docker pull ghcr.io/aldemaroc/forage:1.0.0
docker run --rm -p 127.0.0.1:3672:3672 ghcr.io/aldemaroc/forage:1.0.0
curl http://localhost:3672/health
```

Building from source (`docker compose up -d --build`) stays supported and
produces an identical image.

### Cutting a release (maintainers)

`.github/workflows/release.yml` does the work on a tag push. The tag is the
single source of truth, and the workflow refuses to run when it does not match
`app/__init__.py`:

```bash
# 1. bump app/__init__.py and add the CHANGELOG entry, then commit
# 2. tag and push
git tag -a v1.0.1 -m "Forage 1.0.1"
git push origin v1.0.1
```

The workflow then builds the image, pushes it to `ghcr.io/<owner>/forage` and
opens the GitHub Release with generated notes. To publish a multi-arch
manifest (Apple/ARM servers, Raspberry Pi), add `linux/arm64` to `PLATFORMS` in
the workflow.

> First release only: GHCR creates the package private. Set it to public in the
> repository's *Packages* settings so anonymous `docker pull` works.

## Development

```bash
# Run tests / smoke checks against a running instance (see docs for details)
curl -s -X POST http://localhost:3672/search -H 'Content-Type: application/json' -d '{"query":"test","limit":3}'
```

The app lives in `app/` (FastAPI): config loading, caching, the SearXNG client, the own-SERP engine (`serp.py`), the virtual display, the hybrid extractor, the browser pool, the Firecrawl-compatible router and auth are each in their own module.

## License

[GPL v3](LICENSE). © 2026 Aldemaro Campos.

## Credits

Developed by **Aldemaro Campos and Chico** 🐔
