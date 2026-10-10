import json
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path

import pandas as pd

from config.market_config import (
    SYMBOLS,
)

from config.backtest_config import (
    STATEFUL_THRESHOLDS,
    STATEFUL_ENTRY_MODES,
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

from config.risk_config import (
    MAX_LOSS_PER_TRADE_EUR,
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

SCAN_PATH = (
    BACKTEST_DIR
    / "technical_candidate_scan.csv"
)


def calculate_max_drawdown_r(
    values,
):

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for value in values:

        equity += value

        peak = max(
            peak,
            equity,
        )

        drawdown = (
            peak
            - equity
        )

        max_drawdown = max(
            max_drawdown,
            drawdown,
        )

    return max_drawdown


def summarize(
    trades,
):

    df = pd.DataFrame(
        trades
    )

    if df.empty:

        return {
            "signals_taken": 0,
        }

    counts = (
        df[
            "result"
        ]
        .value_counts()
        .to_dict()
    )

    resolved = df[
        df[
            "result"
        ].isin(
            [
                "WIN",
                "LOSS",
            ]
        )
    ].copy()

    gross_pnl_eur = float(
        resolved[
            "gross_pnl_eur"
        ].sum()
    )

    max_drawdown_eur = (
        calculate_max_drawdown_r(
            resolved[
                "gross_pnl_eur"
            ].tolist()
        )
    )

    wins = int(
        (
            resolved[
                "result"
            ]
            == "WIN"
        ).sum()
    )

    losses = int(
        (
            resolved[
                "result"
            ]
            == "LOSS"
        ).sum()
    )

    total_resolved = (
        wins
        +
        losses
    )

    if total_resolved:

        win_rate = (
            wins
            / total_resolved
        )

        expectancy_r = float(
            resolved["r"].mean()
        )

        total_r = float(
            resolved["r"].sum()
        )

        max_drawdown_r = (
            calculate_max_drawdown_r(
                resolved[
                    "r"
                ].tolist()
            )
        )

    else:

        win_rate = None
        expectancy_r = None
        total_r = 0.0
        max_drawdown_r = None

    gross_win_r = float(
        resolved.loc[
            resolved["r"] > 0,
            "r",
        ].sum()
    )

    gross_loss_r = abs(
        float(
            resolved.loc[
                resolved["r"] < 0,
                "r",
            ].sum()
        )
    )

    profit_factor = (
        gross_win_r
        / gross_loss_r
        if gross_loss_r > 0
        else None
    )

    return {
        "signals_taken":
            int(
                len(df)
            ),

        "wins":
            wins,

        "losses":
            losses,

        "expired":
            int(
                counts.get(
                    "EXPIRED",
                    0,
                )
            ),

        "ambiguous_entry_bar":
            int(
                counts.get(
                    "AMBIGUOUS_ENTRY_BAR",
                    0,
                )
            ),

        "ambiguous_both":
            int(
                counts.get(
                    "AMBIGUOUS_BOTH",
                    0,
                )
            ),

        "open_at_end":
            int(
                counts.get(
                    "OPEN_AT_END",
                    0,
                )
            ),

        "resolved_trades":
            total_resolved,

        "win_rate":
            (
                round(
                    win_rate,
                    4,
                )
                if win_rate
                is not None
                else None
            ),

        "expectancy_r":
            (
                round(
                    expectancy_r,
                    4,
                )
                if expectancy_r
                is not None
                else None
            ),

        "total_r":
            round(
                total_r,
                2,
            ),

        "profit_factor":
            (
                round(
                    profit_factor,
                    3,
                )
                if profit_factor
                is not None
                else None
            ),

        "max_drawdown_r":
            (
                round(
                    max_drawdown_r,
                    2,
                )
                if max_drawdown_r
                is not None
                else None
            ),

        "gross_pnl_eur":
            round(
                gross_pnl_eur,
                2,
            ),

        "max_drawdown_eur":
            round(
                max_drawdown_eur,
                2,
            ),
    }


def run_scenario(
    symbol,
    threshold,
    mode,
    signal_df,
    prepared,
    prepared_by_symbol,
):

    rows = (
        signal_df[
            (
                signal_df[
                    "symbol"
                ]
                == symbol
            )
            &
            (
                signal_df[
                    "setup_quality"
                ]
                >= threshold
            )
            &
            (
                signal_df[
                    "alignment"
                ]
                != "CONFLICT"
            )
            &
            (
                signal_df[
                    "bias"
                ]
                != "NEUTRAL"
            )
        ]
        .sort_values(
            "analysis_time"
        )
    )

    busy_until = None

    trades = []

    plan_rejections = 0

    m5_history = (
        prepared["M5"]
    )

    risk_rejections = 0

    for _, signal in (
        rows.iterrows()
    ):

        signal_time = (
            signal[
                "analysis_time"
            ]
        )

        if (
            busy_until
            is not None
            and
            signal_time
            < busy_until
        ):

            continue

        direction = (
            "BUY"
            if signal["bias"]
            == "BULLISH"
            else "SELL"
        )

        state = (
            build_historical_market_state(
                symbol,
                signal_time,
                prepared,
            )
        )

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

        estimated_loss_eur = (
            estimate_loss_eur(
                plan=plan,
                analysis_time=
                    signal_time,
                prepared_history=
                    prepared_by_symbol,
            )
        )

        if (
            estimated_loss_eur
            >
            MAX_LOSS_PER_TRADE_EUR
        ):

            risk_rejections += 1
            continue

        plan[
            "estimated_loss_eur"
        ] = estimated_loss_eur

        execution = (
            simulate_trade(
                plan=plan,
                signal_time=
                    signal_time,
                m5_history=
                    m5_history,
            )
        )

        busy_until = (
            execution[
                "release_time"
            ]
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

                "bias":
                    signal[
                        "bias"
                    ],

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

                "result":
                    execution[
                        "result"
                    ],

                "r":
                    execution[
                        "r"
                    ],

                "fill_time":
                    execution[
                        "fill_time"
                    ],

                "exit_time":
                    execution[
                        "exit_time"
                    ],

                "estimated_risk_eur":
                    estimated_loss_eur,
    
                "gross_pnl_eur":
                    (
                        execution["r"]
                        * estimated_loss_eur
                        if execution["r"]
                        is not None
                        else None
                    ),
            }

            
        )

    summary = summarize(
        trades
    )

    summary[
        "plan_rejections"
    ] = plan_rejections

    summary[
        "risk_rejections"
    ] = risk_rejections

    return (
        trades,
        summary,
    )


def main():

    print("=" * 90)
    print(
        "BACKTEST - STATEFUL EXECUTION"
    )
    print("=" * 90)

    print()
    print(
        "Modo: TECHNICAL_ONLY"
    )

    print(
        "LLM: NO"
    )

    print(
        "Macro historico: NO"
    )

    print(
        "Resultados monetarios: NO"
    )

    print(
        "Unidad de rendimiento: R"
    )


    print()
    print(
        "Preparando historiales..."
    )

    prepared_by_symbol = {
        symbol:
            prepare_symbol_history(
                symbol
            )

        for symbol
        in SYMBOLS
    }

    signal_df = pd.read_csv(
        SCAN_PATH,
        parse_dates=[
            "analysis_time"
        ],
    )

    all_trades = []

    summaries = []

    for symbol in SYMBOLS:

        print()
        print("=" * 70)
        print(symbol)
        print("=" * 70)

        prepared = (
            prepared_by_symbol[
                symbol
            ]
        )

        for threshold in (
            STATEFUL_THRESHOLDS
        ):

            for mode in (
                STATEFUL_ENTRY_MODES
            ):

                trades, summary = (
                    run_scenario(
                        symbol=symbol,
                        threshold=threshold,
                        mode=mode,
                        signal_df=signal_df,
                        prepared=prepared,
                        prepared_by_symbol=prepared_by_symbol,
                    )
                )

                all_trades.extend(
                    trades
                )

                row = {
                    "symbol":
                        symbol,

                    "threshold":
                        threshold,

                    "mode":
                        mode,

                    **summary,
                }

                summaries.append(
                    row
                )

                print(
                    f">={threshold} "
                    f"{mode:<5} "
                    f"| signals={summary.get('signals_taken', 0):4d} "
                    f"| resolved={summary.get('resolved_trades', 0):4d} "
                    f"| riskReject={summary.get('risk_rejections', 0):4d} "
                    f"| win={summary.get('win_rate')} "
                    f"| expR={summary.get('expectancy_r')} "
                    f"| totalR={summary.get('total_r')} "
                    f"| PF={summary.get('profit_factor')} "
                    f"| DD={summary.get('max_drawdown_r')} "
                    f"| grossEUR={summary.get('gross_pnl_eur')}"
                )

    # ======================================================
    # SAVE
    # ======================================================

    BACKTEST_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    trades_df = pd.DataFrame(
        all_trades
    )

    summary_df = pd.DataFrame(
        summaries
    )

    trades_path = (
        BACKTEST_DIR
        / "stateful_trades.csv"
    )

    summary_path = (
        BACKTEST_DIR
        / "stateful_summary.csv"
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

        "technical_only":
            True,

        "llm_included":
            False,

        "macro_history_included":
            False,

        "portfolio_constraints_included":
            False,

        "account_currency_pnl_included":
            False,

        "ambiguous_intrabar_excluded":
            True,
    }

    metadata_path = (
        BACKTEST_DIR
        / "stateful_metadata.json"
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
    print("=" * 90)
    print("GUARDADO")
    print("=" * 90)

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