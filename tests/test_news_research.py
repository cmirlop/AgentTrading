"""Offline tests for the source-to-NEWS-02 research executable."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.macro_sources.core import ALLOWED_FEEDS, normalize_feed
from src.news_local.fixtures import FIXED_AS_OF
from src.news_local.research import NewsResearchError, run_research


def _registry(source_id="FED_PRESS_RSS", currency="USD") -> dict:
    url, host, feed_format = ALLOWED_FEEDS[source_id]
    is_ics = feed_format == "ICS"
    return {
        "schema_version": 1,
        "sources": [
            {
                "source_id": source_id,
                "source_type": "SCHEDULED_EVENT" if is_ics else "NEWS_RELEASE",
                "format": "ICS" if is_ics else "RSS_OR_ATOM",
                "feed_url": url,
                "catalog_url": "https://www.federalreserve.gov/feeds/feeds.htm",
                "host": host,
                "currencies": [currency],
                "live_enabled": True,
                "terms_status": "SYNTHETIC_TEST_INPUT",
            }
        ],
    }


def _synthetic_fetch(now=FIXED_AS_OF, source_id="FED_PRESS_RSS", currency="USD") -> dict:
    source = _registry(source_id, currency)["sources"][0]
    host = source["host"]
    payload = (
        b"<rss><channel><item><guid>news03-source-release</guid>"
        + f"<title>Synthetic {currency} policy release</title>".encode()
        + f"<link>https://{host}/synthetic-release.htm</link>".encode()
        + b"<pubDate>Thu, 15 Jan 2026 13:10:00 GMT</pubDate></item></channel></rss>"
    )
    normalized = normalize_feed(payload, source, now)
    normalized["fetch_status"] = "FETCHED"
    return {
        "status": "FETCH_OK",
        "requests_made": 1,
        "request_limit": 4,
        "origin_requests": 1,
        "automatic_retry_count": 0,
        "sources": [normalized],
    }


class ResearchPipelineTests(unittest.TestCase):
    def test_source_to_analysis_matrix_for_base_and_quote_drivers(self):
        cases = (
            ("FED_PRESS_RSS", "USD", "EURUSD", {"STRENGTHENS": "DOWN", "WEAKENS": "UP"}),
            ("ECB_PRESS_RSS", "EUR", "EURUSD", {"STRENGTHENS": "UP", "WEAKENS": "DOWN"}),
            ("BANK_OF_CANADA_PRESS_RSS", "CAD", "USDCAD", {"STRENGTHENS": "DOWN", "WEAKENS": "UP"}),
            ("RBA_MEDIA_RSS", "AUD", "AUDUSD", {"STRENGTHENS": "UP", "WEAKENS": "DOWN"}),
        )
        for source_id, currency, symbol, expected in cases:
            with self.subTest(source_id=source_id, symbol=symbol):
                def fake_fetcher(root, registry, *, request_limit, now):
                    return _synthetic_fetch(now, source_id, currency)

                with tempfile.TemporaryDirectory() as directory:
                    report = run_research(
                        root=Path(directory),
                        mode="collect",
                        symbol=symbol,
                        now=FIXED_AS_OF,
                        fetcher=fake_fetcher,
                        source_registry=_registry(source_id, currency),
                    )
                self.assertEqual(report["status"], "PASS")
                self.assertEqual(report["driver_currencies"], [currency])
                self.assertEqual(
                    {item["direction"]: item["pair_direction"] for item in report["conditional_pair_directions"]},
                    expected,
                )

    def test_offline_fixture_is_deterministic_and_does_not_fetch(self):
        def forbidden_fetcher(*args, **kwargs):
            raise AssertionError("offline mode attempted a source fetch")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_research(
                root=root,
                mode="offline",
                symbol="EURUSD",
                fixture="usd_only",
                fetcher=forbidden_fetcher,
                output="reports/private/news_local/first.json",
                now=FIXED_AS_OF,
            )
            repeated = run_research(
                root=root,
                mode="offline",
                symbol="EURUSD",
                fixture="usd_only",
                fetcher=forbidden_fetcher,
                output="reports/private/news_local/repeated.json",
                now=FIXED_AS_OF,
            )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["analysis"]["schema_version"], "2.0")
        self.assertEqual(report["analysis"]["execution"]["request_count"], 0)
        self.assertEqual(report["driver_currencies"], ["USD"])
        self.assertEqual(
            [item["pair_direction"] for item in report["conditional_pair_directions"]],
            ["DOWN", "UP"],
        )
        self.assertEqual(report["analysis"], repeated["analysis"])

    def test_collect_normalizes_macro_feed_and_saves_private_snapshot(self):
        fetch = _synthetic_fetch()
        calls = []

        def fake_fetcher(root, registry, *, request_limit, now):
            calls.append((request_limit, now))
            return fetch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_research(
                root=root,
                mode="collect",
                symbol="EURUSD",
                request_limit=4,
                now=FIXED_AS_OF,
                fetcher=fake_fetcher,
                source_registry=_registry(),
            )
            snapshot = root / report["snapshot_path"]
            stored = json.loads(snapshot.read_text(encoding="utf-8"))

        self.assertEqual(calls, [(4, FIXED_AS_OF)])
        self.assertTrue(report["snapshot_path"].startswith("reports/private/news_local/"))
        self.assertTrue(report["output_path"].startswith("reports/private/news_local/"))
        self.assertEqual(stored["sources"][0]["events"][0]["title"], "Synthetic USD policy release")
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["event_count_received"], 1)
        self.assertEqual(report["eligible_event_count"], 1)
        self.assertEqual(report["analysis"]["driver_assessments"][0]["driver_currency"], "USD")
        self.assertEqual(
            {item["pair_direction"] for item in report["conditional_pair_directions"]},
            {"DOWN", "UP"},
        )
        observations = [
            observation
            for fact in report["analysis"]["confirmed_facts"]
            for observation in fact["observations"]
        ]
        self.assertEqual(observations, [])

    def test_snapshot_replay_is_offline_and_keeps_original_cutoff(self):
        fetch = _synthetic_fetch()

        def fake_fetcher(root, registry, *, request_limit, now):
            return fetch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collected = run_research(
                root=root,
                mode="collect",
                now=FIXED_AS_OF,
                fetcher=fake_fetcher,
                source_registry=_registry(),
            )

            def forbidden_fetcher(*args, **kwargs):
                raise AssertionError("snapshot replay attempted a source fetch")

            replay = run_research(
                root=root,
                mode="offline",
                snapshot=collected["snapshot_path"],
                output="reports/private/news_local/replay.json",
                fetcher=forbidden_fetcher,
                source_registry=_registry(),
                now=FIXED_AS_OF + timedelta(days=1),
            )
        self.assertEqual(replay["status"], "PASS")
        self.assertEqual(replay["source_fetch"]["requests_made"], 0)
        self.assertEqual(replay["analysis_as_of_utc"], "2026-01-15T13:25:00Z")
        self.assertEqual(replay["analysis"]["event_ids_used"], collected["analysis"]["event_ids_used"])

    def test_inaccessible_source_is_partial_not_confirmed_empty(self):
        def unavailable_fetcher(root, registry, *, request_limit, now):
            return {
                "status": "PARTIAL",
                "requests_made": 1,
                "request_limit": 4,
                "origin_requests": 1,
                "automatic_retry_count": 0,
                "sources": [
                    {
                        "source_id": "FED_PRESS_RSS",
                        "status": "UNAVAILABLE",
                        "events": [],
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as directory:
            report = run_research(
                root=Path(directory),
                mode="collect",
                now=FIXED_AS_OF,
                fetcher=unavailable_fetcher,
                source_registry=_registry(),
            )
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["analysis"]["status"], "PENDING")
        self.assertEqual(report["eligible_event_count"], 0)
        self.assertEqual(report["source_fetch"]["sources"][0]["status"], "UNAVAILABLE")

    def test_private_output_path_rejects_public_or_parent_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(NewsResearchError, "PRIVATE_OUTPUT_PATH_REQUIRED"):
                run_research(
                    root=root,
                    mode="offline",
                    fixture="usd_only",
                    output="reports/news_03/public.json",
                    now=FIXED_AS_OF,
                )
            with self.assertRaisesRegex(NewsResearchError, "PRIVATE_OUTPUT_PATH_REQUIRED"):
                run_research(
                    root=root,
                    mode="offline",
                    fixture="usd_only",
                    output="reports/private/news_local/../../public.json",
                    now=FIXED_AS_OF,
                )

    def test_existing_private_result_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "reports" / "private" / "news_local" / "preserve.json"
            destination.parent.mkdir(parents=True)
            destination.write_text("existing local content\n", encoding="utf-8")
            with self.assertRaisesRegex(NewsResearchError, "OUTPUT_ALREADY_EXISTS"):
                run_research(
                    root=root,
                    mode="offline",
                    fixture="usd_only",
                    output="reports/private/news_local/preserve.json",
                    now=FIXED_AS_OF,
                )
            self.assertEqual(destination.read_text(encoding="utf-8"), "existing local content\n")


if __name__ == "__main__":
    unittest.main()
