from datetime import datetime, timezone
from pathlib import Path
import json

import pandas as pd

import argparse

from config.market_config import (
    SYMBOLS,
)

from config.backtest_config import (
    STATEFUL_THRESHOLDS,
    STATEFUL_ENTRY_MODES,
)

from config.risk_config import (
    MAX_OPEN_POSITIONS,
    MAX_LOSS_PER_TRADE_EUR,
)

from src.historical_state import (
    prepare_symbol_history,
    build_historical_market_state,
)

from src.historical_trade_planner import (
    build_historical_trade_plan,
)

from src.historical_execution import (
    simulate_trade,
)

from src.historical_risk import (
    estimate_loss_eur,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

BACKTEST_DIR = (
    BASE_DIR
    / "data"
    / "backtest"
)

# SCAN_PATH = (
#     BACKTEST_DIR
#     / "technical_candidate_scan.csv"
# )


def max_drawdown(
    values,
):

    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for value in values:

        equity += float(value)

        peak = max(
            peak,
            equity,
        )

        max_dd = max(
            max_dd,
            peak - equity,
        )

    return max_dd


def summarize(
    trades,
):

    df = pd.DataFrame(
        trades
    )

    if df.empty:

        return {
            "signals_taken": 0,
            "resolved": 0,
            "gross_pnl_eur": 0.0,
        }

    resolved = df[
        df["result"].isin(
            [
                "WIN",
                "LOSS",
            ]
        )
    ].copy()

    resolved = resolved.sort_values(
        "exit_time"
    )

    wins = resolved[
        resolved["result"]
        == "WIN"
    ]

    losses = resolved[
        resolved["result"]
        == "LOSS"
    ]

    gross_profit = float(
        wins[
            "gross_pnl_eur"
        ].sum()
    )

    gross_loss = abs(
        float(
            losses[
                "gross_pnl_eur"
            ].sum()
        )
    )

    gross_pnl = float(
        resolved[
            "gross_pnl_eur"
        ].sum()
    )

    return {
        "signals_taken":
            int(
                len(df)
            ),

        "resolved":
            int(
                len(resolved)
            ),

        "wins":
            int(
                len(wins)
            ),

        "losses":
            int(
                len(losses)
            ),

        "expired":
            int(
                (
                    df["result"]
                    == "EXPIRED"
                ).sum()
            ),

        "ambiguous":
            int(
                df["result"].isin(
                    [
                        "AMBIGUOUS_ENTRY_BAR",
                        "AMBIGUOUS_BOTH",
                    ]
                ).sum()
            ),

        "win_rate":
            (
                round(
                    len(wins)
                    / len(resolved),
                    4,
                )
                if len(resolved)
                else None
            ),

        "gross_pnl_eur":
            round(
                gross_pnl,
                2,
            ),

        "expectancy_eur":
            (
                round(
                    gross_pnl
                    / len(resolved),
                    3,
                )
                if len(resolved)
                else None
            ),

        "profit_factor_eur":
            (
                round(
                    gross_profit
                    / gross_loss,
                    3,
                )
                if gross_loss > 0
                else None
            ),

        "max_drawdown_eur":
            (
                round(
                    max_drawdown(
                        resolved[
                            "gross_pnl_eur"
                        ].tolist()
                    ),
                    2,
                )
                if len(resolved)
                else None
            ),
    }


def run_scenario(
    threshold,
    mode,
    signals,
    prepared,
    period_end,
):

    eligible = signals[
        (
            signals[
                "setup_quality"
            ]
            >= threshold
        )
        &
        (
            signals[
                "alignment"
            ]
            != "CONFLICT"
        )
        &
        (
            signals[
                "bias"
            ]
            != "NEUTRAL"
        )
    ].copy()


    # Ninguna ejecución puede utilizar
    # velas posteriores al periodo evaluado.

    m5_by_symbol = {}

    for symbol in SYMBOLS:

        m5 = (
            prepared[
                symbol
            ]["M5"]
        )

        m5_by_symbol[
            symbol
        ] = m5[
            m5[
                "bar_close"
            ]
            <= period_end
        ].copy()

    # ------------------------------------------------------
    # IMPORTANTE:
    # varias parejas pueden producir señal exactamente
    # en el mismo cierre M15.
    #
    # No usamos el orden accidental del CSV.
    # Priorizamos:
    #
    # 1) mayor setup_quality
    # 2) mayor fuerza absoluta del score
    # 3) símbolo como desempate determinista
    # ------------------------------------------------------

    eligible[
        "abs_score"
    ] = (
        eligible[
            "overall_score"
        ].abs()
    )

    eligible = eligible.sort_values(
        [
            "analysis_time",
            "setup_quality",
            "abs_score",
            "symbol",
        ],
        ascending=[
            True,
            False,
            False,
            True,
        ],
    )

    # symbol -> release_time
    active = {}

    trades = []

    skipped_symbol_busy = 0
    skipped_global_slots = 0
    plan_rejections = 0
    risk_rejections = 0

    for _, signal in (
        eligible.iterrows()
    ):

        signal_time = (
            signal[
                "analysis_time"
            ]
        )

        # ==============================================
        # LIBERAR SLOTS
        # ==============================================

        expired_symbols = [
            symbol
            for symbol, release_time
            in active.items()
            if release_time
            <= signal_time
        ]

        for symbol in (
            expired_symbols
        ):

            del active[
                symbol
            ]

        symbol = (
            signal[
                "symbol"
            ]
        )

        # ==============================================
        # UNA RESERVA POR SIMBOLO
        # ==============================================

        if symbol in active:

            skipped_symbol_busy += 1
            continue

        # ==============================================
        # MAXIMO GLOBAL
        # ==============================================

        if (
            len(active)
            >= MAX_OPEN_POSITIONS
        ):

            skipped_global_slots += 1
            continue

        direction = (
            "BUY"
            if signal[
                "bias"
            ]
            == "BULLISH"
            else "SELL"
        )

        # ==============================================
        # MARKET STATE
        # ==============================================

        state = (
            build_historical_market_state(
                symbol=symbol,
                analysis_time=
                    signal_time,
                prepared_history=
                    prepared[
                        symbol
                    ],
            )
        )

        # ==============================================
        # PLAN
        # ==============================================

        plan = (
            build_historical_trade_plan(
                symbol=symbol,
                direction=direction,
                mode=mode,
                market_state=state,
            )
        )

        if (
            plan["status"]
            != "VALID"
        ):

            plan_rejections += 1
            continue

        # ==============================================
        # RIESGO EN EUR
        # ==============================================

        estimated_loss = (
            estimate_loss_eur(
                plan=plan,
                analysis_time=
                    signal_time,
                prepared_history=
                    prepared,
            )
        )

        if (
            estimated_loss
            >
            MAX_LOSS_PER_TRADE_EUR
        ):

            risk_rejections += 1
            continue

        # ==============================================
        # EJECUCION HISTORICA
        # ==============================================

        execution = (
            simulate_trade(
                plan=plan,
                signal_time=signal_time,
                m5_history=
                    m5_by_symbol[
                        symbol
                    ],
            )
        )

        release_time = (
            execution[
                "release_time"
            ]
        )

        # Una pendiente ya reserva el slot.
        active[
            symbol
        ] = release_time

        r_value = (
            execution[
                "r"
            ]
        )

        pnl_eur = (
            r_value
            * estimated_loss
            if r_value is not None
            else None
        )

        trades.append(
            {
                "symbol":
                    symbol,

                "threshold":
                    threshold,

                "mode":
                    mode,

                "signal_time":
                    signal_time,

                "setup_quality":
                    int(
                        signal[
                            "setup_quality"
                        ]
                    ),

                "overall_score":
                    float(
                        signal[
                            "overall_score"
                        ]
                    ),

                "direction":
                    direction,

                "entry":
                    plan[
                        "entry"
                    ],

                "stop_loss":
                    plan[
                        "stop_loss"
                    ],

                "take_profit":
                    plan[
                        "take_profit"
                    ],

                "estimated_risk_eur":
                    estimated_loss,

                "result":
                    execution[
                        "result"
                    ],

                "r":
                    r_value,

                "gross_pnl_eur":
                    pnl_eur,

                "fill_time":
                    execution[
                        "fill_time"
                    ],

                "exit_time":
                    execution[
                        "exit_time"
                    ],

                "release_time":
                    release_time,
            }
        )

    summary = summarize(
        trades
    )

    summary[
        "skipped_symbol_busy"
    ] = skipped_symbol_busy

    summary[
        "skipped_global_slots"
    ] = skipped_global_slots

    summary[
        "plan_rejections"
    ] = plan_rejections

    summary[
        "risk_rejections"
    ] = risk_rejections

    summary[
        "by_symbol"
    ] = {
        symbol:
            int(
                sum(
                    1
                    for trade
                    in trades
                    if trade[
                        "symbol"
                    ]
                    == symbol
                )
            )

        for symbol
        in SYMBOLS
    }

    return (
        trades,
        summary,
    )


def main():


    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--scan",
        default="technical_candidate_scan.csv",
    )

    parser.add_argument(
        "--name",
        default="portfolio",
    )

    args = parser.parse_args()

    scan_path = (
        BACKTEST_DIR
        / args.scan
    )

    print("=" * 100)
    print(
        "BACKTEST - PORTFOLIO"
    )
    print("=" * 100)

    print()
    print(
        "Max slots globales:",
        MAX_OPEN_POSITIONS,
    )

    print(
        "Pendientes reservan slot: SI"
    )

    print(
        "Max 1 reserva por simbolo: SI"
    )

    print(
        "Macro historico: NO"
    )

    print(
        "LLM: NO"
    )

    print(
        "Comision/slippage: NO"
    )

    signals = pd.read_csv(
        scan_path,
        parse_dates=[
            "analysis_time"
        ],
    )

    signals[
        "analysis_time"
    ] = pd.to_datetime(
        signals[
            "analysis_time"
        ],
        utc=True,
    )

    period_start = (
        signals[
            "analysis_time"
        ].min()
    )

    period_end = (
        signals[
            "analysis_time"
        ].max()
    )

    print()
    print(
        "Scan:",
        scan_path,
    )

    print(
        "Periodo:",
        period_start,
        "->",
        period_end,
    )

    print()
    print(
        "Preparando historiales..."
    )

    prepared = {
        symbol:
            prepare_symbol_history(
                symbol
            )

        for symbol
        in SYMBOLS
    }

    all_trades = []
    summaries = []

    for threshold in (
        STATEFUL_THRESHOLDS
    ):

        for mode in (
            STATEFUL_ENTRY_MODES
        ):

            trades, summary = (
                run_scenario(
                    threshold=
                        threshold,

                    mode=
                        mode,

                    signals=
                        signals,

                    prepared=
                        prepared,

                    period_end=
                        period_end, 
                )
            )

            all_trades.extend(
                trades
            )

            row = {
                "threshold":
                    threshold,

                "mode":
                    mode,

                **{
                    key: value
                    for key, value
                    in summary.items()
                    if key
                    != "by_symbol"
                },

                "EURUSD_taken":
                    summary[
                        "by_symbol"
                    ]["EURUSD"],

                "USDCAD_taken":
                    summary[
                        "by_symbol"
                    ]["USDCAD"],

                "AUDUSD_taken":
                    summary[
                        "by_symbol"
                    ]["AUDUSD"],
            }

            summaries.append(
                row
            )

            print(
                f">={threshold} "
                f"{mode:<5} "
                f"| taken={summary['signals_taken']:4d} "
                f"| resolved={summary['resolved']:4d} "
                f"| win={summary['win_rate']} "
                f"| expEUR={summary['expectancy_eur']} "
                f"| pnlEUR={summary['gross_pnl_eur']} "
                f"| PF={summary['profit_factor_eur']} "
                f"| DD={summary['max_drawdown_eur']} "
                f"| globalBlocked="
                f"{summary['skipped_global_slots']:5d}"
            )

            print(
                "        "
                f"EURUSD={summary['by_symbol']['EURUSD']} "
                f"USDCAD={summary['by_symbol']['USDCAD']} "
                f"AUDUSD={summary['by_symbol']['AUDUSD']}"
            )

    trades_df = pd.DataFrame(
        all_trades
    )

    summary_df = pd.DataFrame(
        summaries
    )

    trades_path = (
        BACKTEST_DIR
        / f"{args.name}_trades.csv"
    )

    summary_path = (
        BACKTEST_DIR
        / f"{args.name}_summary.csv"
    )

    trades_df.to_csv(
        trades_path,
        index=False,
    )

    summary_df.to_csv(
        summary_path,
        index=False,
    )

    metadata = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "max_global_slots":
            MAX_OPEN_POSITIONS,

        "pending_orders_reserve_slot":
            True,

        "one_active_reservation_per_symbol":
            True,

        "simultaneous_signal_priority":
            [
                "setup_quality_desc",
                "abs_overall_score_desc",
                "symbol_asc",
            ],

        "macro_history_included":
            False,

        "llm_included":
            False,

        "commission_included":
            False,

        "slippage_included":
            False,

        "period_start":
            period_start.isoformat(),

        "period_end":
            period_end.isoformat(),

        "execution_capped_at_period_end":
            True,

        "source_scan":
            str(scan_path),
    }

    metadata_path = (
        BACKTEST_DIR
        / f"{args.name}_metadata.json"
    )

    with open(
        metadata_path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            metadata,
            file,
            indent=2,
        )

    print()
    print("=" * 100)
    print("GUARDADO")
    print("=" * 100)

    print(
        "Trades:",
        trades_path,
    )

    print(
        "Summary:",
        summary_path,
    )

    print(
        "Metadata:",
        metadata_path,
    )


if __name__ == "__main__":
    main()