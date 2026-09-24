# Firecrawl compatibility

Forage exposes a Firecrawl-compatible API on the same port as its native one,
so a client written against Firecrawl can be pointed at Forage by changing only
the base URL.

| Endpoint | Status |
|---|---|
| `POST /v1/scrape` | Implemented. Full request handling, Firecrawl response envelope. |
| `POST /v1/search` | Implemented (v1 shape). Results only: `scrapeOptions` are not applied. |
| `POST /v1/crawl`, `/v1/map`, `/v1/batch/scrape`, `/v1/extract`, `/v1/parse` | Not implemented (404). |

Both endpoints are auth-gated by the same `auth.enabled` / `FORAGE_API_KEYS`
setting as the native API. With `auth.enabled: false` (default) a Bearer token
sent by a Firecrawl client is simply ignored.

## `POST /v1/scrape`

### Request

Firecrawl's own fields, in Firecrawl's casing:

```json
{
  "url": "https://example.com",
  "formats": ["markdown"],
  "onlyMainContent": true,
  "timeout": 30000
}
```

`formats` is accepted in both Firecrawl shapes: a list of strings
(`["markdown"]`) or a list of objects (`[{"type": "markdown"}]`), plus a bare
string. Unrecognized bodies do not fail: every other field Firecrawl documents
(`actions`, `headers`, `proxy`, `waitFor`, `maxAge`, `includeTags`,
`screenshotOptions`, ...) is accepted and ignored, and the names are reported
back in `data.warning`.

| Field | Mapping |
|---|---|
| `url` | Required. `http`/`https` only. |
| `formats` | `markdown` (default) and `html`/`rawHtml` are served. Anything else is ignored with a warning. |
| `onlyMainContent` | `only_main_content` for the extraction (default `true`, same as Firecrawl). |
| `timeout` | Milliseconds in, seconds internally, clamped to 1-120s. |
| everything else | Parsed, ignored, listed in `data.warning`. Not an error. |

### Response (success)

```json
{
  "success": true,
  "data": {
    "markdown": "# Title\n\nContent...",
    "metadata": {
      "title": "Example Domain",
      "sourceURL": "https://example.com",
      "url": "https://example.com",
      "statusCode": 200,
      "cacheState": "miss"
    }
  }
}
```

- `markdown` is the same clean markdown `POST /extract` returns (`method`
  static, browser or a document type; see the README).
- `html` (and `rawHtml`, when asked for) come from the same HTML extraction.
  Forage has a single HTML representation per page, so both fields carry the
  page HTML rather than Firecrawl's "cleaned html" vs "raw html" pair.
- `metadata.sourceURL` is the final URL after any domain rewrite (Reddit and
  friends), which is what Firecrawl reports.
- `metadata.cacheState` mirrors the `X-Forage-Cache` response header
  (`hit`/`miss`/`bypass`/`disabled`).
- `data.warning` is present only when something requested was ignored.
- Firecrawl's `metadata.proxyUsed`, `creditsUsed` and friends are omitted:
  Forage runs no proxy and bills nothing, and inventing values would be worse
  than leaving the field out.

### Response (failure)

```json
{ "success": false, "code": "SCRAPE_ALL_ENGINES_FAILED", "error": "Blocked by anti-bot challenge (Cloudflare or similar)" }
```

| Situation | HTTP | `code` |
|---|---|---|
| Missing or non-http(s) `url` | 400 | `BAD_REQUEST` |
| Forage could not extract the page | 500 | `SCRAPE_ALL_ENGINES_FAILED` |
| The page returned no usable text | 500 | `SCRAPE_ALL_ENGINES_FAILED` |
| Extraction hit the timeout | 408 | `SCRAPE_TIMEOUT` |
| `auth.enabled: true` and the key is wrong | 401 | (FastAPI detail, no code) |
| Any other server-side failure | 500 | `UNKNOWN_ERROR` |

Codes are taken from Firecrawl's own `ErrorCodes` enum: a client that keys on
`SCRAPE_ALL_ENGINES_FAILED` to fall back to its own renderer (that is what
pi-web-agent does) behaves exactly as it would against Firecrawl.

## `POST /v1/search`

Speaks the v1 shape, where `data` is an **array** of documents. Note that the
Firecrawl v2 API changed this to an object (`{"web": [...]}`); this endpoint
does not.

```json
{ "query": "proxmox server", "limit": 5, "lang": "pt-BR" }
```

```json
{
  "success": true,
  "data": [
    { "url": "https://...", "title": "...", "description": "...", "position": 1 }
  ],
  "id": "5f1c2f2e-..."
}
```

- `limit` defaults to 5 and is clamped to 1-100.
- `lang` (v1 name) and `language` are both accepted.
- Search runs through the configured Forage provider (`searxng` or `forage`),
  with the same cache as `POST /search`; `X-Forage-Cache` is returned.
- `scrapeOptions` is not applied: returning page content would mean one extra
  extraction per result. A request carrying it gets a `warning` and plain
  results. Use `/v1/scrape` for content.
- `tbs`, `country`, `location`, `filter` and friends are ignored with a
  warning: the underlying providers do not support them.

## Using it

```bash
# Forage's own port serves both APIs
curl -s -X POST http://localhost:3672/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}'
```

pi-web-agent (the extension that motivated this): set the fetch backend to
`firecrawl` with base URL `http://localhost:3672`, and leave the API key empty
unless `auth.enabled` is on. Its `doctor` probes `/v1/scrape` and should report
`fetch backend: firecrawl ok`.

## Not implemented on purpose

- **`json` / `extract` / `summary` formats**: these need an LLM. Forage is a
  scraper with no model in the loop, and returning the markdown under a `json`
  key would be a lie a client would happily parse.
- **`screenshot`**: no screenshot pipeline (a headless browser could produce
  one, but it is a separate feature, not a compatibility shim).
- **`links` / `images`**: the extraction pipeline does not keep the link list.
- **`actions`** (click, type, wait): Firecrawl runs a browser script before
  extracting. Silently ignoring it would return a different page than the
  client asked for, which is why it is reported in `data.warning` instead.
- **`/v1/crawl`, `/v1/map`, `/v1/batch/scrape`**: crawling is out of scope for
  a single-container scrape service. `POST /extract` already takes up to 20
  URLs per call.
