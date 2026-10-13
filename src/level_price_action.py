import pandas as pd


def _closed_bars(
    df,
    analysis_time,
    count,
):

    analysis_time = pd.Timestamp(
        analysis_time
    )

    bars = df[
        df["bar_close"]
        <= analysis_time
    ].tail(
        count
    )

    return bars.copy()


def _bar_metrics(
    row,
    atr,
):

    candle_range = float(
        row["high"]
        -
        row["low"]
    )

    body = float(
        abs(
            row["close"]
            -
            row["open"]
        )
    )

    upper_wick = float(
        row["high"]
        -
        max(
            row["open"],
            row["close"],
        )
    )

    lower_wick = float(
        min(
            row["open"],
            row["close"],
        )
        -
        row["low"]
    )

    if candle_range > 0:

        close_location = float(
            (
                row["close"]
                -
                row["low"]
            )
            /
            candle_range
        )

        body_fraction = float(
            body
            /
            candle_range
        )

        upper_wick_fraction = float(
            upper_wick
            /
            candle_range
        )

        lower_wick_fraction = float(
            lower_wick
            /
            candle_range
        )

    else:

        close_location = 0.5
        body_fraction = 0.0
        upper_wick_fraction = 0.0
        lower_wick_fraction = 0.0

    return {
        "body_atr":
            round(
                body / atr,
                3,
            ),

        "range_atr":
            round(
                candle_range / atr,
                3,
            ),

        "body_fraction":
            round(
                body_fraction,
                3,
            ),

        "upper_wick_fraction":
            round(
                upper_wick_fraction,
                3,
            ),

        "lower_wick_fraction":
            round(
                lower_wick_fraction,
                3,
            ),

        "close_location":
            round(
                close_location,
                3,
            ),
    }


def _timeframe_metrics(
    bars,
    level_low,
    level_high,
    atr,
    active_level,
):

    if bars.empty:

        return {}

    closes = bars["close"]
    opens = bars["open"]

    first_close = float(
        closes.iloc[0]
    )

    last_close = float(
        closes.iloc[-1]
    )

    if active_level == "SUPPORT":

        approach_move_atr = (
            first_close
            -
            last_close
        ) / atr

        toward_mask = (
            closes
            <
            opens
        )

        through_mask = (
            closes
            <
            level_low
        )

        max_breach = max(
            0.0,
            (
                level_low
                -
                float(
                    bars["low"].min()
                )
            )
            /
            atr,
        )

        distance_from_zone = (
            last_close
            -
            level_high
        ) / atr

    else:

        approach_move_atr = (
            last_close
            -
            first_close
        ) / atr

        toward_mask = (
            closes
            >
            opens
        )

        through_mask = (
            closes
            >
            level_high
        )

        max_breach = max(
            0.0,
            (
                float(
                    bars["high"].max()
                )
                -
                level_high
            )
            /
            atr,
        )

        distance_from_zone = (
            level_low
            -
            last_close
        ) / atr

    touch_mask = (
        (bars["low"] <= level_high)
        &
        (bars["high"] >= level_low)
    )

    inside_close_mask = (
        (closes >= level_low)
        &
        (closes <= level_high)
    )

    recent_volume = (
        bars["tick_volume"]
        .tail(3)
        .mean()
    )

    earlier_volume = (
        bars["tick_volume"]
        .iloc[:-3]
        .mean()
        if len(bars) > 3
        else None
    )

    if (
        earlier_volume is not None
        and
        earlier_volume > 0
    ):

        volume_ratio = (
            recent_volume
            /
            earlier_volume
        )

    else:

        volume_ratio = None

    last_bar = _bar_metrics(
        bars.iloc[-1],
        atr,
    )

    return {
        "bars":
            int(
                len(bars)
            ),

        "approach_move_atr":
            round(
                float(
                    approach_move_atr
                ),
                3,
            ),

        "bars_toward_level":
            int(
                toward_mask.sum()
            ),

        "zone_touches":
            int(
                touch_mask.sum()
            ),

        "closes_inside_zone":
            int(
                inside_close_mask.sum()
            ),

        "closes_through_level":
            int(
                through_mask.sum()
            ),

        "max_breach_atr":
            round(
                float(
                    max_breach
                ),
                3,
            ),

        "last_close_distance_from_zone_atr":
            round(
                float(
                    distance_from_zone
                ),
                3,
            ),

        "recent_volume_ratio":
            (
                round(
                    float(
                        volume_ratio
                    ),
                    3,
                )
                if volume_ratio
                is not None
                else None
            ),

        "last_bar":
            last_bar,
    }


