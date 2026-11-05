import sys
import json
import tempfile
import unittest
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_phase1 import (
    _assess_baseline_comparison,
    _external_validation_status,
    _source_snapshot,
    _verify_refresh_compatibility,
    scan_order_execution_calls,
)


class ValidationProvenanceTests(unittest.TestCase):
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
            findings = scan_order_execution_calls(root)
        self.assertEqual({item["file"] for item in findings}, set(examples))

    def test_only_documented_execution_differences_are_expected(self):
        expected = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "ambiguous", "before": 1, "after": 2}],
        })
        self.assertEqual(expected["status"], "PASS")
        self.assertEqual(expected["assessment"], "EXPECTED_EXECUTION_CLASSIFICATION_OR_COST_CORRECTION")
        capacity_change = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "plan_rejections", "before": 10, "after": 12}],
        })
        self.assertEqual(capacity_change["status"], "PASS")
        self.assertIn("slot occupancy", capacity_change["explanation"])
        unexpected = _assess_baseline_comparison({
            "status": "FAIL",
            "differences": [{"field": "opportunities_detected", "before": 10, "after": 11}],
        })
        self.assertEqual(unexpected["status"], "FAIL")

    def test_external_results_require_matching_source_provenance(self):
        snapshot = _source_snapshot()
        relative = "reports/_test_external_provenance.json"
        path = ROOT / relative
        try:
            path.write_text(__import__("json").dumps({
                "status": "PASS",
                "generated_at_utc": "2026-11-05T00:00:00Z",
                "traceability": {"validation_fingerprint": snapshot},
                "command_executed": ["python", "external-check"],
            }), encoding="utf-8")
            self.assertEqual(_external_validation_status(relative, snapshot)["status"], "PASS")
            stale = dict(snapshot)
            stale["validation_commit"] = "different-commit"
            result = _external_validation_status(relative, stale)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("validation_commit", result["stale_fields"])
            old = json.loads(path.read_text(encoding="utf-8"))
            old["generated_at_utc"] = "2020-01-01T00:00:00Z"
            path.write_text(__import__("json").dumps(old), encoding="utf-8")
            result = _external_validation_status(relative, snapshot)
            self.assertEqual(result["status"], "PENDING")
            self.assertIn("24 hours", result["reason"])
        finally:
            path.unlink(missing_ok=True)

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
