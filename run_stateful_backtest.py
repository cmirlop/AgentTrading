import json
import argparse
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
from config.backtest_cost_config import COST_SCENARIOS


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
    transaction_costs_eur = float(resolved["transaction_cost_eur"].fillna(0).sum()) if "transaction_cost_eur" in resolved else 0.0
    costs_incurred_eur = float(df["transaction_cost_eur"].fillna(0).sum()) if "transaction_cost_eur" in df else 0.0
    net_pnl_eur = float(resolved["net_pnl_eur"].sum()) if "net_pnl_eur" in resolved else gross_pnl_eur
    net_r_series = resolved["net_r"] if "net_r" in resolved else resolved["r"]
    net_win_count = int((net_r_series > 0).sum()) if len(net_r_series) else 0
    net_loss_count = int((net_r_series < 0).sum()) if len(net_r_series) else 0
    net_profit_factor = (
        float(net_r_series[net_r_series > 0].sum()) / abs(float(net_r_series[net_r_series < 0].sum()))
        if net_loss_count else None
    )

    max_drawdown_eur = (
        calculate_max_drawdown_r(
            resolved[
                "gross_pnl_eur"
            ].tolist()
        )
    )
    net_max_drawdown_eur = (
        calculate_max_drawdown_r(resolved["net_pnl_eur"].dropna().tolist())
        if "net_pnl_eur" in resolved and resolved["net_pnl_eur"].notna().any()
        else 0.0
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

        "orders_executed": int(df["fill_time"].notna().sum()) if "fill_time" in df else 0,

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

        "pending_at_end": int(counts.get("PENDING_AT_END", 0)),

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

        "transaction_costs_eur": round(transaction_costs_eur, 2),
        "costs_incurred_eur": round(costs_incurred_eur, 2),
        "net_pnl_eur": round(net_pnl_eur, 2),
        "net_expectancy_eur": round(net_pnl_eur / total_resolved, 3) if total_resolved else None,
        "net_expectancy_r": round(float(net_r_series.mean()), 4) if len(net_r_series) else None,
        "net_total_r": round(float(net_r_series.sum()), 2) if len(net_r_series) else 0.0,
        "net_win_rate": round(net_win_count / len(net_r_series), 4) if len(net_r_series) else None,
        "net_profit_factor": round(net_profit_factor, 3) if net_profit_factor is not None else None,
        "ambiguous_total": int(df["result"].astype(str).str.startswith("AMBIGUOUS").sum()),
        "open_at_end_total": int(df["result"].eq("OPEN_AT_END").sum()),

        "max_drawdown_eur":
            round(
                max_drawdown_eur,
                2,
            ),

        "net_max_drawdown_eur": round(net_max_drawdown_eur, 2),
    }


def run_scenario(
    symbol,
    threshold,
    mode,
    signal_df,
    prepared,
    prepared_by_symbol,
    cost_scenario="BASE",
    spread_floor_points=0.0,
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
    plan_rejection_reasons = {}

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
            reason = plan.get("error") or (plan.get("errors") or ["PLAN_REJECTED"])[0]
            plan_rejection_reasons[reason] = plan_rejection_reasons.get(reason, 0) + 1
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
                cost_scenario=cost_scenario,
                risk_amount_eur=estimated_loss_eur,
                spread_floor_points=spread_floor_points,
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

                "gross_r": execution.get("gross_r"),
                "net_r": execution.get("net_r"),
                "transaction_cost_r": execution.get("transaction_cost_r"),
                "transaction_cost_eur": execution.get("transaction_cost_eur"),
                "net_pnl_eur": execution.get("net_r") * estimated_loss_eur if execution.get("net_r") is not None else None,
                "cost_scenario": execution.get("cost_scenario"),
                "session_utc": "ASIA" if signal_time.hour < 7 else "EUROPE" if signal_time.hour < 13 else "US" if signal_time.hour < 21 else "ROLLOVER",

                "fill_time":
                    execution[
                        "fill_time"
                    ],

                "exit_time":
                    execution[
                        "exit_time"
                    ],
                "spread_floor_assumption_points": execution.get("spread_floor_assumption_points", 0.0),
                "spread_bars_examined": execution.get("spread_bars_examined", 0),
                "unverified_zero_spread_bar_count": execution.get("unverified_zero_spread_bar_count", 0),
                "spread_floor_applied_bar_count": execution.get("spread_floor_applied_bar_count", 0),
                "spread_data_quality": execution.get("spread_data_quality"),

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

    summary["opportunities_detected"] = int(len(rows))
    summary["accepted_plans"] = len(trades)
    summary["rejected_plans"] = plan_rejections + risk_rejections
    summary["plan_rejection_reasons"] = plan_rejection_reasons
    summary["cost_scenario"] = cost_scenario
    summary["by_session_net_pnl_eur"] = {
        str(session): round(float(group.loc[group["result"].isin(["WIN", "LOSS"]), "net_pnl_eur"].fillna(0).sum()), 2)
        for session, group in pd.DataFrame(trades).groupby("session_utc")
    } if trades else {}

    return (
        trades,
        summary,
    )


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", default=str(SCAN_PATH))
    parser.add_argument("--cost-scenario", choices=list(COST_SCENARIOS), default="BASE")
    parser.add_argument("--output-dir", default=str(BACKTEST_DIR))
    parser.add_argument("--name", default="stateful")
    parser.add_argument("--spread-floor-points", type=float, choices=[0.0, 5.0, 10.0, 20.0], default=0.0,
                        help="Research-only minimum spread assumption in MT5 points; 0 preserves existing scenario behavior.")
    parser.add_argument("--threshold", type=int, choices=list(STATEFUL_THRESHOLDS), default=None,
                        help="Optional single pre-existing threshold for focused research runs.")
    parser.add_argument("--mode", choices=list(STATEFUL_ENTRY_MODES), default=None,
                        help="Optional single existing entry mode for focused research runs.")
    args = parser.parse_args()

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
        "Cost scenario:", args.cost_scenario
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
        args.scan,
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

        selected_thresholds = [args.threshold] if args.threshold is not None else STATEFUL_THRESHOLDS
        selected_modes = [args.mode] if args.mode is not None else STATEFUL_ENTRY_MODES
        for threshold in selected_thresholds:

            for mode in selected_modes:

                trades, summary = (
                    run_scenario(
                        symbol=symbol,
                        threshold=threshold,
                        mode=mode,
                        signal_df=signal_df,
                        prepared=prepared,
                        prepared_by_symbol=prepared_by_symbol,
                        cost_scenario=args.cost_scenario,
                        spread_floor_points=args.spread_floor_points,
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

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / f"{args.name}_trades.csv"

    summary_path = output_dir / f"{args.name}_summary.csv"

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

        "cost_scenario": args.cost_scenario,
        "spread_floor_assumption_points": args.spread_floor_points,
        "spread_floor_is_broker_quote": False,
        "cost_assumptions": COST_SCENARIOS[args.cost_scenario],
        "source_scan": str(args.scan),
    }

    metadata_path = output_dir / f"{args.name}_metadata.json"

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
