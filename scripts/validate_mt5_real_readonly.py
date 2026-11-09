"""Bounded, read-only MT5 validation for an explicitly authorized REAL account.

The module imports no productive execution workflow.  The only terminal API
methods used after connection are named in READ_ONLY_METHODS; the safety gate
is rechecked around every such call.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import risk_config  # noqa: E402
from src.mt5_deal_history import calculate_realized_net_pnl  # noqa: E402
from src.risk_engine import (  # noqa: E402
    _risk_per_lot,
    calculate_currency_exposure,
    calculate_position_size,
    evaluate_portfolio_limits,
)


SYMBOLS = ("EURUSD", "USDCAD", "AUDUSD")
TIMEFRAME_NAMES = ("M5", "M15", "H1", "H4", "D1")
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
READ_ONLY_METHODS = frozenset(
    {
        "account_info",
        "positions_get",
        "orders_get",
        "history_deals_get",
        "symbol_info",
        "symbol_info_tick",
        "copy_rates_from_pos",
        "order_calc_profit",
        "order_calc_margin",
    }
)


class SafetyGateError(RuntimeError):
    """Raised before or immediately after a read when read-only is unverified."""


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _finite(value: Any, *, positive: bool = False) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and (number > 0 if positive else True)


def _verify_terminal_gate(api: Any) -> dict[str, Any]:
    terminal = api.terminal_info()
    if terminal is None:
        raise SafetyGateError("TERMINAL_INFO_UNAVAILABLE")
    disabled = _attr(terminal, "tradeapi_disabled")
    connected = _attr(terminal, "connected")
    if disabled is not True:
        raise SafetyGateError("TRADEAPI_DISABLED_NOT_VERIFIED_TRUE")
    if connected is not True:
        raise SafetyGateError("TERMINAL_CONNECTION_LOST_OR_UNVERIFIED")
    return {"connected": True, "tradeapi_disabled": True}


def _read(api: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    if method not in READ_ONLY_METHODS:
        raise SafetyGateError("READ_METHOD_NOT_ALLOWLISTED")
    _verify_terminal_gate(api)
    result = getattr(api, method)(*args, **kwargs)
    _verify_terminal_gate(api)
    return result


def _enum_name(api: Any, value: Any, names: tuple[str, ...]) -> str | None:
    for name in names:
        if getattr(api, name, object()) == value:
            return name
    return None


def _public_projection(private_report: dict[str, Any]) -> dict[str, Any]:
    """Construct a public report from an explicit allowlist of safe fields."""
    account = private_report.get("account", {})
    symbols: dict[str, Any] = {}
    for symbol in SYMBOLS:
        item = private_report.get("symbols", {}).get(symbol, {})
        risk = private_report.get("risk", {}).get(symbol, {})
        history = private_report.get("history", {}).get(symbol, {})
        metadata = item.get("metadata", {})
        symbols[symbol] = {
            "status": item.get("status", "PENDING"),
            "metadata_valid": item.get("metadata_valid"),
            "spread_points": item.get("spread_points", metadata.get("spread_points")),
            "spread_quality": item.get("spread_quality", "UNKNOWN"),
            "risk_status": risk.get("status", "PENDING"),
            "history": {
                tf: {
                    "status": details.get("status", "PENDING"),
                    "latest_closed_utc": details.get("latest_closed_utc"),
                    "new_bars": details.get("new_bars", 0),
                    "data_quality": details.get("data_quality", "UNKNOWN"),
                }
                for tf, details in history.items()
            },
        }
    return {
        "schema_version": 1,
        "generated_utc": private_report.get("generated_utc"),
        "status": private_report.get("status", "MT5_REAL_READ_ONLY_PENDING"),
        "account_type": account.get("type", "UNKNOWN"),
        "connection": account.get("connection", "PENDING"),
        "read_only_safety": {
            "status": account.get("read_only_safety", "PENDING"),
            "tradeapi_disabled": account.get("tradeapi_disabled"),
        },
        "broker_data": private_report.get("broker_data_status", "PENDING"),
        "risk_engine": private_report.get("risk_status", "PENDING"),
        "daily_loss_control": private_report.get("daily_loss_status", "PENDING"),
        "symbols": symbols,
        "open_position_count": private_report.get("open_position_count"),
        "pending_order_count": private_report.get("pending_order_count"),
        "daily_history_status": private_report.get("daily_history_status", "PENDING"),
        "history_root": private_report.get("history_root"),
        "errors": list(private_report.get("safe_errors", [])),
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
        "demo_validation": "PENDING_NOT_APPLICABLE_NO_DEMO_ACCOUNT",
    }


def _portfolio_decision(errors: list[str]) -> tuple[str, str]:
    """A valid risk-cap rejection is a successful enforcement check."""
    if not errors:
        return "PASS", "ALLOW"
    incomplete = {
        "EXISTING_POSITION_RISK_UNAVAILABLE",
        "PROPOSED_RISK_UNAVAILABLE",
        "CURRENCY_EXPOSURE_UNAVAILABLE",
        "ACCOUNT_EQUITY_UNAVAILABLE",
    }
    if set(errors) & incomplete:
        return "PENDING", "INSUFFICIENT_DATA"
    limit_errors = {"MAX_PORTFOLIO_RISK_EXCEEDED"}
    if all(error in limit_errors or error.startswith("CURRENCY_EXPOSURE_LIMIT_EXCEEDED_") for error in errors):
        return "PASS", "BLOCKED_BY_CONFIGURED_LIMIT"
    return "FAIL", "EVALUATION_ERROR"


def _refresh_report_classification(report: dict[str, Any]) -> dict[str, Any]:
    for item in report.get("portfolio_limits", {}).values():
        if item.get("reason"):
            item["status"], item["decision"] = "PENDING", "INSUFFICIENT_DATA"
            continue
        errors = list(item.get("errors", []))
        item["status"], item["decision"] = _portfolio_decision(errors)
        item["engine_approved"] = not errors
    risk_items = list(report.get("risk", {}).values())
    limit_items = list(report.get("portfolio_limits", {}).values())
    report["risk_status"] = (
        "PASS"
        if len(risk_items) == len(SYMBOLS)
        and all(item.get("status") == "PASS" for item in risk_items)
        and report.get("portfolio_exposure", {}).get("status") == "PASS"
        and len(limit_items) == len(SYMBOLS)
        and all(item.get("status") == "PASS" for item in limit_items)
        else "PARTIAL"
    )
    if report.get("status") != "MT5_REAL_READ_ONLY_FAIL":
        report["status"] = (
            "MT5_REAL_READ_ONLY_PASS"
            if report.get("broker_data_status") == "PASS"
            and report.get("risk_status") == "PASS"
            and report.get("daily_loss_status") == "PASS"
            and report.get("history_status") == "PASS"
            else "MT5_REAL_READ_ONLY_PENDING"
        )
    return report


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json_preserving(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        archive = path.parent / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = archive / f"{path.stem}-{stamp}{path.suffix}"
        if not archived.exists():
            shutil.copy2(path, archived)
        else:
            archived = archive / f"{path.stem}-{stamp}-{os.getpid()}{path.suffix}"
            shutil.copy2(path, archived)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _field_record(record: Any) -> dict[str, Any]:
    if hasattr(record, "_asdict"):
        return record._asdict()
    if isinstance(record, dict):
        return record
    names = getattr(getattr(record, "dtype", None), "names", None)
    if names:
        result = {}
        for field in names:
            value = record[field]
            result[field] = value.item() if hasattr(value, "item") else value
        return result
    fields = getattr(record, "_fields", ())
    return {field: getattr(record, field) for field in fields}


def _local_latest(path: Path) -> datetime | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = csv.DictReader(handle)
            latest = None
            for row in rows:
                raw = row.get("time")
                if not raw:
                    continue
                candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if candidate.tzinfo is None:
                    raise ValueError("LOCAL_HISTORY_TIMEZONE_MISSING")
                candidate = candidate.astimezone(timezone.utc)
                latest = candidate if latest is None or candidate > latest else latest
            return latest
    except (OSError, ValueError, csv.Error):
        return None


def _rate_epoch(row: Any) -> int:
    return int(_attr(row, "time"))


def _serialize_rates(
    api: Any,
    symbol: str,
    timeframe_name: str,
    timeframe: Any,
    output_dir: Path,
    now: datetime,
) -> dict[str, Any]:
    seconds = TIMEFRAME_SECONDS[timeframe_name]
    local_path = ROOT / "data" / "market" / symbol / f"{timeframe_name}.csv"
    local_latest = _local_latest(local_path)
    now_epoch = int(now.timestamp())
    if local_latest is None:
        count = 1000
    else:
        estimated = math.ceil(max(0, now_epoch - int(local_latest.timestamp())) / seconds) + 4
        count = max(2, min(1000, estimated))
    raw = _read(api, "copy_rates_from_pos", symbol, timeframe, 0, count)
    if raw is None:
        return {
            "status": "PENDING",
            "reason": "HISTORY_UNAVAILABLE",
            "local_latest_utc": local_latest.isoformat() if local_latest else None,
            "new_bars": 0,
            "data_quality": "UNKNOWN",
        }

    records = [_field_record(row) for row in raw]
    records.sort(key=lambda row: int(row["time"]))
    duplicate_count = len(records) - len({_rate_epoch(row) for row in records})
    closed: list[dict[str, Any]] = []
    invalid_ohlc = 0
    invalid_values = 0
    for row in records:
        epoch = int(row["time"])
        if epoch + seconds > now_epoch:
            continue
        try:
            o, h, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
            spread = int(row.get("spread", 0))
            tick_volume = float(row.get("tick_volume", 0))
            real_volume = float(row.get("real_volume", 0))
            valid = all(math.isfinite(x) and x > 0 for x in (o, h, low, close))
            valid = valid and low <= min(o, close) and h >= max(o, close) and low <= h
            if not valid:
                invalid_ohlc += 1
                continue
            if spread < 0 or tick_volume < 0 or real_volume < 0:
                invalid_values += 1
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid_values += 1
            continue
        closed.append(row)

    timestamps = [int(row["time"]) for row in closed]
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:]) if b - a > seconds * 1.5]
    latest_closed = datetime.fromtimestamp(timestamps[-1], timezone.utc) if timestamps else None
    local_epoch = int(local_latest.timestamp()) if local_latest else -1
    new_rows = [row for row in closed if int(row["time"]) > local_epoch]
    valid = bool(closed) and duplicate_count == 0 and invalid_ohlc == 0 and invalid_values == 0
    data_quality = "PASS" if valid else "FAIL"
    file_name = None
    digest = None
    if new_rows and valid:
        destination = output_dir / f"{symbol}_{timeframe_name}.csv"
        fields = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
        with destination.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in new_rows:
                record = {key: row.get(key) for key in fields}
                record["time"] = datetime.fromtimestamp(int(row["time"]), timezone.utc).isoformat()
                writer.writerow(record)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        file_name = str(destination.relative_to(ROOT)).replace("\\", "/")
    positive_spreads = [int(row.get("spread", 0)) for row in closed if int(row.get("spread", 0)) > 0]
    return {
        "status": "PASS" if valid else "FAIL",
        "local_latest_utc": local_latest.isoformat() if local_latest else None,
        "latest_closed_utc": latest_closed.isoformat() if latest_closed else None,
        "returned_rates": len(records),
        "closed_bars": len(closed),
        "new_bars": len(new_rows),
        "duplicate_count": duplicate_count,
        "invalid_ohlc_count": invalid_ohlc,
        "invalid_values_count": invalid_values,
        "market_gap_count": len(gaps),
        "largest_gap_seconds": max(gaps) if gaps else None,
        "spread_zero_count": sum(int(row.get("spread", 0)) == 0 for row in closed),
        "spread_positive_min_points": min(positive_spreads) if positive_spreads else None,
        "spread_positive_median_points": sorted(positive_spreads)[len(positive_spreads) // 2] if positive_spreads else None,
        "spread_positive_max_points": max(positive_spreads) if positive_spreads else None,
        "tick_volume_zero_count": sum(float(row.get("tick_volume", 0)) == 0 for row in closed),
        "real_volume_zero_count": sum(float(row.get("real_volume", 0)) == 0 for row in closed),
        "data_quality": data_quality,
        "file": file_name,
        "sha256": digest,
    }


def _risk_check(api: Any, account: Any, account_currency: str, symbol: str, meta: dict[str, Any], tick: Any, rates: dict[str, float]) -> dict[str, Any]:
    tick_size = float(meta["trade_tick_size"])
    scenarios: dict[str, Any] = {}
    engine_metadata = dict(meta)
    engine_metadata["trade_tick_value_loss_currency"] = account_currency
    engine_metadata["account_currency"] = account_currency
    equity = float(_attr(account, "equity"))
    balance = float(_attr(account, "balance"))
    order_types = {"BUY": api.ORDER_TYPE_BUY, "SELL": api.ORDER_TYPE_SELL}
    for direction in ("BUY", "SELL"):
        entry = float(_attr(tick, "ask" if direction == "BUY" else "bid"))
        stop = entry - 10 * tick_size if direction == "BUY" else entry + 10 * tick_size
        broker_profit = _read(api, "order_calc_profit", order_types[direction], symbol, 1.0, entry, stop)
        engine_risk, valuation_method = _risk_per_lot(entry, stop, engine_metadata, account_currency, rates)
        if broker_profit is None or not _finite(broker_profit):
            comparison = "PENDING"
            difference_percent = None
        else:
            broker_risk = abs(float(broker_profit))
            difference_percent = abs(broker_risk - engine_risk) / max(broker_risk, engine_risk, 1e-12) * 100
            comparison = "PASS" if abs(broker_risk - engine_risk) <= max(0.02, broker_risk * 0.02) else "FAIL"
        sizing = calculate_position_size(
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop,
            equity=equity,
            balance=balance,
            account_currency=account_currency,
            symbol_metadata=engine_metadata,
            conversion_rates=rates,
            max_risk_percent=risk_config.MAX_RISK_PER_TRADE_PERCENT,
            max_loss_absolute=risk_config.MAX_LOSS_PER_TRADE_EUR,
            max_loss_currency="EUR",
            risk_reference=risk_config.RISK_REFERENCE,
        )
        sizing = {key: value for key, value in sizing.items() if key not in {"risk_reference_amount"}}
        scenarios[direction] = {
            "entry_reference": entry,
            "hypothetical_stop": stop,
            "broker_order_calc_profit_1_lot": float(broker_profit) if _finite(broker_profit) else None,
            "engine_risk_per_lot_account": round(engine_risk, 8),
            "engine_valuation_method": valuation_method,
            "broker_comparison": comparison,
            "difference_percent": round(difference_percent, 6) if difference_percent is not None else None,
            "position_sizing": sizing,
        }
    statuses = {item["broker_comparison"] for item in scenarios.values()}
    statuses.update("FAIL" if item["position_sizing"].get("status") == "REJECTED" and item["position_sizing"].get("errors") != ["VOLUME_BELOW_MINIMUM_RISK_BUDGET"] else "PASS" for item in scenarios.values())
    overall = "FAIL" if "FAIL" in statuses else "PENDING" if "PENDING" in statuses else "PASS"
    return {"status": overall, "scenarios": scenarios}


def _classify_existing(api: Any, positions: Any, orders: Any, metadata: dict[str, dict[str, Any]], rates: dict[str, float], account_currency: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    risks: list[dict[str, Any]] = []
    exposure_positions: list[dict[str, Any]] = []
    exposure_orders: list[dict[str, Any]] = []
    errors: list[str] = []
    for record in positions:
        symbol = str(_attr(record, "symbol", ""))
        side_value = _attr(record, "type")
        side = "BUY" if side_value == getattr(api, "POSITION_TYPE_BUY", object()) else "SELL" if side_value == getattr(api, "POSITION_TYPE_SELL", object()) else None
        entry = _attr(record, "price_open")
        stop = _attr(record, "sl")
        volume = _attr(record, "volume")
        current = _attr(record, "price_current") or entry
        trade = {"symbol": symbol, "side": side, "volume": volume, "price_current": current}
        meta = metadata.get(symbol)
        if meta and side and _finite(entry, positive=True) and _finite(stop, positive=True) and _finite(volume, positive=True):
            order_type = api.ORDER_TYPE_BUY if side == "BUY" else api.ORDER_TYPE_SELL
            calculated = _read(api, "order_calc_profit", order_type, symbol, float(volume), float(entry), float(stop))
            if calculated is not None and _finite(calculated):
                trade["risk_to_sl_account"] = max(0.0, -float(calculated))
                risks.append({"risk_to_sl_account": trade["risk_to_sl_account"], "source": "position_sl"})
            else:
                errors.append("POSITION_SL_RISK_UNAVAILABLE")
        else:
            errors.append("POSITION_SL_OR_METADATA_UNAVAILABLE")
        exposure_positions.append(trade)

    pending_types = (
        ("ORDER_TYPE_BUY", "BUY"), ("ORDER_TYPE_BUY_LIMIT", "BUY"), ("ORDER_TYPE_BUY_STOP", "BUY"),
        ("ORDER_TYPE_BUY_STOP_LIMIT", "BUY"), ("ORDER_TYPE_SELL", "SELL"), ("ORDER_TYPE_SELL_LIMIT", "SELL"),
        ("ORDER_TYPE_SELL_STOP", "SELL"), ("ORDER_TYPE_SELL_STOP_LIMIT", "SELL"),
    )
    for record in orders:
        symbol = str(_attr(record, "symbol", ""))
        side = _enum_name(api, _attr(record, "type"), tuple(name for name, _ in pending_types))
        side = next((direction for name, direction in pending_types if name == side), None)
        volume = _attr(record, "volume_current") or _attr(record, "volume_initial")
        entry = _attr(record, "price_open")
        stop = _attr(record, "sl")
        trade = {"symbol": symbol, "side": side, "volume": volume, "entry": entry}
        meta = metadata.get(symbol)
        if meta and side and _finite(entry, positive=True) and _finite(stop, positive=True) and _finite(volume, positive=True):
            order_type = api.ORDER_TYPE_BUY if side == "BUY" else api.ORDER_TYPE_SELL
            calculated = _read(api, "order_calc_profit", order_type, symbol, float(volume), float(entry), float(stop))
            if calculated is not None and _finite(calculated):
                trade["risk_to_sl_account"] = max(0.0, -float(calculated))
                risks.append({"risk_to_sl_account": trade["risk_to_sl_account"], "source": "pending_order_sl"})
            else:
                errors.append("PENDING_ORDER_SL_RISK_UNAVAILABLE")
        else:
            errors.append("PENDING_ORDER_SL_OR_METADATA_UNAVAILABLE")
        exposure_orders.append(trade)
    return exposure_positions, exposure_orders, errors


def run_validation(api: Any, *, refresh_history: bool = True) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_utc": now.isoformat(),
        "status": "MT5_REAL_READ_ONLY_PENDING",
        "account": {"type": "UNKNOWN", "connection": "PENDING", "read_only_safety": "PENDING", "tradeapi_disabled": None},
        "symbols": {},
        "risk": {},
        "history": {},
        "safe_errors": [],
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
    }
    initialized = False
    try:
        initialized = bool(api.initialize())
        if not initialized:
            report["safe_errors"].append("MT5_INITIALIZE_FAILED")
            return report
        terminal = api.terminal_info()
        if terminal is None:
            report["safe_errors"].append("TERMINAL_INFO_UNAVAILABLE")
            return report
        report["account"]["tradeapi_disabled"] = _attr(terminal, "tradeapi_disabled") is True
        if report["account"]["tradeapi_disabled"] is not True:
            report["account"]["read_only_safety"] = "FAIL"
            report["safe_errors"].append("TRADEAPI_DISABLED_NOT_VERIFIED_TRUE")
            report["status"] = "MT5_REAL_READ_ONLY_FAIL"
            return report
        if _attr(terminal, "connected") is not True:
            report["safe_errors"].append("TERMINAL_CONNECTION_LOST_OR_UNVERIFIED")
            return report
        report["account"]["read_only_safety"] = "PASS"
        account = _read(api, "account_info")
        if account is None:
            report["safe_errors"].append("ACCOUNT_INFO_UNAVAILABLE")
            return report
        real_mode = getattr(api, "ACCOUNT_TRADE_MODE_REAL", None)
        if real_mode is None or _attr(account, "trade_mode") != real_mode:
            report["account"]["type"] = "NON_REAL_OR_UNVERIFIED"
            report["account"]["connection"] = "CONNECTED"
            report["safe_errors"].append("ACCOUNT_TRADE_MODE_IS_NOT_VERIFIED_REAL")
            report["status"] = "MT5_REAL_READ_ONLY_FAIL"
            return report
        report["account"]["type"] = "REAL"
        report["account"]["connection"] = "PASS"
        account_currency = str(_attr(account, "currency", "")).upper()
        if len(account_currency) != 3:
            report["safe_errors"].append("ACCOUNT_CURRENCY_UNAVAILABLE")
            return report

        metadata: dict[str, dict[str, Any]] = {}
        ticks: dict[str, Any] = {}
        rates: dict[str, float] = {}
        for symbol in SYMBOLS:
            info = _read(api, "symbol_info", symbol)
            tick = _read(api, "symbol_info_tick", symbol)
            ticks[symbol] = tick
            if info is None or tick is None:
                report["symbols"][symbol] = {"status": "PENDING", "metadata_valid": False, "spread_quality": "UNKNOWN", "reason": "SYMBOL_INFO_OR_TICK_UNAVAILABLE"}
                continue
            bid, ask = _attr(tick, "bid"), _attr(tick, "ask")
            point = _attr(info, "point")
            tick_size = _attr(info, "trade_tick_size")
            values = {
                "point": point,
                "trade_tick_size": tick_size,
                "trade_tick_value": _attr(info, "trade_tick_value"),
                "trade_tick_value_loss": _attr(info, "trade_tick_value_loss"),
                "trade_contract_size": _attr(info, "trade_contract_size"),
                "volume_min": _attr(info, "volume_min"),
                "volume_max": _attr(info, "volume_max"),
                "volume_step": _attr(info, "volume_step"),
                "currency_base": _attr(info, "currency_base"),
                "currency_profit": _attr(info, "currency_profit"),
                "currency_margin": _attr(info, "currency_margin"),
                "digits": _attr(info, "digits"),
            }
            valid = (
                all(_finite(values[key], positive=True) for key in ("point", "trade_tick_size", "trade_tick_value_loss", "trade_contract_size", "volume_min", "volume_max", "volume_step"))
                and _finite(bid, positive=True)
                and _finite(ask, positive=True)
                and float(ask) >= float(bid)
                and len(str(values["currency_base"] or "")) == 3
                and len(str(values["currency_profit"] or "")) == 3
                and float(values["volume_min"]) <= float(values["volume_max"])
            )
            spread_points = (float(ask) - float(bid)) / float(point) if valid else None
            spread_limit = risk_config.MAX_SPREAD_POINTS_BY_SYMBOL.get(symbol)
            spread_quality = "PASS" if valid and spread_points is not None and spread_points <= spread_limit else "WIDE_OR_INVALID"
            public_meta = {
                **values,
                "bid": bid,
                "ask": ask,
                "spread_price": float(ask) - float(bid) if valid else None,
                "spread_points": round(spread_points, 4) if spread_points is not None else None,
                "spread_limit_points": spread_limit,
                "metadata_valid": valid,
            }
            report["symbols"][symbol] = {
                "status": "PASS" if valid else "FAIL",
                "metadata_valid": valid,
                "metadata": public_meta,
                "spread_quality": spread_quality,
            }
            if valid:
                metadata[symbol] = {**values, "trade_tick_value_loss_currency": account_currency, "account_currency": account_currency}
                rates[symbol] = (float(bid) + float(ask)) / 2.0

        # Read-only account state and today's UTC history; identifiers are never stored.
        positions = _read(api, "positions_get")
        orders = _read(api, "orders_get")
        state_available = positions is not None and orders is not None
        if positions is None or orders is None:
            report["safe_errors"].append("POSITIONS_OR_ORDERS_UNAVAILABLE")
            positions = positions or ()
            orders = orders or ()
        report["open_position_count"] = len(positions)
        report["pending_order_count"] = len(orders)
        position_rows, order_rows, exposure_errors = _classify_existing(api, positions or (), orders or (), metadata, rates, account_currency)
        report["portfolio_exposure"] = {"status": "PENDING", "errors": exposure_errors}
        if state_available:
            try:
                exposure = calculate_currency_exposure(position_rows + order_rows, metadata, rates, account_currency)
                equity = float(_attr(account, "equity"))
                limit = equity * risk_config.MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE
                exceeded = [currency for currency, amount in exposure["gross_account"].items() if amount > limit]
                report["portfolio_exposure"] = {
                    "status": "FAIL" if exceeded else "PASS",
                    "currency_exposure": exposure,
                    "limit_exceeded_currencies": exceeded,
                    "position_count": len(position_rows),
                    "pending_order_count": len(order_rows),
                    "risk_to_sl_errors": exposure_errors,
                }
            except (ValueError, TypeError, ZeroDivisionError):
                report["portfolio_exposure"]["errors"] = list(dict.fromkeys(exposure_errors + ["CURRENCY_EXPOSURE_UNAVAILABLE"]))

        for symbol, meta in metadata.items():
            tick = ticks[symbol]
            try:
                report["risk"][symbol] = _risk_check(api, account, account_currency, symbol, meta, tick, rates)
            except SafetyGateError:
                raise
            except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                report["risk"][symbol] = {"status": "PENDING", "reason": "RISK_INPUT_OR_CONVERSION_UNAVAILABLE"}

        report["portfolio_limits"] = {}
        if state_available:
            for symbol, risk_item in report["risk"].items():
                sizing = risk_item.get("scenarios", {}).get("BUY", {}).get("position_sizing", {})
                if sizing.get("status") != "VALID":
                    report["portfolio_limits"][symbol] = {"status": "PENDING", "reason": "HYPOTHETICAL_SIZE_UNAVAILABLE"}
                    continue
                plan = {
                    "symbol": symbol,
                    "direction": "BUY",
                    "volume": sizing["volume"],
                    "entry": risk_item["scenarios"]["BUY"]["entry_reference"],
                    "estimated_loss_account": sizing["risk_amount_account"],
                }
                evaluated = evaluate_portfolio_limits(
                    positions=position_rows,
                    pending_orders=order_rows,
                    proposed_plan=plan,
                    symbol_metadata=metadata,
                    conversion_rates=rates,
                    account_currency=account_currency,
                    equity=float(_attr(account, "equity")),
                    max_portfolio_risk_percent=risk_config.MAX_PORTFOLIO_RISK_PERCENT,
                    max_currency_exposure_equity_multiple=risk_config.MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE,
                )
                status, decision = _portfolio_decision(evaluated["errors"])
                report["portfolio_limits"][symbol] = {
                    "status": status,
                    "decision": decision,
                    "engine_approved": evaluated["approved"],
                    "errors": evaluated["errors"],
                }

        day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
        deals = _read(api, "history_deals_get", day_start, now)
        daily = calculate_realized_net_pnl(deals, api, window_start_utc=day_start, window_end_utc=now)
        if daily.get("available") and _finite(_attr(account, "equity"), positive=True):
            daily_limit = float(_attr(account, "equity")) * risk_config.MAX_DAILY_LOSS_PERCENT / 100
            blocked = float(daily["realized_net_pnl"]) <= -daily_limit
            report["daily_loss_status"] = "PASS"
            report["daily_loss_evaluation"] = {
                "status": "LIMIT_REACHED_BLOCKING" if blocked else "BELOW_LIMIT_ALLOWING_PROPOSALS",
                "realized_net_pnl_account": daily["realized_net_pnl"],
                "daily_loss_limit_percent": risk_config.MAX_DAILY_LOSS_PERCENT,
                "trade_deal_count": daily["trade_deal_count"],
                "commission_deal_count": daily["commission_deal_count"],
                "excluded_non_trade_deal_count": daily["excluded_non_trade_deal_count"],
                "history_deal_count": daily["history_deal_count"],
            }
        else:
            report["daily_loss_status"] = "PENDING"
            report["daily_loss_evaluation"] = {"status": "PENDING", "reason": daily.get("reason", "DAILY_EQUITY_UNAVAILABLE")}
        report["daily_history_status"] = "PASS" if daily.get("available") else "PENDING"

        if refresh_history:
            data_dir = ROOT / "data" / "market_history" / "mt5_real_readonly" / stamp
            data_dir.mkdir(parents=True, exist_ok=False)
            report["history_root"] = str(data_dir.relative_to(ROOT)).replace("\\", "/")
            timeframes = {name: getattr(api, f"TIMEFRAME_{name}", None) for name in TIMEFRAME_NAMES}
            history_manifest: dict[str, Any] = {"source": "MetaTrader5 copy_rates_from_pos", "generated_utc": now.isoformat(), "symbols": {}}
            for symbol in SYMBOLS:
                report["history"][symbol] = {}
                history_manifest["symbols"][symbol] = {}
                for name, timeframe in timeframes.items():
                    if timeframe is None:
                        result = {"status": "PENDING", "reason": "TIMEFRAME_CONSTANT_UNAVAILABLE", "new_bars": 0, "data_quality": "UNKNOWN"}
                    else:
                        result = _serialize_rates(api, symbol, name, timeframe, data_dir, now)
                    report["history"][symbol][name] = result
                    history_manifest["symbols"][symbol][name] = {
                        key: result.get(key)
                        for key in ("latest_closed_utc", "new_bars", "data_quality", "file", "sha256", "market_gap_count")
                    }
            manifest_path = data_dir / "manifest.json"
            _write_json_preserving(manifest_path, history_manifest)
            report["history_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

        statuses = [item.get("status", "PENDING") for item in report["symbols"].values()]
        risk_statuses = [item.get("status", "PENDING") for item in report["risk"].values()]
        history_statuses = [item.get("status", "PENDING") for values in report.get("history", {}).values() for item in values.values()]
        report["broker_data_status"] = "PASS" if len(statuses) == len(SYMBOLS) and all(x == "PASS" for x in statuses) else "PARTIAL"
        report["history_status"] = "PASS" if len(history_statuses) == len(SYMBOLS) * len(TIMEFRAME_NAMES) and all(x == "PASS" for x in history_statuses) else "PARTIAL"
        _refresh_report_classification(report)
    except SafetyGateError as exc:
        report["account"]["read_only_safety"] = "FAIL"
        report["safe_errors"].append(str(exc))
        report["status"] = "MT5_REAL_READ_ONLY_FAIL"
    except Exception as exc:  # Keep exception messages and API payloads out of reports.
        report["safe_errors"].append(f"UNEXPECTED_{type(exc).__name__.upper()}")
        if report["account"].get("read_only_safety") == "PASS":
            report["status"] = "MT5_REAL_READ_ONLY_PENDING"
    finally:
        if initialized:
            try:
                api.shutdown()
            except Exception:
                report["safe_errors"].append("MT5_SHUTDOWN_FAILED")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-history-refresh", action="store_true", help="Only validate connection and risk; do not fetch price bars")
    parser.add_argument("--reproject-existing", action="store_true", help="Rebuild sanitized summaries from the existing private report without connecting to MT5")
    args = parser.parse_args(argv)
    private_path = ROOT / "reports" / "private" / "mt5_real_readonly_validation.json"
    summary_path = ROOT / "reports" / "mt5_real_readonly_summary.json"
    history_path = ROOT / "reports" / "mt5_history_refresh_summary.json"
    if args.reproject_existing:
        if not private_path.is_file():
            parser.error("No existe el informe privado que se quiere reproyectar")
        report = json.loads(private_path.read_text(encoding="utf-8"))
        _refresh_report_classification(report)
        report["reclassification"] = {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "reason": "Risk-limit blocks are valid outcomes when calculation and source data are complete.",
            "mt5_reconnected": False,
        }
    else:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            now = datetime.now(timezone.utc).isoformat()
            report = {
                "schema_version": 1,
                "generated_utc": now,
                "status": "MT5_REAL_READ_ONLY_PENDING",
                "account": {"type": "UNKNOWN", "connection": "PENDING", "read_only_safety": "PENDING", "tradeapi_disabled": None},
                "safe_errors": ["METATRADER5_PYTHON_MODULE_UNAVAILABLE"],
                "orders_sent_by_process": 0,
                "trades_executed_by_process": 0,
            }
        else:
            report = run_validation(mt5, refresh_history=not args.no_history_refresh)
    _write_json_preserving(private_path, report)
    public = _public_projection(report)
    _write_json_preserving(summary_path, public)
    history_summary = {
        "generated_utc": report.get("generated_utc"),
        "status": report.get("history_status", "NOT_RUN"),
        "history_root": report.get("history_root"),
        "symbols": {
            symbol: {
                tf: {key: values.get(key) for key in ("status", "local_latest_utc", "latest_closed_utc", "new_bars", "data_quality", "market_gap_count", "file", "sha256")}
                for tf, values in timeframes.items()
            }
            for symbol, timeframes in report.get("history", {}).items()
        },
        "errors": list(report.get("safe_errors", [])),
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
    }
    _write_json_preserving(history_path, history_summary)
    print(json.dumps(public, indent=2, sort_keys=True))
    return 0 if report.get("status") == "MT5_REAL_READ_ONLY_PASS" else 2 if report.get("status") == "MT5_REAL_READ_ONLY_FAIL" else 3


if __name__ == "__main__":
    raise SystemExit(main())
