import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from src.daily_risk_guard import evaluate_daily_risk
from src.mt5_deal_history import calculate_realized_net_pnl
from src.risk_preflight import evaluate_plan_risk, release_plan_risk_reservation


class MockMT5DealTypes:
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_TYPE_BALANCE = 2
    DEAL_TYPE_CREDIT = 3
    DEAL_TYPE_CHARGE = 4
    DEAL_TYPE_CORRECTION = 5
    DEAL_TYPE_BONUS = 6
    DEAL_TYPE_COMMISSION = 7
    DEAL_TYPE_COMMISSION_DAILY = 8
    DEAL_TYPE_COMMISSION_MONTHLY = 9
    DEAL_TYPE_AGENT_DAILY = 10
    DEAL_TYPE_AGENT_MONTHLY = 11
    DEAL_TYPE_INTEREST = 12
    DEAL_TYPE_BUY_CANCELED = 13
    DEAL_TYPE_SELL_CANCELED = 14
    DEAL_TYPE_DIVIDEND = 15
    DEAL_TYPE_DIVIDEND_FRANKED = 16
    DEAL_TYPE_TAX = 17


def _deal(ticket, deal_type, *, profit=0, commission=0, swap=0, fee=0,
          position_id=0, order=0, timestamp=1793870000):
    return SimpleNamespace(
        ticket=ticket,
        type=deal_type,
        profit=profit,
        commission=commission,
        swap=swap,
        fee=fee,
        position_id=position_id,
        order=order,
        time=timestamp,
    )


def _concurrent_runtime():
    metadata = {
        "EURUSD": {"trade_contract_size": 100000, "currency_base": "EUR", "currency_profit": "USD"},
        "AUDUSD": {"trade_contract_size": 100000, "currency_base": "AUD", "currency_profit": "USD"},
    }
    return {
        "account": {"currency": "USD", "equity": 100000},
        "risk_snapshot": {
            "available": True,
            "realized_net_pnl": 0,
            "open_risk_account": 0,
            "pending_risk_account": 0,
            "positions": [],
            "pending_orders": [],
            "symbol_metadata": metadata,
            "conversion_rates": {"EURUSD": 1.1, "AUDUSD": 0.7},
        },
    }


def _plan(symbol="EURUSD", loss=1300):
    entry = 1.1 if symbol == "EURUSD" else 0.7
    return {
        "symbol": symbol,
        "direction": "BUY",
        "order_type": "BUY_LIMIT",
        "entry": entry,
        "stop_loss": entry - 0.01,
        "take_profit": entry + 0.02,
        "volume": 0.01,
        "estimated_loss_account": loss,
    }


def _reservation_worker(state_path, barrier, queue, plan):
    barrier.wait(timeout=20)
    result = evaluate_plan_risk(plan, _concurrent_runtime(), state_path=state_path)
    queue.put((result["approved"], result["errors"], result.get("reservation_token")))


class RealizedDailyDealPnlTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 5, 0, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 5, 23, 59, tzinfo=timezone.utc)

    def test_trade_profit_loss_commission_swap_and_fee_sum_once(self):
        deals = [
            _deal(1, MockMT5DealTypes.DEAL_TYPE_BUY, profit=25, commission=-2, swap=-1, fee=-0.5),
            _deal(2, MockMT5DealTypes.DEAL_TYPE_SELL, profit=-10, commission=-1),
        ]
        result = calculate_realized_net_pnl(
            deals, MockMT5DealTypes,
            window_start_utc=self.start, window_end_utc=self.end,
        )
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["realized_net_pnl"], 10.5)
        self.assertEqual(result["trade_deal_count"], 2)

    def test_deposits_withdrawals_and_other_balance_movements_are_excluded(self):
        trades = [_deal(1, MockMT5DealTypes.DEAL_TYPE_BUY, profit=5, commission=-1)]
        movements = [
            _deal(2, MockMT5DealTypes.DEAL_TYPE_BALANCE, profit=10000),
            _deal(3, MockMT5DealTypes.DEAL_TYPE_BALANCE, profit=-5000),
            _deal(4, MockMT5DealTypes.DEAL_TYPE_CREDIT, profit=9000),
            _deal(5, MockMT5DealTypes.DEAL_TYPE_CHARGE, profit=-999),
            _deal(6, MockMT5DealTypes.DEAL_TYPE_INTEREST, profit=888),
        ]
        result = calculate_realized_net_pnl(
            trades + movements, MockMT5DealTypes,
            window_start_utc=self.start, window_end_utc=self.end,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["realized_net_pnl"], 4)
        self.assertEqual(result["excluded_non_trade_deal_count"], len(movements))

    def test_standalone_linked_commission_is_included_once(self):
        result = calculate_realized_net_pnl(
            [
                _deal(1, MockMT5DealTypes.DEAL_TYPE_BUY, profit=10, position_id=88),
                _deal(2, MockMT5DealTypes.DEAL_TYPE_COMMISSION, commission=-2, position_id=88),
            ],
            MockMT5DealTypes,
            window_start_utc=self.start,
            window_end_utc=self.end,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["realized_net_pnl"], 8)
        self.assertEqual(result["commission_deal_count"], 1)

    def test_trade_with_zero_commission_is_valid_and_deterministic(self):
        trade = _deal(1, MockMT5DealTypes.DEAL_TYPE_SELL, profit=-4)
        one = calculate_realized_net_pnl([trade], MockMT5DealTypes)
        two = calculate_realized_net_pnl([trade], MockMT5DealTypes)
        self.assertEqual(one, two)
        self.assertEqual(one["realized_net_pnl"], -4)

    def test_incomplete_unlinked_duplicate_and_out_of_window_history_fail_closed(self):
        missing = SimpleNamespace(type=MockMT5DealTypes.DEAL_TYPE_BUY, profit=1)
        cases = [
            (None, "DAILY_DEAL_HISTORY_UNAVAILABLE"),
            ([missing], "DAILY_DEAL_TIMESTAMP_MISSING"),
            ([_deal(1, 999)], "DAILY_DEAL_TYPE_UNKNOWN"),
            ([_deal(1, MockMT5DealTypes.DEAL_TYPE_COMMISSION, commission=-1)], "DAILY_COMMISSION_UNLINKED"),
            ([_deal(1, MockMT5DealTypes.DEAL_TYPE_BUY, timestamp=1)], "DAILY_DEAL_OUTSIDE_UTC_WINDOW"),
            ([_deal(1, MockMT5DealTypes.DEAL_TYPE_BUY)] * 2, "DAILY_DEAL_HISTORY_DUPLICATE"),
        ]
        for deals, reason in cases:
            with self.subTest(reason=reason):
                result = calculate_realized_net_pnl(
                    deals, MockMT5DealTypes,
                    window_start_utc=self.start, window_end_utc=self.end,
                )
                self.assertFalse(result["available"])
                self.assertEqual(result["reason"], reason)


