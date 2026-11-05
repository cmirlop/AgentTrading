"""Safe classification and net PnL calculation for MT5 deal history."""

from __future__ import annotations

from datetime import datetime, timezone
import math


TRADE_TYPE_NAMES = ("DEAL_TYPE_BUY", "DEAL_TYPE_SELL")
COMMISSION_TYPE_NAMES = (
    "DEAL_TYPE_COMMISSION",
    "DEAL_TYPE_COMMISSION_DAILY",
    "DEAL_TYPE_COMMISSION_MONTHLY",
    "DEAL_TYPE_AGENT_DAILY",
    "DEAL_TYPE_AGENT_MONTHLY",
)
NON_TRADE_TYPE_NAMES = (
    "DEAL_TYPE_BALANCE",
    "DEAL_TYPE_CREDIT",
    "DEAL_TYPE_CHARGE",
    "DEAL_TYPE_CORRECTION",
    "DEAL_TYPE_BONUS",
    "DEAL_TYPE_INTEREST",
    "DEAL_TYPE_BUY_CANCELED",
    "DEAL_TYPE_SELL_CANCELED",
    "DEAL_TYPE_DIVIDEND",
    "DEAL_TYPE_DIVIDEND_FRANKED",
    "DEAL_TYPE_TAX",
)
FINANCIAL_FIELDS = ("profit", "commission", "swap", "fee")


def _constant_map(mt5_api):
    names = TRADE_TYPE_NAMES + COMMISSION_TYPE_NAMES + NON_TRADE_TYPE_NAMES
    return {
        getattr(mt5_api, name): name
        for name in names
        if hasattr(mt5_api, name)
    }


def _finite_field(deal, field):
    marker = object()
    value = getattr(deal, field, marker)
    if value is marker or value is None:
        raise ValueError("DAILY_DEAL_DATA_INCOMPLETE")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("DAILY_DEAL_DATA_INVALID") from None
    if not math.isfinite(result):
        raise ValueError("DAILY_DEAL_DATA_INVALID")
    return result


def _validate_time(deal, window_start_utc, window_end_utc):
    if window_start_utc is None or window_end_utc is None:
        return
    if window_start_utc.tzinfo is None or window_end_utc.tzinfo is None:
        raise ValueError("DAILY_DEAL_WINDOW_INVALID")
    raw = getattr(deal, "time", None)
    if raw is None:
        raw_msc = getattr(deal, "time_msc", None)
        if raw_msc is None:
            raise ValueError("DAILY_DEAL_TIMESTAMP_MISSING")
        raw = float(raw_msc) / 1000.0
    try:
        timestamp = datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError("DAILY_DEAL_TIMESTAMP_INVALID") from None
    start = window_start_utc.astimezone(timezone.utc)
    end = window_end_utc.astimezone(timezone.utc)
    if timestamp < start or timestamp > end:
        raise ValueError("DAILY_DEAL_OUTSIDE_UTC_WINDOW")


def calculate_realized_net_pnl(
    deals,
    mt5_api,
    *,
    window_start_utc=None,
    window_end_utc=None,
):
    """Return trading PnL after trade-linked commission, swap and fee.

    Balance, credit, charges, dividends, interest and other non-trading deals
    are explicitly excluded.  Commission-only deal types count only when the
    record links to an order or position. Unknown/malformed records invalidate
    the history rather than silently weakening the daily-loss guard.
    """
    if deals is None:
        return {"available": False, "reason": "DAILY_DEAL_HISTORY_UNAVAILABLE"}
    try:
        records = list(deals)
    except (TypeError, ValueError):
        return {"available": False, "reason": "DAILY_DEAL_HISTORY_INCOMPLETE"}

    types = _constant_map(mt5_api)
    if any(not hasattr(mt5_api, name) for name in TRADE_TYPE_NAMES):
        return {"available": False, "reason": "DAILY_DEAL_TYPE_CONSTANTS_UNAVAILABLE"}
    trade_types = {getattr(mt5_api, name) for name in TRADE_TYPE_NAMES}
    commission_types = {
        getattr(mt5_api, name) for name in COMMISSION_TYPE_NAMES if hasattr(mt5_api, name)
    }
    non_trade_types = {
        getattr(mt5_api, name) for name in NON_TRADE_TYPE_NAMES if hasattr(mt5_api, name)
    }
    total = 0.0
    trade_count = 0
    commission_count = 0
    excluded_count = 0
    seen_tickets = set()
    try:
        for deal in records:
            if deal is None or not hasattr(deal, "type"):
                raise ValueError("DAILY_DEAL_DATA_INCOMPLETE")
            raw_type = getattr(deal, "type")
            if raw_type not in types:
                raise ValueError("DAILY_DEAL_TYPE_UNKNOWN")
            _validate_time(deal, window_start_utc, window_end_utc)
            ticket = getattr(deal, "ticket", None)
            if ticket is not None:
                ticket_key = str(ticket)
                if ticket_key in seen_tickets:
                    raise ValueError("DAILY_DEAL_HISTORY_DUPLICATE")
                seen_tickets.add(ticket_key)

            if raw_type in non_trade_types:
                excluded_count += 1
                continue
            if raw_type not in trade_types and raw_type not in commission_types:
                raise ValueError("DAILY_DEAL_TYPE_UNKNOWN")

            if raw_type in commission_types:
                position_id = getattr(deal, "position_id", 0)
                order_id = getattr(deal, "order", 0)
                if not position_id and not order_id:
                    raise ValueError("DAILY_COMMISSION_UNLINKED")
                commission_count += 1
            else:
                trade_count += 1

            total += sum(_finite_field(deal, field) for field in FINANCIAL_FIELDS)
            if not math.isfinite(total):
                raise ValueError("DAILY_DEAL_DATA_INVALID")
    except ValueError as exc:
        reason = str(exc) if str(exc).startswith("DAILY_") else "DAILY_DEAL_DATA_INVALID"
        return {"available": False, "reason": reason}

    return {
        "available": True,
        "realized_net_pnl": round(total, 8),
        "trade_deal_count": trade_count,
        "commission_deal_count": commission_count,
        "excluded_non_trade_deal_count": excluded_count,
        "history_deal_count": len(records),
        "window_start_utc": window_start_utc.isoformat() if window_start_utc else None,
        "window_end_utc": window_end_utc.isoformat() if window_end_utc else None,
    }
