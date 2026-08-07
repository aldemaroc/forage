#!/usr/bin/env python3
"""Generate benchmark markdown tables from the engine JSON results.

Reads {BASE}_{engine}.json and prints:
  1. Full table (URL x engine) with "✅ (Xs)" / "❌ (reason)" cells
  2. Aggregate stats (accessible per engine + mean time)
  3. Summary table for the README (English)

Usage: python3 bench_report.py [--base /tmp/forage_bench2.json]
"""

import argparse
import json
import sys

ENGINES = ["playwright", "patchright", "scrapling"]
DEFAULT_BASE = "/tmp/forage_bench.json"

REASON_LABEL = {
    "anti-bot": "anti-bot",
    "timeout": "timeout",
    "no usable data": "no usable data",
    "error": "error",
}


def load(base: str) -> tuple[list[str], dict[str, dict[str, dict]]]:
    urls = []
    results = {}
    for eng in ENGINES:
        stem, ext = base.rsplit(".", 1) if "." in base else (base, "json")
        path = f"{stem}_{eng}.{ext}"
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            urls = data["urls"]
            results[eng] = data["results"].get(eng, {})
        except FileNotFoundError:
            print(f"MISSING: {path}", file=sys.stderr)
            results[eng] = {}
    return urls, results


def cell(r: dict | None) -> str:
    if r is None:
        return "n/a"
    if r.get("ok"):
        return f"✅ ({r['seconds']}s)"
    reason = REASON_LABEL.get(r.get("reason"), r.get("reason", "error"))
    return f"❌ ({reason})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    args = parser.parse_args()
    urls, results = load(args.base)

    print("## Benchmark table\n")
    print("| URL | playwright | patchright | scrapling |")
    print("|---|---|---|---|")
    for url in urls:
        row = [f"`{url}`"]
        for eng in ENGINES:
            row.append(cell(results.get(eng, {}).get(url)))
        print("| " + " | ".join(row) + " |")

    print("\n## Aggregate stats\n")
    print("| Engine | Accessible | Inaccessible | Mean time (success) | Mean time (all) |")
    print("|---|---|---|---|---|")
    summary = []
    for eng in ENGINES:
        rs = list(results.get(eng, {}).values())
        ok = [r for r in rs if r and r.get("ok")]
        fail = [r for r in rs if r and not r.get("ok")]
        mean_ok = sum(r["seconds"] for r in ok) / len(ok) if ok else 0
        mean_all = sum(r["seconds"] for r in rs) / len(rs) if rs else 0
        print(f"| {eng} | {len(ok)} | {len(fail)} | {mean_ok:.1f}s | {mean_all:.1f}s |")
        summary.append((eng, len(ok), len(fail), mean_ok))

    print("\n## README summary\n")
    print("| Engine | Accessible Websites | Inaccessible | Mean scrape time |")
    print("|---|---|---|---|")
    for eng, ok, fail, mean_ok in summary:
        print(f"| {eng} | {ok} | {fail} | {mean_ok:.1f}s |")


if __name__ == "__main__":
    main()
