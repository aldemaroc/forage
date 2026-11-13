# Changelog

Notable changes per release. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/): releases before 1.0.0 were the
testing phase, and their history lives in the commit log.

## [Unreleased]

## [1.2.1] - 2026-11-13

### Changed

- **Browsers are killed by session, and the orphan reaper is gone (issue #14).**
  Every Playwright/Patchright driver now starts as a session leader, and the tree it
  spawns gets its own sessions (Chromium calls `setsid` itself, the crashpad handler
  does it again). The owner collects those sessions while the tree is alive, closes
  the browser gracefully, then kills every collected session, on success, error,
  timeout or cancellation. No process-tree guess decides what dies: the sessions were
  recorded from the driver that started them.
- The case that needs a survivor is a worker that dies before it can close its
  browsers (OOM kill, crash, `kill -9`). The supervisor, the only process that
  outlives every worker, runs a session janitor: it kills the tree of a driver whose
  owner is gone (from a registry that records both pids and start times), and then
  sweeps Playwright browsers with no live driver above them plus crashpad handlers no
  live browser claims. Recovery there takes up to `server.janitor_interval` (30s by
  default), which is the only window where a leaked browser can survive.
- `forage_reaper_sweeps_total` and `forage_reaper_kills_total` are replaced by
  `forage_janitor_sweeps_total` and `forage_janitor_kills_total`. Dashboards that
  query the old names go empty; the new counters count sessions, not processes.
- `server.reap_orphans` and `server.reap_interval` are gone. Config files that
  still set them keep loading: both keys are ignored with a warning. The janitor
  interval is `server.janitor_interval` (default 30).

## [1.2.0] - 2026-11-12

### Added

- **Prometheus metrics (`GET /metrics`, issue #11).** Forage now exposes
  counters, histograms and gauges for traffic, searches, extractions, browser
  renders/launches, cache hit/miss, orphan-reaper kills and container/process
  memory, on top of `forage_info` with the version and engine. The exposition
  also carries SearXNG's six engine series (`searxng_engines_*` with
  `engine_name`) so a dashboard or scrape config written for SearXNG works
  unchanged when it is pointed at Forage, and the same payload is served at
  `/admin/metrics`, the path Firecrawl uses. `metrics.open_metrics` reproduces
  SearXNG's password gate (HTTP Basic, username ignored) and a disabled
  endpoint answers `404 open metrics is disabled`. Labels come from closed sets
  only (route templates, not URLs), and prometheus_client runs in multiprocess
  mode so counters merge across `server.workers` instead of each worker
  reporting only its own. See `docs/METRICS.md`.
- **Idle release (`browser.close_pages_when_idle` and
  `browser.session_idle_timeout`).** The scrapling page pool kept up to
  `max_instances` tabs per worker warm between renders, and every warm tab
  holds a Chromium renderer plus the decoded DOM (~70 MB per heavy page): ten
  forced renders left the container 765 MB heavier and it never came back down.
  The pool's tabs now close as soon as no render is in flight, and the whole
  session (Chromium tree plus Playwright driver, ~450 MB) closes after
  `session_idle_timeout` seconds of idleness and is relaunched on the next
  render. The session is also created lazily now instead of at boot, so an idle
  Forage keeps no browser at all: 153 MB and 9 processes, against 683 MB before
  the first request.
- **Cache sweep (`cache.sweep_interval`, 30s).** Expired entries used to sit in
  the dict until someone asked for the key or the LRU pushed them out, keeping
  page contents in memory long after they stopped being useful. A background
  task drops them and calls `malloc_trim` so the freed arenas go back to the
  kernel instead of staying in the process's RSS.
- **Orphan sweep (`server.reap_orphans`, on by default).** A worker that dies
  (OOM kill, crash, restart) leaves its Playwright driver and the Chromium
  trees it started re-parented to the container's PID 1, and `docker-init` only
  reaps *dead* children: the live orphan keeps its task slots and memory
  forever with nobody left to close it. The supervisor process now scans for
  processes whose parent is PID 1 (excluding itself, Chromium's crashpad
  handlers, which double-fork on purpose, and the shared Xvfb) and kills their
  whole subtree with `SIGKILL`, every `server.reap_interval` seconds and once
  at startup. A browser that belongs to a live worker is always a descendant of
  that worker, never a child of PID 1, so the sweep cannot touch a healthy
  search.

### Fixed

- **The search browser is closed even when the request is cancelled or the
  launch fails.** `SearchBrowser.stop()` wrapped each close in
  `except Exception`, so a `CancelledError` (a `BaseException`) escaped before
  `playwright.stop()` and the node driver plus Chromium stayed alive for the
  life of the container. `__aenter__` had the mirror problem: a launch that
  dies at `new_page()` raises before `__aexit__` ever runs, so a failed launch
  (a crashed Chromium, a dead CDP endpoint) leaked a driver tree as well.
  Cleanup now runs in a shielded task with per-step timeouts, closes the
  browser explicitly in local mode, and runs when `start()` fails. Measured in
  a disposable container: 3 failed searches used to leak 3 driver trees (about
  90 container tasks each); 50 failed and 50 successful searches now end at
  the baseline process count.

- **The Xvfb display belongs to the supervisor, not to a worker.** When the
  worker that started it died, the display survived as an orphan that the
  remaining worker was still using: killing it takes headful search down, and
  leaving it alive defeats the point of an orphan sweep. It is now started
  before uvicorn spawns the workers, so it goes away with the container.

## [1.1.0] - 2026-11-09

A SearXNG-compatible read API, plus the container fix that keeps the search
path alive over weeks of uptime.

### Fixed

- **The compose file runs the container with an init process (`init: true`).**
  Every one-shot search browser leaves Chromium helper processes
  (`chrome_crashpad_handler`, `zygote`) that get re-parented to PID 1 when the
  browser closes. With the app as PID 1 nothing called `wait()`, so they piled
  up as zombies at roughly 4-5 per search, each one still holding a task slot
  in the container's cgroup. After ~150 searches the container reached systemd's
  `DefaultTasksMax` (1027 on a host with `threads-max=6847`) and every new
  search failed with `pthread_create: Resource temporarily unavailable`:
  `POST /search` answered 500 while `/extract` kept working, because that
  browser is long-lived. Docker's init reaps the orphans (measured: 0 zombies
  after a run of searches, task count flat).

