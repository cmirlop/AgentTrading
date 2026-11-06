from math import isfinite

import pandas as pd

from config.execution_config import (
    PENDING_ORDER_EXPIRATION_MINUTES,
)

from src.historical_trade_planner import (
    SYMBOL_POINT,
)
from config.backtest_cost_config import COST_SCENARIOS
from config.risk_config import LOT_SIZE
from src.spread_quality import classify_spread, effective_spread_points


def _settlement(result, gross_r, fill_time, exit_time, release_time, *,
                plan, point, scenario, risk_amount_eur, filled,
                spread_floor_points=0.0, spread_audit=None):
    costs = COST_SCENARIOS[scenario]
    round_turn = result in {
        "WIN", "LOSS", "AMBIGUOUS_ENTRY_BAR", "AMBIGUOUS_BOTH", "AMBIGUOUS_EXPIRY_BAR",
    }
    sides = 2 if round_turn else 1 if filled else 0
    volume = float(plan.get("volume", LOT_SIZE))
    commission_factor = 1.0 if round_turn else 0.5 if filled else 0.0
    commission_eur = costs["commission_eur_per_lot_round_turn"] * volume * commission_factor
    slippage_price = costs["slippage_points_per_side"] * point * sides
    risk_distance = abs(float(plan["entry"]) - float(plan["stop_loss"]))
    slippage_r = slippage_price / risk_distance if risk_distance > 0 else None
    commission_r = commission_eur / float(risk_amount_eur) if risk_amount_eur and float(risk_amount_eur) > 0 else (0.0 if commission_eur == 0 else None)
    cost_r = None if slippage_r is None or commission_r is None else slippage_r + commission_r
    net_r = gross_r - cost_r if gross_r is not None and cost_r is not None else None
    slippage_eur = (
        slippage_r * float(risk_amount_eur)
        if slippage_r is not None and risk_amount_eur is not None and float(risk_amount_eur) > 0
        else 0.0 if slippage_r == 0 else None
    )
    cost_eur = commission_eur + slippage_eur if slippage_eur is not None else None
    return {
        "result": result,
        "r": gross_r,
        "gross_r": gross_r,
        "net_r": net_r,
        "transaction_cost_r": cost_r,
        "transaction_cost_eur": round(cost_eur, 8) if cost_eur is not None else None,
        "commission_eur": round(commission_eur, 8),
        "slippage_points": costs["slippage_points_per_side"] * sides,
        "spread_model": "HISTORICAL_BID_ASK_EMBEDDED",
        "cost_scenario": scenario,
        "assumptions_unverified": costs["assumptions_unverified"],
        "fill_time": fill_time,
        "exit_time": exit_time,
        "release_time": release_time,
        "spread_floor_assumption_points": float(spread_floor_points),
        "spread_bars_examined": int((spread_audit or {}).get("bars_examined", 0)),
        "unverified_zero_spread_bar_count": int((spread_audit or {}).get("unverified_zero_spread_bar_count", 0)),
        "spread_floor_applied_bar_count": int((spread_audit or {}).get("spread_floor_applied_bar_count", 0)),
        "spread_data_quality": (
            "IMPUTED_ASSUMPTION" if (spread_audit or {}).get("spread_floor_applied_bar_count", 0)
            else "OBSERVED_ZERO_UNVERIFIED" if (spread_audit or {}).get("unverified_zero_spread_bar_count", 0)
            else "OBSERVED_VALID" if (spread_audit or {}).get("bars_examined", 0)
            else "MISSING"
        ),
    }


