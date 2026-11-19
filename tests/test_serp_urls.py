"""Unit tests for the SERP result URL recovery.

Run inside the container (bs4 and the app deps live there):

    docker compose build && docker compose run --rm --entrypoint python forage -m unittest discover -s tests -v

or against a running container:

    docker cp app/. forage:/srv/forage/app/ && docker cp tests/. forage:/srv/forage/tests/ \
      && docker exec -i forage python -m unittest discover -s tests -v
"""

import base64
import unittest
from urllib.parse import quote

from app.browser import redirect_target_from_body
from app.serp import (
    _apply_redirect_resolution,
    _cite_url,
    _dedupe_key,
    _parse_bing,
    _parse_brave,
    _parse_ddg,
    _parse_google,
    _redirect_token,
    _unwrap_href,
)


def bing_href(target: str) -> str:
    token = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=deadbeefJmltdHM9&u={token}&ntb=1"


class CiteTest(unittest.TestCase):
    def test_breadcrumb_segments_are_joined(self):
        self.assertEqual(
            _cite_url("https://www.python.org \u203a downloads"),
            "https://www.python.org/downloads",
        )
        self.assertEqual(
            _cite_url("https://forum.jellyfin.org \u203a t-trickplay"),
            "https://forum.jellyfin.org/t-trickplay",
        )
        self.assertEqual(
            _cite_url("www.example.com \u203a blog \u203a article-foo"),
            "https://www.example.com/blog/article-foo",
        )

    def test_full_url_is_kept(self):
        self.assertEqual(
            _cite_url("https://docs.python.org/3/library/asyncio-task.html"),
            "https://docs.python.org/3/library/asyncio-task.html",
        )

    def test_truncated_tail_is_dropped(self):
        self.assertEqual(
            _cite_url("https://medium.com \u203a mastering-\u2026"),
            "https://medium.com",
        )
        self.assertEqual(
            _cite_url("https://www.reddit.com \u203a r \u203a zfs \u203a comments \u203a 11n1iii"),
            "https://www.reddit.com/r/zfs/comments/11n1iii",
        )

    def test_non_url_text_returns_empty(self):
        self.assertEqual(_cite_url("h\u00e1 8 meses"), "")
        self.assertEqual(_cite_url("3 coment\u00e1rios  \u00b7  h\u00e1 1 ano"), "")
        self.assertEqual(_cite_url(""), "")


class UnwrapTest(unittest.TestCase):
    def test_bing_ck_a(self):
        target = "https://www.python.org/downloads/?ref=abc"
        self.assertEqual(_unwrap_href(bing_href(target)), target)

    def test_duckduckgo_uddg(self):
        target = "https://patents.google.com/patent/WO2008089067A1/en"
        href = "//duckduckgo.com/l/?uddg=" + quote(target, safe="") + "&rut=38a5c0"
        self.assertEqual(_unwrap_href(href), target)

    def test_google_url_q(self):
        href = "/url?q=" + quote("https://example.com/blog/post?a=1", safe="") + "&sa=U&ved=abc"
        self.assertEqual(_unwrap_href(href), "https://example.com/blog/post?a=1")

    def test_yahoo_ru(self):
        href = (
            "https://r.search.yahoo.com/_ylt=abc/RV=2/RE=1/RO=10/"
            "RU=" + quote("https://example.com/page", safe="") + "/RK=2/RS=abc-"
        )
        self.assertEqual(_unwrap_href(href), "https://example.com/page")

    def test_plain_and_unusable_hrefs(self):
        self.assertEqual(_unwrap_href("https://example.com/a"), "https://example.com/a")
        self.assertEqual(_unwrap_href("/go/v1#Af oqF46"), "")
        self.assertEqual(_unwrap_href(""), "")

    def test_redirect_token(self):
        self.assertEqual(
            _redirect_token("/goto?url=CAESabc123"),
            "https://www.google.com/goto?url=CAESabc123",
        )
        self.assertEqual(_redirect_token("https://example.com"), "")
        self.assertEqual(_redirect_token("/url?q=https://example.com"), "")


class RedirectBodyTest(unittest.TestCase):
    def test_meta_refresh_token_chain(self):
        body = (
            '<html><head><title>Google</title>'
            '<meta content="3;url=/goto?url=CAESfAHEIk" http-equiv="refresh"></head></html>'
        )
        self.assertEqual(
            redirect_target_from_body(body, "https://www.google.com/goto?url=CAES1"),
            "https://www.google.com/goto?url=CAESfAHEIk",
        )

    def test_moved_page_without_location(self):
        body = (
            '<HTML><HEAD><TITLE>302 Moved</TITLE></HEAD><BODY>'
            '<A HREF="https://forums.truenas.com/t/zfs-error/49967/4">here</A>'
            "</BODY></HTML>"
        )
        self.assertEqual(
            redirect_target_from_body(body, "https://www.google.com/goto?url=CAES1"),
            "https://forums.truenas.com/t/zfs-error/49967/4",
        )

    def test_nothing_to_follow(self):
        self.assertIsNone(redirect_target_from_body("<html>nope</html>", "https://x/"))


