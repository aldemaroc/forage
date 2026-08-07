# Benchmark: browser engines

Comparison of the three browser engines (`playwright`, `patchright`, `scrapling`)
across 50 real sites: the 30 most-visited domains on the web (Similarweb) plus
20 sites an agent would access (documentation, tools, reference).

Run on 2026-08-07 on the same machine, three identical containers (image
`forage:0.6.0-scrapling`) whose configs differ only in `browser.engine`. The
tests ran in parallel with the exact same URL list and the exact same criteria.

**Round 2** (after the `browser.solve_cloudflare: false` optimization): the
table below. Round 1 (with Scrapling's built-in solver) had scrapling at 49/50
with a 4.2s mean; the optimization cut the mean to 3.6s. Round-to-round
variation on Cloudflare-protected sites (stackoverflow, tiktok) is expected:
their anti-bot is intermittent, not a regression in any engine.

**Criteria**: ✅ = extraction succeeded (no error, content ≥ 100 chars,
non-empty title). ❌ = anti-bot (challenge/block), timeout, or no usable data
(too short / empty content).

## Full table (round 2)

| URL | playwright | patchright | scrapling |
|---|---|---|---|
| `https://www.google.com/about/` | ✅ (1.4s) | ✅ (1.4s) | ✅ (1.4s) |
| `https://www.youtube.com/watch?v=jNQXAC9IVRw` | ✅ (7.1s) | ✅ (6.6s) | ✅ (9.1s) |
| `https://www.facebook.com/help/` | ✅ (8.1s) | ✅ (8.9s) | ✅ (6.7s) |
| `https://www.instagram.com/explore/tags/technology/` | ✅ (7.8s) | ✅ (7.9s) | ✅ (5.0s) |
| `https://www.whatsapp.com/download` | ✅ (2.1s) | ✅ (1.7s) | ✅ (2.3s) |
| `https://en.wikipedia.org/wiki/Web_scraping` | ✅ (0.7s) | ✅ (0.7s) | ✅ (0.7s) |
| `https://x.com/NASA` | ✅ (7.4s) | ✅ (8.1s) | ✅ (7.6s) |
| `https://www.amazon.com/gp/bestsellers/books` | ✅ (3.2s) | ✅ (3.4s) | ✅ (3.6s) |
| `https://www.tiktok.com/@tiktok` | ✅ (8.2s) | ❌ (no usable data) | ❌ (no usable data) |
| `https://finance.yahoo.com/quote/AAPL/` | ✅ (11.3s) | ✅ (10.9s) | ✅ (12.3s) |
| `https://old.reddit.com/r/selfhosted/` | ✅ (1.8s) | ✅ (1.9s) | ✅ (1.8s) |
| `https://www.netflix.com/title/80192098` | ✅ (4.5s) | ✅ (4.7s) | ✅ (4.7s) |
| `https://www.linkedin.com/jobs/` | ✅ (2.7s) | ✅ (2.8s) | ✅ (2.7s) |
| `https://www.twitch.tv/directory` | ✅ (8.4s) | ✅ (9.4s) | ✅ (8.7s) |
| `https://www.bing.com/search?q=web+scraping` | ✅ (0.6s) | ✅ (0.6s) | ✅ (0.6s) |
| `https://www.office.com/` | ✅ (2.2s) | ✅ (2.2s) | ✅ (2.4s) |
| `https://openai.com/news/` | ✅ (4.1s) | ✅ (3.8s) | ✅ (7.1s) |
| `https://discord.com/download` | ✅ (0.3s) | ✅ (0.3s) | ✅ (0.3s) |
| `https://www.pinterest.com/ideas/` | ✅ (6.8s) | ✅ (4.5s) | ✅ (2.6s) |
| `https://www.ebay.com/deals` | ✅ (1.9s) | ✅ (2.1s) | ✅ (13.9s) |
| `https://www.microsoft.com/windows` | ✅ (1.8s) | ✅ (1.7s) | ✅ (1.7s) |
| `https://www.msn.com/en-us/news` | ✅ (4.7s) | ✅ (5.7s) | ✅ (5.5s) |
| `https://starwars.fandom.com/wiki/Luke_Skywalker` | ✅ (16.1s) | ✅ (16.6s) | ✅ (24.6s) |
| `https://g1.globo.com/tecnologia/` | ✅ (1.4s) | ✅ (1.7s) | ✅ (1.4s) |
| `https://www.uol.com.br/esporte/` | ✅ (0.8s) | ✅ (1.1s) | ✅ (0.9s) |
| `https://www.dailymail.co.uk/news/index.html` | ❌ (anti-bot) | ❌ (anti-bot) | ✅ (3.8s) |
| `https://zoom.us/pricing` | ✅ (15.0s) | ✅ (14.7s) | ✅ (15.5s) |
| `https://github.com/trending` | ✅ (3.0s) | ✅ (3.6s) | ✅ (1.4s) |
| `https://www.canva.com/templates/` | ✅ (6.5s) | ✅ (7.5s) | ✅ (7.4s) |
| `https://www.shopify.com/free-trial` | ✅ (0.6s) | ✅ (0.4s) | ✅ (0.5s) |
| `https://docs.python.org/3/tutorial/introduction.html` | ✅ (0.4s) | ✅ (0.3s) | ✅ (0.3s) |
| `https://developer.mozilla.org/en-US/docs/Web/JavaScript` | ✅ (0.2s) | ✅ (0.3s) | ✅ (0.2s) |
| `https://fastapi.tiangolo.com/tutorial/` | ✅ (0.4s) | ✅ (0.3s) | ✅ (0.4s) |
| `https://docs.docker.com/get-started/` | ✅ (0.9s) | ✅ (0.8s) | ✅ (0.8s) |
| `https://kubernetes.io/docs/concepts/` | ✅ (0.8s) | ✅ (0.8s) | ✅ (0.8s) |
| `https://hermes-agent.nousresearch.com/docs` | ✅ (0.4s) | ✅ (0.4s) | ✅ (0.4s) |
| `https://nousresearch.com/` | ✅ (0.4s) | ✅ (0.4s) | ✅ (0.4s) |
| `https://github.com/aldemaroc/forage` | ✅ (1.8s) | ✅ (1.1s) | ✅ (0.7s) |
| `https://scrapling.readthedocs.io/en/latest/` | ✅ (0.3s) | ✅ (0.3s) | ✅ (0.4s) |
| `https://playwright.dev/python/docs/intro` | ✅ (0.4s) | ✅ (0.4s) | ✅ (0.2s) |
| `https://www.python.org/about/` | ✅ (0.2s) | ✅ (0.2s) | ✅ (0.2s) |
| `https://nodejs.org/en/learn/getting-started/introduction-to-nodejs` | ✅ (0.6s) | ✅ (0.6s) | ✅ (0.6s) |
| `https://react.dev/learn` | ✅ (3.3s) | ✅ (4.0s) | ✅ (3.7s) |
| `https://docs.github.com/en` | ✅ (1.4s) | ✅ (1.4s) | ✅ (1.4s) |
| `https://stackoverflow.com/questions/2018026/what-are-the-differences-between-type-and-isinstance` | ❌ (anti-bot) | ❌ (anti-bot) | ❌ (error) |
| `https://news.ycombinator.com/item?id=1` | ✅ (1.1s) | ✅ (1.1s) | ✅ (1.2s) |
| `https://www.kernel.org/doc/html/latest/process/howto.html` | ✅ (0.2s) | ✅ (0.2s) | ✅ (0.2s) |
| `https://www.w3schools.com/python/python_intro.asp` | ✅ (2.6s) | ✅ (1.9s) | ✅ (2.1s) |
| `https://docs.ansible.com/ansible/latest/getting_started/index.html` | ✅ (0.4s) | ✅ (0.3s) | ✅ (0.4s) |
| `https://arxiv.org/abs/2301.00234` | ✅ (0.3s) | ✅ (0.2s) | ✅ (0.2s) |

## Notes

- **The `solve_cloudflare: false` optimization is confirmed**: scrapling's mean
  dropped from 4.2s (round 1) to 3.6s (round 2). Previously slow cases
  improved: linkedin 11.2s->2.7s, x.com 12.6s->7.6s, yahoo 17.4s->12.3s,
  fandom 30.1s->24.6s, pinterest 3.7s->2.6s.
- **scrapling remains the only engine that passes dailymail's Cloudflare**
  (3.8s; playwright/patchright fail with anti-bot in both rounds).
- **stackoverflow is intermittent (Cloudflare variation)**: it passed on
  scrapling in round 1 (5.3s) and in an isolated test (2.4s); in round 2
  Cloudflare answered 403 even to the browser (failed on all three). Not a
  regression from the optimization.
- **TikTok varies between rounds**: the login puzzle captcha is intermittent
  (in round 2 playwright passed in 8.2s, the others did not). Site limitation.
- **ebay is the most unpredictable**: round 1 scrapling 2.4s (static) vs
  playwright 32.1s (browser); round 2 flipped (scrapling 13.9s, playwright
  1.9s). The static fetch works sometimes; when it does not, the browser path
  kicks in and the time jumps.
- **fandom (3.3MB of HTML) is the slowest on every engine**: 16s
  (playwright/patchright) to 24.6s (scrapling). Rendering and parsing the whole
  page costs.
- **Documentation sites (agent category) are all fast** (0.2-4.0s) and
  accessible on every engine, since the content is static.

## Reproduce

```bash
# 1. Start three containers whose configs differ only in browser.engine
# 2. Run in parallel (same URL list, same criteria)
python3 benchmark_forage_engines.py --engine playwright --out /tmp/forage_bench.json
python3 benchmark_forage_engines.py --engine patchright --out /tmp/forage_bench.json
python3 benchmark_forage_engines.py --engine scrapling --out /tmp/forage_bench.json
# 3. Generate the tables
python3 bench_report.py --base /tmp/forage_bench.json
```
