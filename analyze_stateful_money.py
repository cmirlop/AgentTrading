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
    / "stateful_trades.csv"
)

OUTPUT_PATH = (
    BACKTEST_DIR
    / "monetary_summary.csv"
)


def max_drawdown(
    values,
):

    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for value in values:

        equity += float(
            value
        )

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

    print("=" * 110)
    print(
        "BACKTEST - "
        "MONETARY ANALYSIS"
    )
    print("=" * 110)

    df = pd.read_csv(
        TRADES_PATH,
        parse_dates=[
            "signal_time",
            "fill_time",
            "exit_time",
        ],
    )

    df[
        "estimated_risk_eur"
    ] = pd.to_numeric(
        df[
            "estimated_risk_eur"
        ],
        errors="coerce",
    )

    df[
        "gross_pnl_eur"
    ] = pd.to_numeric(
        df[
            "gross_pnl_eur"
        ],
        errors="coerce",
    )

    rows = []

    group_columns = [
        "symbol",
        "threshold",
        "mode",
    ]

    for (
        symbol,
        threshold,
        mode,
    ), group in df.groupby(
        group_columns
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

        gross_wins = float(
            wins[
                "gross_pnl_eur"
            ].sum()
        )

        gross_losses = abs(
            float(
                losses[
                    "gross_pnl_eur"
                ].sum()
            )
        )

        total_eur = float(
            resolved[
                "gross_pnl_eur"
            ].sum()
        )

        expectancy_eur = (
            float(
                resolved[
                    "gross_pnl_eur"
                ].mean()
            )
            if len(
                resolved
            )
            else None
        )

        profit_factor_eur = (
            gross_wins
            / gross_losses
            if gross_losses > 0
            else None
        )

        dd_eur = (
            max_drawdown(
                resolved[
                    "gross_pnl_eur"
                ].tolist()
            )
            if len(
                resolved
            )
            else None
        )

        average_risk = (
            float(
                resolved[
                    "estimated_risk_eur"
                ].mean()
            )
            if len(
                resolved
            )
            else None
        )

        average_win_risk = (
            float(
                wins[
                    "estimated_risk_eur"
                ].mean()
            )
            if len(
                wins
            )
            else None
        )

        average_loss_risk = (
            float(
                losses[
                    "estimated_risk_eur"
                ].mean()
            )
            if len(
                losses
            )
            else None
        )

        p95_risk = (
            float(
                resolved[
                    "estimated_risk_eur"
                ].quantile(
                    0.95
                )
            )
            if len(
                resolved
            )
            else None
        )

        max_risk = (
            float(
                resolved[
                    "estimated_risk_eur"
                ].max()
            )
            if len(
                resolved
            )
            else None
        )

        row = {
            "symbol":
                symbol,

            "threshold":
                int(
                    threshold
                ),

            "mode":
                mode,

            "resolved":
                len(
                    resolved
                ),

            "wins":
                len(
                    wins
                ),

            "losses":
                len(
                    losses
                ),

            "win_rate":
                (
                    len(wins)
                    / len(resolved)
                    if len(resolved)
                    else None
                ),

            "gross_pnl_eur":
                total_eur,

            "expectancy_eur":
                expectancy_eur,

            "profit_factor_eur":
                profit_factor_eur,

            "max_drawdown_eur":
                dd_eur,

            "avg_risk_eur":
                average_risk,

            "avg_risk_win_eur":
                average_win_risk,

            "avg_risk_loss_eur":
                average_loss_risk,

            "p95_risk_eur":
                p95_risk,

            "max_risk_eur":
                max_risk,
        }

        rows.append(
            row
        )

    summary = pd.DataFrame(
        rows
    )

    summary = summary.sort_values(
        [
            "symbol",
            "threshold",
            "mode",
        ]
    )

    numeric_columns = [
        "win_rate",
        "gross_pnl_eur",
        "expectancy_eur",
        "profit_factor_eur",
        "max_drawdown_eur",
        "avg_risk_eur",
        "avg_risk_win_eur",
        "avg_risk_loss_eur",
        "p95_risk_eur",
        "max_risk_eur",
    ]

    summary[
        numeric_columns
    ] = (
        summary[
            numeric_columns
        ]
        .round(3)
    )

    summary.to_csv(
        OUTPUT_PATH,
        index=False,
    )

    columns = [
        "symbol",
        "threshold",
        "mode",
        "resolved",
        "win_rate",
        "gross_pnl_eur",
        "expectancy_eur",
        "profit_factor_eur",
        "max_drawdown_eur",
        "avg_risk_eur",
        "avg_risk_win_eur",
        "avg_risk_loss_eur",
        "max_risk_eur",
    ]

    print()
    print(
        summary[
            columns
        ].to_string(
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