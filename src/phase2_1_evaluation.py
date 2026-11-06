"""Offline research helpers for Phase 2.1.

This module deliberately has no MT5, LLM, network, or order-execution imports.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import random
from typing import Any

import pandas as pd


RESOLVED = {"WIN", "LOSS"}
AMBIGUOUS_PREFIX = "AMBIGUOUS"
ENGINEERING_STATES = {"COMPLETE", "INCOMPLETE", "FAILED"}
STRATEGY_STATES = {"PROMISING", "NOT_PROMISING", "INCONCLUSIVE"}
LLM_VALUE_STATES = {
    "EVIDENCE_OF_IMPROVEMENT",
    "NO_EVIDENCE_OF_IMPROVEMENT",
    "EVIDENCE_OF_DEGRADATION",
    "INCONCLUSIVE_INSUFFICIENT_DATA",
}
KNOWN_DECISION_FIELDS = {
    "symbol",
    "analysis_time",
    "session",
    "bias",
    "overall_score",
    "alignment",
    "setup_quality",
    "candidate_65",
    "m5_score",
    "m15_score",
    "h1_score",
    "h4_score",
    "d1_score",
    "direction",
    "session_utc",
}


def sha256_file(path: str) -> str:
    digest = sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_timestamp(value: Any, *, field: str = "timestamp") -> pd.Timestamp:
    """Parse an explicit timezone timestamp and normalize it to UTC."""
    if value is None or pd.isna(value):
        raise ValueError(f"{field.upper()}_MISSING")
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field.upper()}_INVALID") from None
    if stamp.tzinfo is None:
        raise ValueError(f"{field.upper()}_TIMEZONE_MISSING")
    return stamp.tz_convert("UTC")


def stable_event_id(row: Any) -> str:
    """Stable event key from symbol and signal timestamp, never outcome fields."""
    symbol = str(row.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("EVENT_SYMBOL_MISSING")
    timestamp = utc_timestamp(row.get("analysis_time", row.get("signal_time")), field="event_time")
    # One technical candidate exists per symbol and M15 close in the frozen scan.
    key = {"symbol": symbol, "event_time_utc": timestamp.isoformat()}
    return sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def attach_event_ids(frame: pd.DataFrame, timestamp_col: str) -> pd.DataFrame:
    if "symbol" not in frame or timestamp_col not in frame:
        raise ValueError("EVENT_KEY_COLUMNS_MISSING")
    result = frame.copy()
    result["event_id"] = [
        stable_event_id({"symbol": row.symbol, "analysis_time": getattr(row, timestamp_col)})
        for row in result.itertuples(index=False)
    ]
    return result


def chronological_split(
    frame: pd.DataFrame,
    timestamp_col: str = "analysis_time",
    *,
    train_fraction: float = 0.60,
    validation_fraction: float = 0.20,
    embargo_hours: int = 24,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Assign rows by unique UTC dates and mark a symmetric embargo at cutoffs."""
    if train_fraction <= 0 or validation_fraction <= 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("SPLIT_FRACTIONS_INVALID")
    if timestamp_col not in frame:
        raise ValueError("SPLIT_TIMESTAMP_COLUMN_MISSING")

    result = frame.copy()
    timestamps = [utc_timestamp(value, field=timestamp_col) for value in result[timestamp_col]]
    result["_phase2_utc"] = pd.Series(timestamps, index=result.index)
    days = sorted(result["_phase2_utc"].dt.normalize().drop_duplicates().tolist())
    if len(days) < 5:
        raise ValueError("SPLIT_TOO_FEW_DAYS")

    train_days = max(1, int(math.floor(len(days) * train_fraction)))
    validation_days = max(1, int(math.floor(len(days) * validation_fraction)))
    if train_days + validation_days >= len(days):
        train_days = max(1, len(days) - 2)
        validation_days = 1
    validation_cut = days[train_days]
    test_cut = days[train_days + validation_days]
    end_exclusive = days[-1] + pd.Timedelta(days=1)
    embargo = pd.Timedelta(hours=embargo_hours)

    labels: list[str] = []
    for stamp in result["_phase2_utc"]:
        if stamp < validation_cut:
            label = "TRAIN"
        elif stamp < test_cut:
            label = "VALIDATION"
        else:
            label = "TEST"
        if abs(stamp - validation_cut) <= embargo or abs(stamp - test_cut) <= embargo:
            label = "EMBARGO"
        labels.append(label)
    result["period"] = labels
    result = result.drop(columns=["_phase2_utc"])

    info = {
        "method": "chronological_unique_UTC_days",
        "requested_fractions": {"train": train_fraction, "validation": validation_fraction, "test": 1 - train_fraction - validation_fraction},
        "unique_utc_days": len(days),
        "train_days_before_embargo": train_days,
        "validation_days_before_embargo": validation_days,
        "test_days_before_embargo": len(days) - train_days - validation_days,
        "first_utc_day": days[0].isoformat(),
        "last_utc_day": days[-1].isoformat(),
        "train_end_exclusive_utc": validation_cut.isoformat(),
        "test_start_utc": test_cut.isoformat(),
        "dataset_end_exclusive_utc": end_exclusive.isoformat(),
        "embargo_hours": embargo_hours,
        "rows_by_period": result["period"].value_counts().to_dict(),
    }
    return result, info


