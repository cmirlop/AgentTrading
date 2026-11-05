import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.risk_engine import calculate_position_size, convert_currency, evaluate_portfolio_limits
from src.daily_risk_guard import evaluate_daily_risk
from src.risk_preflight import evaluate_plan_risk, release_plan_risk_reservation


class RiskSizingTests(unittest.TestCase):
    def setUp(self):
        self.meta = {
            "trade_tick_size": 0.00001,
            "trade_tick_value_loss": 1.0,
            "trade_tick_value_loss_currency": "USD",
            "trade_contract_size": 100000,
            "currency_base": "EUR",
            "currency_profit": "USD",
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        self.kwargs = dict(
            symbol="EURUSD", direction="BUY", entry=1.1, stop_loss=1.099,
            equity=10000, balance=10000, account_currency="USD",
            symbol_metadata=self.meta, conversion_rates={"EURUSD": 1.1},
            max_risk_percent=0.5, max_loss_absolute=5, max_loss_currency="EUR",
            risk_reference="equity",
        )

    def test_size_respects_tick_value_percent_and_absolute_cap(self):
        result = calculate_position_size(**self.kwargs)
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(result["volume"], 0.05)
        self.assertLessEqual(result["risk_amount_account"], result["risk_budget_account"])

    def test_invalid_or_missing_stop_loss_rejected(self):
        invalid = dict(self.kwargs, stop_loss=1.1)
        self.assertIn("STOP_LOSS_INVALID", calculate_position_size(**invalid)["errors"])
        missing = dict(self.kwargs, stop_loss=None)
        self.assertEqual(calculate_position_size(**missing)["status"], "REJECTED")

    def test_minimum_volume_cannot_override_budget(self):
        meta = dict(self.meta, volume_min=0.1)
        result = calculate_position_size(**dict(self.kwargs, symbol_metadata=meta))
        self.assertIn("VOLUME_BELOW_MINIMUM_RISK_BUDGET", result["errors"])

    def test_missing_symbol_metadata_fails_closed(self):
        result = calculate_position_size(**dict(self.kwargs, symbol_metadata={"volume_min": 0.01}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(result["errors"])

    def test_required_currency_conversion_is_used(self):
        self.assertAlmostEqual(convert_currency(10, "EUR", "USD", {"EURUSD": 1.1}), 11)
        result = calculate_position_size(**dict(self.kwargs, account_currency="CAD", conversion_rates={"EURUSD": 1.1}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("CURRENCY_CONVERSION_MISSING", result["errors"])


class DailyRiskTests(unittest.TestCase):
    def test_daily_loss_locks_across_restart_and_resets_at_utc_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            first = evaluate_daily_risk(
                realized_net_pnl=-60, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=5, state_path=path,
                now=datetime(2026, 4, 8, 14, tzinfo=timezone.utc),
            )
            self.assertFalse(first["approved"])
            self.assertIn("DAILY_LOSS_LIMIT_REACHED", first["errors"])
            restarted = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=5, state_path=path,
                now=datetime(2026, 4, 8, 18, tzinfo=timezone.utc),
            )
            self.assertIn("DAILY_LOSS_LIMIT_LOCKED", restarted["errors"])
            tomorrow = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=5, state_path=path,
                now=datetime(2026, 4, 9, 0, 1, tzinfo=timezone.utc),
            )
            self.assertTrue(tomorrow["approved"])

    def test_open_pending_risk_and_new_proposal_count_toward_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=20, pending_risk=15, proposed_risk=20,
                equity=1000, max_loss_percent=5, state_path=Path(directory) / "guard.json",
            )
            self.assertFalse(result["approved"])
            self.assertIn("DAILY_RISK_BUDGET_EXCEEDED", result["errors"])

    def test_duplicate_proposal_rejected_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            kwargs = dict(realized_net_pnl=0, open_risk=0, pending_risk=0,
                          equity=1000, max_loss_percent=5, state_path=path,
                          proposal_id="same-plan")
            self.assertTrue(evaluate_daily_risk(**kwargs)["approved"])
            result = evaluate_daily_risk(**kwargs)
            self.assertIn("DUPLICATE_PROPOSAL", result["errors"])


class PortfolioExposureTests(unittest.TestCase):
    def test_usd_exposure_across_pairs_is_aggregated(self):
        metadata = {
            "EURUSD": {"trade_contract_size": 100000, "currency_base": "EUR", "currency_profit": "USD"},
            "AUDUSD": {"trade_contract_size": 100000, "currency_base": "AUD", "currency_profit": "USD"},
        }
        plan = {"symbol": "EURUSD", "direction": "BUY", "entry": 1.1, "volume": 0.2, "estimated_loss_account": 2}
        limits = evaluate_portfolio_limits(
            positions=[{"symbol": "AUDUSD", "side": "BUY", "volume": 0.2, "price_current": 0.7, "risk_to_sl_account": 1}],
            pending_orders=[], proposed_plan=plan, symbol_metadata=metadata,
            conversion_rates={"EURUSD": 1.1, "AUDUSD": 0.7}, account_currency="USD",
            equity=10000, max_portfolio_risk_percent=2,
            max_currency_exposure_equity_multiple=1,
        )
        self.assertFalse(limits["approved"])
        self.assertIn("CURRENCY_EXPOSURE_LIMIT_EXCEEDED_USD", limits["errors"])

    def test_unknown_existing_risk_or_symbol_metadata_rejected(self):
        result = evaluate_portfolio_limits(
            positions=[{"symbol": "EURUSD", "side": "BUY", "volume": 0.1, "price_current": 1.1}],
            pending_orders=[], proposed_plan={"symbol": "EURUSD", "direction": "BUY", "entry": 1.1, "volume": 0.01, "estimated_loss_account": 1},
            symbol_metadata={}, conversion_rates={}, account_currency="USD", equity=10000,
            max_portfolio_risk_percent=2, max_currency_exposure_equity_multiple=1,
        )
        self.assertFalse(result["approved"])
        self.assertTrue(result["errors"])

    def test_preflight_approves_once_then_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = {
                "account": {"currency": "USD", "equity": 10000},
                "risk_snapshot": {
                    "available": True,
                    "realized_net_pnl": 0,
                    "open_risk_account": 0,
                    "pending_risk_account": 0,
                    "positions": [],
                    "pending_orders": [],
                    "symbol_metadata": {
                        "EURUSD": {"trade_contract_size": 100000, "currency_base": "EUR", "currency_profit": "USD"},
                    },
                    "conversion_rates": {"EURUSD": 1.1},
                },
            }
            plan = {"symbol": "EURUSD", "direction": "BUY", "order_type": "BUY_LIMIT",
                    "entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12,
                    "volume": 0.01, "estimated_loss_account": 10}
            state_path = Path(directory) / "guard.json"
            first = evaluate_plan_risk(plan, runtime, state_path=state_path)
            second = evaluate_plan_risk(plan, runtime, state_path=state_path)
            self.assertTrue(first["approved"], first["errors"])
            self.assertFalse(second["approved"])
            self.assertIn("DUPLICATE_PROPOSAL", second["errors"])
            self.assertFalse(release_plan_risk_reservation(plan, state_path=state_path))
            self.assertFalse(release_plan_risk_reservation(
                plan, state_path=state_path, reservation_token="wrong-owner"
            ))
            self.assertTrue(release_plan_risk_reservation(
                plan, state_path=state_path, reservation_token=first["reservation_token"]
            ))
            self.assertTrue(evaluate_plan_risk(plan, runtime, state_path=state_path)["approved"])

    def test_preflight_rejects_unavailable_runtime_snapshot(self):
        result = evaluate_plan_risk({}, {"account": {}, "risk_snapshot": {"available": False}})
        self.assertFalse(result["approved"])
        self.assertIn("RISK_SNAPSHOT_UNAVAILABLE", result["errors"])

    def test_preflight_aggregates_simultaneous_proposal_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = {
                "EURUSD": {"trade_contract_size": 100000, "currency_base": "EUR", "currency_profit": "USD"},
                "AUDUSD": {"trade_contract_size": 100000, "currency_base": "AUD", "currency_profit": "USD"},
            }
            runtime = {
                "account": {"currency": "USD", "equity": 100000},
                "risk_snapshot": {
                    "available": True, "realized_net_pnl": 0, "open_risk_account": 0,
                    "pending_risk_account": 0, "positions": [], "pending_orders": [],
                    "symbol_metadata": metadata,
                    "conversion_rates": {"EURUSD": 1.1, "AUDUSD": 0.7},
                },
            }
            path = Path(directory) / "guard.json"
            first = {"symbol": "EURUSD", "direction": "BUY", "order_type": "BUY_LIMIT",
                     "entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12,
                     "volume": 0.01, "estimated_loss_account": 1200}
            second = {"symbol": "AUDUSD", "direction": "BUY", "order_type": "BUY_LIMIT",
                      "entry": 0.7, "stop_loss": 0.69, "take_profit": 0.72,
                      "volume": 0.01, "estimated_loss_account": 1200}
            self.assertTrue(evaluate_plan_risk(first, runtime, state_path=path)["approved"])
            result = evaluate_plan_risk(second, runtime, state_path=path)
            self.assertFalse(result["approved"])
            self.assertIn("DAILY_RISK_BUDGET_EXCEEDED", result["errors"])
            self.assertIn("MAX_PORTFOLIO_RISK_EXCEEDED", result["errors"])


if __name__ == "__main__":
    unittest.main()
