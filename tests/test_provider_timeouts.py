"""The Forage plugin's client-side timeouts come from the environment.

The plugin subclasses Hermes' ``WebSearchProvider``, so importing it outside the
agent needs that module stubbed first. Nothing here talks to a Forage instance:
``httpx`` is replaced by a recorder that keeps the timeout it was called with.

Run inside the container (httpx lives there):

    docker exec -w /srv/forage forage python -m unittest tests.test_provider_timeouts -v
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "web" / "forage" / "provider.py"


def _load_plugin():
    """Import the plugin with a stub ``agent`` package and a clean module state."""
    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []
    provider_base = types.ModuleType("agent.web_search_provider")

    class WebSearchProvider:  # minimal stand-in for the real base class
        pass

    setattr(provider_base, "WebSearchProvider", WebSearchProvider)
    sys.modules.setdefault("agent", agent_pkg)
    sys.modules.setdefault("agent.web_search_provider", provider_base)

    spec = importlib.util.spec_from_file_location("forage_plugin_under_test", PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load the plugin from {PLUGIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class _Recorder:
    """Stands in for ``httpx.post`` and ``httpx.AsyncClient``."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"success": True, "data": []}

    def post(self, url, **kwargs):
        self.calls.append({"url": url, "timeout": kwargs.get("timeout")})
        return _FakeResponse(self.payload)

    def AsyncClient(self, **kwargs):  # noqa: N802 (mirrors httpx's name)
        self.calls.append({"timeout": kwargs.get("timeout")})
        return _FakeAsyncClient(self.payload)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, **_kwargs):
        return _FakeResponse(self._payload)


class TimeoutEnvTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in (
            "FORAGE_SEARCH_TIMEOUT", "FORAGE_EXTRACT_TIMEOUT"
        )}

    def tearDown(self):
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value

    def test_defaults(self):
        self.assertEqual(plugin._search_timeout(), 90.0)
        self.assertEqual(plugin._extract_timeout(), 60.0)

    def test_env_overrides_each_side_independently(self):
        os.environ["FORAGE_SEARCH_TIMEOUT"] = "120"
        self.assertEqual(plugin._search_timeout(), 120.0)
        self.assertEqual(plugin._extract_timeout(), 60.0)

        os.environ["FORAGE_EXTRACT_TIMEOUT"] = "45"
        self.assertEqual(plugin._extract_timeout(), 45.0)
        self.assertEqual(plugin._search_timeout(), 120.0)

    def test_fractional_and_spaced_values(self):
        os.environ["FORAGE_SEARCH_TIMEOUT"] = " 7.5 "
        self.assertEqual(plugin._search_timeout(), 7.5)

    def test_unusable_values_fall_back(self):
        for raw in ("abc", "0", "-3", ""):
            os.environ["FORAGE_SEARCH_TIMEOUT"] = raw
            with self.subTest(raw=raw):
                self.assertEqual(plugin._search_timeout(), 90.0)

    def test_value_is_read_per_call_not_cached(self):
        os.environ["FORAGE_SEARCH_TIMEOUT"] = "100"
        self.assertEqual(plugin._search_timeout(), 100.0)
        os.environ["FORAGE_SEARCH_TIMEOUT"] = "130"
        self.assertEqual(plugin._search_timeout(), 130.0)


class SearchUsesEnvTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("FORAGE_SEARCH_TIMEOUT")
        self.provider = plugin.ForageWebSearchProvider()

    def tearDown(self):
        os.environ.pop("FORAGE_SEARCH_TIMEOUT", None)
        if self._saved is not None:
            os.environ["FORAGE_SEARCH_TIMEOUT"] = self._saved

    def test_search_forwards_the_env_timeout(self):
        recorder = _Recorder({"success": True, "data": {"web": []}})
        original = plugin.httpx.post
        setattr(plugin.httpx, "post", recorder.post)
        try:
            os.environ["FORAGE_SEARCH_TIMEOUT"] = "75"
            self.provider.search("anything", 3)
            os.environ.pop("FORAGE_SEARCH_TIMEOUT")
            self.provider.search("anything", 3)
        finally:
            setattr(plugin.httpx, "post", original)

        self.assertEqual([call["timeout"] for call in recorder.calls], [75.0, 90.0])


class ExtractUsesEnvTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("FORAGE_EXTRACT_TIMEOUT")
        self.provider = plugin.ForageWebSearchProvider()
        self._original = (
            plugin.httpx.AsyncClient,
            plugin._screen,
            plugin._merge,
            plugin._recheck,
        )

    def tearDown(self):
        client, screen, merge, recheck = self._original
        setattr(plugin.httpx, "AsyncClient", client)
        setattr(plugin, "_screen", screen)
        setattr(plugin, "_merge", merge)
        setattr(plugin, "_recheck", recheck)
        os.environ.pop("FORAGE_EXTRACT_TIMEOUT", None)
        if self._saved is not None:
            os.environ["FORAGE_EXTRACT_TIMEOUT"] = self._saved

    def test_extract_forwards_the_env_timeout(self):
        recorder = _Recorder({"success": True, "data": []})

        async def _screen(urls):
            return list(urls), {}

        async def _recheck(items):
            return items

        def _merge(_urls, _refused, items):
            return items

        setattr(plugin.httpx, "AsyncClient", recorder.AsyncClient)
        setattr(plugin, "_screen", _screen)
        setattr(plugin, "_recheck", _recheck)
        setattr(plugin, "_merge", _merge)

        os.environ["FORAGE_EXTRACT_TIMEOUT"] = "40"
        asyncio.run(self.provider.extract(["https://example.com"]))
        os.environ.pop("FORAGE_EXTRACT_TIMEOUT")
        asyncio.run(self.provider.extract(["https://example.com"]))

        self.assertEqual([call["timeout"] for call in recorder.calls], [40.0, 60.0])


if __name__ == "__main__":
    unittest.main()
