from config.trade_planner_config import (
    TARGET_RISK_REWARD,
    ATR_STOP_MULTIPLIER,
    ENTRY_BUFFER_ATR_MULTIPLIER,
    SWING_LOOKBACK_BARS,
    MIN_BUFFER_POINTS,
)


SYMBOL_POINT = {
    "EURUSD": 0.00001,
    "USDCAD": 0.00001,
    "AUDUSD": 0.00001,
}


def build_historical_trade_plan(
    symbol,
    direction,
    mode,
    market_state,
):

    point = SYMBOL_POINT[
        symbol
    ]

    m15 = market_state[
        "timeframes"
    ]["M15"]

    m5 = market_state[
        "timeframes"
    ]["M5"]

    indicators = m15[
        "indicators"
    ]

    m15_candles = m15[
        "recent_candles"
    ]

    if len(
        m15_candles
    ) < SWING_LOOKBACK_BARS:

        return {
            "status": "REJECTED",
            "error": "NOT_ENOUGH_M15_CANDLES",
        }

    atr = indicators[
        "atr14"
    ]

    ema20 = indicators[
        "ema20"
    ]

    if (
        atr is None
        or atr <= 0
        or ema20 is None
    ):

        return {
            "status": "REJECTED",
            "error": "INDICATORS_UNAVAILABLE",
        }

    recent = m15_candles[
        -SWING_LOOKBACK_BARS:
    ]

    recent_high = max(
        x["high"]
        for x in recent
    )

    recent_low = min(
        x["low"]
        for x in recent
    )

    # ======================================================
    # PRECIO DISPONIBLE EN ESE INSTANTE
    # ======================================================

    last_m5 = (
        m5[
            "recent_candles"
        ][-1]
    )

    bid_reference = float(
        last_m5["close"]
    )

    spread_points = float(
        last_m5.get(
            "spread",
            0,
        )
        or 0
    )

    spread_price = (
        spread_points
        * point
    )

    ask_reference = (
        bid_reference
        +
        spread_price
    )

    buffer_distance = max(
        MIN_BUFFER_POINTS
        * point,

        atr
        * ENTRY_BUFFER_ATR_MULTIPLIER,
    )

    # ======================================================
    # ENTRY
    # ======================================================

    if mode == "LIMIT":

        if direction == "BUY":

            order_type = (
                "BUY_LIMIT"
            )

            entry = float(
                ema20
            )

            if (
                entry
                >= ask_reference
            ):

                return {
                    "status":
                        "REJECTED",

                    "error":
                        "BUY_LIMIT_NOT_BELOW_MARKET",
                }

        elif direction == "SELL":

            order_type = (
                "SELL_LIMIT"
            )

            entry = float(
                ema20
            )

            if (
                entry
                <= bid_reference
            ):

                return {
                    "status":
                        "REJECTED",

                    "error":
                        "SELL_LIMIT_NOT_ABOVE_MARKET",
                }

        else:

            raise ValueError(
                direction
            )

    elif mode == "STOP":

        if direction == "BUY":

            order_type = (
                "BUY_STOP"
            )

            entry = max(
                recent_high
                + buffer_distance,

                ask_reference
                + point,
            )

        elif direction == "SELL":

            order_type = (
                "SELL_STOP"
            )

            entry = min(
                recent_low
                - buffer_distance,

                bid_reference
                - point,
            )

        else:

            raise ValueError(
                direction
            )

    else:

        raise ValueError(
            mode
        )

    # ======================================================
    # STOP LOSS
    # ======================================================

    atr_stop_distance = (
        atr
        * ATR_STOP_MULTIPLIER
    )

    if direction == "BUY":

        technical_stop = (
            recent_low
            - buffer_distance
        )

        atr_stop = (
            entry
            - atr_stop_distance
        )

        stop_loss = min(
            technical_stop,
            atr_stop,
        )

    else:

        technical_stop = (
            recent_high
            + buffer_distance
        )

        atr_stop = (
            entry
            + atr_stop_distance
        )

        stop_loss = max(
            technical_stop,
            atr_stop,
        )

    risk_distance = abs(
        entry
        - stop_loss
    )

    if risk_distance <= 0:

        return {
            "status":
                "REJECTED",

            "error":
                "INVALID_RISK_DISTANCE",
        }

    # ======================================================
    # TP
    # ======================================================

    if direction == "BUY":

        take_profit = (
            entry
            +
            risk_distance
            * TARGET_RISK_REWARD
        )

    else:

        take_profit = (
            entry
            -
            risk_distance
            * TARGET_RISK_REWARD
        )

    digits = 5

    return {
        "status":
            "VALID",

        "symbol":
            symbol,

        "direction":
            direction,

        "mode":
            mode,

        "order_type":
            order_type,

        "entry":
            round(
                entry,
                digits,
            ),

        "stop_loss":
            round(
                stop_loss,
                digits,
            ),

        "take_profit":
            round(
                take_profit,
                digits,
            ),

        "target_rr":
            TARGET_RISK_REWARD,

        "risk_distance":
            risk_distance,

        "spread_points_at_signal":
            spread_points,

        "bid_reference":
            bid_reference,

        "ask_reference":
            ask_reference,

        "m15_atr":
            atr,

        "recent_high":
            recent_high,

        "recent_low":
            recent_low,
    }