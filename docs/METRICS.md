# Metrics

Forage exposes Prometheus metrics at two paths, both serving the same payload:

| Path | Why both exist |
|---|---|
| `GET /metrics` | The path SearXNG uses for its own OpenMetrics endpoint, so a scrape config or dashboard written for SearXNG can be pointed at Forage. |
| `GET /admin/metrics` | The path Firecrawl uses for its admin metrics (plain Prometheus text). Same exposition, so a Firecrawl-style scrape keeps working. |

Both are `text/plain; version=0.0.4`, no auth by default.

```yaml
metrics:
  enabled: true          # false -> both paths answer 404 "open metrics is disabled"
  open_metrics: ""       # a password here turns on HTTP Basic auth, like SearXNG
```

With `open_metrics` set, a scrape must send HTTP Basic auth whose **password**
matches it; the username is ignored (SearXNG compares only the password too).
A missing or wrong credential answers `401`.

```console
$ curl -s -u prometheus:segredo http://127.0.0.1:3672/metrics | head
```

## One number per container, not per worker

`server.workers > 1` means several processes answer on the same port, and a
scrape lands on whichever one the kernel picks. Forage therefore runs
prometheus_client in multiprocess mode: `PROMETHEUS_MULTIPROC_DIR` is set to
`/tmp/forage-prometheus` before the library is imported, each worker writes its
own file there, and `MultiProcessCollector` merges them at scrape time. The
supervisor wipes that directory before starting uvicorn, so counters from a
previous run never survive a restart.

Counters and histograms merge exactly. **Averages cannot** be merged from
per-worker gauges: the three response-time averages report the highest worker
average (`livemax`) and reliability the lowest (`livemin`), so a slow or failing
worker is never hidden behind a healthy one.

## Series

### Traffic

| Series | Type | Labels |
|---|---|---|
| `forage_http_requests_total` | counter | `method`, `route`, `status` |
| `forage_http_request_duration_seconds` | histogram | `method`, `route` |

`route` is the **route template** (`/v1/scrape`, `/search`), never the URL: a
public instance would otherwise mint one series per requested URL.

### Search

| Series | Type | Labels |
|---|---|---|
| `forage_searches_total` | counter | `provider` (`forage`/`searxng`), `cache` (`hit`/`miss`/`bypass`/`disabled`), `status` (`ok`/`error`) |
| `forage_search_duration_seconds` | histogram | `provider`, `cache` |
| `forage_search_results` | histogram | – |

### Extraction

| Series | Type | Labels |
|---|---|---|
| `forage_extractions_total` | counter | `method` (`static`, `browser`, `browser+solver`, `browser+readability`, `pdf`, `docx`, `xlsx`, `pptx`, `rtf`, `other`), `status` (`ok`/`error`/`cached`) |
| `forage_extract_duration_seconds` | histogram | `method` |
| `forage_extract_chars` | histogram | – |

`method` is whitelisted in `app/main.py`: an unexpected value lands in `other`
instead of creating a series nobody planned for.

### Browser

| Series | Type | Labels |
|---|---|---|
| `forage_browser_renders_total` | counter | `engine`, `status` (`ok`/`error`) |
| `forage_browser_render_duration_seconds` | histogram | `engine` |
| `forage_browser_launches_total` | counter | `engine` (`scrapling`, `search`), `status` |
| `forage_browser_launch_duration_seconds` | histogram | `engine` |
| `forage_browser_session_open` | gauge | `engine` |
| `forage_browser_session_closes_total` | counter | `engine`, `reason` (`idle`, `budget`, `shutdown`) |

`forage_browser_session_open` is the one to watch when you care about idle
memory: 0 on every engine means no Chromium tree is alive. Launches are the
1-2 s the first render after an idle period pays (`browser.session_idle_timeout`).

### Cache

| Series | Type | Labels |
|---|---|---|
| `forage_cache_events_total` | counter | `op` (`search`/`extract`), `result` (`hit`/`miss`) |
| `forage_cache_entries` | gauge | `op` |

### Session janitor

| Series | Type | Labels |
|---|---|---|
| `forage_janitor_sweeps_total` | counter | – |
| `forage_janitor_kills_total` | counter | – |

Every browser driver runs in its own session and the code that opened it kills
that session when it closes. `kills` above zero means the janitor had to step in:
a worker died before it could close (OOM, crash, restart), or a Playwright browser
was left with no live driver above it. Reaping crashpad handlers of dead browsers
also counts here, which is why a small non-zero number is normal after a hard
kill, and not a sign of a leak.

### Process and container

| Series | Type | Notes |
|---|---|---|
| `forage_process_rss_bytes` | gauge | RSS of the scraped worker |
| `forage_process_threads` | gauge | threads in that worker |
| `forage_process_open_fds` | gauge | open file descriptors |
| `forage_container_memory_bytes` | gauge | memory charged to the cgroup, browsers included |
| `forage_container_tasks` | gauge | PIDs/tasks in the cgroup (the number `pids_limit` caps) |
| `forage_uptime_seconds` | gauge | seconds since that worker started |
| `forage_info` | gauge (always 1) | `version`, `engine`, `provider`, `python` |

The runtime gauges are read at scrape time (from `/proc` and the cgroup files),
so nothing costs anything when nobody scrapes.

### SearXNG-compatible engine series

Same names, same `engine_name` label and same meaning as SearXNG's `/metrics`,
so existing SearXNG dashboards and alerts work unchanged:

| Series | Type | Meaning |
|---|---|---|
| `searxng_engines_request_count_total` | counter | searches sent to that engine |
| `searxng_engines_result_count_total` | counter | results returned by that engine |
| `searxng_engines_response_time_total_seconds` | gauge | average time per engine call (retries included) |
| `searxng_engines_response_time_http_seconds` | gauge | average share spent inside the browser render |
| `searxng_engines_response_time_processing_seconds` | gauge | average remainder (parse and classify) |
| `searxng_engines_reliability_total` | gauge | percentage of calls that returned a usable page |

Differences from SearXNG worth knowing:

* these series only exist when `search.provider: forage` (its own SERP engines);
  with `provider: searxng` the engines are SearXNG's, so scrape SearXNG instead;
* the averages are per-worker maxima (`livemax`) as explained above;
* reliability is a percentage, SearXNG calls it a counter — the value semantics
  are the same, only the exposition type differs.

SearXNG's human-facing `/stats` and `/stats/errors` pages are HTML views over the
same numbers and are **not** implemented; the data is here, in Prometheus form.

## Scrape config

```yaml
scrape_configs:
  - job_name: forage
    scrape_interval: 30s
    static_configs:
      - targets: ["forage:3672"]
    # only when metrics.open_metrics is set
    # basic_auth:
    #   username: prometheus
    #   password: <same value as metrics.open_metrics>
```

Queries that answer the usual questions:

```promql
# searches per minute, split by outcome
sum by (status) (rate(forage_searches_total[5m])) * 60

# p95 extract latency by method
histogram_quantile(0.95, sum by (le, method) (rate(forage_extract_duration_seconds_bucket[5m])))

# are any browsers alive right now?
sum(forage_browser_session_open)

# is the container near the pids ceiling?
max(forage_container_tasks)

# engine reliability below 90%
searxng_engines_reliability_total < 90
```
