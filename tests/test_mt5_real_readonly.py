"""Focused safety tests for the isolated MT5 real-account validator."""

from __future__ import annotations

import ast
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from scripts import validate_mt5_real_readonly as validator
from src.mt5_deal_history import calculate_realized_net_pnl


class FakeAPI:
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_TRADE_MODE_DEMO = 0

    def __init__(self, *, disabled: bool, trade_mode: int = 2):
        self.calls = []
        self.disabled = disabled
        self.trade_mode = trade_mode

    def initialize(self):
        self.calls.append("initialize")
        return True

    def terminal_info(self):
        self.calls.append("terminal_info")
        return SimpleNamespace(connected=True, tradeapi_disabled=self.disabled)

    def account_info(self):
        self.calls.append("account_info")
        return SimpleNamespace(trade_mode=self.trade_mode, currency="USD")

    def shutdown(self):
        self.calls.append("shutdown")


class MT5RealReadonlySafetyTests(unittest.TestCase):
    def test_gate_failure_stops_before_account_or_market_reads(self):
        api = FakeAPI(disabled=False)
        result = validator.run_validation(api)
        self.assertEqual(result["status"], "MT5_REAL_READ_ONLY_FAIL")
        self.assertEqual(result["account"]["read_only_safety"], "FAIL")
        self.assertNotIn("account_info", api.calls)
        self.assertEqual(api.calls, ["initialize", "terminal_info", "shutdown"])

    def test_demo_account_is_not_accepted_or_read_further(self):
        api = FakeAPI(disabled=True, trade_mode=FakeAPI.ACCOUNT_TRADE_MODE_DEMO)
        result = validator.run_validation(api)
        self.assertEqual(result["status"], "MT5_REAL_READ_ONLY_FAIL")
        self.assertEqual(result["account"]["type"], "NON_REAL_OR_UNVERIFIED")
        self.assertEqual(api.calls, ["initialize", "terminal_info", "terminal_info", "account_info", "terminal_info", "shutdown"])

    def test_dynamic_read_method_is_rejected(self):
        api = FakeAPI(disabled=True)
        with self.assertRaises(validator.SafetyGateError):
            validator._read(api, "not_an_allowlisted_method")
        self.assertEqual(api.calls, [])

    def test_public_projection_excludes_account_identifiers_and_financials(self):
        private = {
            "status": "MT5_REAL_READ_ONLY_PENDING",
            "generated_utc": "2026-11-09T00:00:00+00:00",
            "account": {
                "type": "REAL", "connection": "PASS", "read_only_safety": "PASS",
                "tradeapi_disabled": True, "login": 12345678, "server": "private-broker",
                "balance": 50000.0, "equity": 49900.0,
            },
            "symbols": {}, "risk": {}, "history": {},
            "safe_errors": [],
        }
        public_text = json.dumps(validator._public_projection(private), sort_keys=True)
        for secret in ("12345678", "private-broker", "50000.0", "49900.0", '"login"', '"balance"', '"equity"'):
            self.assertNotIn(secret, public_text)
        self.assertIn('"account_type": "REAL"', public_text)
        self.assertIn('"orders_sent_by_process": 0', public_text)

    def test_public_projection_includes_observed_spread_points(self):
        private = {
            "symbols": {"EURUSD": {"metadata_valid": True, "metadata": {"spread_points": 8}, "spread_quality": "PASS"}},
            "risk": {}, "history": {},
        }
        projected = validator._public_projection(private)
        self.assertEqual(projected["symbols"]["EURUSD"]["spread_points"], 8)

    def test_configured_exposure_block_is_a_successful_guard_decision(self):
        status, decision = validator._portfolio_decision([
            "CURRENCY_EXPOSURE_LIMIT_EXCEEDED_USD",
            "CURRENCY_EXPOSURE_LIMIT_EXCEEDED_EUR",
        ])
        self.assertEqual((status, decision), ("PASS", "BLOCKED_BY_CONFIGURED_LIMIT"))

    def test_missing_portfolio_risk_data_remains_pending(self):
        status, decision = validator._portfolio_decision(["EXISTING_POSITION_RISK_UNAVAILABLE"])
        self.assertEqual((status, decision), ("PENDING", "INSUFFICIENT_DATA"))

    def test_history_refresh_excludes_still_open_bar(self):
        class RatesAPI:
            def terminal_info(self):
                return SimpleNamespace(connected=True, tradeapi_disabled=True)

            def copy_rates_from_pos(self, symbol, timeframe, start, count):
                self.assert_call = (symbol, timeframe, start, count)
                closed = int(datetime(2026, 11, 9, 20, 15, tzinfo=timezone.utc).timestamp())
                opened = int(datetime(2026, 11, 9, 20, 20, tzinfo=timezone.utc).timestamp())
                def bar(epoch):
                    return {"time": epoch, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15,
                            "tick_volume": 10, "spread": 2, "real_volume": 0}
                return [bar(closed), bar(opened)]

        original_root = validator.ROOT
        with tempfile.TemporaryDirectory() as temp:
            try:
                validator.ROOT = Path(temp)
                local = validator.ROOT / "data" / "market" / "EURUSD" / "M5.csv"
                local.parent.mkdir(parents=True)
                with local.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"))
                    writer.writerow(("2026-11-09T20:00:00+00:00", 1.1, 1.2, 1.0, 1.15, 5, 2, 0))
                destination = validator.ROOT / "snapshot"
                destination.mkdir()
                result = validator._serialize_rates(
                    RatesAPI(), "EURUSD", "M5", 5, destination,
                    datetime(2026, 11, 9, 20, 21, tzinfo=timezone.utc),
                )
                self.assertEqual(result["status"], "PASS")
                self.assertEqual(result["closed_bars"], 1)
                self.assertEqual(result["new_bars"], 1)
                self.assertEqual(result["latest_closed_utc"], "2026-11-09T20:15:00+00:00")
                with (destination / "EURUSD_M5.csv").open(encoding="utf-8") as handle:
                    saved = list(csv.DictReader(handle))
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]["time"], "2026-11-09T20:15:00+00:00")
            finally:
                validator.ROOT = original_root

    def test_daily_deposits_do_not_offset_realized_trading_loss(self):
        class Constants:
            DEAL_TYPE_BUY = 0
            DEAL_TYPE_SELL = 1
            DEAL_TYPE_BALANCE = 2
            DEAL_TYPE_CREDIT = 3
            DEAL_TYPE_CHARGE = 4
            DEAL_TYPE_CORRECTION = 5
            DEAL_TYPE_BONUS = 6
            DEAL_TYPE_INTEREST = 7
            DEAL_TYPE_BUY_CANCELED = 8
            DEAL_TYPE_SELL_CANCELED = 9
            DEAL_TYPE_DIVIDEND = 10
            DEAL_TYPE_DIVIDEND_FRANKED = 11
            DEAL_TYPE_TAX = 12

        buy = SimpleNamespace(type=0, time=1794182400, profit=-100.0, commission=0.0, swap=0.0, fee=0.0, ticket=1)
        deposit = SimpleNamespace(type=2, time=1794182400, profit=1000.0, commission=0.0, swap=0.0, fee=0.0, ticket=2)
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(1794182400, timezone.utc)
        end = datetime.fromtimestamp(1794182401, timezone.utc)
        result = calculate_realized_net_pnl([buy, deposit], Constants, window_start_utc=start, window_end_utc=end)
        self.assertTrue(result["available"])
        self.assertEqual(result["realized_net_pnl"], -100.0)
        self.assertEqual(result["excluded_non_trade_deal_count"], 1)

    def test_validator_has_no_order_or_position_mutation_route(self):
        source = Path(validator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_attributes = {"order_send", "order_check", "positions_set", "orders_set", "symbol_select", "login"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden_attributes)
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden_attributes)
        self.assertTrue(validator.READ_ONLY_METHODS.issubset({
            "account_info", "positions_get", "orders_get", "history_deals_get",
            "symbol_info", "symbol_info_tick", "copy_rates_from_pos", "copy_rates_range",
            "order_calc_profit", "order_calc_margin",
        }))

    def test_private_report_directory_is_ignored_by_git(self):
        ignore_text = (validator.ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/reports/private/", ignore_text)


if __name__ == "__main__":
    unittest.main()
