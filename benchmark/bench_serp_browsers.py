"""Benchmark: search engine (google/bing/yahoo) X browser engine (playwright/patchright/scrapling).

Runs INSIDE the forage container. Renders the same query on the 3 SERPs with
each of the 3 browser engines and records: status (ok / error / challenge),
time, and how many organic results were parsed. Prints a compact matrix.
"""
import asyncio
import dataclasses
import json
import os
import sys
import time

# The app package lives in app/ next to the repo root, or /srv/forage inside
# the container. Make both work so the script runs from either place.
for _p in ("/srv/forage", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, os.path.abspath(_p))
        break

from app.browser import BrowserPool
from app.config import load_config, deep_merge, DEFAULTS
from app import serp

QUERY = "noticias brasil hoje"
LANG = "pt-BR"
BROWSERS = ["playwright", "patchright", "scrapling"]
SEARCH_ENGINES = ["google", "bing", "yahoo"]
ROUNDS = 4  # 4 rounds per combination to catch intermitence
SLEEP_ROUND = 3  # seconds between rounds

async def main():
    base = dataclasses.replace(load_config(), cache=None)
    pools = {}
    conf = load_config()
    for b in BROWSERS:
        bc = dataclasses.replace(conf.browser, engine=b)
        p = BrowserPool(bc, user_agent=conf.extract.browser_user_agent)
        await p.start()
        pools[b] = p
        print(f"pool {b} started")

    results = {}
    for se in SEARCH_ENGINES:
        for b in BROWSERS:
            key = f"{se}|{b}"
            stats = {"ok": 0, "error": 0, "challenge": 0, "time": [], "n": []}
            for _ in range(ROUNDS):
                spec = serp.SERP_ENGINE_SPECS[se]
                url = spec["build"](QUERY, LANG, 10)
                t0 = time.time()
                try:
                    html = await pools[b].render(url, timeout=20, network_idle_timeout=5)
                    dt = time.time() - t0
                    status, etype, detail = serp._classify(se, str(html))
                    n = len(spec["parse"](str(html), LANG))
                    stats["time"].append(round(dt, 1))
                    stats["n"].append(n)
                    if status == "ok":
                        stats["ok"] += 1
                    elif etype == "challenge":
                        stats["challenge"] += 1
                    else:
                        stats["error"] += 1
                except Exception as exc:
                    dt = time.time() - t0
                    stats["time"].append(round(dt, 1))
                    stats["error"] += 1
                    print(f"  {key} exception: {exc}")
                await asyncio.sleep(SLEEP_ROUND)
            results[key] = stats
            avg_t = sum(stats["time"]) / len(stats["time"]) if stats["time"] else 0
            avg_n = sum(stats["n"]) / len(stats["n"]) if stats["n"] else 0
            print(f"{key}: ok={stats['ok']} chal={stats['challenge']} err={stats['error']} avg_t={avg_t:.1f}s avg_n={avg_n:.1f}")
            await asyncio.sleep(2)

    print("\n=== MATRIX (ok count / avg results / avg time) ===")
    header = "search | " + " | ".join(f"{b:12s}" for b in BROWSERS)
    print(header)
    print("-" * len(header))
    for se in SEARCH_ENGINES:
        cells = []
        for b in BROWSERS:
            s = results.get(f"{se}|{b}", {})
            avg_n = sum(s.get("n", [])) / len(s.get("n", [1])) if s.get("n") else 0
            avg_t = sum(s.get("time", [])) / len(s.get("time", [1])) if s.get("time") else 0
            cells.append(f"{s.get('ok',0)}ok/{avg_n:.0f}r/{avg_t:.1f}s".ljust(12))
        print(f"{se:7s} | " + " | ".join(cells))

    for p in pools.values():
        await p.stop()

asyncio.run(main())
