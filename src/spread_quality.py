"""Conservative labels and diagnostics for historical MT5 spread fields.

The project CSV stores the raw ``copy_rates`` spread column. This module never
rewrites source bars and keeps sensitivity floors explicitly marked as model
assumptions rather than observed broker quotes.
"""

from __future__ import annotations

from math import isfinite
from datetime import datetime, timezone
from typing import Any

import pandas as pd


SPREAD_QUALITY_STATES = {
    "OBSERVED_VALID",
    "OBSERVED_ZERO_UNVERIFIED",
    "MISSING",
    "IMPUTED_ASSUMPTION",
    "INVALID",
}

# Predeclared sensitivity values in MT5 points, not broker-specific prices.
RESEARCH_SPREAD_FLOORS_POINTS = (5.0, 10.0, 20.0)
UTC_SESSION_BINS = (
    (0, 7, "ASIA"),
    (7, 13, "LONDON"),
    (13, 16, "LONDON_NEW_YORK_OVERLAP"),
    (16, 21, "NEW_YORK"),
    (21, 24, "ROLLOVER_OFF_HOURS"),
)


def classify_spread(value: Any) -> str:
    if value is None or value is pd.NA:
        return "MISSING"
    if isinstance(value, str) and not value.strip():
        return "MISSING"
    try:
        if pd.isna(value):
            return "MISSING"
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return "INVALID"
    if not isfinite(numeric) or numeric < 0:
        return "INVALID"
    if numeric == 0:
        return "OBSERVED_ZERO_UNVERIFIED"
    return "OBSERVED_VALID"


def effective_spread_points(raw_value: Any, *, multiplier: float = 1.0, floor_points: float = 0.0) -> tuple[float, str, bool]:
    """Return model spread, source quality, and whether an assumption applied."""
    raw_state = classify_spread(raw_value)
    if raw_state in {"MISSING", "INVALID"}:
        raise ValueError(f"HISTORICAL_SPREAD_{raw_state}")
    try:
        multiplier = float(multiplier)
        floor_points = float(floor_points)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("SPREAD_ASSUMPTION_INVALID") from None
    if not isfinite(multiplier) or multiplier <= 0 or not isfinite(floor_points) or floor_points < 0:
        raise ValueError("SPREAD_ASSUMPTION_INVALID")
    observed = float(raw_value) * multiplier
    assumed = observed < floor_points
    return max(observed, floor_points), ("IMPUTED_ASSUMPTION" if assumed else raw_state), assumed


def utc_session(hour: int) -> str:
    for start, end, label in UTC_SESSION_BINS:
        if start <= int(hour) < end:
            return label
    raise ValueError("UTC_HOUR_INVALID")


