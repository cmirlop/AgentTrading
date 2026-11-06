"""Deterministic, causal indicator warmup comparison for historical bars."""

from __future__ import annotations

from typing import Any

import pandas as pd

from src.indicators import add_indicators


TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
PREDECLARED_WINDOWS = (200, 400, 700, 800)
EMA_SPANS = (20, 50, 200)


def ema200_stabilization_bars(residual_weight: float = 0.001, span: int = 200) -> int:
    """Bars for EMA initialization weight to fall below the fixed 0.1% criterion."""
    import math

    if not 0 < residual_weight < 1 or span < 2:
        raise ValueError("WARMUP_CRITERION_INVALID")
    alpha = 2 / (span + 1)
    return int(math.ceil(math.log(residual_weight) / math.log(1 - alpha))) + 1


def indicator_at_window(bars: pd.DataFrame, decision_time: Any, timeframe: str, window: int) -> dict[str, Any]:
    if timeframe not in TIMEFRAME_MINUTES or window <= 0:
        raise ValueError("WARMUP_REQUEST_INVALID")
    decision = pd.Timestamp(decision_time)
    if decision.tzinfo is None:
        raise ValueError("WARMUP_DECISION_TIMEZONE_MISSING")
    decision = decision.tz_convert("UTC")
    source = bars.copy()
    source["time"] = pd.to_datetime(source["time"], utc=True)
    source = source.sort_values("time").drop_duplicates("time", keep="last")
    closed = source.loc[source["time"] + pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe]) <= decision]
    selected = closed.tail(window).copy()
    if selected.empty:
        return {"bars_used": 0, "warmup_status": "WARMUP_INSUFFICIENT", "values": {}}
    if len(selected) < ema200_stabilization_bars():
        status = "WARMUP_INSUFFICIENT"
    else:
        status = "READY_FOR_COMPARISON"
    calculated = add_indicators(selected)
    last = calculated.iloc[-1]
    keys = [*(f"ema{span}" for span in EMA_SPANS), "rsi14", "atr14"]
    values = {key: None if pd.isna(last[key]) else float(last[key]) for key in keys}
    return {
        "bars_used": int(len(selected)),
        "last_bar_close_utc": (selected["time"].iloc[-1] + pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])).isoformat(),
        "decision_time_utc": decision.isoformat(),
        "warmup_status": status,
        "values": values,
    }


def compare_indicator_windows(bars: pd.DataFrame, decision_time: Any, timeframe: str,
                              windows: tuple[int, ...] = PREDECLARED_WINDOWS) -> dict[str, Any]:
    results = {str(window): indicator_at_window(bars, decision_time, timeframe, window) for window in windows}
    reference_key = str(max(windows))
    reference = results[reference_key]["values"]
    deltas = {}
    changed_relation = {}
    for window, result in results.items():
        deltas[window] = {
            key: (None if result["values"].get(key) is None or reference.get(key) is None
                  else abs(result["values"][key] - reference[key]))
            for key in reference
        }
        relation_changes = {}
        close = None
        if len(bars):
            source = bars.copy()
            source["time"] = pd.to_datetime(source["time"], utc=True)
            cutoff = pd.Timestamp(decision_time).tz_convert("UTC")
            source = source.loc[source["time"] + pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe]) <= cutoff]
            if len(source):
                close = float(source.iloc[-1]["close"])
        for span in EMA_SPANS:
            value = result["values"].get(f"ema{span}")
            ref_value = reference.get(f"ema{span}")
            relation = lambda x: "UNKNOWN" if x is None or close is None else "ABOVE" if close > x else "BELOW" if close < x else "EQUAL"
            relation_changes[f"price_vs_ema{span}"] = relation(value) != relation(ref_value)
        changed_relation[window] = relation_changes
    return {
        "symbol_timeframe": timeframe,
        "decision_time_utc": pd.Timestamp(decision_time).tz_convert("UTC").isoformat(),
        "reference_window_bars": max(windows),
        "windows": results,
        "absolute_indicator_deltas_from_reference": deltas,
        "price_relation_changed_from_reference": changed_relation,
        "any_ema_price_relation_change": {window: any(values.values()) for window, values in changed_relation.items()},
        "causal_cutoff_enforced": True,
    }


def reconstruct_technical_at_window(symbol: str, decision_time: Any, raw_by_timeframe: dict[str, pd.DataFrame],
                                    window: int) -> dict[str, Any]:
    """Rebuild existing technical score from a fixed, strictly prior warmup."""
    from config.market_config import RECENT_CANDLES, TIMEFRAMES
    from src.historical_state import build_historical_market_state
    from src.market_snapshot import build_compact_snapshot
    from src.technical_evaluator import evaluate_technical_state

    decision = pd.Timestamp(decision_time)
    if decision.tzinfo is None:
        raise ValueError("WARMUP_DECISION_TIMEZONE_MISSING")
    decision = decision.tz_convert("UTC")
    prepared = {}
    counts = {}
    for timeframe in TIMEFRAMES:
        raw = raw_by_timeframe[timeframe].copy()
        raw["time"] = pd.to_datetime(raw["time"], utc=True)
        closed = raw.loc[raw["time"] + pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe]) <= decision]
        selected = closed.tail(window).copy()
        counts[timeframe] = int(len(selected))
        if len(selected) < 200:
            return {"status": "WARMUP_INSUFFICIENT", "bars_by_timeframe": counts, "window_bars": window}
        calculated = add_indicators(selected)
        calculated["bar_close"] = calculated["time"] + pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        prepared[timeframe] = calculated
    state = build_historical_market_state(symbol, decision, prepared)
    snapshot = build_compact_snapshot(state, candles_per_timeframe=RECENT_CANDLES.get("M15", 4))
    technical = evaluate_technical_state(snapshot)
    return {
        "status": "READY",
        "warmup_status": "STABILIZED" if window >= ema200_stabilization_bars() else "WARMUP_INSUFFICIENT",
        "window_bars": window,
        "bars_by_timeframe": counts,
        "decision_time_utc": decision.isoformat(),
        "technical": {key: technical.get(key) for key in ("bias", "overall_score", "setup_quality", "alignment", "timeframe_scores")},
    }
