"""Read-only NEWS-04 MacroAgent contract validation, offline only."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.news_local.macro_contract import MacroNewsContractError, read_macro_news_brief
from src.news_local.research import _canonical_sha256, run_research
from tests.test_news_research import _registry, _synthetic_fetch, _synthetic_payload


AS_OF = datetime(2026, 1, 15, 13, 25, tzinfo=timezone.utc)


def _make_report(root: Path) -> dict:
    registry_path = root / "config" / "macro_source_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(_registry()), encoding="utf-8")
    result = run_research(
        root=root,
        mode="collect",
        symbol="EURUSD",
        now=AS_OF,
        output="reports/private/news_local/macro_contract_eurusd.json",
        fetcher=lambda *args, **kwargs: _synthetic_fetch(),
        source_registry=_registry(),
    )
    cache = root / "reports" / "private" / "phase2_2_2e" / "macro_cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "fed_press_rss.feed").write_bytes(_synthetic_payload())
    return result


def _reseal_report(report: dict) -> None:
    report.pop("result_integrity", None)
    report["result_integrity"] = {
        "algorithm": "SHA-256",
        "canonical_payload_sha256": _canonical_sha256(report),
    }


class ReadOnlyMacroContractTests(unittest.TestCase):
    def test_reads_verified_report_and_exposes_information_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            with patch("src.macro_sources.core.fetch_live_sources", side_effect=AssertionError("network called")), patch(
                "src.news_local.providers.LocalOllamaClient", side_effect=AssertionError("Ollama called")
            ):
                brief = read_macro_news_brief(
                    root=root,
                    result_path=report["output_path"],
                    expected_symbol="EURUSD",
                    now_utc=AS_OF,
                )
        self.assertEqual(brief["status"], "PASS")
        self.assertEqual(brief["freshness"], "FRESH")
        self.assertEqual(brief["source_coverage"]["raw_source_payloads_verified"], 1)
        self.assertEqual(len(brief["events_used"]), 1)
        self.assertEqual(brief["events_used"][0]["economic_observations"], [])
        self.assertEqual(brief["directional_hypotheses"][0]["direction_hypothesis"], "UNKNOWN")
        self.assertEqual(brief["directional_hypotheses"][0]["origin"], "deterministic")
        self.assertEqual(len(brief["conditional_pair_scenarios"]), 2)
        self.assertTrue(brief["informational_only"])
        self.assertFalse(brief["operational_signal"])
        self.assertFalse(brief["trading_decisions_enabled"])
        self.assertEqual(brief["external_fetches_during_read"], 0)
        self.assertEqual(brief["llm_calls_during_read"], 0)

    def test_rejects_tampered_source_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            (root / "reports/private/phase2_2_2e/macro_cache/fed_press_rss.feed").write_bytes(b"tampered")
            with self.assertRaisesRegex(MacroNewsContractError, "SOURCE_PAYLOAD_HASH_MISMATCH"):
                read_macro_news_brief(root=root, result_path=report["output_path"], now_utc=AS_OF)

    def test_rejects_tampered_snapshot_even_if_json_remains_parseable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            snapshot_path = root / report["snapshot_path"]
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["sources"][0]["events"][0]["title"] = "Changed after collection"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaisesRegex(MacroNewsContractError, "SNAPSHOT_INTEGRITY_INVALID"):
                read_macro_news_brief(root=root, result_path=report["output_path"], now_utc=AS_OF)

    def test_rejects_resealed_snapshot_that_disagrees_with_cached_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            snapshot_path = root / report["snapshot_path"]
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["sources"][0]["events"][0]["title"] = "Resealed but not source-backed"
            unsigned = {key: value for key, value in snapshot.items() if key != "integrity"}
            snapshot["integrity"]["canonical_payload_sha256"] = _canonical_sha256(unsigned)
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            result_path = root / report["output_path"]
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            saved["snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            saved["snapshot_integrity"] = {
                "status": "HASH_VERIFIED",
                "algorithm": "SHA-256",
                "canonical_payload_sha256": snapshot["integrity"]["canonical_payload_sha256"],
            }
            _reseal_report(saved)
            result_path.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(MacroNewsContractError, "SOURCE_NORMALIZATION_MISMATCH"):
                read_macro_news_brief(root=root, result_path=report["output_path"], now_utc=AS_OF)

    def test_rejects_analysis_with_forged_evidence_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            result_path = root / report["output_path"]
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            saved["analysis"]["event_ids_used"] = ["MADE-UP-EVENT"]
            _reseal_report(saved)
            result_path.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(MacroNewsContractError, "ANALYSIS_PROVENANCE_INVALID"):
                read_macro_news_brief(root=root, result_path=report["output_path"], now_utc=AS_OF)

    def test_rejects_tampered_llm_hypothesis_before_reading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            result_path = root / report["output_path"]
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            saved["analysis"]["uncertainties"].append("tampered result")
            result_path.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(MacroNewsContractError, "RESULT_INTEGRITY_INVALID"):
                read_macro_news_brief(root=root, result_path=report["output_path"], now_utc=AS_OF)

    def test_rejects_legacy_snapshot_without_integrity_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _make_report(root)
            snapshot_path = root / report["snapshot_path"]
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["schema_version"] = "1.0"
            snapshot.pop("integrity")
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            saved = json.loads((root / report["output_path"]).read_text(encoding="utf-8"))
            saved["snapshot_sha256"] = __import__("hashlib").sha256(snapshot_path.read_bytes()).hexdigest()
            saved["snapshot_integrity"] = {
                "status": "LEGACY_UNVERIFIED",
                "algorithm": None,
                "canonical_payload_sha256": None,
            }
            _reseal_report(saved)
            (root / report["output_path"]).write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(MacroNewsContractError, "SNAPSHOT_INTEGRITY_REQUIRED"):
                read_macro_news_brief(root=root, result_path=report["output_path"], now_utc=AS_OF)


if __name__ == "__main__":
    unittest.main()