def audit_spread_values(values: pd.Series) -> dict[str, Any]:
    numeric = pd.to_numeric(values, errors="coerce")
    states = pd.Series([classify_spread(value) for value in values], index=values.index)
    observed = numeric[states.eq("OBSERVED_VALID")]
    total = int(len(values))
    counts = states.value_counts().to_dict()
    return {
        "records": total,
        "quality_counts": {state: int(counts.get(state, 0)) for state in sorted(SPREAD_QUALITY_STATES)},
        "zero_fraction_all_rows": round(float(states.eq("OBSERVED_ZERO_UNVERIFIED").mean()), 8) if total else None,
        "missing_fraction_all_rows": round(float(states.eq("MISSING").mean()), 8) if total else None,
        "invalid_fraction_all_rows": round(float(states.eq("INVALID").mean()), 8) if total else None,
        "positive_observed_fraction_all_rows": round(float(states.eq("OBSERVED_VALID").mean()), 8) if total else None,
        "median_positive_points": float(observed.median()) if len(observed) else None,
        "p50_all_numeric_points": float(numeric.median()) if numeric.notna().any() else None,
        "p95_all_numeric_points": float(numeric.quantile(0.95)) if numeric.notna().any() else None,
        "p99_all_numeric_points": float(numeric.quantile(0.99)) if numeric.notna().any() else None,
        "max_points": float(numeric.max()) if numeric.notna().any() else None,
        "values_at_least_20_points": int((numeric >= 20).sum()),
        "source_values_modified": False,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def audit_spread_timeline(records: list[tuple[datetime, Any]]) -> dict[str, Any]:
    """Summarize raw MT5 spread points without imputing or converting them.

    ``OBSERVED_VALID`` only means that a positive finite value was present in
    the historical field. It does not establish that it is a complete
    bid/ask execution-cost series.
    """
    normalized: list[tuple[datetime, Any, str]] = []
    counts = {state: 0 for state in SPREAD_QUALITY_STATES}
    for timestamp, value in records:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("SPREAD_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
        utc_time = timestamp.astimezone(timezone.utc)
        state = classify_spread(value)
        counts[state] += 1
        normalized.append((utc_time, value, state))

    positive: list[float] = []
    for _, value, state in normalized:
        if state == "OBSERVED_VALID":
            positive.append(float(value))

    sessions: dict[str, list[tuple[Any, str]]] = {label: [] for _, _, label in UTC_SESSION_BINS}
    by_month: dict[str, list[str]] = {}
    for timestamp, value, state in normalized:
        sessions[utc_session(timestamp.hour)].append((value, state))
        by_month.setdefault(timestamp.strftime("%Y-%m"), []).append(state)

    def summarize(items: list[tuple[Any, str]]) -> dict[str, Any]:
        state_counts = {state: sum(item_state == state for _, item_state in items) for state in SPREAD_QUALITY_STATES}
        observed = [float(value) for value, state in items if state == "OBSERVED_VALID"]
        total = len(items)
        return {
            "records": total,
            "quality_counts": state_counts,
            "zero_percent": round(100 * state_counts["OBSERVED_ZERO_UNVERIFIED"] / total, 4) if total else None,
            "positive_percent": round(100 * state_counts["OBSERVED_VALID"] / total, 4) if total else None,
            "missing_percent": round(100 * state_counts["MISSING"] / total, 4) if total else None,
            "invalid_percent": round(100 * state_counts["INVALID"] / total, 4) if total else None,
            "positive_points_percentiles": {
                f"p{int(q * 100)}": _percentile(observed, q) for q in (0.0, 0.5, 0.9, 0.95, 0.99, 1.0)
            },
        }

    monthly = []
    for month, states in sorted(by_month.items()):
        observed_count = sum(state == "OBSERVED_VALID" for state in states)
        zero_count = sum(state == "OBSERVED_ZERO_UNVERIFIED" for state in states)
        monthly.append({
            "month_utc": month,
            "records": len(states),
            "positive_observed_records": observed_count,
            "zero_unverified_records": zero_count,
            "positive_observed_coverage_percent": round(100 * observed_count / len(states), 4) if states else None,
            "period_has_no_observed_positive_spreads": bool(states) and observed_count == 0,
        })

    ordered_times = [timestamp for timestamp, _, _ in normalized]
    return {
        "records": len(normalized),
        "quality_counts": counts,
        "zero_percent": round(100 * counts["OBSERVED_ZERO_UNVERIFIED"] / len(normalized), 4) if normalized else None,
        "positive_percent": round(100 * counts["OBSERVED_VALID"] / len(normalized), 4) if normalized else None,
        "missing_percent": round(100 * counts["MISSING"] / len(normalized), 4) if normalized else None,
        "invalid_percent": round(100 * counts["INVALID"] / len(normalized), 4) if normalized else None,
        "positive_points_percentiles": {
            f"p{int(q * 100)}": _percentile(positive, q) for q in (0.0, 0.5, 0.9, 0.95, 0.99, 1.0)
        },
        "observed_positive_spread_coverage": {
            "records_with_positive_value": len(positive),
            "first_positive_utc": min((t for t, _, state in normalized if state == "OBSERVED_VALID"), default=None).isoformat() if positive else None,
            "last_positive_utc": max((t for t, _, state in normalized if state == "OBSERVED_VALID"), default=None).isoformat() if positive else None,
            "months_with_positive_values": sum(not item["period_has_no_observed_positive_spreads"] for item in monthly),
            "total_months": len(monthly),
        },
        "by_utc_session": {session: summarize(items) for session, items in sessions.items()},
        "by_utc_month": monthly,
        "spread_units": "raw MT5 points; no point-to-price conversion",
        "imputation_applied": False,
        "source_values_modified": False,
    }
