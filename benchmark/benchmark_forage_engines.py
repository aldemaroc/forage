#!/usr/bin/env python3
"""Forage engine benchmark: 50 sites (top-30 web + 20 agent-relevant) x 3 engines.

Each engine runs against its own container port. Measures wall time per URL and
classifies the outcome:
  OK              - content extracted (no error, >= MIN_CHARS, non-empty title)
  anti-bot        - challenge/block detected
  timeout         - request or scrape timed out
  no usable data  - 200-ish but content too short / empty title

Usage:
  python3 benchmark_forage_engines.py [--out /tmp/bench.json]
"""

import argparse
import json
import time
import urllib.request

# Engine -> local port (containers started beforehand)
ENGINES = {
    "playwright": 3672,
    "patchright": 3673,
    "scrapling": 3674,
}

MIN_CHARS = 100  # minimum content length to count as usable data

URLS = [
    # ---- Top 30 most-visited domains (real, non-home URLs) ----
    ("https://www.google.com/about/", "top"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "top"),
    ("https://www.facebook.com/help/", "top"),
    ("https://www.instagram.com/explore/tags/technology/", "top"),
    ("https://www.whatsapp.com/download", "top"),
    ("https://en.wikipedia.org/wiki/Web_scraping", "top"),
    ("https://x.com/NASA", "top"),
    ("https://www.amazon.com/gp/bestsellers/books", "top"),
    ("https://www.tiktok.com/@tiktok", "top"),
    ("https://finance.yahoo.com/quote/AAPL/", "top"),
    ("https://old.reddit.com/r/selfhosted/", "top"),
    ("https://www.netflix.com/title/80192098", "top"),
    ("https://www.linkedin.com/jobs/", "top"),
    ("https://www.twitch.tv/directory", "top"),
    ("https://www.bing.com/search?q=web+scraping", "top"),
    ("https://www.office.com/", "top"),
    ("https://openai.com/news/", "top"),
    ("https://discord.com/download", "top"),
    ("https://www.pinterest.com/ideas/", "top"),
    ("https://www.ebay.com/deals", "top"),
    ("https://www.microsoft.com/windows", "top"),
    ("https://www.msn.com/en-us/news", "top"),
    ("https://starwars.fandom.com/wiki/Luke_Skywalker", "top"),
    ("https://g1.globo.com/tecnologia/", "top"),
    ("https://www.uol.com.br/esporte/", "top"),
    ("https://www.dailymail.co.uk/news/index.html", "top"),
    ("https://zoom.us/pricing", "top"),
    ("https://github.com/trending", "top"),
    ("https://www.canva.com/templates/", "top"),
    ("https://www.shopify.com/free-trial", "top"),
    # ---- 20 sites an agent would access (docs, tools, reference) ----
    ("https://docs.python.org/3/tutorial/introduction.html", "agent"),
    ("https://developer.mozilla.org/en-US/docs/Web/JavaScript", "agent"),
    ("https://fastapi.tiangolo.com/tutorial/", "agent"),
    ("https://docs.docker.com/get-started/", "agent"),
    ("https://kubernetes.io/docs/concepts/", "agent"),
    ("https://hermes-agent.nousresearch.com/docs", "agent"),
    ("https://nousresearch.com/", "agent"),
    ("https://github.com/aldemaroc/forage", "agent"),
    ("https://scrapling.readthedocs.io/en/latest/", "agent"),
    ("https://playwright.dev/python/docs/intro", "agent"),
    ("https://www.python.org/about/", "agent"),
    ("https://nodejs.org/en/learn/getting-started/introduction-to-nodejs", "agent"),
    ("https://react.dev/learn", "agent"),
    ("https://docs.github.com/en", "agent"),
    ("https://stackoverflow.com/questions/2018026/what-are-the-differences-between-type-and-isinstance", "agent"),
    ("https://news.ycombinator.com/item?id=1", "agent"),
    ("https://www.kernel.org/doc/html/latest/process/howto.html", "agent"),
    ("https://www.w3schools.com/python/python_intro.asp", "agent"),
    ("https://docs.ansible.com/ansible/latest/getting_started/index.html", "agent"),
    ("https://arxiv.org/abs/2301.00234", "agent"),
]


def scrape(port: int, url: str, timeout: int = 35) -> dict:
    body = json.dumps({"urls": [url]}).encode()
    req = urllib.request.Request(
        f"http://localhost:{port}/extract",
        data=body,
        headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
        method="POST",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        reason = "timeout" if ("timeout" in msg or isinstance(exc, TimeoutError)) else "error"
        return {"ok": False, "reason": reason, "seconds": round(time.monotonic() - start, 1), "detail": str(exc)[:120]}

    elapsed = round(time.monotonic() - start, 1)
    entries = (data or {}).get("data") or []
    if not entries:
        return {"ok": False, "reason": "no usable data", "seconds": elapsed, "detail": "empty data"}
    r = entries[0]
    if "error" in r:
        err = r.get("error", "").lower()
        if "anti-bot" in err or "challenge" in err:
            return {"ok": False, "reason": "anti-bot", "seconds": elapsed, "detail": r.get("error", "")}
        if "timeout" in err:
            return {"ok": False, "reason": "timeout", "seconds": elapsed, "detail": r.get("error", "")}
        return {"ok": False, "reason": "error", "seconds": elapsed, "detail": r.get("error", "")}

    content = r.get("content") or ""
    title = r.get("title") or ""
    if len(content) < MIN_CHARS or not title.strip():
        return {
            "ok": False,
            "reason": "no usable data",
            "seconds": elapsed,
            "detail": f"content={len(content)} chars, title={title[:40]!r}",
        }
    return {
        "ok": True,
        "seconds": elapsed,
        "method": r.get("method", ""),
        "chars": len(content),
        "title": title[:60],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default=None, help="run only this engine (default: all)")
    parser.add_argument("--port", type=int, default=None, help="override engine port")
    parser.add_argument("--out", default="/tmp/forage_bench.json")
    args = parser.parse_args()

    engines = {args.engine: args.port} if args.engine else ENGINES
    if args.engine and args.port is None:
        engines = {args.engine: ENGINES[args.engine]}

    results = {}  # engine -> {url -> result}
    for engine, port in engines.items():
        # health check
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5) as resp:
                health = json.loads(resp.read().decode())
            print(f"[{engine}] health OK: engine={health.get('browser_engine')}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{engine}] health FAILED: {exc}", flush=True)
            continue

        results[engine] = {}
        for url, _cat in URLS:
            r = scrape(port, url)
            results[engine][url] = r
            status = "OK " if r["ok"] else f"ERR({r['reason']})"
            print(f"[{engine}] {status} {r['seconds']:>5.1f}s {url[:70]}", flush=True)
            time.sleep(0.2)

    out_path = args.out
    if args.engine:
        # engine-specific output file (parallel runs must not clobber each other)
        stem, ext = out_path.rsplit(".", 1) if "." in out_path else (out_path, "json")
        out_path = f"{stem}_{args.engine}.{ext}"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"urls": [u for u, _ in URLS], "results": results}, fh, ensure_ascii=False, indent=2)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
