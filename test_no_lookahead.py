import numpy as np
import pandas as pd

from config.backtest_config import (
    BACKTEST_EVALUATION_DAYS,
    NO_LOOKAHEAD_TEST_SAMPLES,
)

from config.market_config import (
    SYMBOLS,
    TIMEFRAMES,
)

from src.historical_state import (
    TF_MINUTES,
    prepare_symbol_history,
    build_historical_market_state,
)


def main():

    print("=" * 80)
    print(
        "BACKTEST - NO LOOKAHEAD TEST"
    )
    print("=" * 80)

    total_checks = 0

    for symbol in SYMBOLS:

        print()
        print(symbol)

        prepared = (
            prepare_symbol_history(
                symbol
            )
        )

        m15 = prepared[
            "M15"
        ]

        end_time = (
            m15[
                "bar_close"
            ].iloc[-1]
        )

        start_time = (
            end_time
            -
            pd.Timedelta(
                days=
                    BACKTEST_EVALUATION_DAYS
            )
        )

        candidates = m15[
            (
                m15[
                    "bar_close"
                ]
                >= start_time
            )
            &
            (
                m15[
                    "bar_close"
                ]
                <= end_time
            )
        ]

        if len(candidates) < (
            NO_LOOKAHEAD_TEST_SAMPLES
        ):

            raise RuntimeError(
                f"{symbol}: "
                "no hay suficientes muestras"
            )

        indexes = np.linspace(
            0,
            len(candidates) - 1,
            NO_LOOKAHEAD_TEST_SAMPLES,
            dtype=int,
        )

        for index in indexes:

            analysis_time = (
                candidates.iloc[
                    index
                ][
                    "bar_close"
                ]
            )

            state = (
                build_historical_market_state(
                    symbol,
                    analysis_time,
                    prepared,
                )
            )

            for timeframe in TIMEFRAMES:

                tf_state = (
                    state[
                        "timeframes"
                    ][timeframe]
                )

                last_close = (
                    pd.Timestamp(
                        tf_state[
                            "last_bar_close"
                        ]
                    )
                )

                if (
                    last_close
                    >
                    analysis_time
                ):

                    raise AssertionError(
                        f"LOOKAHEAD {symbol} "
                        f"{timeframe}: "
                        f"{last_close} > "
                        f"{analysis_time}"
                    )

                duration = pd.Timedelta(
                    minutes=
                        TF_MINUTES[
                            timeframe
                        ]
                )

                for candle in (
                    tf_state[
                        "recent_candles"
                    ]
                ):

                    candle_open = (
                        pd.Timestamp(
                            candle["time"]
                        )
                    )

                    candle_close = (
                        candle_open
                        + duration
                    )

                    if (
                        candle_close
                        >
                        analysis_time
                    ):

                        raise AssertionError(
                            "LOOKAHEAD EN "
                            "RECENT_CANDLES: "
                            f"{symbol} "
                            f"{timeframe}"
                        )

                total_checks += 1

        # Comprobar warm-up de D1 al principio
        first_time = (
            candidates.iloc[0][
                "bar_close"
            ]
        )

        first_state = (
            build_historical_market_state(
                symbol,
                first_time,
                prepared,
            )
        )

        d1_ema200 = (
            first_state[
                "timeframes"
            ]["D1"][
                "indicators"
            ][
                "ema200"
            ]
        )

        print(
            " Evaluation start:",
            first_time,
        )

        print(
            " D1 EMA200 at start:",
            d1_ema200,
        )

        if d1_ema200 is None:

            raise AssertionError(
                f"{symbol}: "
                "D1 EMA200 sin warm-up"
            )

        print(
            " [OK] sin look-ahead"
        )

    print()
    print(
        "[OK] Checks realizados:",
        total_checks,
    )


if __name__ == "__main__":
    main()