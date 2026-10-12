import pandas as pd


DEFAULT_HORIZON_MINUTES = 180

BREAK_BUFFER_ATR = 0.10

HOLD_CONFIRMATION_ATR = 0.50


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

    if thesis_name == "UNCLEAR":

        return {
            "status":
                "NOT_EVALUATED",

            "reason":
                "UNCLEAR_THESIS",
        }

    if thesis_name.startswith(
        "SUPPORT_"
    ):

        level_type = (
            "SUPPORT"
        )

        level = levels.get(
            "nearest_support"
        )

    elif thesis_name.startswith(
        "RESISTANCE_"
    ):

        level_type = (
            "RESISTANCE"
        )

        level = levels.get(
            "nearest_resistance"
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

    m5 = prepared_history[
        "M5"
    ]

    start_index = (
        m5[
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
        m5[
            "time"
        ].searchsorted(
            end_time,
            side="left",
        )
    )

    future = m5.iloc[
        start_index:end_index
    ].copy()

    if future.empty:

        return {
            "status":
                "INVALID",

            "error":
                "NO_FUTURE_M5_DATA",
        }

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
            "HOLD"
            if thesis_name
            == "SUPPORT_HOLD"
            else "BREAK"
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
            "HOLD"
            if thesis_name
            == "RESISTANCE_HOLD"
            else "BREAK"
        )

    actual_outcome = (
        "UNRESOLVED"
    )

    event_time = None

    event_bar = None

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

    for _, row in (
        future.iterrows()
    ):

        low = float(
            row[
                "low"
            ]
        )

        high = float(
            row[
                "high"
            ]
        )

        if level_type == "SUPPORT":

            break_hit = (
                low
                <=
                break_threshold
            )

            hold_hit = (
                high
                >=
                hold_threshold
            )

        else:

            break_hit = (
                high
                >=
                break_threshold
            )

            hold_hit = (
                low
                <=
                hold_threshold
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
                _utc(
                    row[
                        "time"
                    ]
                )
                .isoformat()
            )

            event_bar = {
                "open":
                    float(
                        row[
                            "open"
                        ]
                    ),

                "high":
                    high,

                "low":
                    low,

                "close":
                    float(
                        row[
                            "close"
                        ]
                    ),
            }

            break

        if break_hit:

            actual_outcome = (
                "BREAK"
            )

            event_time = (
                _utc(
                    row[
                        "time"
                    ]
                )
                .isoformat()
            )

            event_bar = {
                "open":
                    float(
                        row[
                            "open"
                        ]
                    ),

                "high":
                    high,

                "low":
                    low,

                "close":
                    float(
                        row[
                            "close"
                        ]
                    ),
            }

            break

        if hold_hit:

            actual_outcome = (
                "HOLD"
            )

            event_time = (
                _utc(
                    row[
                        "time"
                    ]
                )
                .isoformat()
            )

            event_bar = {
                "open":
                    float(
                        row[
                            "open"
                        ]
                    ),

                "high":
                    high,

                "low":
                    low,

                "close":
                    float(
                        row[
                            "close"
                        ]
                    ),
            }

            break

    if actual_outcome in {
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