def simulate_trade(
    plan,
    signal_time,
    m5_history,
    cost_scenario="BASE",
    risk_amount_eur=None,
    spread_floor_points=0.0,
):

    cost_scenario = str(cost_scenario).upper()
    if cost_scenario not in COST_SCENARIOS:
        raise ValueError("UNKNOWN_COST_SCENARIO")
    try:
        spread_floor_points = float(spread_floor_points)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("SPREAD_ASSUMPTION_INVALID") from None
    if not isfinite(spread_floor_points) or spread_floor_points < 0:
        raise ValueError("SPREAD_ASSUMPTION_INVALID")

    spread_audit = {
        "bars_examined": 0,
        "unverified_zero_spread_bar_count": 0,
        "spread_floor_applied_bar_count": 0,
    }

    def _finalize(*args, **kwargs):
        return _settlement(
            *args,
            spread_floor_points=spread_floor_points,
            spread_audit=spread_audit,
            **kwargs,
        )

    symbol = plan[
        "symbol"
    ]

    direction = plan[
        "direction"
    ]

    order_type = plan[
        "order_type"
    ]

    entry = plan[
        "entry"
    ]

    sl = plan[
        "stop_loss"
    ]

    tp = plan[
        "take_profit"
    ]

    point = SYMBOL_POINT[
        symbol
    ]

    if "spread" not in m5_history.columns:
        raise ValueError("HISTORICAL_SPREAD_UNAVAILABLE")
    required_columns = {"time", "bar_close", "high", "low"}
    missing_columns = sorted(required_columns - set(m5_history.columns))
    if missing_columns:
        raise ValueError("HISTORICAL_EXECUTION_COLUMNS_MISSING")
    if not m5_history["time"].is_monotonic_increasing or m5_history["time"].duplicated().any():
        raise ValueError("HISTORICAL_BARS_NOT_STRICTLY_CHRONOLOGICAL")
    if m5_history[["time", "bar_close"]].isna().any().any():
        raise ValueError("HISTORICAL_BAR_TIME_INVALID")
    if (m5_history["bar_close"] < m5_history["time"]).any():
        raise ValueError("HISTORICAL_BAR_CLOSE_BEFORE_OPEN")
    if direction not in {"BUY", "SELL"}:
        raise ValueError("DIRECTION_INVALID")
    if order_type not in {"BUY_LIMIT", "BUY_STOP", "SELL_LIMIT", "SELL_STOP"}:
        raise ValueError("ORDER_TYPE_INVALID")
    try:
        numeric_plan = [float(entry), float(sl), float(tp), float(plan.get("volume", LOT_SIZE))]
    except (TypeError, ValueError):
        raise ValueError("TRADE_PLAN_NUMERIC_DATA_INVALID") from None
    if not all(isfinite(value) for value in numeric_plan) or numeric_plan[3] <= 0:
        raise ValueError("TRADE_PLAN_NUMERIC_DATA_INVALID")
    if (direction == "BUY" and not (float(sl) < float(entry) < float(tp))) or (
        direction == "SELL" and not (float(tp) < float(entry) < float(sl))
    ):
        raise ValueError("TRADE_PLAN_PRICE_ORDER_INVALID")

    if "spread" not in m5_history.columns:
        raise ValueError("HISTORICAL_SPREAD_UNAVAILABLE")

    signal_time = pd.Timestamp(
        signal_time
    )

    expiration = (
        signal_time
        +
        pd.Timedelta(
            minutes=
                PENDING_ORDER_EXPIRATION_MINUTES
        )
    )

    # Primera vela que empieza DESPUES
    # de haber generado la señal.
    start_index = (
        m5_history[
            "time"
        ].searchsorted(
            signal_time,
            side="left",
        )
    )

    if start_index >= len(m5_history):
        final_time = signal_time
        if not m5_history.empty and "bar_close" in m5_history:
            final_time = m5_history["bar_close"].iloc[-1]
        return _finalize(
            "PENDING_AT_END", None, None, final_time, final_time,
            plan=plan, point=point, scenario=cost_scenario,
            risk_amount_eur=risk_amount_eur, filled=False,
        )

    filled = False
    fill_time = None

    for index in range(
        start_index,
        len(m5_history),
    ):

        bar = m5_history.iloc[
            index
        ]

        bar_open_time = (
            bar["time"]
        )

        bar_close_time = (
            bar["bar_close"]
        )

        if not filled and bar_open_time >= expiration:
            return _finalize(
                "EXPIRED", 0.0, None, expiration, expiration,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=False,
            )

        raw_spread = bar["spread"]
        raw_quality = classify_spread(raw_spread)
        if raw_quality == "MISSING":
            raise ValueError("HISTORICAL_SPREAD_UNAVAILABLE")
        if raw_quality == "INVALID":
            raise ValueError("HISTORICAL_SPREAD_INVALID")
        effective_points, _, floor_applied = effective_spread_points(
            raw_spread,
            multiplier=COST_SCENARIOS[cost_scenario]["historical_spread_multiplier"],
            floor_points=spread_floor_points,
        )
        spread_audit["bars_examined"] += 1
        if raw_quality == "OBSERVED_ZERO_UNVERIFIED":
            spread_audit["unverified_zero_spread_bar_count"] += 1
        if floor_applied:
            spread_audit["spread_floor_applied_bar_count"] += 1
        spread_price = effective_points * point
        if not isfinite(spread_price) or spread_price < 0:
            raise ValueError("HISTORICAL_SPREAD_INVALID")

        bid_high = float(
            bar["high"]
        )

        bid_low = float(
            bar["low"]
        )

        ask_high = (
            bid_high
            +
            spread_price
        )

        ask_low = (
            bid_low
            +
            spread_price
        )

        # ==================================================
        # PENDING
        # ==================================================

        if not filled:

            trigger = False

            if (
                order_type
                == "BUY_LIMIT"
            ):

                trigger = (
                    ask_low
                    <= entry
                )

            elif (
                order_type
                == "BUY_STOP"
            ):

                trigger = (
                    ask_high
                    >= entry
                )

            elif (
                order_type
                == "SELL_LIMIT"
            ):

                trigger = (
                    bid_high
                    >= entry
                )

            elif (
                order_type
                == "SELL_STOP"
            ):

                trigger = (
                    bid_low
                    <= entry
                )

            if not trigger:
                if bar_close_time >= expiration:
                    return _finalize(
                        "EXPIRED", 0.0, None, expiration, expiration,
                        plan=plan, point=point, scenario=cost_scenario,
                        risk_amount_eur=risk_amount_eur, filled=False,
                    )
                continue

            if bar_close_time >= expiration:
                return _finalize(
                    "AMBIGUOUS_EXPIRY_BAR", None, bar_open_time, bar_close_time, bar_close_time,
                    plan=plan, point=point, scenario=cost_scenario, risk_amount_eur=risk_amount_eur,
                    filled=True,
                )

            filled = True

            fill_time = (
                bar_open_time
            )

            # ----------------------------------------------
            # MISMA VELA DEL FILL
            # ----------------------------------------------
            #
            # No conocemos el camino intrabar.
            # Si en la misma vela se toca SL o TP,
            # el resultado es ambiguo.

            if direction == "BUY":

                same_bar_exit = (
                    bid_low <= sl
                    or
                    bid_high >= tp
                )

            else:

                same_bar_exit = (
                    ask_high >= sl
                    or
                    ask_low <= tp
                )

            if same_bar_exit:

                return _finalize(
                    "AMBIGUOUS_ENTRY_BAR", None, fill_time, bar_close_time, bar_close_time,
                    plan=plan, point=point, scenario=cost_scenario,
                    risk_amount_eur=risk_amount_eur, filled=True,
                )

            continue

        # ==================================================
        # POSICION ABIERTA - VELAS POSTERIORES
        # ==================================================

        if direction == "BUY":

            stop_hit = (
                bid_low
                <= sl
            )

            tp_hit = (
                bid_high
                >= tp
            )

        else:

            stop_hit = (
                ask_high
                >= sl
            )

            tp_hit = (
                ask_low
                <= tp
            )

        if (
            stop_hit
            and
            tp_hit
        ):

                return _finalize(
                "AMBIGUOUS_BOTH", None, fill_time, bar_close_time, bar_close_time,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=True,
            )

        if stop_hit:

            return _finalize(
                "LOSS", -1.0, fill_time, bar_close_time, bar_close_time,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=True,
            )

        if tp_hit:

            return _finalize(
                "WIN", float(plan["target_rr"]), fill_time, bar_close_time, bar_close_time,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=True,
            )

    # Final del histórico.
    final_time = m5_history["bar_close"].iloc[-1]
    if not filled and final_time >= expiration:
        return _finalize(
            "EXPIRED", 0.0, None, expiration, expiration,
            plan=plan, point=point, scenario=cost_scenario,
            risk_amount_eur=risk_amount_eur, filled=False,
        )
    return _finalize(
        "OPEN_AT_END" if filled else "PENDING_AT_END", None, fill_time, final_time, final_time,
        plan=plan, point=point, scenario=cost_scenario,
        risk_amount_eur=risk_amount_eur, filled=filled,
    )
