"""Fail-closed conversion of quoted-currency costs to EUR."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def _aware_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def convert_cost_to_eur(
    amount_quote: float,
    quote_to_eur_rate: float | None,
    *,
    rate_available_utc: datetime | str | None,
    decision_time_utc: datetime | str,
) -> dict[str, Any]:
    """Convert only with a positive, point-in-time EUR-per-quote FX rate."""
    try:
        amount = float(amount_quote)
    except (TypeError, ValueError, OverflowError):
        return {"status": "COST_CONVERSION_INVALID", "amount_eur": None}
    if not math.isfinite(amount) or amount < 0:
        return {"status": "COST_CONVERSION_INVALID", "amount_eur": None}
    available = _aware_utc(rate_available_utc)
    decision = _aware_utc(decision_time_utc)
    try:
        rate = float(quote_to_eur_rate) if quote_to_eur_rate is not None else None
    except (TypeError, ValueError, OverflowError):
        rate = None
    if decision is None or available is None or rate is None or not math.isfinite(rate) or rate <= 0:
        return {"status": "COST_CONVERSION_UNVERIFIED", "amount_eur": None}
    if available > decision:
        return {
            "status": "COST_CONVERSION_LOOKAHEAD",
            "amount_eur": None,
            "rate_available_utc": available.isoformat(),
            "decision_time_utc": decision.isoformat(),
        }
    return {
        "status": "PASS",
        "amount_eur": round(amount * rate, 8),
        "quote_to_eur_rate": rate,
        "rate_available_utc": available.isoformat(),
        "decision_time_utc": decision.isoformat(),
    }
