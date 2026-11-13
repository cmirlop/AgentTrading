import sys
import json
import hashlib
import importlib.metadata
import subprocess
import tempfile
import unittest
import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_phase1 import (
    _assess_baseline_comparison,
    _classify_technical_source_differences,
    _compare_trade_records,
    _external_scope_fingerprint,
    _external_scope_snapshot,
    _external_scope_data_fingerprint,
    _external_validation_status,
    _latest_phase1_local_provider_report,
    _normalized_text_sha256,
    _phase_exit_code,
    _phase_status,
    _source_snapshot,
    _probe_logic_sha256,
    _redact_sensitive_text,
    _selection_root_has_release_crossing,
    _sanitize_public_report,
    _verified_dataset_blob,
    _verified_probe_source,
    _verify_backtest_reuse_compatibility,
    _verify_transitive_selection_attribution,
    _verify_refresh_compatibility,
    scan_order_execution_calls,
)


class ValidationProvenanceTests(unittest.TestCase):
    def test_public_report_redacts_local_paths_and_credential_assignments(self):
        payload = {
            "commands": [sys.executable, str(ROOT / "reports" / "phase1_validation.json")],
            "temporary": str(Path(tempfile.gettempdir()) / "phase1-run"),
            "details": "api_key=example-secret account_id=123456 server=private-host",
        }
        safe = _sanitize_public_report(payload)
        rendered = json.dumps(safe, sort_keys=True)
        self.assertIn("<PYTHON>", rendered)
        self.assertIn("<REPOSITORY>", rendered)
        self.assertIn("<TEMP>", rendered)
        for private in (str(Path.home()), str(ROOT), "example-secret", "123456", "private-host"):
            self.assertNotIn(private, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_sensitive_text_scrubber_redacts_machine_home_prefix(self):
        safe = _redact_sensitive_text(f"python={sys.executable} repo={ROOT}")
        self.assertNotIn(str(Path.home()), safe)
        self.assertNotIn(str(ROOT), safe)

    def test_phase_exit_code_distinguishes_failure_pending_and_complete(self):
        self.assertEqual(_phase_exit_code("FAIL"), 1)
        self.assertEqual(_phase_exit_code("INCOMPLETE_PENDING_MANUAL"), 2)
        self.assertEqual(_phase_exit_code("PHASE1_COMPLETE"), 0)
        self.assertEqual(_phase_exit_code("UNKNOWN"), 1)

    def test_latest_local_live_report_is_selected_by_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "reports" / "phase1_final_local"
            report_dir.mkdir(parents=True)
            (report_dir / "ollama_live_older.json").write_text(json.dumps({
                "provider": "ollama", "generated_at_utc": "2026-11-11T17:00:00+00:00",
            }), encoding="utf-8")
            (report_dir / "ollama_live_newer.json").write_text(json.dumps({
                "provider": "ollama", "generated_at_utc": "2026-11-11T19:00:00Z",
            }), encoding="utf-8")
            (report_dir / "ollama_live_wrong_provider.json").write_text(json.dumps({
                "provider": "deepseek", "generated_at_utc": "2026-11-11T20:00:00Z",
            }), encoding="utf-8")
            with patch("scripts.validate_phase1.ROOT", root):
                selected = _latest_phase1_local_provider_report("ollama", "fallback.json")
        self.assertEqual(selected, "reports/phase1_final_local/ollama_live_newer.json")

    def test_refresh_rejects_prior_report_without_exact_fingerprint(self):
        report = {"traceability": {}, "checks": {"historical_regression": {"status": "PASS"}}}
        self.assertTrue(_verify_refresh_compatibility(report, _source_snapshot()))

    def test_refresh_rejects_changed_dataset_code_or_config_hash(self):
        snapshot = _source_snapshot()
        report = {
            "traceability": {"validation_fingerprint": dict(snapshot)},
            "checks": {"historical_regression": {"status": "PASS"}},
        }
        self.assertEqual(_verify_refresh_compatibility(report, snapshot), [])
        changed = dict(snapshot)
        changed["dataset_sha256"] = "different-dataset-hash"
        self.assertIn(
            "dataset_sha256 differs from prior full validation",
            _verify_refresh_compatibility(report, changed),
        )
        changed = dict(snapshot)
        changed["relevant_file_sha256"] = {**snapshot["relevant_file_sha256"], "src/historical_execution.py": "changed"}
        self.assertIn(
            "relevant_file_sha256 differs from prior full validation",
            _verify_refresh_compatibility(report, changed),
        )
        changed = dict(snapshot)
        changed["backtest_config_sha256"] = {**snapshot["backtest_config_sha256"], "config/backtest_cost_config.py": "changed"}
        self.assertIn(
            "backtest_config_sha256 differs from prior full validation",
            _verify_refresh_compatibility(report, changed),
        )
        changed = dict(snapshot)
        changed["baseline_metrics_sha256"] = "changed-baseline"
        self.assertIn(
            "baseline_metrics_sha256 differs from prior full validation",
            _verify_refresh_compatibility(report, changed),
        )
        changed = dict(snapshot)
        changed["dependencies"] = {**snapshot["dependencies"], "pydantic": "different-version"}
        self.assertIn("dependencies differs from prior full validation", _verify_refresh_compatibility(report, changed))
        changed = dict(snapshot)
        changed["python_version"] = "different-python-runtime"
        self.assertIn("python_version differs from prior full validation", _verify_refresh_compatibility(report, changed))

    def test_merge_commit_and_branch_change_preserve_identical_content_provenance(self):
        snapshot = _source_snapshot()
        prior = dict(snapshot, validation_commit="pre-merge-sha", branch="codex/phase1-final-validation")
        report = {
            "traceability": {"validation_fingerprint": prior},
            "checks": {"historical_regression": {"status": "PENDING"}},
        }
        self.assertEqual(_verify_refresh_compatibility(report, snapshot), [])

    def test_transitive_causal_evidence_rejects_changed_replay_source_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports" / "phase1_final_local").mkdir(parents=True)
            private_dir = root / "reports" / "private"
            private_dir.mkdir(parents=True)
            source = root / "run_portfolio_backtest.py"
            source.write_text("changed replay source\n", encoding="utf-8")
            private = {
                "status": "PASS",
                "dataset_sha256": "frozen-dataset",
                "pre_finalization_commit": "a" * 40,
                "current_commit": "b" * 40,
                "configuration": {"cost_scenario": "BASE", "spread_floor_points": 0.0, "llm_enabled": False},
                "runtime": {
                    "python_version": sys.version,
                    "dependencies": {name: importlib.metadata.version(name) for name in ("pandas", "numpy")},
                },
                "source_hashes": {
                    "current": {"run_portfolio_backtest.py": hashlib.sha256(b"original replay source\n").hexdigest()},
                    "old": {},
                },
            }
            private_path = private_dir / "causal.json"
            private_path.write_text(json.dumps(private), encoding="utf-8")
            summary = {
                "schema_version": 1,
                "status": "PASS",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "dataset_sha256": "frozen-dataset",
                "pre_finalization_commit": "a" * 40,
                "current_commit": "b" * 40,
                "private_evidence": "reports/private/causal.json",
                "private_evidence_sha256": hashlib.sha256(private_path.read_bytes()).hexdigest(),
            }
            (root / "reports" / "phase1_final_local" / "historical_causal_attribution_summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            with patch("scripts.validate_phase1.ROOT", root):
                result = _verify_transitive_selection_attribution("frozen-dataset")
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("Historical replay source changed", result["reason"])

    def test_backtest_evidence_reuse_accepts_merge_metadata_and_rejects_source_changes(self):
        snapshot = _source_snapshot()
        baseline = json.loads((ROOT / "reports/phase1_baseline_metrics.json").read_text(encoding="utf-8"))
        dataset_hash = baseline["dataset"]["sha256"]
        history = {
            "dataset_sha256": dataset_hash,
            "dataset_source": {
                "kind": "verified_git_blob", "commit": "a" * 40,
                "path": "data/backtest/technical_candidate_scan.csv", "sha256": dataset_hash,
            },
            "scenario_runs": {name: {"status": "PASS"} for name in (
                "portfolio_base_1", "portfolio_base_2", "portfolio_conservative", "portfolio_stress",
                "stateful_base_1", "stateful_base_2", "stateful_conservative", "stateful_stress",
            )},
            "repeatability_portfolio_base": {"status": "PASS"},
            "repeatability_portfolio_base_trades": {"status": "PASS"},
            "repeatability_stateful_base": {"status": "PASS"},
            "repeatability_stateful_base_trades": {"status": "PASS"},
            "portfolio_base_summary": [{}], "portfolio_conservative_summary": [{}],
            "portfolio_stress_summary": [{}], "stateful_base_summary": [{}],
            "stateful_conservative_summary": [{}], "stateful_stress_summary": [{}],
        }
        prior_fingerprint = dict(snapshot, validation_commit="a" * 40, branch="codex/pre-merge")
        prior = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "traceability": {"validation_fingerprint": prior_fingerprint},
            "checks": {"historical_regression": history},
        }
        with patch("scripts.validate_phase1._verified_dataset_blob", return_value=(b"verified", None)):
            result = _verify_backtest_reuse_compatibility(prior, snapshot)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_validation_commit"], "a" * 40)
        changed = json.loads(json.dumps(prior))
        changed["traceability"]["validation_fingerprint"]["relevant_file_sha256"]["src/historical_execution.py"] = "changed"
        with patch("scripts.validate_phase1._verified_dataset_blob", return_value=(b"verified", None)):
            result = _verify_backtest_reuse_compatibility(changed, snapshot)
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("relevant_file_sha256.src/historical_execution.py", result["differences"])

    def test_source_hash_is_stable_across_windows_and_unix_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf.py"
            crlf = root / "crlf.py"
            lf.write_bytes(b"value = 1\nprint(value)\n")
            crlf.write_bytes(b"value = 1\r\nprint(value)\r\n")
            self.assertEqual(_normalized_text_sha256(lf), _normalized_text_sha256(crlf))

    def test_transitive_root_uses_the_immediate_candidate_time_and_side(self):
        root = {
            "root_trade_id": "root",
            "root_release_before": "2026-05-21 13:35:00+00:00",
            "root_release_after": "2026-05-21 11:30:00+00:00",
            "chain": ["outer", "intermediate", "root"],
            "chain_steps": [{
                "trade_id": "outer",
                "present_side": "old",
                "identity": {"signal_time": "2026-05-21T13:45:00+00:00"},
            }, {
                "trade_id": "intermediate",
                "present_side": "new",
                "identity": {"signal_time": "2026-05-21T11:45:00+00:00"},
            }],
        }
        self.assertTrue(_selection_root_has_release_crossing(root))
        root["chain_steps"][-1]["identity"]["signal_time"] = "2026-05-21T13:45:00+00:00"
        self.assertFalse(_selection_root_has_release_crossing(root))

    def test_historical_dataset_is_reused_only_from_matching_git_blob_hash(self):
        blob = b"frozen dataset\n"
        expected = hashlib.sha256(blob).hexdigest()
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=blob, stderr=b"")
        with patch("scripts.validate_phase1.subprocess.run", return_value=completed):
            data, error = _verified_dataset_blob("a" * 40, "data/scan.csv", expected)
        self.assertEqual(data, blob)
        self.assertIsNone(error)
        with patch("scripts.validate_phase1.subprocess.run", return_value=completed):
            data, error = _verified_dataset_blob("a" * 40, "data/scan.csv", "0" * 64)
        self.assertIsNone(data)
        self.assertIn("does not match", error)
        data, error = _verified_dataset_blob("merge", "data/scan.csv", expected)
        self.assertIsNone(data)
        self.assertIn("full Git commit hash", error)

    def test_legacy_provider_probe_can_follow_content_equivalent_merge_blob(self):
        source = "class ProbeAnswer: pass\n"
        recorded_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        equivalent_commit = "b" * 40
        with patch("scripts.validate_phase1._git_source_at_commit", side_effect=[None, source]), patch(
            "scripts.validate_phase1._git", return_value=equivalent_commit
        ):
            recovered, verified_commit = _verified_probe_source("a" * 40, recorded_hash)
        self.assertEqual(recovered, source)
        self.assertEqual(verified_commit, equivalent_commit)
        with patch("scripts.validate_phase1._git_source_at_commit", side_effect=[None, source]), patch(
            "scripts.validate_phase1._git", return_value=equivalent_commit
        ):
            recovered, verified_commit = _verified_probe_source("a" * 40, "0" * 64)
        self.assertIsNone(recovered)
        self.assertIsNone(verified_commit)

    def test_final_phase_status_distinguishes_failure_from_manual_pending(self):
        self.assertEqual(_phase_status(True, ["PASS", "PENDING"], "PASS"), "INCOMPLETE_PENDING_MANUAL")
        self.assertEqual(_phase_status(False, ["PASS", "PENDING"], "PASS"), "FAIL")
        self.assertEqual(_phase_status(True, ["PASS"], "FAIL"), "FAIL")
        self.assertEqual(_phase_status(True, ["PASS"], "PASS"), "PHASE1_COMPLETE")

    def test_provider_scope_ignores_merge_metadata_but_tracks_probe_logic(self):
        snapshot = _source_snapshot()
        scope = _external_scope_snapshot("deepseek", snapshot)
        self.assertEqual(scope["probe_logic_sha256"], _probe_logic_sha256(
            (ROOT / "scripts/validate_phase1_llm_live.py").read_text(encoding="utf-8")
        ))
        source = (ROOT / "scripts/validate_phase1_llm_live.py").read_text(encoding="utf-8")
        metadata_only = source.replace('"traceability": _traceability(provider),', '"traceability": _traceability("metadata-only"),')
        prompt_change = source.replace("Return status PASS", "Return status WAIT", 1)
        self.assertEqual(_probe_logic_sha256(source), _probe_logic_sha256(metadata_only))
        self.assertNotEqual(_probe_logic_sha256(source), _probe_logic_sha256(prompt_change))

    def test_scan_finds_direct_import_alias_and_dynamic_send_paths(self):
        examples = {
            "direct.py": "import MetaTrader5 as mt5\nmt5.order_send({})\n",
            "imported.py": "from MetaTrader5 import order_send as submit\nsubmit({})\n",
            "star.py": "from MetaTrader5 import *\norder_send({})\n",
            "aliased_attribute.py": "import MetaTrader5 as mt5\nsend = mt5.order_send\nsend({})\n",
            "dynamic.py": "import MetaTrader5 as mt5\ngetattr(mt5, 'order_send')({})\n",
            "variable.py": "import MetaTrader5 as mt5\nmethod = 'order_send'\ngetattr(mt5, method)({})\n",
            "eval.py": "eval('mt5.order_send(request)')\n",
            "mapping.py": "api['order_send'](request)\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in examples.items():
                (root / name).write_text(source, encoding="utf-8")
            ignored = root / ".venv" / "Lib" / "site-packages" / "dependency.py"
            ignored.parent.mkdir(parents=True)
            ignored.write_text("import MetaTrader5 as mt5\nmt5.order_send({})\n", encoding="utf-8")
            findings = scan_order_execution_calls(root)
        self.assertEqual({item["file"] for item in findings}, set(examples))

    def test_any_unattributed_economic_difference_remains_pending_review(self):
        ambiguous_change = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "ambiguous", "before": 1, "after": 2}],
        })
        self.assertEqual(ambiguous_change["status"], "PENDING")
        self.assertEqual(ambiguous_change["assessment"], "UNATTRIBUTED_DIFFERENCES_PENDING_REVIEW")
        capacity_change = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "plan_rejections", "before": 10, "after": 12}],
        })
        self.assertEqual(capacity_change["status"], "PENDING")
        self.assertEqual(capacity_change["unattributed_differences"][0]["change_class"], "EXECUTION_OR_ECONOMIC_CHANGE_UNATTRIBUTED")
        profit_factor_change = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "net_profit_factor", "before": 1.1, "after": 1.3}],
        })
        self.assertEqual(profit_factor_change["status"], "PENDING")
        unexpected = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "opportunities_detected", "before": 10, "after": 11}],
        })
        self.assertEqual(unexpected["status"], "FAIL")
        self.assertEqual(unexpected["assessment"], "TECHNICAL_SIGNAL_REGRESSION")

    def test_known_simulator_correction_is_hash_pinned_but_metric_deltas_stay_pending(self):
        correction = {
            "file": "src/historical_execution.py",
            "before": "03022d66350fddc5f6a8d0d83da5745893bed96155fad71224635563245d584a",
            "after": "0a9f36b496429f0ce577a237e9f6ffab36a0dd7d29f5fe7d8b9fa3caa7d70531",
        }
        source_result = _classify_technical_source_differences([correction])
        self.assertEqual(source_result["status"], "PASS")
        self.assertEqual(
            source_result["simulator_correction_changes"][0]["change_class"],
            "DOCUMENTED_SIMULATOR_CORRECTION",
        )
        economic = _assess_baseline_comparison({
            "differences": [{"field": "net_pnl_eur", "before": 2.0, "after": 1.5}],
        }, [source_result["simulator_correction_changes"][0]["correction_id"]])
        self.assertEqual(economic["status"], "PENDING")
        self.assertEqual(economic["simulator_correction_differences"], [])
        self.assertEqual(
            economic["differences"][0]["simulator_correction_candidate_ids"],
            ["HISTORICAL_EXECUTION_VALIDATION_EXPIRY_AND_COSTS"],
        )
        unknown = dict(correction, after="f" * 64)
        self.assertEqual(_classify_technical_source_differences([unknown])["status"], "FAIL")

    def test_current_simulator_changes_are_hash_pinned_and_separate_from_signal_changes(self):
        differences = [
            {
                "file": "run_portfolio_backtest.py",
                "before": "2177d5567f91666533d35935fc339016b823b6dca7e74a0ffdadd2a25fe23e99",
                "after": "5521d032f899e32a60df013870ed2fe0da81b9f2d8368430410c0912197fb62a",
            },
            {
                "file": "run_stateful_backtest.py",
                "before": "57acb952979efb1ef20f9bdc725322f13de8c357cb0d202eecbb6c5d4651528a",
                "after": "a71615abb6980c4b90426396b1c1435efc85eeb2ff082cfbe65a2e7ed057745d",
            },
            {
                "file": "src/historical_execution.py",
                "before": "03022d66350fddc5f6a8d0d83da5745893bed96155fad71224635563245d584a",
                "after": "6c648843c3056b36baec803f38f17a561a7634ba574ca208bd134840f103f0da",
            },
        ]
        result = _classify_technical_source_differences(differences)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["documented_non_signal_changes"]), 2)
        self.assertEqual(len(result["simulator_correction_changes"]), 1)
        self.assertEqual(result["unexplained_differences"], [])
        unknown = dict(differences[0], after="f" * 64)
        self.assertEqual(_classify_technical_source_differences([unknown])["status"], "FAIL")

    def test_stable_trade_id_comparison_reports_plan_state_and_cost_changes(self):
        before = [{
            "symbol": "EURUSD", "threshold": 65, "mode": "LIMIT", "signal_time": "2026-01-01T00:00:00Z",
            "direction": "BUY", "entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12,
            "fill_time": "2026-01-01T00:05:00Z", "exit_time": "2026-01-01T01:00:00Z",
            "result": "WIN", "transaction_cost_eur": 0.1, "net_pnl_eur": 2.0,
        }]
        self.assertEqual(_compare_trade_records(before, before)["status"], "PASS")
        after = [dict(before[0], entry=1.101, transaction_cost_eur=0.2, net_pnl_eur=1.9)]
        comparison = _compare_trade_records(before, after)
        self.assertEqual(comparison["status"], "FAIL")
        self.assertEqual({item["field"] for item in comparison["differences"]}, {"entry", "transaction_cost_eur", "net_pnl_eur"})

    def test_external_results_require_matching_source_provenance(self):
        snapshot = _source_snapshot()
        # This test validates hash reuse, not the environment's installed SDK.
        # Supply a deterministic dependency snapshot and cover missing-SDK
        # behavior in a separate explicit assertion below.
        snapshot["dependencies"] = {**snapshot["dependencies"], "openai": "test-sdk-version"}
        relative = "reports/_test_external_provenance_deepseek.json"
        path = ROOT / relative
        try:
            prior = dict(snapshot, validation_commit="pre-merge-sha", branch="codex/phase1-final-validation")
            path.write_text(__import__("json").dumps({
                "status": "PASS",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "configuration": _external_scope_snapshot("deepseek", snapshot)["configuration"],
                "traceability": {
                    "validation_fingerprint": prior,
                    "external_scope": _external_scope_snapshot("deepseek", snapshot),
                    "external_scope_fingerprint": _external_scope_fingerprint("deepseek", snapshot),
                },
                "command_executed": ["python", "external-check"],
            }), encoding="utf-8")
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["evidence_reused"])
            self.assertEqual(result["result_origin"], "REUSED_VERIFIED_EXTERNAL_EVIDENCE")
            stale = dict(snapshot)
            changed_scope = _external_scope_snapshot("deepseek", snapshot)
            changed_scope["files"]["src/agents/deepseek_client.py"] = "changed-code-hash"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["traceability"]["external_scope"] = changed_scope
            data["traceability"]["external_scope_fingerprint"] = _external_scope_data_fingerprint(changed_scope)
            path.write_text(json.dumps(data), encoding="utf-8")
            result = _external_validation_status(relative, stale)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("files.src/agents/deepseek_client.py", result["stale_fields"])
            changed_scope = _external_scope_snapshot("deepseek", snapshot)
            changed_scope["dependencies"]["pydantic"] = "different-version"
            data["traceability"]["external_scope"] = changed_scope
            data["traceability"]["external_scope_fingerprint"] = _external_scope_data_fingerprint(changed_scope)
            path.write_text(json.dumps(data), encoding="utf-8")
            result = _external_validation_status(relative, snapshot)
            self.assertIn("dependencies.pydantic", result["stale_fields"])
            old = json.loads(path.read_text(encoding="utf-8"))
            original_scope = _external_scope_snapshot("deepseek", snapshot)
            old["traceability"]["external_scope"] = original_scope
            old["traceability"]["external_scope_fingerprint"] = _external_scope_data_fingerprint(original_scope)
            old["generated_at_utc"] = "2020-01-01T00:00:00Z"
            path.write_text(__import__("json").dumps(old), encoding="utf-8")
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("24 hours", result["reason"])
        finally:
            path.unlink(missing_ok=True)

    def test_external_result_remains_pending_when_provider_sdk_is_missing(self):
        snapshot = _source_snapshot()
        snapshot["dependencies"] = {**snapshot["dependencies"], "openai": "NOT_INSTALLED"}
        scope = _external_scope_snapshot("deepseek", snapshot)
        relative = "reports/_test_external_provenance_deepseek_missing_sdk.json"
        path = ROOT / relative
        try:
            path.write_text(json.dumps({
                "status": "PASS",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "configuration": scope["configuration"],
                "traceability": {
                    "validation_fingerprint": dict(snapshot),
                    "external_scope": scope,
                    "external_scope_fingerprint": _external_scope_data_fingerprint(scope),
                },
                "command_executed": ["synthetic-fixture"],
            }), encoding="utf-8")
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("missing_provider_dependency_version", result["stale_fields"])
        finally:
            path.unlink(missing_ok=True)

    def test_mt5_real_readonly_evidence_is_fresh_scoped_and_keeps_demo_separate(self):
        snapshot = _source_snapshot()
        scope = _external_scope_snapshot("mt5_real", snapshot)
        relative = "reports/_test_mt5_real_readonly_status.json"
        path = ROOT / relative

        def write_fixture(**overrides):
            report = {
                "status": "MT5_REAL_READ_ONLY_PASS",
                "generated_utc": datetime.now(timezone.utc).isoformat(),
                "account_type": "REAL",
                "connection": "PASS",
                "read_only_safety": {"status": "PASS", "tradeapi_disabled": True},
                "broker_data": "PASS",
                "risk_engine": "PASS",
                "daily_loss_control": "PASS",
                "history_refresh_status": "NOT_RUN",
                "demo_validation": "PENDING_NOT_APPLICABLE_NO_DEMO_ACCOUNT",
                "orders_sent_by_process": 0,
                "trades_executed_by_process": 0,
                "symbols": {
                    symbol: {"metadata_valid": True, "risk_status": "PASS"}
                    for symbol in ("EURUSD", "USDCAD", "AUDUSD")
                },
                "traceability": {
                    "validation_fingerprint": snapshot,
                    "external_scope": scope,
                    "external_scope_fingerprint": _external_scope_data_fingerprint(scope),
                },
                "command_executed": ["python", "scripts/validate_mt5_real_readonly.py", "--no-history-refresh"],
            }
            report.update(overrides)
            path.write_text(json.dumps(report), encoding="utf-8")
            return report

        try:
            write_fixture()
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["evidence_reused"])
            self.assertEqual(result["result_origin"], "REUSED_VERIFIED_EXTERNAL_EVIDENCE")
            self.assertEqual(result["origin_validation_commit"], snapshot["validation_commit"])

            write_fixture(generated_utc="2020-01-01T00:00:00Z")
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("24 hours", result["reason"])

            write_fixture(symbols={"EURUSD": {"metadata_valid": True, "risk_status": "PASS"}})
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PENDING")

            for bad_safety in (
                {"account_type": "DEMO"},
                {"read_only_safety": {"status": "FAIL", "tradeapi_disabled": False}},
                {"orders_sent_by_process": 1},
            ):
                write_fixture(**bad_safety)
                result = _external_validation_status(relative, snapshot)
                self.assertEqual(result["status"], "FAIL")

            changed_snapshot = dict(snapshot)
            changed_snapshot["relevant_file_sha256"] = {
                **snapshot["relevant_file_sha256"],
                "scripts/validate_mt5_real_readonly.py": "changed-source-hash",
            }
            write_fixture()
            result = _external_validation_status(relative, changed_snapshot)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("files.scripts/validate_mt5_real_readonly.py", result["stale_fields"])
        finally:
            path.unlink(missing_ok=True)

    def test_absent_mt5_real_report_remains_pending(self):
        relative = "reports/_test_missing_mt5_real_readonly_status.json"
        path = ROOT / relative
        path.unlink(missing_ok=True)
        self.assertEqual(_external_validation_status(relative, _source_snapshot())["status"], "PENDING")

    def test_main_runtime_has_no_order_send_call_and_requires_python_trading_disabled(self):
        self.assertEqual(scan_order_execution_calls(ROOT), [])
        pipeline = (ROOT / "run_dry_pipeline.py").read_text(encoding="utf-8")
        orchestrator = (ROOT / "src/orchestration/orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("if not terminal.tradeapi_disabled", pipeline)
        self.assertIn("if not terminal_info.tradeapi_disabled", orchestrator)
        pipeline_calls = {
            node.func.attr if isinstance(node.func, ast.Attribute)
            else node.func.id if isinstance(node.func, ast.Name)
            else None
            for node in ast.walk(ast.parse(pipeline))
            if isinstance(node, ast.Call)
        }
        self.assertIn("order_check", pipeline_calls)
        self.assertNotIn("order_send", pipeline_calls)


if __name__ == "__main__":
    unittest.main()
