from pprint import pprint

from src.historical_state import (
    prepare_symbol_history,
)

from src.historical_level_outcome import (
    evaluate_level_thesis,
)

import pandas as pd

SYMBOL = "USDCAD"

history = prepare_symbol_history(
    SYMBOL
)


CASES = [
    {
        "name":
            "07:30 - antes BREAK",

        "levels": {
            "analysis_time":
                "2026-04-08T07:30:00+00:00",

            "reference_atr_m15":
                0.000646,

            "nearest_support": {
                "low":
                    1.38314,

                "high":
                    1.38356,
            },

            "nearest_resistance":
                None,
        },
    },

    {
        "name":
            "08:00 - antes HOLD",

        "levels": {
            "analysis_time":
                "2026-04-08T08:00:00+00:00",

            "reference_atr_m15":
                0.000600,

            "nearest_support": {
                "low":
                    1.38315,

                "high":
                    1.38333,
            },

            "nearest_resistance":
                None,
        },
    },
]


for case in CASES:

    analysis_time = pd.Timestamp(
        case["levels"][
            "analysis_time"
        ]
    )

    level = case["levels"][
        "nearest_support"
    ]

    recent = history["M5"][
        (
            history["M5"]["bar_close"]
            >
            analysis_time
            -
            pd.Timedelta(
                minutes=15
            )
        )
        &
        (
            history["M5"]["bar_close"]
            <=
            analysis_time
        )
    ].copy()

    recent["touch"] = (
        (
            recent["low"]
            <=
            level["high"]
        )
        &
        (
            recent["high"]
            >=
            level["low"]
        )
    )

    print(
        "\nRECENT TOUCH DEBUG"
    )

    print(
        recent[
            [
                "time",
                "bar_close",
                "high",
                "low",
                "touch",
            ]
        ].to_string(
            index=False
        )
    )

    result = evaluate_level_thesis(
        thesis={
            "thesis":
                "SUPPORT_HOLD",
        },

        levels=
            case["levels"],

        prepared_history=
            history,

        horizon_minutes=
            180,
    )

    print(
        "\n"
        + "=" * 100
    )

    print(
        case["name"]
    )

    print(
        "=" * 100
    )

    pprint(
        result,
        sort_dicts=False,
    )