class DailyReservationLifecycleTests(unittest.TestCase):
    def test_restart_preserves_reservation_but_utc_day_change_expires_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            now = datetime(2026, 11, 5, 23, 59, tzinfo=timezone.utc)
            first = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, proposed_risk=20,
                equity=1000, max_loss_percent=10, state_path=path,
                now=now, proposal_id="proposal",
            )
            self.assertTrue(first["approved"])
            restarted = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=10, state_path=path,
                now=now.replace(minute=59), reserve_proposal=False,
            )
            self.assertEqual(restarted["reserved_proposal_risk"], 20)
            next_day = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=10, state_path=path,
                now=datetime(2026, 11, 6, 0, 1, tzinfo=timezone.utc),
                reserve_proposal=False,
            )
            self.assertEqual(next_day["reserved_proposal_risk"], 0)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["day"], "2026-11-06")

    def test_expired_reservation_stops_consuming_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            first_time = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
            first = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, proposed_risk=20,
                equity=1000, max_loss_percent=10, state_path=path,
                now=first_time, proposal_id="proposal", reservation_ttl_minutes=1,
            )
            self.assertTrue(first["approved"])
            second = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, proposed_risk=20,
                equity=1000, max_loss_percent=10, state_path=path,
                now=first_time.replace(minute=2), proposal_id="proposal",
                reservation_ttl_minutes=1,
            )
            self.assertTrue(second["approved"])
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["reservations"]["proposal"]["status"], "ACTIVE")
            self.assertTrue(any(event["type"] == "RESERVATION_EXPIRED" for event in state["events"]))

    def test_release_requires_owner_token_and_records_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            result = evaluate_plan_risk(_plan(loss=100), _concurrent_runtime(), state_path=path)
            self.assertTrue(result["approved"], result["errors"])
            self.assertFalse(release_plan_risk_reservation(
                _plan(loss=100), state_path=path, reservation_token="other-process-token"
            ))
            self.assertTrue(release_plan_risk_reservation(
                _plan(loss=100), state_path=path,
                reservation_token=result["reservation_token"],
            ))
            state = json.loads(path.read_text(encoding="utf-8"))
            reservation = state["reservations"][result["reservation_id"]]
            self.assertEqual(reservation["status"], "RELEASED")
            self.assertTrue(any(event["type"] == "RESERVATION_RELEASED" for event in state["events"]))

    def test_write_failure_keeps_previous_file_valid_and_denies_new_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            baseline = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=10, state_path=path, reserve_proposal=False,
            )
            self.assertTrue(baseline["approved"])
            before = path.read_bytes()
            with patch("src.daily_risk_guard.os.replace", side_effect=OSError("simulated")):
                rejected = evaluate_daily_risk(
                    realized_net_pnl=0, open_risk=0, pending_risk=0, proposed_risk=10,
                    equity=1000, max_loss_percent=10, state_path=path,
                    proposal_id="no-write",
                )
            self.assertFalse(rejected["approved"])
            self.assertIn("DAILY_RISK_STATE_WRITE_FAILED", rejected["errors"])
            self.assertEqual(path.read_bytes(), before)
            json.loads(path.read_text(encoding="utf-8"))

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            path.write_text("{invalid", encoding="utf-8")
            result = evaluate_daily_risk(
                realized_net_pnl=0, open_risk=0, pending_risk=0, equity=1000,
                max_loss_percent=10, state_path=path,
            )
            self.assertFalse(result["approved"])
            self.assertIn("DAILY_RISK_STATE_UNREADABLE", result["errors"])

    def test_lock_failure_fails_closed(self):
        if os.name == "nt":
            self.skipTest("fcntl lock injection is POSIX-specific; Windows lock is exercised by spawned-process tests")
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            with patch.object(fcntl, "flock", side_effect=OSError("simulated lock failure")):
                result = evaluate_daily_risk(
                    realized_net_pnl=0, open_risk=0, pending_risk=0,
                    equity=1000, max_loss_percent=10, state_path=path,
                )
            self.assertFalse(result["approved"])
            self.assertIn("DAILY_RISK_STATE_LOCK_FAILED", result["errors"])

    def test_two_real_processes_cannot_exceed_daily_or_portfolio_budget(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "guard.json")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [
                context.Process(target=_reservation_worker, args=(path, barrier, queue, _plan("EURUSD"))),
                context.Process(target=_reservation_worker, args=(path, barrier, queue, _plan("AUDUSD"))),
            ]
            for process in processes:
                process.start()
            results = [queue.get(timeout=45) for _ in processes]
            for process in processes:
                process.join(timeout=45)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(bool(item[0]) for item in results), 1, results)
            state = json.loads(Path(path).read_text(encoding="utf-8"))
            active = [item for item in state["reservations"].values() if item["status"] == "ACTIVE"]
            self.assertEqual(len(active), 1)
            self.assertLessEqual(sum(item["risk"] for item in active), 2000)

    def test_two_real_processes_cannot_reserve_duplicate_plan(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "guard.json")
            barrier = context.Barrier(2)
            queue = context.Queue()
            plan = _plan(loss=100)
            processes = [
                context.Process(target=_reservation_worker, args=(path, barrier, queue, plan)),
                context.Process(target=_reservation_worker, args=(path, barrier, queue, plan)),
            ]
            for process in processes:
                process.start()
            results = [queue.get(timeout=45) for _ in processes]
            for process in processes:
                process.join(timeout=45)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(bool(item[0]) for item in results), 1, results)
            self.assertEqual(sum("DUPLICATE_PROPOSAL" in item[1] for item in results), 1, results)


if __name__ == "__main__":
    unittest.main()
