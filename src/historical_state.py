from pathlib import Path

import pandas as pd

from config.market_config import (
    TIMEFRAMES,
    RECENT_CANDLES,
)

from src.indicators import (
    add_indicators,
    get_indicator_snapshot,
    safe_number,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

MARKET_DIR = (
    BASE_DIR
    / "data"
    / "market"
)


TF_MINUTES = {
    "M5": 5,
    "M15": 15,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def prepare_symbol_history(
    symbol,
):

    prepared = {}

    for timeframe in TIMEFRAMES:

        path = (
            MARKET_DIR
            / symbol
            / f"{timeframe}.csv"
        )

        df = pd.read_csv(
            path
        )

        df["time"] = pd.to_datetime(
            df["time"],
            utc=True,
        )

        df = (
            df
            .sort_values("time")
            .drop_duplicates(
                subset=["time"],
                keep="last",
            )
            .reset_index(drop=True)
        )

        # Los indicadores actuales son causales:
        # rolling hacia atrás, EMA, RSI, ATR, etc.
        #
        # Por tanto pueden precomputarse una sola vez.
        df = add_indicators(
            df
        )

        duration = pd.Timedelta(
            minutes=
                TF_MINUTES[
                    timeframe
                ]
        )

        df[
            "bar_close"
        ] = (
            df["time"]
            + duration
        )

        prepared[
            timeframe
        ] = df

    return prepared


def _recent_candles(
    df,
    count,
):

    rows = df.tail(
        count
    )

    result = []

    for _, row in (
        rows.iterrows()
    ):

        result.append(
            {
                "time":
                    row[
                        "time"
                    ].isoformat(),

                "open":
                    safe_number(
                        row["open"]
                    ),

                "high":
                    safe_number(
                        row["high"]
                    ),

                "low":
                    safe_number(
                        row["low"]
                    ),

                "close":
                    safe_number(
                        row["close"]
                    ),

                "tick_volume":
                    safe_number(
                        row[
                            "tick_volume"
                        ]
                    ),

                "spread":
                    safe_number(
                        row["spread"]
                    ),
            }
        )

    return result


def build_historical_market_state(
    symbol,
    analysis_time,
    prepared_history,
):

    analysis_time = (
        pd.Timestamp(
            analysis_time
        )
    )

    if analysis_time.tzinfo is None:

        analysis_time = (
            analysis_time.tz_localize(
                "UTC"
            )
        )

    else:

        analysis_time = (
            analysis_time.tz_convert(
                "UTC"
            )
        )

    state = {
        "symbol":
            symbol,

        "analysis_time":
            analysis_time.isoformat(),

        "analysis_timeframe":
            "M15",

        "data_quality": {
            "status":
                "OK",

            "issues":
                [],
        },

        "timeframes": {},
    }

    for timeframe in TIMEFRAMES:

        full_df = (
            prepared_history[
                timeframe
            ]
        )

        # searchsorted evita recorrer todo el DataFrame
        # en cada timestamp histórico.

        end_index = (
            full_df[
                "bar_close"
            ].searchsorted(
                analysis_time,
                side="right",
            )
        )

        if end_index <= 0:

            raise RuntimeError(
                f"{symbol} {timeframe}: "
                "sin historia disponible "
                f"en {analysis_time}"
            )

        df = full_df.iloc[
            :end_index
        ]

        

        last = df.iloc[-1]

        state[
            "timeframes"
        ][timeframe] = {
            "bars_loaded":
                int(
                    len(df)
                ),

            "last_bar_open":
                last[
                    "time"
                ].isoformat(),

            "last_bar_close":
                last[
                    "bar_close"
                ].isoformat(),

            "data_quality": {
                "status":
                    "OK",

                "issues":
                    [],
            },

            "indicators":
                get_indicator_snapshot(
                    df
                ),

            "recent_candles":
                _recent_candles(
                    df,
                    RECENT_CANDLES[
                        timeframe
                    ],
                ),
        }

    return state