"""Prometheus metrics for Forage (issue #11).

Design rules, in order of importance:

1. **Bounded cardinality.** No URL, query or hostname ever becomes a label: a
   public search service sees unbounded values, and Prometheus pays for every
   distinct combination. Labels here are taken from closed sets (method, engine,
   provider, cache state, pipe/route template).
2. **Cheap when nobody scrapes.** Every metric is a Counter/Gauge/Histogram from
   ``prometheus_client``: increments are a dict lookup plus a lock, and the
   text formatting only happens inside ``GET /metrics``.
3. **One number per container, not per worker.** Forage runs with
   ``server.workers > 1``; a plain registry would answer with the counters of
   whichever worker took the scrape. With ``PROMETHEUS_MULTIPROC_DIR`` set (the
   default inside the container, see ``app.main``), prometheus_client writes one
   file per process and ``MultiProcessCollector`` merges them at scrape time.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import multiprocess
from prometheus_client.core import GaugeMetricFamily

#: True when prometheus_client is in multiprocess mode (uvicorn workers).
MULTIPROCESS = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))

# Metrics are always registered in this private registry. In multiprocess mode
# the values that matter live in the per-worker files, so the *exposed* registry
# is a different one holding only MultiProcessCollector: registering the objects
# in both would emit every family twice (once from the file, once from the live
# object), which Prometheus rejects as duplicate series.
REG = CollectorRegistry()

if MULTIPROCESS:
    # MultiProcessCollector validates the directory the moment it is built, so
    # it has to exist before the registry is wired (the supervisor wipes it at
    # boot; a worker only ever recreates it).
    os.makedirs(os.environ["PROMETHEUS_MULTIPROC_DIR"], exist_ok=True)
    EXPOSITION = CollectorRegistry()
    multiprocess.MultiProcessCollector(EXPOSITION)
else:
    EXPOSITION = REG

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_PROCESS_START = time.monotonic()


def _gauge(name: str, doc: str, labels: tuple = (), mode: str = "livesum") -> Gauge:
    """Gauge that also works in multiprocess mode.

    ``mode`` decides how the per-worker values are merged on scrape:
    ``livesum`` adds the workers that are still alive (cache entries, open
    sessions), ``livemax`` takes the largest (host-level numbers every worker
    reports identically, like container memory).
    """
    if MULTIPROCESS:
        return Gauge(name, doc, labels, registry=REG, multiprocess_mode=mode)
    return Gauge(name, doc, labels, registry=REG)


# --- Traffic ---------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "forage_http_requests_total",
    "HTTP requests served, by method, route template and status code.",
    ("method", "route", "status"),
    registry=REG,
)

HTTP_DURATION = Histogram(
    "forage_http_request_duration_seconds",
    "Wall-clock time to answer an HTTP request.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120),
    registry=REG,
)

# --- Search ----------------------------------------------------------------

SEARCHES = Counter(
    "forage_searches_total",
    "Searches executed, by provider, cache state and outcome.",
    ("provider", "cache", "status"),
    registry=REG,
)

SEARCH_DURATION = Histogram(
    "forage_search_duration_seconds",
    "Time to answer a search (cache hits included, they are near zero).",
    ("provider", "cache"),
    buckets=(0.05, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60),
    registry=REG,
)

SEARCH_RESULTS = Histogram(
    "forage_search_results",
    "Result count per search.",
    buckets=(0, 1, 3, 5, 10, 20, 30, 50, 100),
    registry=REG,
)

# --- Extraction ------------------------------------------------------------

EXTRACTIONS = Counter(
    "forage_extractions_total",
    "URL extractions, by fetch method and outcome.",
    ("method", "status"),
    registry=REG,
)

EXTRACT_DURATION = Histogram(
    "forage_extract_duration_seconds",
    "Time to extract one URL.",
    ("method",),
    buckets=(0.05, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60),
    registry=REG,
)

EXTRACT_CHARS = Histogram(
    "forage_extract_chars",
    "Characters returned per extracted URL (payload size, not memory).",
    buckets=(100, 1_000, 10_000, 50_000, 100_000, 250_000, 500_000, 1_000_000),
    registry=REG,
)

# --- Browser ---------------------------------------------------------------

BROWSER_RENDERS = Counter(
    "forage_browser_renders_total",
    "Browser renders, by engine and outcome.",
    ("engine", "status"),
    registry=REG,
)

BROWSER_RENDER_DURATION = Histogram(
    "forage_browser_render_duration_seconds",
    "Time spent inside a browser render.",
    ("engine",),
    buckets=(0.1, 0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60),
    registry=REG,
)

BROWSER_LAUNCHES = Counter(
    "forage_browser_launches_total",
    "Session/browser launches, by engine and outcome. A launch is the 1-2s the "
    "first render after an idle period pays.",
    ("engine", "status"),
    registry=REG,
)

BROWSER_LAUNCH_DURATION = Histogram(
    "forage_browser_launch_duration_seconds",
    "Time to start a browser session.",
    ("engine",),
    buckets=(0.25, 0.5, 1, 1.5, 2, 3, 5, 10, 20),
    registry=REG,
)

BROWSER_SESSION_OPEN = _gauge(
    "forage_browser_session_open",
    "1 while the worker holds a live browser session, 0 after the idle close.",
    ("engine",),
    mode="livesum",
)

BROWSER_SESSION_CLOSES = Counter(
    "forage_browser_session_closes_total",
    "Browser sessions closed, by engine and reason (idle, budget, death).",
    ("engine", "reason"),
    registry=REG,
)

# --- Cache -----------------------------------------------------------------

CACHE_EVENTS = Counter(
    "forage_cache_events_total",
    "Cache lookups, by operation and result.",
    ("op", "result"),
    registry=REG,
)

CACHE_ENTRIES = _gauge(
    "forage_cache_entries",
    "Entries currently held by the in-memory cache.",
    ("op",),
    mode="livesum",
)

# --- Session janitor -------------------------------------------------------

JANITOR_SWEEPS = Counter(
    "forage_janitor_sweeps_total",
    "Session janitor sweeps run.",
    registry=REG,
)

JANITOR_KILLS = Counter(
    "forage_janitor_kills_total",
    "Browser sessions killed after their owner was gone or nobody was driving them.",
    registry=REG,
)

# --- Process / container ---------------------------------------------------

INFO = _gauge(
    "forage_info",
    "Build info; always 1.",
    ("version", "engine", "provider", "python"),
    mode="livemax",
)


# --- SearXNG-compatible engine series --------------------------------------
#
# SearXNG exposes engine statistics at ``GET /metrics`` (gated by
# ``general.open_metrics``) with exactly these six series, every one labelled
# by ``engine_name``. Forage serves the same surface so a dashboard or scrape
# config written for SearXNG keeps working when it is pointed at Forage.
#
# Two deliberate differences, both caused by prometheus_client's multiprocess
# merge: counters merge exactly, but an *average* cannot be merged from
# per-worker gauges. The three response-time gauges therefore report the
# highest worker average (``livemax``) and reliability the lowest (``livemin``),
# which is what matters for alerting and never hides the worst worker.

SEARXNG_REQUEST_COUNT = Counter(
    "searxng_engines_request_count_total",
    "The total amount of user requests made to this engine",
    ("engine_name",),
    registry=REG,
)

SEARXNG_RESULT_COUNT = Counter(
    "searxng_engines_result_count_total",
    "The total amount of results returned by the engine",
    ("engine_name",),
    registry=REG,
)

SEARXNG_TIME_TOTAL = _gauge(
    "searxng_engines_response_time_total_seconds",
    "The average total response time of the engine",
    ("engine_name",),
    mode="livemax",
)

SEARXNG_TIME_PROCESSING = _gauge(
    "searxng_engines_response_time_processing_seconds",
    "The average processing response time of the engine",
    ("engine_name",),
    mode="livemax",
)

SEARXNG_TIME_HTTP = _gauge(
    "searxng_engines_response_time_http_seconds",
    "The average HTTP response time of the engine",
    ("engine_name",),
    mode="livemax",
)

SEARXNG_RELIABILITY = _gauge(
    "searxng_engines_reliability_total",
    "The overall reliability of the engine",
    ("engine_name",),
    mode="livemin",
)

# engine -> [sent, ok, results, sum_total, sum_http]
_ENGINE_STATE: dict[str, list] = {}


# --- Recording helpers -----------------------------------------------------


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_DURATION.labels(method=method, route=route).observe(max(seconds, 0.0))


def observe_search(
    provider: str, cache: str, status: str, seconds: float, results: int
) -> None:
    SEARCHES.labels(provider=provider, cache=cache, status=status).inc()
    SEARCH_DURATION.labels(provider=provider, cache=cache).observe(max(seconds, 0.0))
    SEARCH_RESULTS.observe(max(results, 0))


def observe_extract(method: str, status: str, seconds: float, chars: int) -> None:
    EXTRACTIONS.labels(method=method, status=status).inc()
    EXTRACT_DURATION.labels(method=method).observe(max(seconds, 0.0))
    if chars:
        EXTRACT_CHARS.observe(chars)


def observe_cache(op: str, result: str) -> None:
    CACHE_EVENTS.labels(op=op, result=result).inc()


def set_cache_entries(op: str, entries: int) -> None:
    CACHE_ENTRIES.labels(op=op).set(max(entries, 0))


def observe_render(engine: str, status: str, seconds: float) -> None:
    BROWSER_RENDERS.labels(engine=engine, status=status).inc()
    BROWSER_RENDER_DURATION.labels(engine=engine).observe(max(seconds, 0.0))


def observe_launch(engine: str, status: str, seconds: float) -> None:
    BROWSER_LAUNCHES.labels(engine=engine, status=status).inc()
    BROWSER_LAUNCH_DURATION.labels(engine=engine).observe(max(seconds, 0.0))


def set_session_open(engine: str, is_open: bool) -> None:
    BROWSER_SESSION_OPEN.labels(engine=engine).set(1 if is_open else 0)


def observe_session_close(engine: str, reason: str) -> None:
    BROWSER_SESSION_CLOSES.labels(engine=engine, reason=reason).inc()


def observe_janitor(sweeps: int = 0, kills: int = 0) -> None:
    if sweeps:
        JANITOR_SWEEPS.inc(sweeps)
    if kills:
        JANITOR_KILLS.inc(kills)


def observe_engine(
    engine: str, *, results: int = 0, total: float = 0.0, http: float = 0.0, ok: bool = False
) -> None:
    """Record one SERP fetch for the SearXNG-compatible engine series.

    ``total`` is the whole engine call (retries and parsing included), ``http``
    is the share of it spent inside the browser render, so processing is the
    difference. Called once per engine per search.
    """
    state = _ENGINE_STATE.setdefault(engine, [0, 0, 0, 0.0, 0.0])
    state[0] += 1
    state[1] += 1 if ok else 0
    state[2] += max(results, 0)
    state[3] += max(total, 0.0)
    state[4] += max(http, 0.0)

    sent, successes, _results, total_sum, http_sum = state
    avg_total = total_sum / sent
    avg_http = http_sum / sent
    SEARXNG_REQUEST_COUNT.labels(engine_name=engine).inc()
    SEARXNG_RESULT_COUNT.labels(engine_name=engine).inc(max(results, 0))
    SEARXNG_TIME_TOTAL.labels(engine_name=engine).set(avg_total)
    SEARXNG_TIME_HTTP.labels(engine_name=engine).set(avg_http)
    SEARXNG_TIME_PROCESSING.labels(engine_name=engine).set(max(avg_total - avg_http, 0.0))
    SEARXNG_RELIABILITY.labels(engine_name=engine).set(100.0 * successes / sent)


def set_info(version: str, engine: str, provider: str) -> None:
    import platform

    INFO.labels(
        version=version,
        engine=engine,
        provider=provider,
        python=f"{platform.python_implementation()}-{platform.python_version()}",
    ).set(1)


# --- Runtime gauges (read at scrape time) ----------------------------------


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _rss_bytes() -> Optional[int]:
    """RSS of this process, from /proc/self/statm (pages * page size)."""
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _threads() -> Optional[int]:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _open_fds() -> Optional[int]:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def _cgroup_file(name: str, v1_name: str) -> Optional[int]:
    """Read a cgroup value, tolerating both the v2 and v1 layouts."""
    for path in (f"/sys/fs/cgroup/{name}", f"/sys/fs/cgroup/memory/{v1_name}"):
        value = _read_int(path)
        if value is not None:
            return value
    return None


class _RuntimeCollector:
    """Process and container facts, read when the scrape happens.

    These used to be gauges written by every worker, which is wrong in
    multiprocess mode: a gauge keeps the last value its process wrote, so a
    worker that had a browser open when it was last scraped kept reporting the
    browser's memory (and its 400+ tasks) long after the session closed, and
    ``livemax`` then picked that stale number. Reading ``/proc`` and the cgroup
    inside ``collect()`` reports the truth for the instant of the scrape, and
    costs nothing when nobody scrapes.
    """

    def collect(self):
        rss = _rss_bytes()
        if rss is not None:
            yield GaugeMetricFamily(
                "forage_process_rss_bytes", "Resident memory of this worker process.", value=float(rss)
            )
        threads = _threads()
        if threads is not None:
            yield GaugeMetricFamily(
                "forage_process_threads", "Threads in this worker process.", value=float(threads)
            )
        fds = _open_fds()
        if fds is not None:
            yield GaugeMetricFamily(
                "forage_process_open_fds",
                "Open file descriptors in this worker process.",
                value=float(fds),
            )
        memory = _cgroup_file("memory.current", "memory.usage_in_bytes")
        if memory is not None:
            yield GaugeMetricFamily(
                "forage_container_memory_bytes",
                "Memory charged to the container's cgroup (all workers, browsers included).",
                value=float(memory),
            )
        tasks = _cgroup_file("pids.current", "pids.current")
        if tasks is not None:
            yield GaugeMetricFamily(
                "forage_container_tasks",
                "PIDs/tasks in the container's cgroup.",
                value=float(tasks),
            )
        yield GaugeMetricFamily(
            "forage_uptime_seconds",
            "Seconds since this worker started.",
            value=float(time.monotonic() - _PROCESS_START),
        )


EXPOSITION.register(_RuntimeCollector())


def render() -> bytes:
    """Format the registry as Prometheus text exposition."""
    return generate_latest(EXPOSITION)
