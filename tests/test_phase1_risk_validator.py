import importlib
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

mt5_fake = types.ModuleType("MetaTrader5")
mt5_fake.TRADE_ACTION_PENDING = 5
with patch.dict(sys.modules, {"MetaTrader5": mt5_fake}):
    risk_validator = importlib.import_module("src.risk_validator")

from config.execution_config import MAGIC_NUMBER, ORDER_COMMENT


class RiskValidatorTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "status": "VALID", "symbol": "EURUSD", "volume": 0.05,
            "broker_constraints": {"volume_min": 0.01, "volume_max": 100, "volume_step": 0.01},
            "risk_sizing": {"status": "VALID"}, "direction": "BUY",
            "entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12,
            "estimated_loss_eur": 4.95, "estimated_loss_account": 5.445,
            "risk_reward": 2.0, "spread_points": 2,
            "margin_required_eur": 10, "free_margin_eur": 1000,
        }
        self.candidate = {
            "allowed": True, "status": "CANDIDATE_LONG",
            "macro": {"new_trades_blocked": False, "upcoming_high_events": [], "upcoming_critical_events": []},
        }
        self.request = {
            "action": 5, "symbol": "EURUSD", "magic": MAGIC_NUMBER,
            "comment": ORDER_COMMENT, "volume": 0.05,
            "expiration": int(datetime.now(timezone.utc).timestamp()) + 600,
        }
        self.order_check = types.SimpleNamespace(retcode=0)
        self.terminal = types.SimpleNamespace(tradeapi_disabled=True)

    def validate(self, plan=None, request=None, portfolio_risk=None):
        return risk_validator.validate_dry_run(
            symbol="EURUSD", candidate=self.candidate, trade_plan=plan or self.plan,
            request=request or self.request, order_check_result=self.order_check,
            terminal_info=self.terminal,
            portfolio_risk=portfolio_risk or {"approved": True, "errors": []},
        )

    def test_dynamic_broker_volume_is_accepted_when_within_constraints(self):
        result = self.validate()
        self.assertTrue(result["approved"], result["errors"])

    def test_below_minimum_and_request_volume_mismatch_are_rejected(self):
        plan = dict(self.plan, broker_constraints={"volume_min": 0.1, "volume_max": 100, "volume_step": 0.01})
        self.assertIn("VOLUME_BELOW_MINIMUM", self.validate(plan=plan)["errors"])
        request = dict(self.request, volume=0.01)
        self.assertIn("REQUEST_VOLUME_MISMATCH", self.validate(request=request)["errors"])

    def test_missing_portfolio_preflight_is_fail_closed(self):
        result = self.validate(portfolio_risk={"approved": False, "errors": ["MAX_PORTFOLIO_RISK_EXCEEDED"]})
        self.assertFalse(result["approved"])
        self.assertIn("MAX_PORTFOLIO_RISK_EXCEEDED", result["errors"])


if __name__ == "__main__":
    unittest.main()
