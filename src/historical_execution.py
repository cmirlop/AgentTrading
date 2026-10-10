import pandas as pd

from config.execution_config import (
    PENDING_ORDER_EXPIRATION_MINUTES,
)

from src.historical_trade_planner import (
    SYMBOL_POINT,
)


def simulate_trade(
    plan,
    signal_time,
    m5_history,
):

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

        spread_price = (
            float(
                bar["spread"]
            )
            * point
        )

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

            if (
                bar_open_time
                >= expiration
            ):

                return {
                    "result":
                        "EXPIRED",

                    "r":
                        0.0,

                    "fill_time":
                        None,

                    "exit_time":
                        expiration,

                    "release_time":
                        expiration,
                }

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

                return {
                    "result":
                        "AMBIGUOUS_ENTRY_BAR",

                    "r":
                        None,

                    "fill_time":
                        fill_time,

                    "exit_time":
                        bar_close_time,

                    "release_time":
                        bar_close_time,
                }

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

            return {
                "result":
                    "AMBIGUOUS_BOTH",

                "r":
                    None,

                "fill_time":
                    fill_time,

                "exit_time":
                    bar_close_time,

                "release_time":
                    bar_close_time,
            }

        if stop_hit:

            return {
                "result":
                    "LOSS",

                "r":
                    -1.0,

                "fill_time":
                    fill_time,

                "exit_time":
                    bar_close_time,

                "release_time":
                    bar_close_time,
            }

        if tp_hit:

            return {
                "result":
                    "WIN",

                "r":
                    float(
                        plan[
                            "target_rr"
                        ]
                    ),

                "fill_time":
                    fill_time,

                "exit_time":
                    bar_close_time,

                "release_time":
                    bar_close_time,
            }

    # Final del histórico.
    return {
        "result":
            (
                "OPEN_AT_END"
                if filled
                else "EXPIRED_AT_END"
            ),

        "r":
            None,

        "fill_time":
            fill_time,

        "exit_time":
            m5_history[
                "bar_close"
            ].iloc[-1],

        "release_time":
            m5_history[
                "bar_close"
            ].iloc[-1],
    }