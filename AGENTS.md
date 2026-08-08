# AGENTS.md

Guidance for AI agents and contributors working on this repository. Read this
before changing code.

## What this is

Forage is a self-hosted, single-container web search and extraction service:
a REST API that fetches a URL and returns clean, structured content (markdown)
without running a cloud scraper. It is designed to be lightweight, fast and
easy to operate: plain HTTP fetch first, headless browser only when the page
really needs JavaScript.

The project is developed by the repository owner together with an AI coding
assistant; the repository is public and must stay professional. Everything in
this repo (code, docs, commits) is written in English.

## Architecture

Single Python service (FastAPI + uvicorn), one container. Modules in `app/`:

| Module | Responsibility |
|---|---|
| `main.py` | FastAPI app, endpoints, pool lifespan, auth dependency |
| `config.py` | YAML loader: defaults -> deep merge -> validation; typed dataclasses |
| `cache.py` | Thread-safe TTL LRU cache (search and extract), `clear()` for purge |
| `searxng.py` | SearXNG client (`/search?format=json`) -> Hermes envelope |
| `extract.py` | Hybrid extraction heuristic (static -> browser), SPA/challenge detection |
| `browser.py` | In-process Chromium pool (playwright/patchright) OR single Scrapling session; semaphore, cleanup, page_action |
| `documents.py` | PDF/docx/xlsx/pptx/rtf extraction from raw bytes (never through the browser) |
| `auth.py` | Bearer API keys (env `FORAGE_API_KEYS`), constant-time comparison |

API: `GET /health`, `POST /search`, `POST /extract`, `POST /admin/cache/purge`.

## Core design decisions (do not reverse without a strong reason)

- **Static first, browser as fallback.** A full browser is expensive (RAM and
  latency). Most pages extract fine with plain HTTP + trafilatura. The browser
  is the exception, not the default.
- **Hybrid decision flow** in `extract.py` (in order):
  1. Domain override `force_render` / request `force_render` / `wait_for` -> browser
  2. Static fetch (httpx)
  3. HTTP 401/403/429 -> browser
  4. `needs_browser_render(html, text)` -> browser if ANY check fires
  5. Otherwise deliver the static result
- **`needs_browser_render` runs 4 independent checks; any one of them sends the
  page to the browser:**
  1. SPA framework markers in the static HTML (`id="root"`, `id="__next"`,
     `data-reactroot`, `__NEXT_DATA__`, `ng-version`, `__INITIAL_STATE__`,
     `data-svelte`, `id="__gatsby"`, `ytInitialData`, `ytcfg`, ...).
  2. Content density: HTML >= 50 KB with extracted text <= max(500, 1% of
     HTML). A big shell with almost no text means content is mounted by JS.
  3. Empty `<main>`: a `<main>` element exists in the static HTML but holds
     <= 100 chars of visible text (placeholder awaiting client-side render).
  4. Extracted text below `min_content_chars` (default 200).
- **Markdown output via trafilatura** (`output_format="markdown"`): preserves
  headings, bold, lists, code blocks. Tested against html2text/markdownify/
  readability-lxml: trafilatura markdown is the best cost/benefit.
- **Extract engine is pluggable via config** (`extract.engine`, default
  `trafilatura`; per-domain via the `engine` override key; request-level
  `engine` param is absolute). The `readability` engine runs Mozilla
  Readability.js inside the page (`page.evaluate`, no Node runtime) and
  converts the article with markdownify. It exists because trafilatura's
  main-content heuristic drops non-article blocks (Amazon buybox), and
  `full_text` only recovers them as plain text. `.amazon.*` uses it by
  default.
- **Stealth built in, no extra dependency**: `browser.stealth: true` (default)
  sets `--disable-blink-features=AutomationControlled` + an init script that
  masks `navigator.webdriver`/`chrome`/`languages`/`plugins`, plus a real
  desktop Chrome UA. Do not add playwright-stealth/curl_cffi unless a real case
  demands it (YAGNI, maintenance cost).
- **Engine is pluggable via config** (`browser.engine`):
  `playwright` (default in example), `patchright`, `scrapling` (default on the
  production instance since 2026-08-07; strongest anti-bot), `obscura`
  (experimental Rust/V8 browser via CDP).
- **Cache in memory, per operation**: search ON (TTL 300s, protects SearXNG
  engines from bot detection), extract OFF by default (always fresh; TTL 120s
  on the instance). Master switch `cache.enabled` + per-section toggles.
- **Config reload = container restart only.** The config decides whether
  browser processes start; hot reload would be fragile. 1-2s downtime is fine.
- **`raw_content_markdown: true` (default)**: `raw_content` mirrors the clean
  markdown so consumers that read `raw_content` first (Hermes' web extract
  tool) get clean text. `false` returns raw HTML in `raw_content`.
- **Auth optional (Bearer)**: `auth.enabled: false` default (local use + bind
  127.0.0.1). Keys via env, `hmac.compare_digest`. `/health` always open.

## API contract (critical, tested against the Hermes consumer)

- `POST /search` returns the envelope
  `{"success": true, "data": {"web": [{title, url, description, position}]}}`.
- `POST /extract` returns a LIST of result dicts per URL, one entry per URL
  (not the envelope): `{url, title, content, raw_content, method}`.
- A URL that could not be extracted returns `{url, error}`.
- `method` is one of `static`, `browser`, `browser+solver`, `browser+readability`,
  or a document method (pdf/docx/xlsx/pptx/rtf).

## Configuration

All options live in `config.yaml` (instance) / `config.example.yaml` (repo).
Full reference: `docs/CONFIG.md`.

