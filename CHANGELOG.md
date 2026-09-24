# Changelog

Notable changes per release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/): releases before 1.0.0 were the
testing phase, and their history lives in the commit log.

## [1.0.0] - 2026-09-24

First tagged release: the API, the configuration surface and the container
image are now considered stable.

### Added

- **Firecrawl-compatible API** (`app/firecrawl.py`): `POST /v1/scrape` answers
  in Firecrawl's envelope (document + `metadata` block) and with Firecrawl's
  error codes, so a Firecrawl client only needs its base URL changed.
  `POST /v1/search` speaks the v1 search shape as well. Formats served:
  `markdown` (default), `html`, `rawHtml`; every other Firecrawl option is
  accepted, ignored, and reported in `data.warning`. See
  [docs/FIRECRAWL.md](docs/FIRECRAWL.md).
- **Published container images**: pushing a `v*` tag builds the image and
  publishes it to `ghcr.io/aldemaroc/forage`, then opens the GitHub Release
  with generated notes (`.github/workflows/release.yml`). Tags follow the
  semver tag: `1.0.0`, `1.0`, `1`, `latest`.
- **Search browser configuration** (`browser.search.*`): the SERP browser is
  configured separately from the extract browser, with its own engine, CDP URL,
  headful/headless mode, humanization, pacing and retries.
- **Automatic virtual display** (`app/display.py`): a headful browser needs an
  X display, and the container now starts its own Xvfb on demand, reusing an
  existing one and clearing a stale `/tmp/.X<n>-lock` left by a restart.

### Changed

- **The SERP browser is opened per search and closed at the end of it**
  (`SearchBrowser`), instead of living in a warm pool. A search no longer keeps
  a browser resident between queries, and every engine of one search shares the
  same session, so cookies and history accumulate as a real user's would.
  Extraction keeps its warm pool unchanged.
- **SERP search is rate limited and retried in place**: a challenged render is
  retried on the same browser (`browser.search.retries`) after a backoff
  instead of immediately spending the next browser engine, and renders are
  paced process-wide (`browser.search.min_interval`, jittered). A burst of
  consecutive renders is what search engines answer with an anti-bot page.
- **`browser.search_engine_overrides` only honors `playwright`/`patchright`**,
  the engines that can be launched per search. `scrapling` keeps a session of
  its own and `obscura`/`chrome-local` are CDP endpoints: point
  `browser.search.cdp_url` at those instead. Unusable entries are dropped with
  a warning instead of failing.
- **Defaults aligned with the shipped example**: `extract.engine` is now
  `readability` (Readability.js + markdownify) and `browser.engine` is now
  `scrapling` (strongest anti-bot). A config that sets neither key behaves like
  `config.example.yaml`. The extract engine only applies to pages that already
  need a render, so nothing gets slower by default.
- **`docker-compose.yml` now references the published image**
  (`ghcr.io/aldemaroc/forage:1.0.0`). `build: .` still builds locally and
  compose tags that build with the same name.
- **No external network by default**: the compose no longer joins
  `searxng_default`, so the stack runs on its own. SearXNG is opt-in and now
  needs a `docker-compose.override.yml` joining that network, shown in
  [docs/SEARXNG.md](docs/SEARXNG.md).

### Upgrading from the testing phase

```bash
cd forage
git pull
# a local build keeps working; drop the old local image name
docker rmi forage:0.9.0 2>/dev/null || true
docker compose up -d --build
curl http://localhost:3672/health     # → "version":"1.0.0"
```

An existing `config.yaml` keeps working: every key it sets wins over the
defaults. If it does not set `extract.engine`/`browser.engine`, extraction now
runs `readability` + `scrapling`, the same pair `config.example.yaml` ships.

If you search through SearXNG (`search.provider: searxng`), add the override
file from [docs/SEARXNG.md](docs/SEARXNG.md) **before** recreating the
container: the compose no longer joins the SearXNG network on its own.
`browser.search.headless` defaults to `false` (headful), which is what makes
the own-SERP engines usable against Google; the Xvfb display is started by the
app, no Docker flags needed.
