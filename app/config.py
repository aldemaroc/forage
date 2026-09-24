"""Configuration loading for Forage.

Resolution order:
  1. Built-in defaults (this module)
  2. YAML file (FORAGE_CONFIG env var, default /etc/forage/config.yaml)
  3. Env vars (secrets / overrides only)

The YAML file deep-merges over the defaults, so partial files are fine.
Secrets NEVER belong in the YAML; they come from the environment.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml

DEFAULT_CONFIG_PATH = "/etc/forage/config.yaml"

DEFAULTS: Dict[str, Any] = {
    "server": {
        "host": "0.0.0.0",
        "port": 3672,
        "workers": 2,
        "log_level": "info",
    },
    "cache": {
        "enabled": True,
        "max_entries": 500,
        "search": {"enabled": True, "ttl": 300},
        "extract": {"enabled": False, "ttl": 3600},
    },
    "search": {
        "searxng_url": "http://searxng:8080",
        "default_lang": "pt-BR",
        "engines": ["google", "bing", "duckduckgo", "brave"],
        "timeout": 15,
        "provider": "forage",             # "forage" (default; own SERP engines) or "searxng"
        "serp_timeout": 20,               # provider=forage: seconds per SERP render
    },
    "extract": {
        "timeout": 30,
        "max_content_chars": 100000,
        "only_main_content": True,
        "engine": "readability",   # "readability" (default; Readability.js + markdownify) or "trafilatura"
        "user_agent": "ForageBot/0.1 (+https://github.com/aldemaroc/forage)",
        "browser_user_agent": None,
        "respect_robots": False,
        "force_render": False,
        "wait_for": None,
        "min_content_chars": 200,
        "raw_content_markdown": True,
        "prefer_markdown": True,   # negotiate Accept: text/markdown; use native markdown when the server serves it
        "domain_overrides": {},
    },
    "browser": {
        "engine": "scrapling",   # "scrapling" (default; StealthyFetcher, strongest anti-bot), "playwright", "patchright", "chrome-local"
        "cdp_url": "",       # engine=obscura: CDP endpoint of the Obscura server (http://host:port or ws://host:port)
        "min_idle": 1,
        "max_instances": 5,
        "idle_timeout": 60,
        "headless": True,
        "search": {
            # Browser that renders the SERP pages (only used when
            # search.provider=forage). Independent from the extract browser:
            # search defaults to headful, because headless Chromium is flagged
            # as automation by Google on the very first request. The instance
            # is opened at the start of a search and closed at the end of it.
            "headless": False,
            "local": True,          # allow the browser shipped in the container
            "cdp_url": "",          # when set (or browser.cdp_url), CDP wins over local
            "engine": "playwright",
            "humanize": True,       # mouse move + small scroll before each SERP
            "min_interval": 5.0,    # seconds between SERP renders (jittered)
            "retries": 2,           # per-engine retries when classified as a challenge
            "retry_backoff": 6.0,
            "settle_ms": 1200,      # extra wait after the results start rendering
            "user_agent": None,
        },
        "launch_timeout": 30,
        "stealth": True,
        "network_idle_timeout": 5,
        "scroll_steps": 0,
        "challenge_timeout": 15,
        "solve_cloudflare": False,
        "fallback_solver": True,
        # Per-search-engine browser overrides (provider=forage only).
        # Key = search engine id (google/bing/yahoo/duckduckgo), value = browser
        # engine id OR an ordered LIST of browser engine ids (fallback chain:
        # if the first one hits a challenge, the next one is tried, e.g.
        # google: [scrapling, chrome-local]). Engines not listed here use
        # browser.engine. Empty dict = all use browser.engine.
        "search_engine_overrides": {},
    },
    "auth": {"enabled": False},
}

logger = logging.getLogger(__name__)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (new dict)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 3672
    workers: int = 2
    log_level: str = "info"


@dataclass(frozen=True)
class CacheOpConfig:
    enabled: bool = True
    ttl: int = 300


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    max_entries: int = 500
    search: CacheOpConfig = field(default_factory=lambda: CacheOpConfig(True, 300))
    extract: CacheOpConfig = field(default_factory=lambda: CacheOpConfig(False, 3600))


@dataclass(frozen=True)
class SearchConfig:
    searxng_url: str = "http://host.docker.internal:8080"
    default_lang: str = "pt-BR"
    engines: tuple = ("google", "bing", "brave", "startpage")
    timeout: int = 15
    provider: str = "searxng"  # "searxng" (default) or "forage" (own SERP engines)
    serp_timeout: int = 20  # provider=forage: seconds per SERP render


@dataclass(frozen=True)
class DomainOverride:
    """Per-domain extraction overrides.

    Fields are Optional so an override can set only what it cares about.
    ``pattern`` is the YAML key it was declared under (e.g. ``amazon.*`` or
    ``reddit.com/r/``); it is carried here so url_rewrite knows the match.
    """

    pattern: str
    force_render: Optional[bool] = None
    full_text: Optional[bool] = None
    engine: Optional[str] = None    # "trafilatura" (default) or "readability"
    wait_for: Optional[str] = None
    url_rewrite: Optional[str] = None
    scroll: Optional[bool] = None
    timeout: Optional[int] = None
    network_idle_timeout: Optional[int] = None
    challenge_timeout: Optional[int] = None


@dataclass(frozen=True)
class ExtractConfig:
    timeout: int = 30
    max_content_chars: int = 100000
    only_main_content: bool = True
    engine: str = "trafilatura"   # "trafilatura" (default) or "readability"
    user_agent: str = "ForageBot/0.1 (+https://github.com/aldemaroc/forage)"
    browser_user_agent: Optional[str] = None
    respect_robots: bool = False
    force_render: bool = False
    wait_for: Optional[str] = None
    min_content_chars: int = 200
    raw_content_markdown: bool = True
    prefer_markdown: bool = True
    domain_overrides: tuple = ()


@dataclass(frozen=True)
class SearchBrowserConfig:
    """Browser used to render SERP pages (``search.provider=forage``).

    Deliberately separate from the extract browser:

    - search defaults to **headful**. Headless Chromium (playwright,
      patchright and scrapling alike) is answered with an anti-bot page by
      Google on the first request; a headful browser on a virtual display
      renders the SERP normally.
    - the browser is **opened per search and closed at the end of it**, so
      there is no idle browser holding memory between searches.
    - ``cdp_url`` (falling back to ``browser.cdp_url``) points at an existing
      Chrome over CDP. When both CDP and ``local`` are configured, CDP wins.
    """

    headless: bool = False
    local: bool = True
    cdp_url: str = ""
    engine: str = "playwright"  # local mode: playwright or patchright
    humanize: bool = True
    min_interval: float = 5.0
    retries: int = 2
    retry_backoff: float = 6.0
    settle_ms: int = 1200
    user_agent: Optional[str] = None


@dataclass(frozen=True)
class BrowserConfig:
    engine: str = "playwright"  # "playwright" (default), "patchright", "scrapling" or "obscura"
    cdp_url: str = ""  # engine=obscura: CDP endpoint (http://host:port or ws://host:port)
    min_idle: int = 1
    max_instances: int = 5
    idle_timeout: int = 60
    headless: bool = True
    search: SearchBrowserConfig = field(default_factory=SearchBrowserConfig)
    launch_timeout: int = 30
    stealth: bool = True
    network_idle_timeout: int = 5
    scroll_steps: int = 0
    challenge_timeout: int = 15  # max seconds to wait for an anti-bot challenge to auto-resolve (scrapling engine)
    solve_cloudflare: bool = False  # scrapling: use its built-in Cloudflare solver (adds ~5s/page) or the page_action poll (default)
    fallback_solver: bool = True  # on anti-bot failure, retry with scrapling + built-in solver as last resort
    search_engine_overrides: dict = field(default_factory=dict)  # search engine -> browser engine (provider=forage)


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool = False


@dataclass(frozen=True)
class ForageConfig:
    server: ServerConfig
    cache: CacheConfig
    search: SearchConfig
    extract: ExtractConfig
    browser: BrowserConfig
    auth: AuthConfig
    source_path: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_path: str) -> "ForageConfig":
        server = data.get("server", {})
        cache = data.get("cache", {})
        search = data.get("search", {})
        extract = data.get("extract", {})
        browser = data.get("browser", {})
        auth = data.get("auth", {})
        return cls(
            server=ServerConfig(**server),
            cache=CacheConfig(
                enabled=cache.get("enabled", True),
                max_entries=cache.get("max_entries", 500),
                search=CacheOpConfig(**cache.get("search", {})),
                extract=CacheOpConfig(**cache.get("extract", {})),
            ),
            search=SearchConfig(
                searxng_url=search.get("searxng_url", DEFAULTS["search"]["searxng_url"]),
                default_lang=search.get("default_lang", DEFAULTS["search"]["default_lang"]),
                engines=tuple(search.get("engines", DEFAULTS["search"]["engines"])),
                timeout=search.get("timeout", DEFAULTS["search"]["timeout"]),
                provider=search.get("provider", DEFAULTS["search"]["provider"]),
                serp_timeout=search.get("serp_timeout", DEFAULTS["search"]["serp_timeout"]),
            ),
            extract=ExtractConfig(
                **{
                    **extract,
                    "domain_overrides": tuple(
                        DomainOverride(pattern=pattern, **over)
                        for pattern, over in extract.get("domain_overrides", {}).items()
                    ),
                }
            ),
            browser=BrowserConfig(
                **{
                    **{k: v for k, v in browser.items() if k != "search"},
                    "search": SearchBrowserConfig(**browser.get("search", {})),
                }
            ),
            auth=AuthConfig(**auth),
            source_path=source_path,
        )

    def validate(self) -> None:
        """Raise ValueError on invalid configuration values."""
        if not (0 < self.server.port < 65536):
            raise ValueError(f"server.port inválida: {self.server.port}")
        if self.server.workers < 1:
            raise ValueError(f"server.workers deve ser >= 1: {self.server.workers}")
        if self.cache.max_entries < 1:
            raise ValueError(f"cache.max_entries deve ser >= 1: {self.cache.max_entries}")
        for name, op in (("search", self.cache.search), ("extract", self.cache.extract)):
            if op.ttl < 0:
                raise ValueError(f"cache.{name}.ttl deve ser >= 0: {op.ttl}")
        if self.browser.max_instances < 0 or self.browser.min_idle < 0:
            raise ValueError("browser pool sizes devem ser >= 0")
        if self.browser.scroll_steps < 0:
            raise ValueError("browser.scroll_steps deve ser >= 0")
        if self.browser.challenge_timeout < 0:
            raise ValueError("browser.challenge_timeout deve ser >= 0")
        if self.browser.solve_cloudflare and self.browser.engine != "scrapling":
            raise ValueError("browser.solve_cloudflare só se aplica ao engine scrapling")
        if self.browser.engine not in ("playwright", "patchright", "scrapling", "obscura", "chrome-local"):
            raise ValueError(
                f"browser.engine inválido: {self.browser.engine} (use playwright, patchright, scrapling, obscura ou chrome-local)"
            )
        if self.browser.engine in ("obscura", "chrome-local") and not self.browser.cdp_url:
            raise ValueError(
                f"browser.engine={self.browser.engine} exige browser.cdp_url (ex.: http://172.20.0.1:9222)"
            )
        if self.extract.engine not in ("trafilatura", "readability"):
            raise ValueError(
                f"extract.engine inválido: {self.extract.engine} (use trafilatura ou readability)"
            )
        if self.browser.min_idle > self.browser.max_instances and self.browser.max_instances > 0:
            raise ValueError("browser.min_idle não pode exceder browser.max_instances")
        if self.search.provider not in ("searxng", "forage"):
            raise ValueError(
                f"search.provider inválido: {self.search.provider} (use searxng ou forage)"
            )
        if self.search.serp_timeout < 1 or self.search.serp_timeout > 120:
            raise ValueError(
                f"search.serp_timeout deve estar entre 1 e 120s: {self.search.serp_timeout}"
            )
        valid_browsers = ("playwright", "patchright", "scrapling", "obscura", "chrome-local")
        sb = self.browser.search
        if sb.engine not in ("playwright", "patchright"):
            raise ValueError(
                f"browser.search.engine inválido: {sb.engine} "
                "(use playwright ou patchright; scrapling/obscura/chrome-local não "
                "se aplicam ao browser de busca)"
            )
        if sb.min_interval < 0:
            raise ValueError(f"browser.search.min_interval deve ser >= 0: {sb.min_interval}")
        if sb.retries < 0:
            raise ValueError(f"browser.search.retries deve ser >= 0: {sb.retries}")
        if sb.retry_backoff < 0:
            raise ValueError(f"browser.search.retry_backoff deve ser >= 0: {sb.retry_backoff}")
        if sb.settle_ms < 0:
            raise ValueError(f"browser.search.settle_ms deve ser >= 0: {sb.settle_ms}")
        if not sb.local and not (sb.cdp_url or self.browser.cdp_url):
            raise ValueError(
                "browser.search.local=false exige browser.search.cdp_url "
                "(ou browser.cdp_url)"
            )
        for se, be in self.browser.search_engine_overrides.items():
            chain = be if isinstance(be, (list, tuple)) else [be]
            for b in chain:
                if b not in valid_browsers:
                    raise ValueError(
                        f"browser.search_engine_overrides[{se}]: browser engine inválido {b} "
                        f"(use {', '.join(valid_browsers)})"
                    )
        for override in self.extract.domain_overrides:
            if not override.pattern:
                raise ValueError("extract.domain_overrides: padrão de domínio não pode ser vazio")
            if override.url_rewrite and "/" not in override.url_rewrite:
                raise ValueError(
                    f"extract.domain_overrides[{override.pattern}]: url_rewrite deve ser "
                    "'host[/path-prefix]' (ex.: old.reddit.com/r/): {override.url_rewrite!r}"
                )
            if override.timeout is not None and not (1 <= override.timeout <= 120):
                raise ValueError(
                    f"extract.domain_overrides[{override.pattern}]: timeout deve estar entre 1 e 120s"
                )
            if override.network_idle_timeout is not None and not (0 <= override.network_idle_timeout <= 60):
                raise ValueError(
                    f"extract.domain_overrides[{override.pattern}]: network_idle_timeout deve estar entre 0 e 60s"
                )
            if override.challenge_timeout is not None and not (0 <= override.challenge_timeout <= 120):
                raise ValueError(
                    f"extract.domain_overrides[{override.pattern}]: challenge_timeout deve estar entre 0 e 120s"
                )
            if override.engine is not None and override.engine not in ("trafilatura", "readability"):
                raise ValueError(
                    f"extract.domain_overrides[{override.pattern}]: engine deve ser trafilatura ou readability"
                )


def _load_yaml(path: str) -> Dict[str, Any]:
    """Load YAML file, returning {} when the file does not exist."""
    if not os.path.exists(path):
        logger.info("Config file not found (%s), using defaults", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Falha ao parsear config YAML ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Config YAML ({path}) deve ser um mapa no nível raiz")
    return data


def load_config(path: Optional[str] = None) -> ForageConfig:
    """Load configuration from defaults + optional YAML file.

    Args:
        path: Override FORAGE_CONFIG env var and the built-in default.
    """
    config_path = path or os.environ.get("FORAGE_CONFIG") or DEFAULT_CONFIG_PATH
    file_data = _load_yaml(config_path)
    merged = deep_merge(DEFAULTS, file_data)
    config = ForageConfig.from_dict(merged, source_path=config_path)
    config.validate()
    return config
