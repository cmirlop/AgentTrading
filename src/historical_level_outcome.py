import pandas as pd


DEFAULT_HORIZON_MINUTES = 180

BREAK_BUFFER_ATR = 0.10

HOLD_CONFIRMATION_ATR = 0.10

CONFIRMATION_CLOSURES = 2

TOUCH_LOOKBACK_MINUTES = 15

def _utc(
    value,
):

    value = pd.Timestamp(
        value
    )

    if value.tzinfo is None:

        return value.tz_localize(
            "UTC"
        )

    return value.tz_convert(
        "UTC"
    )


def evaluate_level_thesis(
    thesis,
    levels,
    prepared_history,
    horizon_minutes=
        DEFAULT_HORIZON_MINUTES,
    active_level=None,
):

    analysis_time = _utc(
        levels[
            "analysis_time"
        ]
    )

    atr = float(
        levels[
            "reference_atr_m15"
        ]
    )

    if atr <= 0:

        return {
            "status":
                "INVALID",

            "error":
                "ATR_INVALID",
        }

    thesis_name = thesis.get(
        "thesis"
    )

    is_unclear = thesis_name == "UNCLEAR"

    if is_unclear and active_level not in {"SUPPORT", "RESISTANCE"}:
        return {
            "status": "NOT_EVALUATED",
            "reason": "NO_ACTIVE_LEVEL_FOR_UNCLEAR_THESIS",
        }

    if not is_unclear and thesis_name.startswith(
        "SUPPORT_"
    ):

        level_type = (
            "SUPPORT"
        )

        level = levels.get(
            "nearest_support"
        )

    elif not is_unclear and thesis_name.startswith(
        "RESISTANCE_"
    ):

        level_type = (
            "RESISTANCE"
        )

        level = levels.get(
            "nearest_resistance"
        )

    elif is_unclear and active_level in {"SUPPORT", "RESISTANCE"}:
        level_type = active_level
        level = levels.get(
            "nearest_support"
            if level_type == "SUPPORT"
            else "nearest_resistance"
        )
    else:

        return {
            "status":
                "INVALID",

            "error":
                "UNKNOWN_THESIS",
        }

    if level is None:

        return {
            "status":
                "INVALID",

            "error":
                "TARGET_LEVEL_MISSING",
        }

    outcome_timeframe = (
        "M15"
        if "M15" in prepared_history
        else "M5"
    )
    outcome_bars = prepared_history[
        outcome_timeframe
    ]

    start_index = (
        outcome_bars[
            "time"
        ].searchsorted(
            analysis_time,
            side="left",
        )
    )

    end_time = (
        analysis_time
        +
        pd.Timedelta(
            minutes=
                horizon_minutes
        )
    )

    end_index = (
        outcome_bars[
            "time"
        ].searchsorted(
            end_time,
            side="left",
        )
    )

    future = outcome_bars.iloc[
        start_index:end_index
    ].copy()

    if future.empty:

        return {
            "status":
                "INVALID",

            "error":
                f"NO_FUTURE_{outcome_timeframe}_DATA",
        }

    lookback_start = (
        analysis_time
        -
        pd.Timedelta(
            minutes=
                TOUCH_LOOKBACK_MINUTES
        )
    )

    recent = outcome_bars[
        (
            outcome_bars["bar_close"]
            >
            lookback_start
        )
        &
        (
            outcome_bars["bar_close"]
            <=
            analysis_time
        )
    ].copy()

    break_buffer = (
        atr
        *
        BREAK_BUFFER_ATR
    )

    hold_distance = (
        atr
        *
        HOLD_CONFIRMATION_ATR
    )

    if level_type == "SUPPORT":

        break_threshold = (
            float(
                level[
                    "low"
                ]
            )
            -
            break_buffer
        )

        hold_threshold = (
            float(
                level[
                    "high"
                ]
            )
            +
            hold_distance
        )

        expected_outcome = (
            None
            if is_unclear
            else (
                "HOLD"
                if thesis_name == "SUPPORT_HOLD"
                else "BREAK"
            )
        )

    else:

        break_threshold = (
            float(
                level[
                    "high"
                ]
            )
            +
            break_buffer
        )

        hold_threshold = (
            float(
                level[
                    "low"
                ]
            )
            -
            hold_distance
        )

        expected_outcome = (
            None
            if is_unclear
            else (
                "HOLD"
                if thesis_name == "RESISTANCE_HOLD"
                else "BREAK"
            )
        )

    level_low = float(
        level["low"]
    )

    level_high = float(
        level["high"]
    )

    level_touched_before_future = False

    touch_time = None

    for _, recent_row in (
        recent.iterrows()
    ):

        recent_low = float(
            recent_row["low"]
        )

        recent_high = float(
            recent_row["high"]
        )

        recent_touch = (
            recent_low
            <=
            level_high
            and
            recent_high
            >=
            level_low
        )

        if recent_touch:

            level_touched_before_future = True

            touch_time = (
                _utc(
                    recent_row["bar_close"]
                )
                .isoformat()
            )

    level_touched = (
        level_touched_before_future
    )

    actual_outcome = (
        "UNRESOLVED"
    )

    event_time = None

    event_bar = None

    break_close_streak = 0

    hold_close_streak = 0

    for _, row in (
        future.iterrows()
    ):

        bar_open = float(
            row["open"]
        )

        bar_open_time = _utc(
            row["time"]
        )

        bar_close_time = _utc(
            row["bar_close"]
        )

        low = float(
            row["low"]
        )

        high = float(
            row["high"]
        )

        close = float(
            row["close"]
        )

        touch_hit = (
            low
            <=
            level_high
            and
            high
            >=
            level_low
        )

        touch_at_open = (
            level_low
            <=
            bar_open
            <=
            level_high
        )

        touched_before_event = (
            level_touched
            or
            touch_at_open
        )

        if level_type == "SUPPORT":

            break_close = (
                close
                <=
                break_threshold
            )

            hold_close = (
                close
                >=
                hold_threshold
            )

        else:

            break_close = (
                close
                >=
                break_threshold
            )

            hold_close = (
                close
                <=
                hold_threshold
            )

        break_close_streak = (
            break_close_streak + 1
            if break_close
            else 0
        )

        hold_close_streak = (
            hold_close_streak + 1
            if hold_close
            else 0
        )

        break_hit = (
            break_close_streak
            >= CONFIRMATION_CLOSURES
        )

        hold_hit = (
            hold_close_streak
            >= CONFIRMATION_CLOSURES
        )

        if (
            break_hit
            and
            hold_hit
        ):

            actual_outcome = (
                "AMBIGUOUS"
            )

            event_time = (
                bar_close_time.isoformat()
            )

            event_bar = {
                "bar_open_time":
                    bar_open_time.isoformat(),

                "bar_close_time":
                    bar_close_time.isoformat(),

                "open":
                    bar_open,

                "high":
                    high,

                "low":
                    low,

                "close":
                    float(
                        row["close"]
                    ),
            }

            break

        if break_hit:

            actual_outcome = (
                "BREAK"
            )

            event_time = (
                bar_close_time.isoformat()
            )

            event_bar = {
                "bar_open_time":
                    bar_open_time.isoformat(),

                "bar_close_time":
                    bar_close_time.isoformat(),

                "open":
                    bar_open,

                "high":
                    high,

                "low":
                    low,

                "close":
                    float(
                        row["close"]
                    ),
            }

            break

        if hold_hit:

            if touched_before_event:

                actual_outcome = (
                    "HOLD"
                )

                event_time = (
                    bar_close_time.isoformat()
                )

                event_bar = {
                    "bar_open_time":
                        bar_open_time.isoformat(),

                    "bar_close_time":
                        bar_close_time.isoformat(),

                    "open":
                        bar_open,

                    "high":
                        high,

                    "low":
                        low,

                    "close":
                        float(
                            row["close"]
                        ),
                }

                break

            elif touch_hit:

                # La misma vela toca la zona y alcanza
                # confirmación de HOLD. Con OHLC no sabemos
                # qué ocurrió primero intrabar.
                actual_outcome = (
                    "AMBIGUOUS"
                )

                event_time = (
                    bar_close_time.isoformat()
                )

                event_bar = {
                    "bar_open_time":
                        bar_open_time.isoformat(),

                    "bar_close_time":
                        bar_close_time.isoformat(),

                    "open":
                        bar_open,

                    "high":
                        high,

                    "low":
                        low,

                    "close":
                        float(
                            row["close"]
                        ),
                }

                break

        if (
            touch_hit
            and
            not level_touched
        ):

            level_touched = True

            touch_time = (
                _utc(
                    row["bar_close"]
                )
                .isoformat()
            )

    min_low = float(
        future[
            "low"
        ].min()
    )

    max_high = float(
        future[
            "high"
        ].max()
    )

    if is_unclear:

        correct = None

    elif actual_outcome in {
        "HOLD",
        "BREAK",
    }:

        correct = (
            actual_outcome
            ==
            expected_outcome
        )

    else:

        correct = None

    return {
        "status":
            "OK",

        "analysis_time":
            analysis_time.isoformat(),

        "thesis":
            thesis_name,

        "level_type":
            level_type,

        "expected_outcome":
            expected_outcome,

        "actual_outcome":
            actual_outcome,

        "correct":
            correct,

        "horizon_minutes":
            horizon_minutes,

        "atr":
            round(
                atr,
                6,
            ),

        "break_buffer_atr":
            BREAK_BUFFER_ATR,

        "hold_confirmation_atr":
            HOLD_CONFIRMATION_ATR,

        "confirmation_closures":
            CONFIRMATION_CLOSURES,

        "outcome_confirmation_basis":
            f"TWO_CONSECUTIVE_{outcome_timeframe}_CLOSES",

        "touch_required_for_hold":
            True,

        "touch_lookback_minutes":
            TOUCH_LOOKBACK_MINUTES,

        "level_touched_before_future":
            level_touched_before_future,

        "touch_time":
            touch_time,

        "level_low":
            level[
                "low"
            ],

        "level_high":
            level[
                "high"
            ],

        "break_threshold":
            round(
                break_threshold,
                5,
            ),

        "hold_threshold":
            round(
                hold_threshold,
                5,
            ),

        "event_time":
            event_time,

        "event_bar":
            event_bar,

        "future_min_low":
            round(
                min_low,
                5,
            ),

        "future_max_high":
            round(
                max_high,
                5,
            ),
    }