class GoogleParserTest(unittest.TestCase):
    HTML = """
    <div class="MjjYud">
      <a jsname="UWckNb" href="/goto?url=CAESabc123">
        <h3>ZFS error replacing faulty disk</h3>
      </a>
      <cite>https://forums.truenas.com &#8250; t &#8250; zfs-error-replacing-faulty-disk</cite>
      <div class="VwiC3b">How to replace the disk.</div>
    </div>
    <div class="MjjYud">
      <a href="/url?q=https://example.com/a?b%3D1&amp;sa=U">
        <h3>Plain wrapped result</h3>
      </a>
      <cite>https://example.com/a</cite>
    </div>
    """

    def test_token_is_kept_for_the_resolver(self):
        results = _parse_google(self.HTML, "en")
        self.assertEqual(len(results), 2)
        self.assertEqual(
            results[0]["url"], "https://forums.truenas.com/t/zfs-error-replacing-faulty-disk"
        )
        self.assertEqual(results[0]["url_source"], "cite")
        self.assertEqual(
            results[0]["resolve"], "https://www.google.com/goto?url=CAESabc123"
        )

    def test_inline_wrapper_needs_no_request(self):
        results = _parse_google(self.HTML, "en")
        self.assertEqual(results[1]["url"], "https://example.com/a?b=1")
        self.assertEqual(results[1]["url_source"], "href")
        self.assertNotIn("resolve", results[1])


class BingParserTest(unittest.TestCase):
    def test_decodes_ck_a_target(self):
        target = "https://www.python.org/downloads/"
        html = f"""
        <li class="b_algo">
          <h2><a href="{bing_href(target)}">Download Python</a></h2>
          <cite>https://www.python.org &#8250; downloads</cite>
          <div class="b_caption"><p>Get the latest release.</p></div>
        </li>
        """
        results = _parse_bing(html, "en")
        self.assertEqual(results[0]["url"], target)
        self.assertEqual(results[0]["url_source"], "href")


class DuckDuckGoParserTest(unittest.TestCase):
    def test_decodes_uddg(self):
        target = "https://patents.google.com/patent/WO2008089067A1/en"
        href = "//duckduckgo.com/l/?uddg=" + quote(target, safe="") + "&rut=abc"
        html = (
            f'<a class="result__a" href="{href}">Single flux quantum circuit</a>'
            '<a class="result__snippet">A patent.</a>'
        )
        results = _parse_ddg(html, "en")
        self.assertEqual(results[0]["url"], target)
        self.assertEqual(results[0]["url_source"], "href")


class BraveParserTest(unittest.TestCase):
    def test_falls_back_to_cite_when_href_is_a_token(self):
        html = """
        <div class="snippet" data-type="web">
          <a class="l1" href="/go/v1#Af oqF46NZm6">forum.jellyfin.org</a>
          <div class="title">Trickplay support - Jellyfin Forum</div>
          <cite class="snippet-url">https://forum.jellyfin.org &#8250; t-trickplay</cite>
          <div class="generic-snippet"><div class="content">Thread about trickplay.</div></div>
        </div>
        """
        results = _parse_brave(html, "en")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://forum.jellyfin.org/t-trickplay")
        self.assertEqual(results[0]["url_source"], "cite")


class DedupeKeyTest(unittest.TestCase):
    def test_exact_urls_are_deduped_by_url_alone(self):
        a = {"url": "https://example.com/a", "url_source": "href", "title": "One"}
        b = {"url": "https://example.com/a/", "url_source": "resolved", "title": "Two"}
        self.assertEqual(_dedupe_key(a), _dedupe_key(b))

    def test_breadcrumb_urls_keep_the_title_in_the_key(self):
        a = {"url": "https://github.com", "url_source": "cite", "title": "Issue 1"}
        b = {"url": "https://github.com", "url_source": "cite", "title": "Issue 2"}
        self.assertNotEqual(_dedupe_key(a), _dedupe_key(b))

    def test_empty_url_has_no_key(self):
        self.assertEqual(_dedupe_key({"url": "", "title": "x"}), ("", ""))


class ResolveSwitchTest(unittest.IsolatedAsyncioTestCase):
    """`search.resolve_urls: false` must not touch the network or leak tokens."""

    async def test_disabled_switch_drops_the_tokens(self):
        class _Search:
            resolve_urls = False

        class _Config:
            search = _Search()

        results = [
            {"title": "a", "url": "https://example.com", "url_source": "cite",
             "resolve": "https://www.google.com/goto?url=CAES1",
             "resolve_referer": "https://www.google.com/search?q=x"}
        ]
        left = await _apply_redirect_resolution(_Config(), None, results, "https://x/", "google")
        self.assertEqual(left, 0)
        self.assertNotIn("resolve", results[0])
        self.assertNotIn("resolve_referer", results[0])
        self.assertEqual(results[0]["url_source"], "cite")

    async def test_no_tokens_means_no_browser_call(self):
        class _Search:
            resolve_urls = True

        class _Config:
            search = _Search()

        results = [{"title": "a", "url": "https://example.com/a", "url_source": "href"}]
        left = await _apply_redirect_resolution(_Config(), None, results, "https://x/", "bing")
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
