"""Versioned technical-only plan used for paired research comparisons.

The policy deliberately reuses the existing historical 65/LIMIT strategy:
the same deterministic technical candidate filter and the existing historical
plan builder. It never imports or calls an LLM and does not replace the
production planner.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import inspect
import json
import math
from typing import Any

from config.backtest_config import CURRENT_LIVE_THRESHOLD
from config.trade_planner_config import TARGET_RISK_REWARD
from src.historical_trade_planner import SYMBOL_POINT, build_historical_trade_plan
from src.market_snapshot import build_compact_snapshot
from src.technical_evaluator import evaluate_technical_state


POLICY_VERSION = "TECHNICAL_REFERENCE_V1"
PLAN_SOURCE = "TECHNICAL_ONLY_REFERENCE"
ENTRY_MODE = "LIMIT"
TIMEFRAME_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field.upper()}_MISSING")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field.upper()}_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return sha256(body.encode("utf-8")).hexdigest()


def policy_definition() -> dict[str, Any]:
    """Parameters of the pre-existing primary backtest reference, not tuning."""
    implementation = "\n".join(inspect.getsource(function) for function in (
        _utc, _hash, policy_definition, build_technical_reference_plan, _result,
        build_compact_snapshot, evaluate_technical_state, build_historical_trade_plan,
    ))
    return {
        "policy_version": POLICY_VERSION,
        "candidate_threshold": CURRENT_LIVE_THRESHOLD,
        "candidate_rule": "setup_quality >= threshold AND alignment != CONFLICT AND bias != NEUTRAL",
        "direction_rule": {"BULLISH": "BUY", "BEARISH": "SELL"},
        "entry_mode": ENTRY_MODE,
        "plan_builder": "src.historical_trade_planner.build_historical_trade_plan",
        "plan_builder_parameters": {
            "target_risk_reward": TARGET_RISK_REWARD,
            "entry": "M15 EMA20 LIMIT",
            "stop": "max(technical swing stop, ATR stop) per existing builder",
        },
        "lookahead_rule": "every source candle close must be <= opportunity decision time",
        "implementation_sha256": sha256(implementation.encode("utf-8")).hexdigest(),
    }


def build_technical_reference_plan(
    symbol: str,
    market_state: dict[str, Any],
    decision_time_utc: str,
    *,
    constructed_at_utc: str | None = None,
    technical_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a causal 65/LIMIT plan or retain an explicit non-evaluable state."""
    built_at = constructed_at_utc or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    decision_time = None
    built_time = None
    maximum_source_time = None
    symbol = str(symbol).strip().upper()
    try:
        decision_time = _utc(decision_time_utc, "decision_time")
        built_time = _utc(built_at, "constructed_at")
        if built_time < decision_time:
            raise ValueError("PLAN_CONSTRUCTION_PRECEDES_DECISION")
        if symbol not in SYMBOL_POINT:
            raise ValueError("SYMBOL_NOT_SUPPORTED")
        if not isinstance(market_state, dict) or str(market_state.get("symbol", "")).upper() != symbol:
            raise ValueError("MARKET_STATE_SYMBOL_MISMATCH")
        state_time = _utc(market_state.get("analysis_time"), "market_state_analysis_time")
        if state_time != decision_time:
            raise ValueError("MARKET_STATE_DECISION_TIME_MISMATCH")

        maximum_source_time = None
        timeframes = market_state.get("timeframes")
        if not isinstance(timeframes, dict):
            raise ValueError("TIMEFRAME_DATA_MISSING")
        for timeframe, minutes in TIMEFRAME_MINUTES.items():
            data = timeframes.get(timeframe)
            candles = data.get("recent_candles") if isinstance(data, dict) else None
            if not isinstance(candles, list) or not candles:
                raise ValueError(f"{timeframe}_CANDLES_MISSING")
            for candle in candles:
                opened = _utc(candle.get("time"), f"{timeframe}_candle_time")
                closed = opened + timedelta(minutes=minutes)
                if closed > decision_time:
                    raise ValueError(f"FUTURE_{timeframe}_CANDLE")
                maximum_source_time = max(maximum_source_time, closed) if maximum_source_time else closed

        if "spread" not in timeframes["M5"]["recent_candles"][-1]:
            raise ValueError("M5_SPREAD_MISSING")
        raw_spread = timeframes["M5"]["recent_candles"][-1].get("spread")
        if raw_spread is None:
            raise ValueError("M5_SPREAD_MISSING")
        spread = float(raw_spread)
        if not math.isfinite(spread) or spread < 0:
            raise ValueError("M5_SPREAD_INVALID")
        spread_quality = "OBSERVED_ZERO_UNVERIFIED" if spread == 0 else "OBSERVED_VALID"
        supplied_quality = market_state.get("spread_quality")
        if isinstance(supplied_quality, dict):
            spread_quality = str(supplied_quality.get("status") or spread_quality)
        elif isinstance(supplied_quality, str):
            spread_quality = supplied_quality

        compact = build_compact_snapshot(market_state, candles_per_timeframe=4)
        technical = technical_evaluation or evaluate_technical_state(compact)
        bias = str(technical.get("bias", "")).upper()
        alignment = str(technical.get("alignment", "")).upper()
        quality = technical.get("setup_quality")
        if quality is None or not math.isfinite(float(quality)):
            raise ValueError("TECHNICAL_SETUP_QUALITY_INVALID")
        direction = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(bias)
        technical_decision = "WATCH_LONG" if direction == "BUY" else "WATCH_SHORT" if direction == "SELL" else "NO_TRADE"
        technical_summary = {
            "bias": bias,
            "alignment": alignment,
            "setup_quality": float(quality),
            "overall_score": technical.get("overall_score"),
            "timeframe_scores": technical.get("timeframe_scores"),
            "available_evidence_ids": list(compact.get("available_evidence_ids", [])),
        }
        if float(quality) < CURRENT_LIVE_THRESHOLD or alignment == "CONFLICT" or direction is None:
            return _result(
                status="NO_TECHNICAL_CANDIDATE", symbol=symbol, decision_time=decision_time,
                built_time=built_time, maximum_source_time=maximum_source_time,
                technical_decision="NO_TRADE", direction=None, plan=None,
                reason="TECHNICAL_65_LIMIT_CANDIDATE_RULE_NOT_MET", missing=[],
                spread_quality=spread_quality, technical=technical_summary,
            )

        historical = build_historical_trade_plan(symbol, direction, ENTRY_MODE, market_state)
        if historical.get("status") != "VALID":
            reason = str(historical.get("error") or (historical.get("errors") or ["HISTORICAL_PLAN_REJECTED"])[0])
            return _result(
                status="TECHNICAL_PLAN_NOT_EVALUABLE", symbol=symbol, decision_time=decision_time,
                built_time=built_time, maximum_source_time=maximum_source_time,
                technical_decision=technical_decision, direction=direction, plan=None,
                reason=reason, missing=[], spread_quality=spread_quality, technical=technical_summary,
            )
        plan = {
            "symbol": symbol,
            "direction": direction,
            "order_type": historical["order_type"],
            "entry": float(historical["entry"]),
            "stop_loss": float(historical["stop_loss"]),
            "take_profit": float(historical["take_profit"]),
            "mode": ENTRY_MODE,
            "target_rr": float(historical["target_rr"]),
        }
        if not all(math.isfinite(plan[key]) for key in ("entry", "stop_loss", "take_profit", "target_rr")):
            raise ValueError("PLAN_PRICE_NONFINITE")
        if (direction == "BUY" and not plan["stop_loss"] < plan["entry"] < plan["take_profit"]
                or direction == "SELL" and not plan["take_profit"] < plan["entry"] < plan["stop_loss"]):
            raise ValueError("PLAN_GEOMETRY_INVALID")
        if abs(plan["target_rr"] - TARGET_RISK_REWARD) > 1e-12:
            raise ValueError("PLAN_REWARD_RISK_MISMATCH")
        return _result(
            status="TECHNICAL_PLAN_VALID", symbol=symbol, decision_time=decision_time,
            built_time=built_time, maximum_source_time=maximum_source_time,
            technical_decision=technical_decision, direction=direction, plan=plan,
            reason="TECHNICAL_65_LIMIT_REFERENCE", missing=[], spread_quality=spread_quality,
            technical=technical_summary,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return _result(
            status="TECHNICAL_PLAN_NOT_EVALUABLE", symbol=symbol,
            decision_time=decision_time, built_time=built_time, maximum_source_time=maximum_source_time,
            technical_decision="NO_TRADE", direction=None, plan=None,
            reason=str(exc) or "TECHNICAL_PLAN_INPUT_INVALID", missing=[str(exc) or "TECHNICAL_PLAN_INPUT_INVALID"],
            spread_quality="MISSING", technical=None,
        )


def _result(*, status: str, symbol: str, decision_time: datetime | None,
            built_time: datetime | None, maximum_source_time: datetime | None,
            technical_decision: str, direction: str | None, plan: dict[str, Any] | None,
            reason: str, missing: list[str], spread_quality: str,
            technical: dict[str, Any] | None) -> dict[str, Any]:
    decision = decision_time.isoformat().replace("+00:00", "Z") if decision_time else None
    built = built_time.isoformat().replace("+00:00", "Z") if built_time else None
    source_time = maximum_source_time.isoformat().replace("+00:00", "Z") if maximum_source_time else None
    plan_hash = _hash(plan) if plan is not None else None
    return {
        "schema_version": "1.0",
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "plan_source": PLAN_SOURCE,
        "status": status,
        "symbol": symbol,
        "decision_time_utc": decision,
        "constructed_at_utc": built,
        "maximum_market_data_time_utc": source_time,
        "technical_decision": technical_decision,
        "direction": direction,
        "plan": plan,
        "plan_sha256": plan_hash,
        "reason": reason,
        "missing_fields": list(missing),
        "spread_quality": spread_quality,
        "technical_snapshot": technical,
        "confirmatory_eligible": status == "TECHNICAL_PLAN_VALID" and spread_quality == "OBSERVED_VALID",
    }


# Compute only after the complete policy implementation has been defined.
POLICY_SHA256 = _hash(policy_definition())
