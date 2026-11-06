"""Conservative labels and diagnostics for historical MT5 spread fields.

The project CSV stores the raw ``copy_rates`` spread column. This module never
rewrites source bars and keeps sensitivity floors explicitly marked as model
assumptions rather than observed broker quotes.
"""

from __future__ import annotations

from math import isfinite
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
