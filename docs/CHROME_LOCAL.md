# Browser engine: `chrome-local` (host desktop Chrome via CDP)

`browser.engine: chrome-local` renders pages using the **host's real desktop
Chrome** instead of a headless browser inside the container. It exists because
some anti-bot systems (notably Google's "our systems have detected unusual
traffic from your computer network" challenge) block **every headless browser**,
regardless of IP stack, user agent or stealth tricks, while passing a real
desktop Chrome with a warm user profile.

Validated 2026-09-16 on Google SERPs: scrapling/playwright/patchright all hit
the challenge (~100% after volume), while the same query through the host
Chrome returned full organic results (h3 count 8-10) every time. IPv6 enabled
on the Docker network did **not** help - the block is fingerprint-based, not IP.

## Architecture

```
Forage container                 Host (same machine)
┌──────────────────────┐         ┌──────────────────────────────┐
│ BrowserPool          │  TCP    │ chrome-cdp-relay (socat)     │
│ engine=chrome-local  ├────────►│ 172.20.0.1:9222 ────────────►│ 127.0.0.1:9222
│ cdp_url=http://...   │  CDP    │ (Docker gateway)             │ Chrome (profile
└──────────────────────┘         │                              │ ~/.chrome-hermes)
                                 └──────────────────────────────┘
```

The Chrome process that answers on `127.0.0.1:9222` is a separate systemd
service (`chrome-hermes.service`) with its **own profile**
(`~/.chrome-hermes`), so it does not touch the user's normal desktop Chrome
sessions. See the "Chrome CDP Service" skill / `wiki/infra/chrome-hermes.md`
for that service.

## Host setup (one-time)

1. Install `socat`:

   ```bash
   sudo apt-get install -y socat
   ```

2. Create the relay service so the container can reach the Chrome CDP (which
   by design listens only on `127.0.0.1`):

   ```bash
   sudo tee /etc/systemd/system/chrome-cdp-relay.service > /dev/null <<'EOF'
   [Unit]
   Description=Forward Docker gateway 172.20.0.1:9222 to local Chrome CDP 127.0.0.1:9222
   After=docker.service
   Wants=docker.service

   [Service]
   ExecStart=/usr/bin/socat TCP-LISTEN:9222,bind=172.20.0.1,reuseaddr,fork TCP:127.0.0.1:9222
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   EOF
   sudo systemctl daemon-reload
   sudo systemctl enable --now chrome-cdp-relay.service
   ```

3. Confirm reachability from inside the container:

   ```bash
   docker exec forage python3 -c \
     "import urllib.request, json; print(json.load(urllib.request.urlopen('http://172.20.0.1:9222/json/version'))['Browser'])"
   ```

## Config

```yaml
browser:
  engine: chrome-local          # or keep scrapling as default and override google
  cdp_url: "http://172.20.0.1:9222"

  # Fallback chain per search engine (provider=forage): try scrapling first;
  # if Google shows a CAPTCHA, render through the host Chrome.
  search_engine_overrides:
    google: [scrapling, chrome-local]
```

Notes:

- Any engine id may be a **list** under `search_engine_overrides`; the
  elements are tried in order until one yields a usable page (`ok` /
  `no_results`). A `challenge`/`error` on one browser falls through to the
  next.
- One `BrowserPool` is created per distinct browser engine in use
  (`browser.engine` + every engine mentioned in any override), so
  `chrome-local` and `scrapling` each keep their own CDP connection / session.

## Behavior and pitfalls (validated 2026-09-16)

- **The dispatch matters**: `BrowserPool.render()` must route `chrome-local`
  into the CDP render path (`if self.engine in ("obscura", "chrome-local")`).
  Before that route existed, `chrome-local` fell into the vanilla Playwright
  path with a fresh incognito context + stealth script, which is exactly the
  automation fingerprint Google detects - the same URL succeeded via direct
  CDP but failed through the pool.
- **Never inject `STEALTH_INIT_SCRIPT` into a real Chrome**: overriding
  `navigator.webdriver`/`chrome`/`languages`/`plugins` in a real desktop
  browser is a distinctive automation tell (a genuine Chrome reports
  `webdriver: false` natively). Stealth is for headless engines only.
- **Use the browser's existing default context** (`browser.contexts[0]`), not
  `new_context()`. A new context is an incognito profile with no cookies and
  no history, i.e. a fresh visitor with no trust; the default context carries
  the real profile's cookies and session.
- **Never close the shared default context** in the pool's `finally` - it
  belongs to the desktop Chrome. Close only the page you created.
- **Wait a fixed ~3 s after `domcontentloaded`** instead of `networkidle` for
  this engine: a challenge page settles to networkidle instantly, so the
  networkidle wait returns before the real SERP is confirmed; a short fixed
  settle lets the page finish deciding whether to show the challenge.
- **Concurrency is capped by the same pool semaphore** (`browser.max_instances`);
  each call opens a page on the shared Chrome, so parallel SERP renders
  translate to N tabs in the host Chrome (they are closed when done).
- The relay binds only to `172.20.0.1` (the Docker bridge / compose network
  gateway), so the Chrome CDP is never exposed to the public internet. If the
  compose network changes (different subnet), update the relay `ExecStart`.

## Why IPv6 did not fix Google

The Docker daemon and the `searxng_default` compose network were enabled for
IPv6 (daemon `fixed-cidr-v6`, subnet `fd00:20::/64`); the host has provider
IPv6 and containers now egress with a real public IPv6 (`curl -6 ident.me`
inside the container returns the host's IPv6). The Google challenge persisted
on every headless engine over both stacks - proving the block is
fingerprint-based, not address-based. The real Chrome passes on plain IPv4.
