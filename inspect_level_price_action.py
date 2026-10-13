import pandas as pd

from src.historical_state import (
    prepare_symbol_history,
)


SYMBOL = "USDCAD"

TIMES = [
    "2026-04-08T07:30:00+00:00",
    "2026-04-08T08:00:00+00:00",
]


history = prepare_symbol_history(
    SYMBOL
)


for timestamp in TIMES:

    analysis_time = pd.Timestamp(
        timestamp
    )

    print(
        "\n"
        + "=" * 100
    )

    print(
        "ANALYSIS TIME:",
        timestamp,
    )

    print(
        "=" * 100
    )

    for timeframe, count in [
        ("M5", 10),
        ("M15", 6),
    ]:

        df = history[
            timeframe
        ]

        closed = df[
            df["bar_close"]
            <= analysis_time
        ].tail(
            count
        )

        print(
            f"\n{timeframe}"
        )

        print(
            closed[
                [
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "tick_volume",
                ]
            ].to_string(
                index=False
            )
        )