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
        "force_render_domains": [
            "x.com", "twitter.com", "instagram.com", "linkedin.com", "tiktok.com",
            "youtube.com", "youtu.be",
        ],
        "url_rewrites": [],
        "full_text_domains": [],
    },
    "browser": {
        "engine": "playwright",
        "min_idle": 1,
        "max_instances": 5,
        "idle_timeout": 60,
        "headless": True,
        "launch_timeout": 30,
        "stealth": True,
        "network_idle_timeout": 5,
        "scroll_steps": 0,
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
class UrlRewrite:
    """Prefix rewrite rule: host (www-insensitive) + path prefix -> new host/path.

    Example: match="reddit.com/r/", replace="old.reddit.com"
    rewrites https://www.reddit.com/r/selfhosted/... to
    https://old.reddit.com/r/selfhosted/... (http/https and www are normalized).
    """

    match: str
    replace: str


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
    force_render_domains: tuple = (
        "x.com", "twitter.com", "instagram.com", "linkedin.com", "tiktok.com",
        "youtube.com", "youtu.be",
    )
    url_rewrites: tuple = ()
    full_text_domains: tuple = ()


@dataclass(frozen=True)
class BrowserConfig:
    engine: str = "playwright"  # "playwright" (default) or "patchright"
    min_idle: int = 1
    max_instances: int = 5
    idle_timeout: int = 60
    headless: bool = True
    launch_timeout: int = 30
    stealth: bool = True
    network_idle_timeout: int = 5
    scroll_steps: int = 0


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
                    "url_rewrites": tuple(
                        UrlRewrite(**r) for r in extract.get("url_rewrites", [])
                    ),
                    "full_text_domains": tuple(
                        extract.get("full_text_domains", [])
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
        if self.browser.engine not in ("playwright", "patchright"):
            raise ValueError(f"browser.engine inválido: {self.browser.engine} (use playwright ou patchright)")
        if self.browser.min_idle > self.browser.max_instances and self.browser.max_instances > 0:
            raise ValueError("browser.min_idle não pode exceder browser.max_instances")
        for rule in self.extract.url_rewrites:
            if not rule.match or not rule.replace:
                raise ValueError(
                    "extract.url_rewrites: cada regra precisa de 'match' e 'replace' não vazios"
                )
            if "/" not in rule.match:
                raise ValueError(
                    f"extract.url_rewrites: match deve ser 'host/path-prefix' (ex.: reddit.com/r/): {rule.match!r}"
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