def build_level_price_action(
    prepared_history,
    analysis_time,
    level_facts,
    active_level,
):

    atr = float(
        level_facts[
            "reference_atr_m15"
        ]
    )

    level = level_facts[
        active_level.lower()
    ]

    level_low = float(
        level["low"]
    )

    level_high = float(
        level["high"]
    )

    m5 = _closed_bars(
        prepared_history["M5"],
        analysis_time,
        12,
    )

    m15 = _closed_bars(
        prepared_history["M15"],
        analysis_time,
        6,
    )

    return {
        "active_level":
            active_level,

        "level_low":
            level_low,

        "level_high":
            level_high,

        "atr_m15":
            atr,

        "M5":
            _timeframe_metrics(
                m5,
                level_low,
                level_high,
                atr,
                active_level,
            ),

        "M15":
            _timeframe_metrics(
                m15,
                level_low,
                level_high,
                atr,
                active_level,
            ),
    }

def build_level_price_action_evidence(
    price_action,
):

    evidence = []

    for timeframe in [
        "M5",
        "M15",
    ]:

        metrics = price_action.get(
            timeframe,
            {},
        )

        if not metrics:
            continue

        prefix = (
            f"{timeframe}_LEVEL_"
        )

        approach = float(
            metrics.get(
                "approach_move_atr",
                0.0,
            )
        )

        touches = int(
            metrics.get(
                "zone_touches",
                0,
            )
        )

        closes_inside = int(
            metrics.get(
                "closes_inside_zone",
                0,
            )
        )

        closes_through = int(
            metrics.get(
                "closes_through_level",
                0,
            )
        )

        max_breach = float(
            metrics.get(
                "max_breach_atr",
                0.0,
            )
        )

        distance = float(
            metrics.get(
                "last_close_distance_from_zone_atr",
                0.0,
            )
        )


        # Movimiento neto hacia / lejos del nivel.
        if approach >= 0.75:

            evidence.append(
                prefix
                + "APPROACH_STRONG"
            )

        elif approach >= 0.25:

            evidence.append(
                prefix
                + "APPROACH_MODERATE"
            )

        elif approach <= -0.25:

            evidence.append(
                prefix
                + "MOVING_AWAY"
            )


        # Interacción reciente con la zona.
        if touches == 0:

            evidence.append(
                prefix
                + "UNTESTED_RECENTLY"
            )

        elif touches >= 3:

            evidence.append(
                prefix
                + "REPEATED_TESTS"
            )

        else:

            evidence.append(
                prefix
                + "RECENT_TOUCH"
            )


        if closes_inside > 0:

            evidence.append(
                prefix
                + "CLOSE_INSIDE_ZONE"
            )


        if closes_through > 0:

            evidence.append(
                prefix
                + "CLOSE_THROUGH_LEVEL"
            )


        if closes_through >= 2:

            evidence.append(
                prefix
                + "MULTIPLE_CLOSES_THROUGH_LEVEL"
            )


        if max_breach >= 0.25:

            evidence.append(
                prefix
                + "MEANINGFUL_BREACH"
            )


        # Posición actual respecto a la zona.
        if distance >= 0.0:

            evidence.append(
                prefix
                + "CURRENTLY_PRE_BREAK_SIDE"
            )

        else:

            evidence.append(
                prefix
                + "CURRENTLY_BREAK_SIDE"
            )


    return list(
        dict.fromkeys(
            evidence
        )
    )