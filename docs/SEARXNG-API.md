# SearXNG compatibility

Forage answers SearXNG's read API on the same port as its native one, so a
client whose only search provider is SearXNG can point its **instance URL** at
Forage. This is the counterpart of the Firecrawl shim: same idea, different
client family. [LibreChat](https://www.librechat.ai) motivated it, since its
search providers are `serper`, `searxng`, `tavily` and `keenable`, while
Firecrawl is only accepted there as a *scraper*.

| Endpoint | Status |
|---|---|
| `GET /search?q=...&format=json` | Implemented. SearXNG's parameters and JSON envelope. |
| `GET /?q=...&format=json` | Implemented. SearXNG serves the same search on both paths. |
| `POST /search` | **Forage's native contract**, not SearXNG's. JSON body, Hermes-shaped envelope. |
| `POST /search` (form-encoded, as in SearXNG's docs) | Not implemented. The path already means something else here. |
| `format=html` / `csv` / `rss` | 403. SearXNG answers 403 for a format its settings do not enable. |
| `/config`, `/preferences`, `/autocompleter`, `/stats` | Not implemented (404). |

Everything is served from the same `run_search`: same engines, same browser,
same pacer, same 5-minute search cache (an entry cached through `POST /search`
is reused here, and vice versa).

## Request

```
GET /search?q=<query>&format=json
```

| Parameter | Behaviour |
|---|---|
| `q` | Required. Empty or missing is `400`. |
| `format` | Only `json` (the default when omitted). Anything else is `403`. |
| `language` | Forwarded to the SERP engines (`pt-BR`, `en`, ...). `all` and empty mean the instance default (`search.default_lang`). |
| `engines` | Comma-separated. Forage has `google`, `bing`, `yahoo`, `duckduckgo` (alias `ddg`) and `brave`. Names it does not have are reported in `unresponsive_engines` instead of being silently swapped; if **every** requested engine is unknown, the answer is an empty result set. |
| `categories` | `general` only. Asking for another category (`images`, `videos`, `news`, ...) returns an empty result set with the reason in `unresponsive_engines`: better an honest empty page than unrelated web results. |
| `pageno` | Page 1 only. A higher page returns an empty `results` array, which reads as "no more pages". |
| `time_range`, `safesearch` | Accepted and ignored. The engines behind the SERP rendering expose no such filter. |
| `limit` | Non-standard extension for testing (`1`-`50`, default `10`, one SERP page). Clients that speak SearXNG never send it. |

## Response

SearXNG's envelope, with SearXNG's field names and order:

```bash
curl -s "http://localhost:3672/search?q=proxmox&format=json&language=pt-BR"
```

```json
{
  "query": "proxmox",
  "number_of_results": 10,
  "results": [
    {
      "url": "https://www.proxmox.com/en/",
      "title": "Proxmox Server Solutions",
      "content": "Proxmox Virtual Environment is an open-source ...",
      "engine": "google",
      "template": "default.html",
      "parsed_url": ["https", "www.proxmox.com", "/en/", "", "", ""],
      "img_src": "",
      "thumbnail": "",
      "priority": "",
      "engines": ["google", "bing"],
      "positions": [1],
      "score": 1.0,
      "category": "general",
      "publishedDate": null
    }
  ],
  "answers": [],
  "corrections": [],
  "infoboxes": [],
  "suggestions": [],
  "unresponsive_engines": [["brave", "CAPTCHA"]]
}
```

The response carries the same `X-Forage-Cache: hit|miss|bypass|disabled` header
as the rest of the API.

### What is approximate, on purpose

These fields exist because clients read them, but Forage does not compute them
the way SearXNG does:

- `engine` / `engines`: the engines that answered **this** search (from the
  per-engine status the native API already reports). Forage deduplicates by URL
  before returning, so it no longer knows which engine produced a given hit.
- `score`: derived from the result rank (1.0 downwards). Forage does not
  compute SearXNG's relevance score, and inventing one would be worse than
  returning a monotonic placeholder.
- `answers`, `infoboxes`, `corrections`, `suggestions`: always empty. Forage
  produces none of them.
- `number_of_results`: the size of `results` on this page, not an estimate of
  the corpus. Some SearXNG versions emit this key, some don't; clients ignore
  it either way.

A search where every engine failed answers `200` with `results: []` and the
failures listed in `unresponsive_engines`, which is what SearXNG does and what
clients know how to display. The native `POST /search` keeps returning its own
error payload for the same condition.

## Authentication

The same gate as the rest of the API: `auth.enabled` plus `FORAGE_API_KEYS`
(comma-separated). Both carriers are accepted:

```bash
curl -s -H "Authorization: Bearer $FORAGE_API_KEY" "http://localhost:3672/search?q=teste&format=json"
curl -s "http://localhost:3672/search?q=teste&format=json&api_key=$FORAGE_API_KEY"
```

The query parameter exists because SearXNG itself has no API key, so clients
that support one (`searxngApiKey` in LibreChat) send it that way. With
`auth.enabled: false` (the default) nothing is checked and any token is
ignored.

## LibreChat

`librechat.yaml`:

```yaml
webSearch:
  searchProvider: searxng
  searxngInstanceUrl: "${SEARXNG_INSTANCE_URL}"
  searxngApiKey: "${SEARXNG_API_KEY}"      # only when auth.enabled is true
  scraperProvider: firecrawl
  firecrawlApiUrl: "${FIRECRAWL_API_URL}"
  rerankerType: none
  allowedAddresses:
    - "forage:3672"
    - "firecrawl:3002"
```

`.env`:

```bash
SEARXNG_INSTANCE_URL=http://forage:3672
SEARXNG_API_KEY=local-placeholder-key
FIRECRAWL_API_URL=http://forage:3672
FIRECRAWL_API_KEY=local-placeholder-key
```

Two details that cost time when missed:

- `searxngInstanceUrl` is a **base URL**: LibreChat appends `/search` itself.
  Do not add a trailing slash (`http://forage:3672/` becomes
  `http://forage:3672//search`) and do not include `/search` in the value.
- `allowedAddresses` must list the exact `host:port` pair of a private
  address. LibreChat blocks loopback and private destinations at connect time,
  so a self-hosted Forage fails silently without that entry. Note that the same
  block also covers the scraper: one container serves `/search` (SearXNG
  shape) and `/v2/scrape` (Firecrawl shape) at the same URL.

## Not implemented (and why)

- **`POST /search` with form data.** SearXNG accepts both GET and POST; here
  `POST /search` is the native JSON endpoint with a different envelope, and
  making one route answer two contracts is how a compatibility layer breaks
  the client it was meant to serve. Clients that only speak GET (LibreChat
  among them) are fully covered.
- **HTML / RSS / CSV output.** Forage is an API; a browser hitting the URL gets
  a 403 explaining that only JSON is served.
- **`/config`, `/preferences`, `/autocompleter`, `/stats`.** Not part of "use
  Forage as a search backend". `/config` in particular drives SearXNG's engine
  picker UI, which has no Forage equivalent.
- **Engine names beyond the five Forage has.** No silent aliasing to a
  different engine: an engine that does not exist is reported, not substituted.
