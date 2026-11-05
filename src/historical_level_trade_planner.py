"""Historical, level-anchored entry/stop/target plans for thesis replay.

Plans from this module are for simulation only. They do not place orders.
"""

from __future__ import annotations

from config.trade_planner_config import (
    MIN_RISK_REWARD,
    TARGET_RISK_REWARD,
)
from src.historical_trade_planner import SYMBOL_POINT


THESIS_SETUP = {
    "SUPPORT_HOLD": ("SUPPORT", "BUY", "HOLD_CONFIRMATION"),
    "SUPPORT_BREAK": ("SUPPORT", "SELL", "BREAK_CONFIRMATION"),
    "RESISTANCE_HOLD": ("RESISTANCE", "SELL", "HOLD_CONFIRMATION"),
    "RESISTANCE_BREAK": ("RESISTANCE", "BUY", "BREAK_CONFIRMATION"),
}


def _rejected(reason: str) -> dict:
    return {
        "status": "REJECTED",
        "simulation_only": True,
        "error": reason,
    }


def build_level_trade_plan(
    symbol: str,
    thesis: str,
    levels: dict,
    market_state: dict,
) -> dict:
    """Build a causal entry, stop and TP plan around the thesis level.

    HOLD theses wait for a short-term reversal confirmation. BREAK theses
    wait for price to move beyond the active zone. Both use a stop order so
    the simulated entry occurs only after the trigger is traded.
    """
    if thesis not in THESIS_SETUP:
        return _rejected("THESIS_NOT_OPERATIONAL")
    if levels.get("status") != "OK":
        return _rejected("LEVELS_UNAVAILABLE")

    level_type, direction, setup_type = THESIS_SETUP[thesis]
    active = levels.get(
        "nearest_support" if level_type == "SUPPORT" else "nearest_resistance"
    )
    if not isinstance(active, dict):
        return _rejected("ACTIVE_THESIS_LEVEL_MISSING")

    try:
        point = SYMBOL_POINT[symbol]
        atr = float(levels["reference_atr_m15"])
        zone_low = float(active["low"])
        zone_high = float(active["high"])
        m5_candles = market_state["timeframes"]["M5"]["recent_candles"]
        if not m5_candles:
            return _rejected("M5_CANDLES_UNAVAILABLE")
        last_m5 = m5_candles[-1]
        bid = float(last_m5["close"])
        spread_points = float(last_m5.get("spread") or 0.0)
    except (KeyError, TypeError, ValueError):
        return _rejected("MARKET_INPUTS_INVALID")

    if atr <= 0 or not zone_low < zone_high:
        return _rejected("LEVEL_OR_ATR_INVALID")

    spread_price = spread_points * point
    ask = bid + spread_price
    buffer = max(2 * point, spread_price, atr * 0.10)

    if setup_type == "HOLD_CONFIRMATION":
        recent = m5_candles[-3:]
        if direction == "BUY":
            entry = max(
                max(float(candle["high"]) for candle in recent) + buffer,
                ask + point,
            )
        else:
            entry = min(
                min(float(candle["low"]) for candle in recent) - buffer,
                bid - point,
            )
    elif direction == "BUY":
        entry = max(zone_high + buffer, ask + point)
    else:
        entry = min(zone_low - buffer, bid - point)

    # Invalidate a hold signal if confirmation has already moved too far
    # from the level before the pending entry can be placed.
    entry_level_distance_atr = (
        max(0.0, entry - zone_high)
        if direction == "BUY"
        else max(0.0, zone_low - entry)
    ) / atr
    if setup_type == "HOLD_CONFIRMATION" and entry_level_distance_atr > 1.0:
        return _rejected("HOLD_CONFIRMATION_TOO_FAR_FROM_LEVEL")

    stop_loss = zone_low - buffer if direction == "BUY" else zone_high + buffer
    if direction == "BUY" and stop_loss >= entry:
        return _rejected("BUY_STOP_NOT_ABOVE_STOP_LOSS")
    if direction == "SELL" and stop_loss <= entry:
        return _rejected("SELL_STOP_NOT_BELOW_STOP_LOSS")

    risk = abs(entry - stop_loss)
    if risk <= 0:
        return _rejected("INVALID_RISK_DISTANCE")

    target_distance = risk * TARGET_RISK_REWARD
    if direction == "BUY":
        take_profit = entry + target_distance
        opposing = [
            zone for zone in levels.get("resistances", [])
            if zone.get("zone_id") != active.get("zone_id")
            and float(zone["low"]) > entry
        ]
        if opposing:
            next_level = min(opposing, key=lambda zone: float(zone["low"]))
            structure_target = float(next_level["low"]) - buffer
            if structure_target <= entry:
                return _rejected("NEXT_RESISTANCE_TOO_CLOSE")
            take_profit = min(take_profit, structure_target)
    else:
        take_profit = entry - target_distance
        opposing = [
            zone for zone in levels.get("supports", [])
            if zone.get("zone_id") != active.get("zone_id")
            and float(zone["high"]) < entry
        ]
        if opposing:
            next_level = max(opposing, key=lambda zone: float(zone["high"]))
            structure_target = float(next_level["high"]) + buffer
            if structure_target >= entry:
                return _rejected("NEXT_SUPPORT_TOO_CLOSE")
            take_profit = max(take_profit, structure_target)

    entry = round(entry, 5)
    stop_loss = round(stop_loss, 5)
    take_profit = round(take_profit, 5)
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return _rejected("INVALID_ROUNDED_RISK_DISTANCE")

    actual_rr = abs(take_profit - entry) / risk
    if actual_rr < MIN_RISK_REWARD:
        return _rejected("INSUFFICIENT_RISK_REWARD_TO_NEXT_LEVEL")

    return {
        "status": "VALID",
        "simulation_only": True,
        "symbol": symbol,
        "thesis": thesis,
        "level_type": level_type,
        "level_id": active.get("zone_id"),
        "direction": direction,
        "setup_type": setup_type,
        "mode": "STOP",
        "order_type": "BUY_STOP" if direction == "BUY" else "SELL_STOP",
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "target_rr": round(actual_rr, 4),
        "risk_distance": risk,
        "entry_level_distance_atr": round(entry_level_distance_atr, 3),
        "spread_points_at_signal": spread_points,
        "active_level_low": zone_low,
        "active_level_high": zone_high,
    }
