from pathlib import Path

import pandas as pd


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

TRADES_PATH = (
    BACKTEST_DIR
    / "portfolio_trades.csv"
)

OUTPUT_PATH = (
    BACKTEST_DIR
    / "portfolio_monthly_stability.csv"
)


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


def main():

    print("=" * 120)
    print(
        "BACKTEST - PORTFOLIO MONTHLY STABILITY"
    )
    print("=" * 120)

    df = pd.read_csv(
        TRADES_PATH,
        parse_dates=[
            "signal_time",
            "fill_time",
            "exit_time",
            "release_time",
        ],
    )

    df[
        "signal_month"
    ] = (
        df[
            "signal_time"
        ]
        .dt
        .to_period("M")
        .astype(str)
    )

    rows = []

    for (
        threshold,
        mode,
        month,
    ), group in df.groupby(
        [
            "threshold",
            "mode",
            "signal_month",
        ]
    ):

        resolved = group[
            group[
                "result"
            ].isin(
                [
                    "WIN",
                    "LOSS",
                ]
            )
        ].copy()

        resolved = (
            resolved
            .sort_values(
                "exit_time"
            )
        )

        wins = resolved[
            resolved[
                "result"
            ]
            == "WIN"
        ]

        losses = resolved[
            resolved[
                "result"
            ]
            == "LOSS"
        ]

        gross_win = float(
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

        pnl = float(
            resolved[
                "gross_pnl_eur"
            ].sum()
        )

        row = {
            "threshold":
                int(threshold),

            "mode":
                mode,

            "month":
                month,

            "signals_taken":
                int(
                    len(group)
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
                        group[
                            "result"
                        ]
                        == "EXPIRED"
                    ).sum()
                ),

            "win_rate":
                (
                    len(wins)
                    / len(resolved)
                    if len(resolved)
                    else None
                ),

            "gross_pnl_eur":
                pnl,

            "expectancy_eur":
                (
                    pnl
                    / len(resolved)
                    if len(resolved)
                    else None
                ),

            "profit_factor_eur":
                (
                    gross_win
                    / gross_loss
                    if gross_loss > 0
                    else None
                ),

            "max_drawdown_eur":
                (
                    max_drawdown(
                        resolved[
                            "gross_pnl_eur"
                        ].tolist()
                    )
                    if len(resolved)
                    else None
                ),

            "EURUSD_pnl":
                float(
                    resolved.loc[
                        resolved[
                            "symbol"
                        ]
                        == "EURUSD",
                        "gross_pnl_eur",
                    ].sum()
                ),

            "USDCAD_pnl":
                float(
                    resolved.loc[
                        resolved[
                            "symbol"
                        ]
                        == "USDCAD",
                        "gross_pnl_eur",
                    ].sum()
                ),

            "AUDUSD_pnl":
                float(
                    resolved.loc[
                        resolved[
                            "symbol"
                        ]
                        == "AUDUSD",
                        "gross_pnl_eur",
                    ].sum()
                ),
        }

        rows.append(
            row
        )

    result = pd.DataFrame(
        rows
    )

    numeric = [
        "win_rate",
        "gross_pnl_eur",
        "expectancy_eur",
        "profit_factor_eur",
        "max_drawdown_eur",
        "EURUSD_pnl",
        "USDCAD_pnl",
        "AUDUSD_pnl",
    ]

    result[
        numeric
    ] = (
        result[
            numeric
        ]
        .round(3)
    )

    result = result.sort_values(
        [
            "threshold",
            "mode",
            "month",
        ]
    )

    result.to_csv(
        OUTPUT_PATH,
        index=False,
    )

    # Nos centramos primero en STOP.
    stop = result[
        result[
            "mode"
        ]
        == "STOP"
    ]

    print()
    print(
        stop[
            [
                "threshold",
                "month",
                "signals_taken",
                "resolved",
                "win_rate",
                "gross_pnl_eur",
                "expectancy_eur",
                "profit_factor_eur",
                "max_drawdown_eur",
                "EURUSD_pnl",
                "USDCAD_pnl",
                "AUDUSD_pnl",
            ]
        ].to_string(
            index=False
        )
    )

    print()
    print("=" * 120)
    print(
        "CONSISTENCIA POR ESCENARIO"
    )
    print("=" * 120)

    stability = (
        stop.groupby(
            "threshold"
        )
        .agg(
            months=(
                "month",
                "count",
            ),

            profitable_months=(
                "gross_pnl_eur",
                lambda x:
                    int(
                        (x > 0).sum()
                    ),
            ),

            losing_months=(
                "gross_pnl_eur",
                lambda x:
                    int(
                        (x < 0).sum()
                    ),
            ),

            average_month_eur=(
                "gross_pnl_eur",
                "mean",
            ),

            worst_month_eur=(
                "gross_pnl_eur",
                "min",
            ),

            best_month_eur=(
                "gross_pnl_eur",
                "max",
            ),

            monthly_std_eur=(
                "gross_pnl_eur",
                "std",
            ),
        )
        .reset_index()
        .round(3)
    )

    print()
    print(
        stability.to_string(
            index=False
        )
    )

    print()
    print(
        "Guardado:",
        OUTPUT_PATH,
    )


if __name__ == "__main__":
    main()