import sys
import json
import importlib.util
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_phase1 import _external_validation_status, _redact_sensitive_text, _source_snapshot
from scripts.validate_phase1_mt5 import (
    _daily_loss_limit_check,
    _overall_status,
    _safe_public_summary,
    _traceability,
    validate,
)


class FakeRealAccountMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0

    def __init__(self):
        self.shutdown_called = False
        self.symbol_reads = 0

    def initialize(self):
        return True

    def account_info(self):
        return SimpleNamespace(
            trade_mode=1,
            login=123456789,
            balance=1000.0,
            equity=1000.0,
            currency="USD",
        )

    def terminal_info(self):
        return SimpleNamespace(tradeapi_disabled=True)

    def symbol_info(self, symbol):
        self.symbol_reads += 1
        raise AssertionError("Real accounts must be rejected before symbol reads")

    def shutdown(self):
        self.shutdown_called = True


class MT5SafetyTests(unittest.TestCase):
    def test_real_account_is_rejected_without_symbol_or_order_requests(self):
        terminal = FakeRealAccountMT5()
        report = validate(terminal)
        self.assertEqual(report["status"], "PENDING")
        self.assertEqual(report["reason_code"], "ACCOUNT_NOT_VERIFIABLY_DEMO")
        self.assertEqual(report["checks"]["demo_account"]["status"], "PENDING")
        self.assertEqual(terminal.symbol_reads, 0)
        self.assertTrue(terminal.shutdown_called)
        self.assertNotIn("login", str(report))
        self.assertFalse(report["real_execution_authorized"])
        self.assertEqual(report["order_send_calls"], 0)

    def test_python_api_must_be_disabled_before_market_data_checks(self):
        class FakeDemoWithApiEnabled(FakeRealAccountMT5):
            def account_info(self):
                return SimpleNamespace(trade_mode=0, balance=1000.0, equity=1000.0, currency="USD")

            def terminal_info(self):
                return SimpleNamespace(tradeapi_disabled=False)

        terminal = FakeDemoWithApiEnabled()
        report = validate(terminal)
        self.assertEqual(report["status"], "PENDING")
        self.assertEqual(report["reason_code"], "PYTHON_API_TRADING_NOT_DISABLED")
        self.assertEqual(terminal.symbol_reads, 0)
        self.assertNotIn("account_snapshot", report)

    def test_daily_limit_allows_or_blocks_as_valid_risk_behaviors(self):
        allowed = _daily_loss_limit_check({
            "approved": True, "errors": [], "daily_risk_used": 12.0,
            "daily_loss_limit": 100.0, "locked": False,
        })
        blocked = _daily_loss_limit_check({
            "approved": False, "errors": ["DAILY_LOSS_LIMIT_REACHED"],
            "daily_risk_used": 100.0, "daily_loss_limit": 100.0, "locked": True,
        })
        self.assertEqual(allowed["status"], "PASS")
        self.assertEqual(allowed["behavior"], "PROPOSALS_ALLOWED")
        self.assertEqual(blocked["status"], "PASS")
        self.assertEqual(blocked["behavior"], "PROPOSALS_BLOCKED_DAILY_LIMIT_REACHED")
        self.assertEqual(_overall_status({"daily_loss_limit_evaluation": allowed}), "PASS")
        self.assertEqual(_overall_status({"daily_loss_limit_evaluation": blocked}), "PASS")

    def test_daily_limit_fails_closed_on_missing_or_unexplained_evaluation(self):
        missing = _daily_loss_limit_check({
            "approved": False, "errors": ["DAILY_RISK_DATA_UNAVAILABLE"],
            "daily_risk_used": None, "daily_loss_limit": None, "locked": False,
        })
        unexplained = _daily_loss_limit_check({
            "approved": False, "errors": ["DAILY_RISK_STATE_INVALID"],
            "daily_risk_used": 10.0, "daily_loss_limit": 100.0, "locked": False,
        })
        self.assertEqual(missing["status"], "FAIL")
        self.assertEqual(unexplained["status"], "FAIL")
        self.assertEqual(_overall_status({"daily_loss_limit_evaluation": missing}), "FAIL")

    def test_public_mt5_summary_drops_financial_and_account_fields(self):
        report = {
            "status": "PASS",
            "generated_at_utc": "2026-11-05T00:00:00Z",
            "account_snapshot": {"balance": 123456.78, "equity": 123456.78, "currency": "EUR"},
            "account_login": 987654321,
            "server": "PrivateBroker-Real",
            "checks": {"account_values": {"status": "PASS", "balance": 123456.78}},
            "traceability": {"validation_fingerprint": {
                "validation_commit": "abc123", "branch": "codex/test",
                "source_fingerprint_sha256": "safe-hash",
            }},
        }
        encoded = json.dumps(_safe_public_summary(report))
        for sensitive in ("123456.78", "987654321", "PrivateBroker-Real", "balance", "equity", "account_login"):
            self.assertNotIn(sensitive, encoded)
        self.assertIn('"status": "PASS"', encoded)
        self.assertIn('"order_send_calls": 0', encoded)
        self.assertIn('"validation_fingerprint"', encoded)

    def test_public_pending_summary_preserves_safe_reason_and_origin_provenance(self):
        report = {
            "status": "PENDING",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "reason_code": "ACCOUNT_NOT_VERIFIABLY_DEMO",
            "reason": "do not publish account details",
            "traceability": {"validation_fingerprint": {
                "validation_commit": "a" * 40,
                "branch": "codex/phase1-local-closure",
                "source_fingerprint_sha256": "b" * 64,
                "dataset_sha256": "c" * 64,
                "relevant_file_sha256": {"src/risk_engine.py": "d" * 64},
                "dependencies": {"pydantic": "2.13.5"},
                "python_version": "Python 3.13",
            }},
        }
        summary = _safe_public_summary(report)
        encoded = json.dumps(summary)
        self.assertEqual(summary["reason_code"], "ACCOUNT_NOT_VERIFIABLY_DEMO")
        self.assertIn("not verifiably DEMO", summary["reason"])
        self.assertIn('"validation_commit"', encoded)
        self.assertNotIn("do not publish account details", encoded)
        self.assertNotIn("account_snapshot", encoded)

    @unittest.skipUnless(
        importlib.util.find_spec("MetaTrader5"),
        "MetaTrader5 package unavailable; provider-specific provenance cannot be verified here",
    )
    def test_public_mt5_pending_summary_is_reusable_with_verified_provenance(self):
        relative = "reports/_test_mt5_public_provenance.json"
        path = ROOT / relative
        report = {
            "status": "PENDING",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "reason_code": "ACCOUNT_NOT_VERIFIABLY_DEMO",
            "checks": {"demo_account": {"status": "PENDING"}},
            "traceability": _traceability(),
        }
        try:
            path.write_text(json.dumps(_safe_public_summary(report)), encoding="utf-8")
            result = _external_validation_status(relative, _source_snapshot())
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(result["status"], "PENDING")
        self.assertIn("evidence_reused", result, result)
        self.assertTrue(result["evidence_reused"])
        self.assertEqual(result["reason"], "Connected account is not verifiably DEMO; operational checks were not run.")

    def test_public_text_redacts_credentials_and_account_identifiers(self):
        text = _redact_sensitive_text(
            "api_key=sk-test-value password=hunter2 account_number=12345678 "
            "server=PrivateBroker Bearer tokenvalue"
        )
        for sensitive in ("sk-test-value", "hunter2", "12345678", "PrivateBroker", "tokenvalue"):
            self.assertNotIn(sensitive, text)

    def test_git_reports_exclude_private_details_and_sensitive_field_names(self):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "reports/private/phase1_mt5_validation.json"],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0, ".gitignore must exclude private validation details")
        tracked = subprocess.run(
            ["git", "ls-files", "reports"], cwd=ROOT, text=True, capture_output=True, check=True
        ).stdout.splitlines()
        forbidden_keys = {"balance", "equity", "login", "server", "account_number", "password", "token"}

        def collect_keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield str(key).lower()
                    yield from collect_keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from collect_keys(item)

        for relative in tracked:
            self.assertFalse(relative.startswith("reports/private/"), relative)
            if not relative.lower().endswith(".json"):
                continue
            data = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            present = forbidden_keys.intersection(collect_keys(data))
            self.assertFalse(present, f"sensitive fields in Git report {relative}: {sorted(present)}")


if __name__ == "__main__":
    unittest.main()
