"""Tests for the per-request log (app/reqlog.py)."""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class ReqlogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.path = self.tmp / "requests.jsonl"
        os.environ["FORAGE_REQLOG"] = str(self.path)
        os.environ["FORAGE_REQLOG_MAX_BYTES"] = str(64 * 1024)
        os.environ["FORAGE_REQLOG_FIELD"] = "200"
        import app.reqlog as reqlog

        self.reqlog = importlib.reload(reqlog)

    def tearDown(self) -> None:
        os.environ.pop("FORAGE_REQLOG", None)
        os.environ.pop("FORAGE_REQLOG_MAX_BYTES", None)
        os.environ.pop("FORAGE_REQLOG_FIELD", None)

    def test_record_and_tail_newest_first(self) -> None:
        self.reqlog.record("search", status="ok", query="pizza", results=5, ms=12.5)
        self.reqlog.record("extract", status="error", url="https://x.test/a", error="boom")
        events = self.reqlog.tail(limit=10)
        self.assertEqual([e["kind"] for e in events], ["extract", "search"])
        self.assertEqual(events[1]["results"], 5)
        self.assertEqual(events[0]["error"], "boom")

    def test_each_line_is_valid_json(self) -> None:
        self.reqlog.record("search", status="ok", query="a\nb", engines={"google": "ok"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["engines"], {"google": "ok"})

    def test_filters(self) -> None:
        self.reqlog.record("search", status="ok", query="um")
        self.reqlog.record("search", status="error", query="dois")
        self.reqlog.record("extract", status="ok", url="https://x.test")
        self.assertEqual([e["query"] for e in self.reqlog.tail(kind="search")], ["dois", "um"])
        self.assertEqual(len(self.reqlog.tail(status="error")), 1)
        self.assertEqual(self.reqlog.tail(kind="extract")[0]["url"], "https://x.test")

    def test_long_fields_are_truncated_not_dropped(self) -> None:
        self.reqlog.record("search", status="ok", query="q" * 900)
        event = self.reqlog.tail(limit=1)[0]
        self.assertTrue(event["query"].endswith("…"))
        self.assertLessEqual(len(event["query"]), 201)

    def test_rotation_keeps_the_log_readable(self) -> None:
        setattr(self.reqlog, "MAX_BYTES", 4096)
        for i in range(200):
            self.reqlog.record("search", status="ok", query=f"{i}" * 120, results=i, ms=1.0)
        self.assertTrue(self.path.with_name(self.path.name + ".1").exists())
        self.assertLessEqual(self.path.stat().st_size, self.reqlog.MAX_BYTES + 4096)
        self.assertGreaterEqual(len(self.reqlog.tail(limit=100)), 1)

    def test_disabled_writes_nothing(self) -> None:
        self.reqlog.record("search", status="ok", query="primeiro")
        before = self.path.stat().st_size
        setattr(self.reqlog, "ENABLED", False)
        self.reqlog.record("search", status="ok", query="segundo")
        self.assertEqual(self.path.stat().st_size, before)

    def test_missing_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(self.reqlog.tail(limit=5), [])
        self.assertEqual(self.reqlog.stats()["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