- **A wedged scrapling session no longer hangs the extract endpoint.** The
  shared session's `fetch()` had no wall-clock cap: once the browser behind it
  stopped answering, the request never returned, the HTTP client gave up first
  (Hermes reported `Could not reach Forage at http://localhost:3672`) and the
  semaphore slot was never released, so further renders queued behind it. Every
  scrapling fetch now runs under `asyncio.wait_for` with a hard cap of
  `effective timeout + browser.render_timeout_grace` (20 s by default) and a
  blown cap raises `RenderTimeout`, which the caller turns into a fast error
  item.

### Added

- **SearXNG-compatible read API (`GET /search` and `GET /`).** A thin adapter
  over the same `run_search` answers SearXNG's request parameters and JSON
  envelope, so a client that only speaks SearXNG as a search provider
  (LibreChat, most self-hosted chat UIs) can point its instance URL at Forage:
  same engines, same browser, same pacer, same cache entries as the native API.
  `format=json` only (403 otherwise, like a SearXNG instance with the format
  disabled), `general` category, page 1, `time_range`/`safesearch` accepted and
  ignored; `engine`, `engines` and `score` are derived from the search and
  documented as approximations. Native `POST /search` is untouched. Contract,
  approximations and the LibreChat recipe: `docs/SEARXNG-API.md`.

- **The shared scrapling session is recycled on a budget**
  (`browser.session_max_renders`, default 200 renders, and
  `browser.session_max_age`, default 6 h) and immediately after a blown hard
  cap, since a wedged session does not recover on its own. Recycling is
  deferred while other renders are in flight so it never breaks them, and it
  costs well under a second.
- `browser.render_timeout_grace`, `browser.session_max_renders` and
  `browser.session_max_age` in `docs/CONFIG.md` and `config.example.yaml`.

## [1.0.1] - 2026-09-24

Search latency and diagnostics follow-up to 1.0.0.

### Changed

- **Fallback engines are queried one at a time, stopping at the limit.** All
  engines of a search share one page and one pacer, so the renders were
  serialized anyway; firing every engine up front only produced renders whose
  results were thrown away once the limit was already met.
- **Only the primary engine retries a challenged render.** A fallback answered
  with an anti-bot page now hands the search to the next engine instead of
  spending the search budget on its own retries (measured: two failed fallback
  attempts cost more than the three successful renders before them).
- **The results-selector wait is capped at 4 s** (was 10 s). A SERP that never
  renders the selector is a challenge or an empty page, and waiting longer only
  delayed the next engine.
- **`browser.search.min_interval` defaults to `2.5`** (was `5.0`). Verified
  against Google: five consecutive searches spaced by 2.5 s, five usable SERPs.
- **`brave` is out of the default `search.engines`** (`[google, bing,
  duckduckgo]`). The engine stays implemented and can be re-added by name; it
  is the one whose anti-bot answers a rendered SERP with a captcha most often.

### Fixed

- **`data.engines` reports every engine that was queried.** Hitting the limit
  stopped the collection loop before the status of engines already consulted
  was recorded, so the diagnostics omitted engines that had actually run (and
  the time they cost).

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

### Removed

- **The engine benchmark** (`benchmark/` and `docs/BENCHMARK.md`). Comparing
  browser engines from scratch is no longer part of the project: `scrapling` is
  the default engine, and the per-domain overrides in `config.example.yaml`
  keep the note of what each override was tested against.

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
