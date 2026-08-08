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
        "engines": ["google", "bing", "brave", "startpage"],
        "timeout": 15,
    },
    "extract": {
        "timeout": 30,
        "max_content_chars": 100000,
        "only_main_content": True,
        "user_agent": "ForageBot/0.1 (+https://github.com/aldemaroc/forage)",
        "browser_user_agent": None,
        "respect_robots": False,
        "force_render": False,
        "wait_for": None,
        "min_content_chars": 200,
        "raw_content_markdown": True,
        "domain_overrides": {},
    },
    "browser": {
        "engine": "playwright",
        "cdp_url": "",       # engine=obscura: CDP endpoint of the Obscura server (http://host:port or ws://host:port)
        "min_idle": 1,
        "max_instances": 5,
        "idle_timeout": 60,
        "headless": True,
        "launch_timeout": 30,
        "stealth": True,
        "network_idle_timeout": 5,
        "scroll_steps": 0,
        "challenge_timeout": 15,
        "solve_cloudflare": False,
        "fallback_solver": True,
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
    user_agent: str = "ForageBot/0.1 (+https://github.com/aldemaroc/forage)"
    browser_user_agent: Optional[str] = None
    respect_robots: bool = False
    force_render: bool = False
    wait_for: Optional[str] = None
    min_content_chars: int = 200
    raw_content_markdown: bool = True
    domain_overrides: tuple = ()


@dataclass(frozen=True)
class BrowserConfig:
    engine: str = "playwright"  # "playwright" (default), "patchright", "scrapling" or "obscura"
    cdp_url: str = ""  # engine=obscura: CDP endpoint (http://host:port or ws://host:port)
    min_idle: int = 1
    max_instances: int = 5
    idle_timeout: int = 60
    headless: bool = True
    launch_timeout: int = 30
    stealth: bool = True
    network_idle_timeout: int = 5
    scroll_steps: int = 0
    challenge_timeout: int = 15  # max seconds to wait for an anti-bot challenge to auto-resolve (scrapling engine)
    solve_cloudflare: bool = False  # scrapling: use its built-in Cloudflare solver (adds ~5s/page) or the page_action poll (default)
    fallback_solver: bool = True  # on anti-bot failure, retry with scrapling + built-in solver as last resort


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
            browser=BrowserConfig(**browser),
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
        if self.browser.engine not in ("playwright", "patchright", "scrapling", "obscura"):
            raise ValueError(f"browser.engine inválido: {self.browser.engine} (use playwright, patchright, scrapling ou obscura)")
        if self.browser.engine == "obscura" and not self.browser.cdp_url:
            raise ValueError("browser.engine=obscura exige browser.cdp_url (ex.: http://127.0.0.1:9223)")
        if self.browser.min_idle > self.browser.max_instances and self.browser.max_instances > 0:
            raise ValueError("browser.min_idle não pode exceder browser.max_instances")
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
