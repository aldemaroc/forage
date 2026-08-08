# Integrating Forage with Hermes Agent

Forage ships with a ready-made plugin for [Hermes Agent](https://hermes-agent.nousresearch.com). It implements the `WebSearchProvider` interface, so Hermes uses Forage for both `web_search` and `web_extract`, with no code changes to Hermes itself.

```
Hermes → plugin web/forage (WebSearchProvider) → local HTTP → Forage → SearXNG / pages
```

## 1. Install the plugin

The plugin lives in this repo under `plugins/web/forage/`. Copy it into Hermes' user plugins directory:

```bash
mkdir -p ~/.hermes/plugins/web
cp -r plugins/web/forage ~/.hermes/plugins/web/forage
```

Enable it (Hermes must be able to find it):

```bash
hermes plugins enable web/forage
hermes plugins list | grep forage   # → web-forage ... enabled
```

> **Note for developers**: Hermes loads user plugins as `hermes_plugins.web__forage`, so the plugin's `__init__.py` uses a **relative import** (`from .provider import ...`). Do not change it to `plugins.web.forage.provider`.

## 2. Configure the connection (Hermes `.env`)

```bash
# ~/.hermes/.env
FORAGE_URL=http://localhost:3672
# FORAGE_API_KEY=...          # only if you enable auth on Forage
# FORAGE_BYPASS_CACHE=true    # optional: always bypass Forage's cache
```

Restart the Hermes gateway so the plugin loads:

```bash
systemctl --user restart hermes-gateway   # or however you run the gateway
```

## 3. Point the backends at Forage

```bash
hermes config set web.search_backend forage
hermes config set web.extract_backend forage
```

Restart the gateway again. Then verify:

```bash
# Search
curl ... # or just ask Hermes; or call the tool directly:
# web_search(query="proxmox server")
# web_extract(urls=["https://en.wikipedia.org/wiki/Guineafowl"])
```

### Alternative setup: Forage extract + direct SearXNG search

If you already run Hermes with SearXNG as the search backend (Hermes has a built-in `searxng` provider) and only want Forage for extraction:

```bash
hermes config set web.search_backend searxng
hermes config set web.extract_backend forage
export SEARXNG_URL=http://localhost:8080   # Hermes .env
```

Trade-off: search skips Forage (one less hop, no Forage search cache), while extraction still gets Forage's hybrid static/browser pipeline.

## 3.1 Full-page extraction tool (`forage_full_extract`)

The plugin also registers a custom Hermes **agent tool** that runs extraction
with `only_main_content: false` at runtime to grab forum comments, thread
replies, or any content the default main-content extraction would drop,
without changing the service config:

```
forage_full_extract(urls=[...], format="markdown", force_render=false, wait_for=null, timeout=null)
```

- `urls` (required): one or more URLs to extract full text from.
- `force_render`: force the browser pipeline (JS) instead of static fetch.
- `wait_for`: optional CSS selector to wait for before extracting (browser mode).
- `timeout`: optional per-URL timeout override.

The tool is registered in the `forage` toolset, which Hermes enables by
default for new plugins. If it does not appear after a gateway restart, run
`hermes tools` and make sure the `forage` toolset is enabled for your
platform. The underlying service config (`domain_overrides` rewrites,
`full_text`, stealth, etc.) still applies.

## 4. Enabling auth

1. Forage side: `auth.enabled: true` in Forage's `config.yaml` + `FORAGE_API_KEYS=key1,key2` in Forage's `.env` → `docker compose restart`
2. Hermes side: `FORAGE_API_KEY=<one of the keys>` in Hermes' `.env` → restart the gateway

## How the plugin talks to Forage

- **Search**: `POST {FORAGE_URL}/search` with `{"query", "limit"}` → expects the envelope `{"success": true, "data": {"web": [...]}}`.
- **Extract**: `POST {FORAGE_URL}/extract` with `{"urls", "formats": ["markdown"|"html"]}` → returns a **list** of per-URL dicts (`{url, title, content, raw_content, error?}`).

The service's `raw_content_markdown: true` (default) makes `raw_content` mirror the clean markdown, the exact contract Hermes' `web_extract_tool` expects (it reads `raw_content` first).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `web_extract` shows raw HTML | Set `extract.raw_content_markdown: true` in Forage (default) or pass `format: "markdown"` |
| `'str' object has no attribute 'get'` in web_extract | A provider returned the envelope instead of a list. Not the case with this plugin |
| Plugin not listed as enabled | Run `hermes plugins enable web/forage`; check the folder is `~/.hermes/plugins/web/forage/` |
| 401 from Forage | Auth enabled but `FORAGE_API_KEY` missing/mismatched in Hermes `.env` |
| Search works, extract hangs | Forage browser pool cold start on first extract (~5-10 s); subsequent calls reuse warm browsers |

## Files in this repo

| File | Purpose |
|---|---|
| `plugins/web/forage/plugin.yaml` | Manifest (`kind: backend`, provides `forage`) |
| `plugins/web/forage/__init__.py` | `register(ctx)` → registers the provider |
| `plugins/web/forage/provider.py` | `WebSearchProvider` subclass, HTTP client for Forage |
