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


def _settlement(result, gross_r, fill_time, exit_time, release_time, *,
                plan, point, scenario, risk_amount_eur, filled):
    costs = COST_SCENARIOS[scenario]
    closed = result in {"WIN", "LOSS"}
    sides = 2 if closed else 1 if filled else 0
    volume = float(plan.get("volume", LOT_SIZE))
    commission_factor = 1.0 if closed else 0.5 if filled else 0.0
    commission_eur = costs["commission_eur_per_lot_round_turn"] * volume * commission_factor
    slippage_price = costs["slippage_points_per_side"] * point * sides
    risk_distance = abs(float(plan["entry"]) - float(plan["stop_loss"]))
    slippage_r = slippage_price / risk_distance if risk_distance > 0 else None
    commission_r = commission_eur / float(risk_amount_eur) if risk_amount_eur and float(risk_amount_eur) > 0 else (0.0 if commission_eur == 0 else None)
    cost_r = None if slippage_r is None or commission_r is None else slippage_r + commission_r
    net_r = gross_r - cost_r if gross_r is not None and cost_r is not None else None
    cost_eur = commission_eur + (slippage_r * float(risk_amount_eur) if slippage_r is not None and risk_amount_eur else 0.0)
    return {
        "result": result,
        "r": gross_r,
        "gross_r": gross_r,
        "net_r": net_r,
        "transaction_cost_r": cost_r,
        "transaction_cost_eur": round(cost_eur, 8) if risk_amount_eur is not None or cost_eur == 0 else None,
        "commission_eur": round(commission_eur, 8),
        "slippage_points": costs["slippage_points_per_side"] * sides,
        "spread_model": "HISTORICAL_BID_ASK_EMBEDDED",
        "cost_scenario": scenario,
        "assumptions_unverified": costs["assumptions_unverified"],
        "fill_time": fill_time,
        "exit_time": exit_time,
        "release_time": release_time,
    }


def simulate_trade(
    plan,
    signal_time,
    m5_history,
    cost_scenario="BASE",
    risk_amount_eur=None,
):

    cost_scenario = str(cost_scenario).upper()
    if cost_scenario not in COST_SCENARIOS:
        raise ValueError("UNKNOWN_COST_SCENARIO")

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
        return _settlement(
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
            return _settlement(
                "EXPIRED", 0.0, None, expiration, expiration,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=False,
            )

        if pd.isna(bar["spread"]):
            raise ValueError("HISTORICAL_SPREAD_UNAVAILABLE")

        spread_price = (
            float(
                bar["spread"]
            )
            * point
            * COST_SCENARIOS[cost_scenario]["historical_spread_multiplier"]
        )
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
                continue

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

                return _settlement(
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

            return _settlement(
                "AMBIGUOUS_BOTH", None, fill_time, bar_close_time, bar_close_time,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=True,
            )

        if stop_hit:

            return _settlement(
                "LOSS", -1.0, fill_time, bar_close_time, bar_close_time,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=True,
            )

        if tp_hit:

            return _settlement(
                "WIN", float(plan["target_rr"]), fill_time, bar_close_time, bar_close_time,
                plan=plan, point=point, scenario=cost_scenario,
                risk_amount_eur=risk_amount_eur, filled=True,
            )

    # Final del histórico.
    final_time = m5_history["bar_close"].iloc[-1]
    return _settlement(
        "OPEN_AT_END" if filled else "EXPIRED_AT_END", None, fill_time, final_time, final_time,
        plan=plan, point=point, scenario=cost_scenario,
        risk_amount_eur=risk_amount_eur, filled=filled,
    )
