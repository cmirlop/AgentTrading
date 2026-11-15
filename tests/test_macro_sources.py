from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from src.macro_sources.core import (
    ALLOWED_FEEDS,
    MAX_FEED_BYTES,
    MacroSourceError,
    _NoRedirect,
    fetch_live_sources,
    normalize_feed,
    parse_ics,
    parse_rss_atom,
    safe_calendar_preview,
    event_is_available_as_of,
)


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def source(source_id="FED_PRESS_RSS", **overrides):
    url, host, fmt = ALLOWED_FEEDS[source_id]
    value = {
        "source_id": source_id,
        "source_type": "SCHEDULED_EVENT" if fmt == "ICS" else "NEWS_RELEASE",
        "format": fmt,
        "feed_url": url,
        "host": host,
        "currencies": ["USD"] if source_id != "RBA_MEDIA_RSS" else ["AUD"],
        "live_enabled": True,
    }
    value.update(overrides)
    return value


class MacroParsingTests(unittest.TestCase):
    def test_rss_and_atom_normalize_without_article_body(self):
        rss = b"""<?xml version='1.0'?><rss><channel><item><guid>g1</guid><title>Policy rate decision</title><link>https://example.gov/a</link><pubDate>Sun, 15 Nov 2026 11:00:00 GMT</pubDate><description>article body must not be copied</description></item></channel></rss>"""
        events = parse_rss_atom(rss, source(), NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["currencies"], ["USD"])
        self.assertEqual(events[0]["importance"], "HIGH_RESEARCH")
        self.assertNotIn("article body", str(events[0]))
        sanitized = parse_rss_atom(
            b"<rss><channel><item><guid>x</guid><title><script>run()</script>Public title</title></item></channel></rss>",
            source(), NOW,
        )[0]
        self.assertEqual(sanitized["title"], "Public title")

        atom = b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>atom1</id><title>Meeting</title><link href='https://example.gov/atom'/><published>2026-11-15T10:00:00Z</published></entry></feed>"""
        atom_events = parse_rss_atom(atom, source(), NOW)
        self.assertEqual(atom_events[0]["event_id"], "atom1")
        self.assertEqual(atom_events[0]["url"], "https://example.gov/atom")

        rss1 = b"""<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'><item><guid>rss1</guid><title>Bank announcement</title></item></rdf:RDF>"""
        self.assertEqual(parse_rss_atom(rss1, source("BANK_OF_CANADA_PRESS_RSS"), NOW)[0]["event_id"], "rss1")

    def test_malformed_empty_xxe_and_oversize_feed_fail_or_stay_partial(self):
        with self.assertRaisesRegex(MacroSourceError, "XML_MALFORMED"):
            parse_rss_atom(b"<rss><item>", source(), NOW)
        with self.assertRaisesRegex(MacroSourceError, "DTD_OR_ENTITY"):
            parse_rss_atom(b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///x'>]><rss/>", source(), NOW)
        empty = normalize_feed(b"<rss><channel/></rss>", source(), NOW)
        self.assertEqual(empty["status"], "PARTIAL")
        self.assertEqual(empty["event_count"], 0)
        with self.assertRaisesRegex(MacroSourceError, "FEED_TOO_LARGE"):
            normalize_feed(b"x" * (MAX_FEED_BYTES + 1), source(), NOW)
        with self.assertRaisesRegex(MacroSourceError, "XML_ROOT_INVALID"):
            parse_rss_atom(b"<not-a-feed/>", source(), NOW)
        with self.assertRaisesRegex(MacroSourceError, "ICS_MALFORMED"):
            parse_ics(b"DTSTART:20261201T120000Z", source("BLS_RELEASE_ICS"), NOW)

    def test_ics_exact_timezone_date_only_missing_time_revision_and_dst(self):
        raw = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:policy-1\r\n"
            "SUMMARY:Monetary policy decision\r\nDTSTART:20261203T150000Z\r\n"
            "SEQUENCE:3\r\nLAST-MODIFIED:20261115T110000Z\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:date-only\r\nSUMMARY:Employment data\r\n"
            "DTSTART;VALUE=DATE:20261204\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:no-time\r\nSUMMARY:Unscheduled event\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode()
        events = parse_ics(raw, source("BLS_RELEASE_ICS"), NOW)
        self.assertEqual(events[0]["revision"], 3)
        self.assertEqual(events[0]["scheduled_at_utc"], "2026-12-03T15:00:00Z")
        self.assertEqual(events[1]["timestamp_precision"], "DATE_ONLY")
        self.assertIsNone(events[1]["scheduled_at_utc"])
        self.assertEqual(events[2]["status"], "SCHEDULED_TIME_UNKNOWN")

        ambiguous = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nDTSTART;TZID=America/New_York:20261101T013000\r\nEND:VEVENT\r\nEND:VCALENDAR"
        ambiguous_event = parse_ics(ambiguous, source("BLS_RELEASE_ICS"), NOW)[0]
        self.assertIsNone(ambiguous_event["scheduled_at_utc"])
        self.assertEqual(ambiguous_event["timestamp_precision"], "DST_OR_TIMEZONE_INVALID")
        nonexistent = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:y\r\nDTSTART;TZID=America/New_York:20260308T023000\r\nEND:VEVENT\r\nEND:VCALENDAR"
        self.assertEqual(parse_ics(nonexistent, source("BLS_RELEASE_ICS"), NOW)[0]["timestamp_precision"], "DST_OR_TIMEZONE_INVALID")

    def test_unfolded_ics_lines_and_unknown_floating_timezone(self):
        raw = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:fold\nSUMMARY:Employment \n data\nDTSTART:20261201T120000\nEND:VEVENT\nEND:VCALENDAR"
        event = parse_ics(raw, source("BLS_RELEASE_ICS"), NOW)[0]
        self.assertEqual(event["title"], "Employment data")
        self.assertEqual(event["timestamp_precision"], "FLOATING_TIMEZONE_UNKNOWN")
        self.assertIsNone(event["scheduled_at_utc"])

    def test_source_allowlist_currency_and_point_in_time(self):
        payload = b"<rss><channel><item><guid>a</guid><title>CPI</title><pubDate>Sun, 15 Nov 2026 12:01:00 GMT</pubDate></item></channel></rss>"
        event = normalize_feed(payload, source("RBA_MEDIA_RSS"), NOW)["events"][0]
        self.assertEqual(event["currencies"], ["AUD"])
        self.assertFalse(event_is_available_as_of(event, datetime(2026, 11, 15, 11, 59, tzinfo=timezone.utc)))
        self.assertTrue(event_is_available_as_of(event, datetime(2026, 11, 15, 12, 2, tzinfo=timezone.utc)))
        with self.assertRaisesRegex(MacroSourceError, "NOT_ALLOWLISTED"):
            normalize_feed(payload, source(feed_url="https://127.0.0.1/private"), NOW)

    def test_duplicate_event_ids_keep_first_publication_and_revision_history(self):
        payload = b"""<rss><channel>
        <item><guid>same</guid><title>Policy rate decision</title><pubDate>Sun, 15 Nov 2026 10:00:00 GMT</pubDate></item>
        <item><guid>same</guid><title>Updated policy rate decision</title><pubDate>Sun, 15 Nov 2026 10:00:00 GMT</pubDate><updated>Sun, 15 Nov 2026 11:00:00 GMT</updated></item>
        </channel></rss>"""
        normalized = normalize_feed(payload, source(), NOW)
        self.assertEqual(len(normalized["events"]), 1)
        self.assertEqual(normalized["events"][0]["revision_count"], 2)
        self.assertEqual(normalized["events"][0]["initial_published_at_utc"], "2026-11-15T10:00:00Z")
        self.assertEqual(len(normalized["events"][0]["revision_history"]), 2)

    def test_only_verified_scheduled_items_enter_nonactive_preview(self):
        valid = {
            "source_id": "BLS_RELEASE_ICS", "source_type": "SCHEDULED_EVENT",
            "event_id": "x", "status": "SCHEDULED_VERIFIED", "timestamp_precision": "SECOND",
            "scheduled_at_utc": "2026-11-16T13:30:00Z", "observed_at_utc": "2026-11-15T11:00:00Z",
            "currencies": ["USD"], "importance": "HIGH_RESEARCH",
        }
        future_observed = {**valid, "event_id": "late", "observed_at_utc": "2026-11-15T13:00:00Z"}
        date_only = {**valid, "event_id": "day", "timestamp_precision": "DATE_ONLY"}
        news = {**valid, "event_id": "news", "source_type": "NEWS_RELEASE"}
        preview = safe_calendar_preview([valid, future_observed, date_only, news], NOW)
        self.assertEqual([item["event_id"] for item in preview["events"]], ["x"])
        self.assertFalse(preview["events"][0]["active_risk_gate"])
        self.assertFalse(preview["legacy_calendar_state_modified"])


class MacroFetchSafetyTests(unittest.TestCase):
    def test_redirects_are_rejected(self):
        handler = _NoRedirect()
        with self.assertRaisesRegex(MacroSourceError, "REDIRECT_REJECTED"):
            handler.redirect_request(Mock(), Mock(), 302, "redirect", {}, "https://127.0.0.1/")

    def test_request_limit_duplicate_origins_cache_ttl_and_etag_metadata(self):
        only_fed = {"sources": [source()]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.headers = {"ETag": '"v1"', "Last-Modified": "Sun, 15 Nov 2026 11:00:00 GMT"}
        response.read.return_value = b"<rss><channel/></rss>"
        opener = Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as folder, patch("src.macro_sources.core._assert_public_dns"), patch(
            "src.macro_sources.core.urllib.request.build_opener", return_value=opener
        ):
            first = fetch_live_sources(Path(folder), only_fed, request_limit=1, now=NOW)
            self.assertEqual(first["requests_made"], 1)
            self.assertEqual(first["sources"][0]["fetch_status"], "FETCHED")
            second = fetch_live_sources(Path(folder), only_fed, request_limit=1, now=NOW.replace(minute=15))
            self.assertEqual(second["requests_made"], 0)
            self.assertEqual(second["sources"][0]["fetch_status"], "CACHE_HIT")
            expired = fetch_live_sources(Path(folder), only_fed, request_limit=1, now=NOW.replace(hour=20))
            self.assertEqual(expired["requests_made"], 1)
            self.assertEqual(opener.open.call_count, 2)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("If-none-match"), '"v1"')

    def test_403_429_and_timeout_are_single_attempt_no_retry(self):
        for code in (403, 429):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError("https://example.gov/feed", code, "denied", {}, None)
                with patch("src.macro_sources.core._assert_public_dns"), patch(
                    "src.macro_sources.core.urllib.request.build_opener", return_value=opener
                ):
                    result = fetch_live_sources(Path(folder), {"sources": [source()]}, request_limit=1, now=NOW)
                self.assertEqual(result["requests_made"], 1)
                self.assertEqual(result["automatic_retry_count"], 0)
                self.assertEqual(result["sources"][0]["status"], "UNAVAILABLE")
                self.assertEqual(result["sources"][0]["http_status"], code)
                self.assertEqual(opener.open.call_count, 1)
        with tempfile.TemporaryDirectory() as folder:
            opener = Mock()
            opener.open.side_effect = TimeoutError()
            with patch("src.macro_sources.core._assert_public_dns"), patch(
                "src.macro_sources.core.urllib.request.build_opener", return_value=opener
            ):
                result = fetch_live_sources(Path(folder), {"sources": [source()]}, request_limit=1, now=NOW)
            self.assertEqual(result["sources"][0]["error"], "TIMEOUT_NO_RETRY")
            self.assertEqual(opener.open.call_count, 1)

    def test_parse_blocker_retains_the_actual_http_status_and_payload_hash(self):
        payload = b"<not-a-feed/>"
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.headers = {}
        response.read.return_value = payload
        opener = Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as folder, patch("src.macro_sources.core._assert_public_dns"), patch(
            "src.macro_sources.core.urllib.request.build_opener", return_value=opener
        ):
            result = fetch_live_sources(Path(folder), {"sources": [source()]}, request_limit=1, now=NOW)
        item = result["sources"][0]
        self.assertEqual(item["status"], "BLOCKED")
        self.assertEqual(item["http_status"], 200)
        self.assertEqual(item["error"], "XML_ROOT_INVALID")
        self.assertIsNotNone(item["source_sha256"])

    def test_budget_excludes_nonenabled_ics_and_never_exceeds_four(self):
        registry_path = Path(__file__).resolve().parents[1] / "config" / "macro_source_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(MacroSourceError, "REQUEST_LIMIT_EXCEEDED"):
            fetch_live_sources(Path(tempfile.gettempdir()), registry, request_limit=5, now=NOW)
        self.assertFalse(next(item for item in registry["sources"] if item["source_id"] == "BLS_RELEASE_ICS")["live_enabled"])


class MacroOfflineIsolationTests(unittest.TestCase):
    def test_macro_package_has_no_mt5_import_dependency(self):
        script = "import sys; import src.macro_sources.core; assert 'MetaTrader5' not in sys.modules"
        import subprocess
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