### Domain overrides (`extract.domain_overrides`, since 0.8.0)

One structure per URL pattern, replacing the older separate lists
(`force_render_domains`, `url_rewrites`, `full_text_domains`). A domain can now
combine overrides (e.g. Amazon needs `force_render` + `full_text` for the buy
box price; Reddit needs `url_rewrite` + `full_text` for comments; YouTube needs
`force_render` + `scroll`).

Pattern syntax (www-insensitive, case-insensitive):

| Pattern | Matches |
|---|---|
| `x.com` | host or any subdomain (endswith) |
| `.amazon.*` | base domain + subdomains, wildcard anchored per label (never substring: does NOT match `buyamazon.com`) |
| `reddit.com/r/` | EXACT host + path prefix (does NOT match `old.reddit.com/r/`) |

Available overrides: `force_render`, `full_text`, `engine` ("trafilatura" |
"readability"), `wait_for`, `url_rewrite`, `scroll`, `timeout` (1-120s),
`network_idle_timeout` (0-60s), `challenge_timeout` (0-120s).

Precedence: the most specific override wins (longest pattern); request-level
params (`force_render`/`wait_for` in the call) are ABSOLUTE and beat the
override.

## Golden rules (learned the hard way)

- **When a page seems to be missing data, do NOT blame the browser/fingerprint
  first.** Test the browser engine directly against the URL and save the full
  HTML before concluding. The pipeline itself can lose data in two places:
  1. trafilatura `only_main_content=True` discards non-article content (buy
     boxes inside `<form>`, comments) as boilerplate.
  2. `raw_content = html[:max_content_chars]` (default 100k) truncates large
     pages (a 1.6MB Amazon page keeps the price at position ~272k, cut off).
  Fix with `engine: readability` in the domain override (Readability.js keeps
  the buybox and returns markdown) or by raising `max_content_chars`.
- **`challenge-platform` must NEVER be a challenge marker**: Cloudflare injects
  `/cdn-cgi/challenge-platform/...` into every page it serves, even without an
  active challenge. It caused false "Blocked by anti-bot challenge" on
  openai/discord/fandom/canva/dailymail. Title is the primary signal; markers
  are secondary.
- **Marker `ng-app` needs the attribute form (`ng-app=`)**: bare `ng-app`
  false-positives on "shopping-app" in prose.
- **Cloudflare managed challenge can answer 200 on static fetch**: after the
  static fetch, if `looks_like_challenge` -> force browser instead of
  delivering an error.
- **`network_idle=True` on the Scrapling session hangs streaming pages**
  (x.com never idles). Keep `network_idle=False` on the session and replicate
  network-idle-with-cap inside the `page_action`.
- **`browser.solve_cloudflare: false` is the default**: the native solver waits
  ~5s of networkidle on EVERY page before detecting a challenge (even without
  one). Title polling in `page_action` resolves non-interactive challenges with
  no fixed cost. Turn the solver on only for interactive challenges (rare).
- **`disable_resources=True` (Scrapling) must NOT be used**: it blocks
  stylesheets/fonts and breaks pages (fandom returned 1KB, x.com incomplete).
- **Scrapling's `start()`/`close()` are coroutines**: always `await` them.
- **`host.docker.internal` does not resolve in a custom compose network**: use
  a shared docker network and the service name (`searxng:8080`).
- **File bind mounts do not follow renames**: editing `config.yaml` in place
  swaps the inode, and a file bind mount keeps pointing at the old inode. After
  editing the config, recreate the container (`docker compose up -d --build`).
- **Smooth scrolling is required to trigger lazy load** on some sites
  (IntersectionObserver): `scrollBy`/viewport jumps do not fire it. Use
  `browser.scroll_steps` or the `scroll` override.
- **Sites vary between runs**: anti-bot is intermittent (stackoverflow, tiktok,
  ebay). A failure in one benchmark round is not a regression; re-test isolated
  before concluding.

## Testing

```bash
# Health
curl -s http://localhost:3672/health

# Search
curl -s -X POST http://localhost:3672/search -H 'Content-Type: application/json' \
  -d '{"query":"test","limit":3}'

# Static extract
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://en.wikipedia.org/wiki/Web_scraping"],"formats":["markdown"]}'

# Force browser (x.com)
curl -s -X POST http://localhost:3672/extract -H 'Content-Type: application/json' \
  -d '{"urls":["https://x.com/OpenAI"]}'
```

Reference validation set (regression baseline): Wikipedia/GitHub/docs = static;
x.com = browser; a Cloudflare-protected page = browser + challenge handling.
A benchmark script lives in `benchmark/` (three containers with configs that
differ only in `browser.engine`, same URL list, same criteria; ✅ = no error,
content >= 100 chars, non-empty title).

## Conventions

- All code, docs, commit messages and PR text in **English**.
- No em dashes (—) or en dashes (–) anywhere; use hyphens or commas.
- Do not write anything that reads like generic AI output; be direct and
  specific.
- License: GPL v3 (see LICENSE). Docs/README in English, no personal data.
- Credits in the README follow the project convention.
- No semantic version bump per feature while the project is in testing phase;
  version only when leaving testing (owner decides).
- Do not commit on every change: the owner asks for consolidated commits.

## Docs map

- `README.md` — what it is, quickstart, API reference
- `docs/CONFIG.md` — every config key
- `docs/SEARXNG.md` — SearXNG install + docker network pitfalls
- `docs/HERMES.md` — integration with the Hermes agent
- `docs/BENCHMARK.md` — engine comparison tables
- `config.example.yaml` — default configuration (keep in sync with CONFIG.md)
