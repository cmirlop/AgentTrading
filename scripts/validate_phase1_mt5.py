#!/usr/bin/env python3
"""Read-only MetaTrader 5 DEMO validation. This script never sends orders."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write(report, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _traceability():
    from scripts.validate_phase1 import _source_snapshot

    return {"validation_fingerprint": _source_snapshot()}


def _check(ok, reason=None, **details):
    return {"status": "PASS" if ok else "FAIL", **({"reason": reason} if reason else {}), **details}


def _number(value, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (positive and result <= 0):
        return None
    return result


def _collect_rates(mt5, currencies, symbols):
    pairs = set(symbols)
    currencies = sorted(set(item.upper() for item in currencies if isinstance(item, str) and len(item) == 3))
    for source in currencies:
        for target in currencies:
            if source != target:
                pairs.add(source + target)
    rates = {}
    for symbol in sorted(pairs):
        if not hasattr(mt5, "symbol_info") or mt5.symbol_info(symbol) is None:
            continue
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
        bid = _number(getattr(tick, "bid", None), positive=True)
        ask = _number(getattr(tick, "ask", None), positive=True)
        if bid and ask and ask >= bid:
            rates[symbol] = (bid + ask) / 2
    return rates


def _pending_report(reason, generated_at):
    return {
        "phase": "FASE 1.1 MT5 DEMO",
        "status": "PENDING",
        "generated_at_utc": generated_at,
        "reason": reason,
        "manual_instructions": [
            "Ejecutar desde Windows 11 con la terminal MT5 abierta e iniciada en una cuenta DEMO.",
            "Activar 'Disable algorithmic trading via external Python API' en las opciones de la terminal.",
            "Usar el entorno virtual del proyecto y ejecutar: python scripts/validate_phase1_mt5.py",
        ],
        "account_identifiers_stored": False,
        "real_execution_authorized": False,
        "order_send_calls": 0,
        "traceability": _traceability(),
    }


def validate(mt5=None, *, now=None):
    generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    if mt5 is None:
        try:
            import MetaTrader5 as mt5_module
            mt5 = mt5_module
        except Exception:
            return _pending_report("MetaTrader5 Python module is unavailable in this environment.", generated)

    try:
        initialized = bool(mt5.initialize())
    except Exception:
        initialized = False
    if not initialized:
        return _pending_report("MT5 terminal did not initialize; integration checks were not run.", generated)

    report = {
        "phase": "FASE 1.1 MT5 DEMO",
        "status": "FAIL",
        "generated_at_utc": generated,
        "checks": {},
        "account_identifiers_stored": False,
        "real_execution_authorized": False,
        "order_send_calls": 0,
        "traceability": _traceability(),
    }
    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None or terminal is None:
            report["status"] = "PENDING"
            report["reason"] = "Account or terminal information is unavailable."
            return report

        demo_constant = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        is_demo = demo_constant is not None and getattr(account, "trade_mode", None) == demo_constant
        report["checks"]["demo_account"] = _check(is_demo, "Connected account is not verifiably DEMO.")
        if not is_demo:
            report["reason"] = "Real or unverifiable account rejected before market-data checks."
            return report

        currency = str(getattr(account, "currency", "") or "").upper()
        balance = _number(getattr(account, "balance", None), positive=True)
        equity = _number(getattr(account, "equity", None), positive=True)
        report["account_snapshot"] = {
            "balance": balance,
            "equity": equity,
            "currency": currency or None,
        }
        report["checks"]["account_values"] = _check(
            bool(currency and balance and equity), "Balance, equity or account currency is unavailable."
        )
        report["checks"]["python_trading_disabled"] = _check(
            getattr(terminal, "tradeapi_disabled", None) is True,
            "Terminal does not confirm that external Python trading is disabled.",
        )

        from config.market_config import SYMBOLS
        from config.risk_config import (
            DAILY_LOSS_TIMEZONE,
            DAILY_RISK_RESERVATION_TTL_MINUTES,
            MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE,
            MAX_DAILY_LOSS_PERCENT,
            MAX_LOSS_PER_TRADE_EUR,
            MAX_PORTFOLIO_RISK_PERCENT,
            MAX_RISK_PER_TRADE_PERCENT,
            RISK_REFERENCE,
        )
        from src.mt5_runtime import get_orders_state, get_positions_state
        from build_runtime_state import _risk_snapshot
        from src.risk_engine import _risk_per_lot, calculate_position_size, evaluate_portfolio_limits
        from src.daily_risk_guard import evaluate_daily_risk

        raw_positions = mt5.positions_get()
        raw_orders = mt5.orders_get()
        if raw_positions is None or raw_orders is None:
            report["checks"]["positions_and_pending_orders"] = _check(False, "MT5 position/order read failed.")
            return report
        positions = get_positions_state()
        pending_orders = get_orders_state()
        report["checks"]["positions_and_pending_orders"] = _check(
            True,
            open_positions=len(positions),
            pending_orders=len(pending_orders),
        )

        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)
        midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(midnight_utc, now_utc)
        report["checks"]["daily_deals_read"] = _check(
            deals is not None,
            "Daily UTC deal history unavailable or incomplete.",
            window_start_utc=midnight_utc.isoformat(),
            window_end_utc=now_utc.isoformat(),
            deal_count=len(deals) if deals is not None else None,
        )
        if deals is None:
            return report

        account_state = {"currency": currency, "balance": balance, "equity": equity}
        snapshot = _risk_snapshot(account_state, positions, pending_orders)
        report["checks"]["daily_deal_classification"] = _check(
            snapshot.get("available") is True or "realized_net_pnl" in snapshot,
            snapshot.get("reason"),
            realized_net_pnl=snapshot.get("realized_net_pnl"),
            deal_counts=snapshot.get("daily_deal_counts"),
            daily_window_start_utc=snapshot.get("daily_window_start_utc"),
            daily_window_end_utc=snapshot.get("daily_window_end_utc"),
        )
        if snapshot.get("realized_net_pnl") is None:
            return report

        conversion_currencies = {currency, "EUR", "USD", "CAD", "AUD"}
        for symbol in SYMBOLS:
            info = mt5.symbol_info(symbol)
            if info is None:
                continue
            conversion_currencies.update({
                str(getattr(info, "currency_base", "") or "").upper(),
                str(getattr(info, "currency_profit", "") or "").upper(),
            })
        rates = _collect_rates(mt5, conversion_currencies, SYMBOLS)
        snapshot["conversion_rates"] = rates

        symbol_checks = {}
        symbol_metadata = dict(snapshot.get("symbol_metadata") or {})
        for symbol in ("EURUSD", "USDCAD", "AUDUSD"):
            info = mt5.symbol_info(symbol)
            tick = mt5.symbol_info_tick(symbol) if info is not None else None
            if info is None or tick is None:
                symbol_checks[symbol] = _check(False, "Symbol metadata or current quote unavailable.")
                continue
            fields = {
                "contract_size": _number(getattr(info, "trade_contract_size", None), positive=True),
                "tick_size": _number(getattr(info, "trade_tick_size", None), positive=True),
                "tick_value_loss": _number(getattr(info, "trade_tick_value_loss", None), positive=True),
                "volume_min": _number(getattr(info, "volume_min", None), positive=True),
                "volume_max": _number(getattr(info, "volume_max", None), positive=True),
                "volume_step": _number(getattr(info, "volume_step", None), positive=True),
            }
            metadata_ok = all(value is not None for value in fields.values()) and fields["volume_min"] <= fields["volume_max"]
            tick_bid = _number(getattr(tick, "bid", None), positive=True)
            tick_ask = _number(getattr(tick, "ask", None), positive=True)
            metadata = symbol_metadata.get(symbol, {})
            metadata["trade_tick_value_loss_currency"] = currency
            symbol_metadata[symbol] = metadata
            test_result = {"buy": None, "sell": None, "position_size": None, "portfolio": None}
            if metadata_ok and tick_bid and tick_ask and tick_ask >= tick_bid and equity and balance:
                tick_size = fields["tick_size"]
                distance = max(10 * tick_size, tick_size)
                test_volume = fields["volume_min"]
                for direction, action, price in (
                    ("BUY", getattr(mt5, "ORDER_TYPE_BUY", None), tick_ask),
                    ("SELL", getattr(mt5, "ORDER_TYPE_SELL", None), tick_bid),
                ):
                    if action is None:
                        continue
                    stop = price - distance if direction == "BUY" else price + distance
                    observed = mt5.order_calc_profit(action, symbol, test_volume, price, stop)
                    expected_per_lot, method = _risk_per_lot(
                        price, stop, metadata, currency, rates
                    )
                    expected = expected_per_lot * test_volume
                    actual = abs(float(observed)) if observed is not None else None
                    tolerance = max(0.01, expected * 0.05)
                    test_result[direction.lower()] = {
                        "status": "PASS" if actual is not None and abs(actual - expected) <= tolerance else "FAIL",
                        "order_calc_profit_loss": actual,
                        "risk_engine_loss": expected,
                        "tolerance": tolerance,
                        "valuation_method": method,
                    }

                entry = tick_ask
                stop = entry - distance
                size = calculate_position_size(
                    symbol=symbol,
                    direction="BUY",
                    entry=entry,
                    stop_loss=stop,
                    equity=equity,
                    balance=balance,
                    account_currency=currency,
                    symbol_metadata=metadata,
                    conversion_rates=rates,
                    max_risk_percent=MAX_RISK_PER_TRADE_PERCENT,
                    max_loss_absolute=MAX_LOSS_PER_TRADE_EUR,
                    max_loss_currency="EUR",
                    risk_reference=RISK_REFERENCE,
                )
                test_result["position_size"] = {
                    "status": size.get("status"),
                    "errors": size.get("errors", []),
                    "volume": size.get("volume"),
                    "risk_amount_account": size.get("risk_amount_account"),
                    "risk_budget_account": size.get("risk_budget_account"),
                }
                probe_per_lot, probe_method = _risk_per_lot(
                    entry, stop, metadata, currency, rates
                )
                candidate = {
                    "symbol": symbol,
                    "direction": "BUY",
                    "entry": entry,
                    "volume": test_volume,
                    "estimated_loss_account": probe_per_lot * test_volume,
                }
                test_result["portfolio"] = evaluate_portfolio_limits(
                    positions=positions,
                    pending_orders=pending_orders,
                    proposed_plan=candidate,
                    symbol_metadata=symbol_metadata,
                    conversion_rates=rates,
                    account_currency=currency,
                    equity=equity,
                    max_portfolio_risk_percent=MAX_PORTFOLIO_RISK_PERCENT,
                    max_currency_exposure_equity_multiple=MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE,
                )
                test_result["position_size"]["minimum_volume_probe_method"] = probe_method
            portfolio_errors = (test_result.get("portfolio") or {}).get("errors", [])
            portfolio_data_complete = test_result.get("portfolio") is not None and not any(
                "UNAVAILABLE" in error or "METADATA" in error
                for error in portfolio_errors
            )
            check_ok = (
                metadata_ok
                and bool(tick_bid and tick_ask and tick_ask >= tick_bid)
                and all(test_result[side] and test_result[side]["status"] == "PASS" for side in ("buy", "sell"))
                and bool(test_result["position_size"])
                and (
                    test_result["position_size"].get("status") == "VALID"
                    or test_result["position_size"].get("errors") == ["VOLUME_BELOW_MINIMUM_RISK_BUDGET"]
                )
                and portfolio_data_complete
            )
            symbol_checks[symbol] = _check(check_ok, "Symbol metadata, broker limits or risk comparison failed.", **fields, tests=test_result)

        report["checks"]["symbol_contract_ticks_volumes"] = {
            "status": "PASS" if all(item["status"] == "PASS" for item in symbol_checks.values()) and len(symbol_checks) == 3 else "FAIL",
            "symbols": symbol_checks,
            "conversion_rates_obtained": sorted(rates),
            "conversion_rate_count": len(rates),
        }

        if snapshot.get("available") is not True:
            report["checks"]["risk_snapshot"] = _check(False, snapshot.get("reason") or "Risk snapshot incomplete.")
            return report
        report["checks"]["risk_snapshot"] = _check(
            True,
            open_risk_account=snapshot.get("open_risk_account"),
            pending_risk_account=snapshot.get("pending_risk_account"),
            position_count=len(snapshot.get("positions", [])),
            pending_order_count=len(snapshot.get("pending_orders", [])),
        )

        with tempfile.TemporaryDirectory(prefix="agenttrading-mt5-check-") as temp:
            daily = evaluate_daily_risk(
                realized_net_pnl=snapshot["realized_net_pnl"],
                open_risk=snapshot["open_risk_account"],
                pending_risk=snapshot["pending_risk_account"],
                equity=equity,
                max_loss_percent=MAX_DAILY_LOSS_PERCENT,
                timezone_name=DAILY_LOSS_TIMEZONE,
                state_path=Path(temp) / "daily_guard.json",
                now=now_utc,
                reserve_proposal=False,
                reservation_ttl_minutes=DAILY_RISK_RESERVATION_TTL_MINUTES,
            )
        report["checks"]["daily_loss_limit_evaluation"] = _check(
            isinstance(daily.get("approved"), bool),
            "Daily risk guard could not evaluate a complete MT5 snapshot.",
            approved=daily.get("approved"),
            errors=daily.get("errors", []),
            daily_risk_used=daily.get("daily_risk_used"),
            daily_loss_limit=daily.get("daily_loss_limit"),
            locked=daily.get("locked"),
        )

        report["checks"]["execution_safety"] = {
            "status": "PASS",
            "order_send_called": False,
            "real_execution_authorized": False,
            "request_checks": "order_calc_profit only; hypothetical BUY/SELL price-to-stop probes",
        }
        report["status"] = "PASS" if all(
            check.get("status") == "PASS"
            for key, check in report["checks"].items()
            if key != "daily_loss_limit_evaluation"
        ) else "FAIL"
        return report
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = type(exc).__name__
        report["reason"] = "An MT5 validation check raised an exception; details are suppressed to avoid exposing sensitive terminal data."
        return report
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "reports" / "phase1_mt5_validation.json"))
    args = parser.parse_args(argv)
    report = validate()
    command = [sys.executable, "scripts/validate_phase1_mt5.py"]
    if args.output != str(ROOT / "reports" / "phase1_mt5_validation.json"):
        command += ["--output", args.output]
    report["command_executed"] = command
    _write(report, args.output)
    print(json.dumps({
        "report": args.output,
        "status": report["status"],
        "real_execution_authorized": False,
        "account_identifiers_stored": False,
    }, indent=2))
    return 0 if report["status"] == "PASS" else 2 if report["status"] == "PENDING" else 1


if __name__ == "__main__":
    raise SystemExit(main())
