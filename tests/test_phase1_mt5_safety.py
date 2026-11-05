import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_mt5 import validate


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
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["checks"]["demo_account"]["status"], "FAIL")
        self.assertEqual(terminal.symbol_reads, 0)
        self.assertTrue(terminal.shutdown_called)
        self.assertNotIn("login", str(report))
        self.assertFalse(report["real_execution_authorized"])
        self.assertEqual(report["order_send_calls"], 0)


if __name__ == "__main__":
    unittest.main()
