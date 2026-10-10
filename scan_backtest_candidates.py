from datetime import datetime, timezone
from pathlib import Path
import json
import time

import pandas as pd

import argparse

from config.market_config import SYMBOLS

from config.backtest_config import (
    BACKTEST_EVALUATION_DAYS,
    QUALITY_THRESHOLDS,
    CURRENT_LIVE_THRESHOLD,
)

from src.historical_state import (
    prepare_symbol_history,
    build_historical_market_state,
)

from src.market_snapshot import (
    build_compact_snapshot,
)

from src.technical_evaluator import (
    evaluate_technical_state,
)

from src.session_gate import (
    build_session_state,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

OUTPUT_DIR = (
    BASE_DIR
    / "data"
    / "backtest"
)


def parse_utc(
    value,
):

    ts = pd.Timestamp(
        value
    )

    if ts.tzinfo is None:

        ts = ts.tz_localize(
            "UTC"
        )

    else:

        ts = ts.tz_convert(
            "UTC"
        )

    return ts

def technical_candidate(
    technical,
):

    return (
        technical["setup_quality"]
        >= CURRENT_LIVE_THRESHOLD

        and

        technical["alignment"]
        != "CONFLICT"

        and

        technical["bias"]
        in {
            "BULLISH",
            "BEARISH",
        }
    )


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        default=None,
    )

    parser.add_argument(
        "--end",
        default=None,
    )

    parser.add_argument(
        "--name",
        default="technical",
    )

    args = parser.parse_args()

    print("=" * 90)
    print(
        "BACKTEST - "
        "HISTORICAL TECHNICAL CANDIDATE SCAN"
    )
    print("=" * 90)

    print()
    print(
        "Modo: TECHNICAL_ONLY"
    )

    print(
        "Macro historico: NO DISPONIBLE"
    )

    print(
        "LLM: NO USADO"
    )

    print(
        "Umbral live actual:",
        CURRENT_LIVE_THRESHOLD,
    )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    all_rows = []

    total_start = (
        time.perf_counter()
    )

    # ======================================================
    # SYMBOLS
    # ======================================================

    for symbol in SYMBOLS:

        symbol_start = (
            time.perf_counter()
        )

        print()
        print("=" * 70)
        print(symbol)
        print("=" * 70)

        prepared = (
            prepare_symbol_history(
                symbol
            )
        )

        m15 = prepared[
            "M15"
        ]

        if args.end:

            evaluation_end = (
                parse_utc(
                    args.end
                )
            )

        else:

            evaluation_end = (
                m15[
                    "bar_close"
                ].iloc[-1]
            )

        if args.start:

            evaluation_start = (
                parse_utc(
                    args.start
                )
            )

        else:

            evaluation_start = (
                evaluation_end
                -
                pd.Timedelta(
                    days=
                        BACKTEST_EVALUATION_DAYS
                )
            )

        evaluation_bars = m15[
            (
                m15[
                    "bar_close"
                ]
                >= evaluation_start
            )
            &
            (
                m15[
                    "bar_close"
                ]
                <= evaluation_end
            )
        ].copy()

        print(
            "Evaluation start:",
            evaluation_start,
        )

        print(
            "Evaluation end:",
            evaluation_end,
        )

        print(
            "M15 closes:",
            len(
                evaluation_bars
            ),
        )

        symbol_rows = []

        # ==================================================
        # CADA CIERRE M15
        # ==================================================

        for counter, (
            _,
            m15_row,
        ) in enumerate(
            evaluation_bars.iterrows(),
            start=1,
        ):

            analysis_time = (
                m15_row[
                    "bar_close"
                ]
            )

            # ----------------------------------------------
            # SESSION
            # ----------------------------------------------

            session = (
                build_session_state(
                    analysis_time
                    .to_pydatetime()
                )
            )

            # Nuestra V1 live solo considera
            # London / New York.
            if not session[
                "trading_session_allowed"
            ]:

                continue

            # ----------------------------------------------
            # HISTORICAL MARKET STATE
            # ----------------------------------------------

            state = (
                build_historical_market_state(
                    symbol,
                    analysis_time,
                    prepared,
                )
            )

            snapshot = (
                build_compact_snapshot(
                    state,
                    candles_per_timeframe=4,
                )
            )

            technical = (
                evaluate_technical_state(
                    snapshot
                )
            )

            candidate = (
                technical_candidate(
                    technical
                )
            )

            sessions_open = (
                session[
                    "sessions_open"
                ]
            )

            row = {
                "symbol":
                    symbol,

                "analysis_time":
                    analysis_time,

                "session":
                    (
                        "+".join(
                            sessions_open
                        )
                        if sessions_open
                        else "NONE"
                    ),

                "bias":
                    technical[
                        "bias"
                    ],

                "overall_score":
                    technical[
                        "overall_score"
                    ],

                "alignment":
                    technical[
                        "alignment"
                    ],

                "setup_quality":
                    technical[
                        "setup_quality"
                    ],

                "candidate_65":
                    candidate,

                "m5_score":
                    technical[
                        "timeframe_scores"
                    ]["M5"],

                "m15_score":
                    technical[
                        "timeframe_scores"
                    ]["M15"],

                "h1_score":
                    technical[
                        "timeframe_scores"
                    ]["H1"],

                "h4_score":
                    technical[
                        "timeframe_scores"
                    ]["H4"],

                "d1_score":
                    technical[
                        "timeframe_scores"
                    ]["D1"],
            }

            symbol_rows.append(
                row
            )

            all_rows.append(
                row
            )

            if (
                counter % 1000
                == 0
            ):

                print(
                    f"Procesadas "
                    f"{counter:,} / "
                    f"{len(evaluation_bars):,}"
                )

        symbol_df = pd.DataFrame(
            symbol_rows
        )

        elapsed = (
            time.perf_counter()
            -
            symbol_start
        )

        print()
        print(
            "Velas dentro de sesiones:",
            len(symbol_df),
        )

        if not symbol_df.empty:

            candidates = (
                symbol_df[
                    "candidate_65"
                ].sum()
            )

            print(
                "Candidatos quality>=65:",
                int(
                    candidates
                ),
            )

            print(
                "LONG:",
                int(
                    (
                        symbol_df[
                            "candidate_65"
                        ]
                        &
                        (
                            symbol_df[
                                "bias"
                            ]
                            == "BULLISH"
                        )
                    ).sum()
                ),
            )

            print(
                "SHORT:",
                int(
                    (
                        symbol_df[
                            "candidate_65"
                        ]
                        &
                        (
                            symbol_df[
                                "bias"
                            ]
                            == "BEARISH"
                        )
                    ).sum()
                ),
            )

        print(
            "Tiempo:",
            round(
                elapsed,
                2,
            ),
            "s",
        )

    # ======================================================
    # DATAFRAME GLOBAL
    # ======================================================

    df = pd.DataFrame(
        all_rows
    )

    if df.empty:

        raise RuntimeError(
            "El scan no produjo datos."
        )

    csv_path = (
        OUTPUT_DIR
        / (
            f"{args.name}"
            "_candidate_scan.csv"
        )
    )

    df.to_csv(
        csv_path,
        index=False,
    )

    # ======================================================
    # SUMMARY POR THRESHOLD
    # ======================================================

    threshold_summary = {}

    for threshold in (
        QUALITY_THRESHOLDS
    ):

        eligible = (
            (
                df[
                    "setup_quality"
                ]
                >= threshold
            )
            &
            (
                df[
                    "alignment"
                ]
                != "CONFLICT"
            )
            &
            (
                df[
                    "bias"
                ]
                != "NEUTRAL"
            )
        )

        subset = df[
            eligible
        ]

        threshold_summary[
            str(threshold)
        ] = {
            "total":
                int(
                    len(subset)
                ),

            "bullish":
                int(
                    (
                        subset[
                            "bias"
                        ]
                        == "BULLISH"
                    ).sum()
                ),

            "bearish":
                int(
                    (
                        subset[
                            "bias"
                        ]
                        == "BEARISH"
                    ).sum()
                ),

            "by_symbol": {
                symbol:
                    int(
                        (
                            subset[
                                "symbol"
                            ]
                            == symbol
                        ).sum()
                    )

                for symbol
                in SYMBOLS
            },
        }

    # ======================================================
    # DISTRIBUCIONES
    # ======================================================

    summary = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "mode":
            "TECHNICAL_ONLY",

        "macro_history_included":
            False,

        "llm_included":
            False,

        "evaluation_days":
            BACKTEST_EVALUATION_DAYS,

        "current_live_threshold":
            CURRENT_LIVE_THRESHOLD,

        "rows_in_sessions":
            int(
                len(df)
            ),

        "thresholds":
            threshold_summary,

        "setup_quality_distribution": {
            "mean":
                round(
                    float(
                        df[
                            "setup_quality"
                        ].mean()
                    ),
                    2,
                ),

            "median":
                round(
                    float(
                        df[
                            "setup_quality"
                        ].median()
                    ),
                    2,
                ),

            "p75":
                round(
                    float(
                        df[
                            "setup_quality"
                        ].quantile(
                            0.75
                        )
                    ),
                    2,
                ),

            "p90":
                round(
                    float(
                        df[
                            "setup_quality"
                        ].quantile(
                            0.90
                        )
                    ),
                    2,
                ),

            "max":
                int(
                    df[
                        "setup_quality"
                    ].max()
                ),
        },
    }

    json_path = (
        OUTPUT_DIR
        / (
            f"{args.name}"
            "_candidate_summary.json"
        )
    )

    with open(
        json_path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            summary,
            file,
            indent=2,
            ensure_ascii=False,
        )

    total_elapsed = (
        time.perf_counter()
        -
        total_start
    )

    # ======================================================
    # PRINT SUMMARY
    # ======================================================

    print()
    print("=" * 90)
    print("RESUMEN")
    print("=" * 90)

    print(
        "Velas analizadas en sesiones:",
        len(df),
    )

    print()
    print(
        "Distribucion quality:"
    )

    for key, value in (
        summary[
            "setup_quality_distribution"
        ].items()
    ):

        print(
            f"  {key}: {value}"
        )

    print()
    print(
        "Candidatos por threshold:"
    )

    for threshold in (
        QUALITY_THRESHOLDS
    ):

        data = (
            threshold_summary[
                str(threshold)
            ]
        )

        print(
            f"  >= {threshold:2d}: "
            f"{data['total']:5d} "
            f"| long={data['bullish']:5d} "
            f"| short={data['bearish']:5d}"
        )

    print()
    print(
        "CSV:",
        csv_path,
    )

    print(
        "Summary:",
        json_path,
    )

    print(
        "Tiempo total:",
        round(
            total_elapsed,
            2,
        ),
        "s",
    )


if __name__ == "__main__":
    main()