def validate_feature_columns(columns: list[str] | tuple[str, ...]) -> tuple[bool, list[str]]:
    """Reject outcome/post-decision columns from a feature frame."""
    unexpected = sorted(set(columns) - KNOWN_DECISION_FIELDS)
    return not unexpected, unexpected


def purge_trade_events(
    trades: pd.DataFrame,
    split_info: dict[str, Any],
    *,
    signal_col: str = "signal_time",
    end_col: str = "release_time",
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Assign completed events to a split and purge boundary-crossing outcomes."""
    required = {signal_col, end_col}
    if not required.issubset(trades.columns):
        raise ValueError("TRADE_SPLIT_COLUMNS_MISSING")
    validation_cut = utc_timestamp(split_info["train_end_exclusive_utc"])
    test_cut = utc_timestamp(split_info["test_start_utc"])
    dataset_end = utc_timestamp(split_info["dataset_end_exclusive_utc"])
    embargo = pd.Timedelta(hours=int(split_info["embargo_hours"]))
    out = trades.copy()
    labels: list[str] = []
    reasons: list[str] = []
    for signal_value, end_value in zip(out[signal_col], out[end_col]):
        signal = utc_timestamp(signal_value, field=signal_col)
        end = None if end_value is None or pd.isna(end_value) else utc_timestamp(end_value, field=end_col)
        if abs(signal - validation_cut) <= embargo or abs(signal - test_cut) <= embargo:
            labels.append("EMBARGO")
            reasons.append("SIGNAL_IN_EMBARGO")
            continue
        period = "TRAIN" if signal < validation_cut else "VALIDATION" if signal < test_cut else "TEST"
        upper = validation_cut if period == "TRAIN" else test_cut if period == "VALIDATION" else dataset_end
        # Leave a full embargo between an event's outcome and the next partition.
        effective_upper = upper - embargo if period != "TEST" else upper
        if end is None:
            labels.append(period)
            reasons.append("OPEN_OR_UNKNOWN_END")
        elif end < signal:
            labels.append("PURGED")
            reasons.append("END_BEFORE_SIGNAL")
        elif end >= effective_upper:
            labels.append("PURGED")
            reasons.append("EVENT_CROSSES_PARTITION_OR_EMBARGO")
        else:
            labels.append(period)
            reasons.append("IN_PARTITION")
    out["period"] = labels
    out["purge_reason"] = reasons
    return out, pd.Series(reasons).value_counts().to_dict()


def assert_not_test_for_tuning(period: str) -> None:
    if str(period).upper() == "TEST":
        raise ValueError("TEST_PARTITION_IS_RESERVED")


def validate_llm_provenance(record: dict[str, Any]) -> tuple[bool, list[str]]:
    """Require verifiable attribution before a saved model decision can be used."""
    required = (
        "decision_id",
        "symbol",
        "timestamp",
        "provider",
        "model",
        "prompt_version",
        "payload_hash",
        "response",
        "validation_status",
        "evidence_ids",
    )
    missing = [key for key in required if record.get(key) in (None, "", [])]
    provider = str(record.get("provider", "")).lower()
    if provider and provider not in {"deepseek", "ollama"}:
        missing.append("provider_not_supported")
    if record.get("timestamp"):
        try:
            utc_timestamp(record["timestamp"], field="timestamp")
        except ValueError:
            missing.append("timestamp_invalid")
    return not missing, missing


def pair_on_event_id(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    if "event_id" not in left or "event_id" not in right:
        raise ValueError("PAIR_EVENT_ID_MISSING")
    if left["event_id"].duplicated().any() or right["event_id"].duplicated().any():
        raise ValueError("PAIR_EVENT_ID_DUPLICATE")
    return left.merge(right, on="event_id", how="inner", suffixes=("_technical", "_llm"), validate="one_to_one")


def market_exposure(trades: pd.DataFrame) -> dict[str, Any]:
    result = {"time_in_market_hours": 0.0, "time_in_market_pct": None, "max_simultaneous_filled_positions": 0, "max_open_risk_eur": None}
    if not {"fill_time", "exit_time"}.issubset(trades.columns):
        return result
    intervals = []
    for _, row in trades.iterrows():
        if pd.isna(row.get("fill_time")) or pd.isna(row.get("exit_time")):
            continue
        start = utc_timestamp(row["fill_time"], field="fill_time")
        end = utc_timestamp(row["exit_time"], field="exit_time")
        if end <= start:
            continue
        risk_value = None
        if "estimated_risk_eur" in trades and pd.notna(row.get("estimated_risk_eur")):
            try:
                risk_value = float(row["estimated_risk_eur"])
            except (TypeError, ValueError):
                risk_value = None
        intervals.append((start, end, risk_value))
    if not intervals:
        return result

    intervals.sort(key=lambda item: (item[0], item[1]))
    period_start = min(row[0] for row in intervals)
    period_end = max(row[1] for row in intervals)
    union_start, union_end = intervals[0][0], intervals[0][1]
    union_seconds = 0.0
    for start, end, _ in intervals[1:]:
        if start <= union_end:
            union_end = max(union_end, end)
        else:
            union_seconds += (union_end - union_start).total_seconds()
            union_start, union_end = start, end
    union_seconds += (union_end - union_start).total_seconds()

    events = []
    for start, end, risk_value in intervals:
        events.append((start, 1, risk_value))
        events.append((end, -1, risk_value))
    # End events sort before start events at identical timestamps (half-open intervals).
    events.sort(key=lambda item: (item[0], item[1]))
    count = 0
    risk = 0.0
    max_count = 0
    max_risk = 0.0
    has_risk = any(value is not None for _, _, value in events)
    for _, delta, risk_value in events:
        count += delta
        if risk_value is not None:
            risk += delta * risk_value
        max_count = max(max_count, count)
        max_risk = max(max_risk, risk)
    denominator = (period_end - period_start).total_seconds()
    result.update({
        "time_in_market_hours": round(union_seconds / 3600, 8),
        "time_in_market_pct": round(union_seconds / denominator, 8) if denominator > 0 else None,
        "max_simultaneous_filled_positions": max_count,
        "max_open_risk_eur": round(max_risk, 8) if has_risk else None,
    })
    return result


def summarize_trades(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {
            "proposed": 0,
            "executed": 0,
            "closed": 0,
            "ambiguous": 0,
            "open_or_pending": 0,
            "gross_pnl_eur": 0.0,
            "costs_eur_closed": 0.0,
            "costs_eur_all": 0.0,
            "net_pnl_eur": 0.0,
            "expectancy_net_eur": None,
            "expectancy_net_r": None,
            "profit_factor_net": None,
            "max_drawdown_net_eur": 0.0,
            **market_exposure(trades),
        }
    result_col = trades.get("result", pd.Series(index=trades.index, dtype=str)).fillna("").astype(str)
    closed_mask = result_col.isin(RESOLVED)
    closed = trades.loc[closed_mask].copy()
    gross = pd.to_numeric(closed.get("gross_pnl_eur", closed.get("pnl_eur", 0)), errors="coerce").fillna(0)
    if "transaction_cost_eur" in closed:
        closed_costs = pd.to_numeric(closed["transaction_cost_eur"], errors="coerce").fillna(0)
    else:
        closed_costs = pd.Series(0.0, index=closed.index)
    net = pd.to_numeric(closed.get("net_pnl_eur", gross - closed_costs), errors="coerce").fillna(0)
    gains = float(net[net > 0].sum())
    losses = abs(float(net[net < 0].sum()))
    if "net_r" in closed:
        net_r = pd.to_numeric(closed["net_r"], errors="coerce").dropna()
    elif "estimated_risk_eur" in closed:
        risk = pd.to_numeric(closed["estimated_risk_eur"], errors="coerce")
        net_r = (net / risk.replace(0, float("nan"))).dropna()
    else:
        net_r = pd.Series(dtype=float)

    if "exit_time" in closed and len(closed):
        times = [utc_timestamp(v, field="exit_time") for v in closed["exit_time"]]
        sequence = pd.DataFrame({"time": times, "pnl": net.to_list()}).sort_values("time")
        equity = sequence["pnl"].cumsum()
        drawdown = equity.cummax().clip(lower=0) - equity
        max_drawdown = float(drawdown.max()) if len(drawdown) else 0.0
    else:
        max_drawdown = 0.0

    costs_all = pd.to_numeric(trades.get("transaction_cost_eur", 0), errors="coerce")
    if not isinstance(costs_all, pd.Series):
        costs_all = pd.Series(float(costs_all), index=trades.index)
    costs_all = costs_all.fillna(0)
    executed = int(trades["fill_time"].notna().sum()) if "fill_time" in trades else 0
    ambiguous = int(result_col.str.startswith(AMBIGUOUS_PREFIX).sum())
    unresolved = int((~closed_mask & ~result_col.str.startswith(AMBIGUOUS_PREFIX) & ~result_col.isin({"EXPIRED"})).sum())
    return {
        "proposed": int(len(trades)),
        "executed": executed,
        "closed": int(len(closed)),
        "expired": int(result_col.eq("EXPIRED").sum() + result_col.eq("EXPIRED_AT_END").sum()),
        "ambiguous": ambiguous,
        "open_or_pending": unresolved,
        "gross_pnl_eur": round(float(gross.sum()), 8),
        "costs_eur_closed": round(float(closed_costs.sum()), 8),
        "costs_eur_all": round(float(costs_all.sum()), 8),
        "net_pnl_eur": round(float(net.sum()), 8),
        "expectancy_net_eur": round(float(net.mean()), 8) if len(net) else None,
        "expectancy_net_r": round(float(net_r.mean()), 8) if len(net_r) else None,
        "profit_factor_net": round(gains / losses, 8) if losses > 0 else None,
        "max_drawdown_net_eur": round(max_drawdown, 8),
        "wins_net": int((net > 0).sum()),
        "losses_net": int((net < 0).sum()),
        **market_exposure(trades),
    }


def weekly_block_bootstrap_mean(
    trades: pd.DataFrame,
    *,
    value_col: str = "net_pnl_eur",
    time_col: str = "exit_time",
    seed: int = 20261106,
    repetitions: int = 2000,
    minimum_blocks: int = 10,
) -> dict[str, Any]:
    """95% CI for mean per closed trade using whole UTC weeks as blocks."""
    if trades.empty or value_col not in trades or time_col not in trades:
        return {"status": "INSUFFICIENT_DATA", "reason": "NO_CLOSED_TRADE_SERIES", "blocks": 0, "ci95": None}
    subset = trades.loc[trades["result"].astype(str).isin(RESOLVED), [value_col, time_col]].copy()
    if subset.empty:
        return {"status": "INSUFFICIENT_DATA", "reason": "NO_RESOLVED_TRADES", "blocks": 0, "ci95": None}
    subset[value_col] = pd.to_numeric(subset[value_col], errors="coerce")
    subset = subset.dropna(subset=[value_col, time_col])
    subset["_time"] = [utc_timestamp(value, field=time_col) for value in subset[time_col]]
    # The timestamps are already normalized to UTC; drop the timezone only for
    # bucketing so pandas does not emit a misleading timezone-loss warning.
    subset["_week"] = subset["_time"].dt.tz_localize(None).dt.to_period("W-SUN").astype(str)
    groups = [group[value_col].tolist() for _, group in subset.groupby("_week", sort=True)]
    if len(groups) < minimum_blocks:
        return {
            "status": "INSUFFICIENT_DATA",
            "reason": f"WEEKLY_BLOCKS_BELOW_{minimum_blocks}",
            "blocks": len(groups),
            "closed_trades": int(len(subset)),
            "ci95": None,
        }
    rng = random.Random(seed)
    estimates = []
    for _ in range(repetitions):
        sample = [groups[rng.randrange(len(groups))] for _ in range(len(groups))]
        values = [value for block in sample for value in block]
        estimates.append(sum(values) / len(values))
    estimates.sort()
    lower = estimates[int(0.025 * (len(estimates) - 1))]
    upper = estimates[int(0.975 * (len(estimates) - 1))]
    return {
        "status": "PASS",
        "method": "weekly_block_bootstrap_mean_per_closed_trade",
        "seed": seed,
        "repetitions": repetitions,
        "blocks": len(groups),
        "closed_trades": int(len(subset)),
        "observed_mean": round(float(subset[value_col].mean()), 8),
        "ci95": [round(float(lower), 8), round(float(upper), 8)],
    }


def classify_engineering_status(checks: dict[str, str], required: set[str]) -> str:
    """Classify offline implementation health independently of research findings.

    A missing/unrun check is INCOMPLETE; only an executed failure is FAILED.
    External readiness and the availability of historical model decisions are
    intentionally not engineering checks.
    """
    if not required:
        raise ValueError("ENGINEERING_REQUIRED_CHECKS_EMPTY")
    statuses = {str(checks.get(name, "PENDING")).upper() for name in required}
    if "FAIL" in statuses:
        return "FAILED"
    if statuses - {"PASS", "NOT_APPLICABLE"}:
        return "INCOMPLETE"
    return "COMPLETE"


def classify_strategy_evidence(
    *,
    confirmatory: bool,
    conservative_expectancy: float | None,
    conservative_ci95: list[float] | tuple[float, float] | None,
    walk_forward_expectancies: list[float | None] | None = None,
    symbol_net_pnl: dict[str, float] | None = None,
) -> str:
    """Apply the preregistered, cost-aware decision rule to evidence.

    PROMISING requires a valid untouched confirmatory interval, positive
    conservative expectancy with its lower CI above zero, every preregistered
    walk-forward fold positive, and positive net PnL in at least two symbols.
    NOT_PROMISING requires an untouched confirmatory conservative CI wholly at
    or below zero. Any incomplete or mixed evidence is INCONCLUSIVE.
    """
    if not confirmatory or conservative_expectancy is None or conservative_ci95 is None:
        return "INCONCLUSIVE"
    try:
        lower, upper = map(float, conservative_ci95)
        expectancy = float(conservative_expectancy)
    except (TypeError, ValueError):
        return "INCONCLUSIVE"
    if not all(math.isfinite(value) for value in (lower, upper, expectancy)) or lower > upper:
        return "INCONCLUSIVE"
    if upper <= 0 and expectancy <= 0:
        return "NOT_PROMISING"
    folds = list(walk_forward_expectancies or [])
    symbol_values = list((symbol_net_pnl or {}).values())
    try:
        consistency = bool(folds) and all(value is not None and math.isfinite(float(value)) and float(value) > 0 for value in folds)
        diversified = sum(1 for value in symbol_values if value is not None and math.isfinite(float(value)) and float(value) > 0) >= 2
    except (TypeError, ValueError, OverflowError):
        return "INCONCLUSIVE"
    if expectancy > 0 and lower > 0 and consistency and diversified:
        return "PROMISING"
    return "INCONCLUSIVE"


def classify_llm_value_add(
    *,
    valid_pairs: int,
    confirmatory: bool,
    paired_mean_difference: float | None,
    paired_ci95: list[float] | tuple[float, float] | None,
    independent_blocks: int | None,
    minimum_independent_blocks: int = 10,
) -> str:
    """Classify paired LLM evidence; no pairs remain an explicit research result."""
    try:
        valid_pairs = int(valid_pairs)
        independent_blocks = int(independent_blocks) if independent_blocks is not None else None
        minimum_independent_blocks = int(minimum_independent_blocks)
    except (TypeError, ValueError, OverflowError):
        return "INCONCLUSIVE_INSUFFICIENT_DATA"
    if (
        valid_pairs <= 0
        or not confirmatory
        or paired_mean_difference is None
        or paired_ci95 is None
        or independent_blocks is None
        or independent_blocks < minimum_independent_blocks
    ):
        return "INCONCLUSIVE_INSUFFICIENT_DATA"
    try:
        lower, upper = map(float, paired_ci95)
        mean = float(paired_mean_difference)
    except (TypeError, ValueError):
        return "INCONCLUSIVE_INSUFFICIENT_DATA"
    if not all(math.isfinite(value) for value in (lower, upper, mean)) or lower > upper:
        return "INCONCLUSIVE_INSUFFICIENT_DATA"
    if lower > 0 and mean > 0:
        return "EVIDENCE_OF_IMPROVEMENT"
    if upper < 0 and mean < 0:
        return "EVIDENCE_OF_DEGRADATION"
    return "NO_EVIDENCE_OF_IMPROVEMENT"


def month_group_summary(trades: pd.DataFrame, group_columns: list[str]) -> list[dict[str, Any]]:
    if trades.empty or "signal_time" not in trades:
        return []
    data = trades.copy()
    data["_signal"] = [utc_timestamp(value, field="signal_time") for value in data["signal_time"]]
    data["month_utc"] = data["_signal"].dt.strftime("%Y-%m")
    keys = [column for column in group_columns if column in data]
    outputs = []
    for group_key, group in data.groupby(["month_utc", *keys], dropna=False, sort=True):
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        outputs.append(dict(zip(["month_utc", *keys], map(str, group_key))) | summarize_trades(group))
    return outputs
