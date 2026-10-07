import json
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path

import MetaTrader5 as mt5

from config.market_config import (
    SYMBOLS,
)

from src.session_gate import (
    build_session_state,
)

from src.candidate_gate import (
    evaluate_candidate,
)

from src.trade_planner import (
    build_trade_plan,
)

from src.order_request import (
    build_mt5_request,
)

from src.risk_validator import (
    validate_dry_run,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

STATE_DIR = (
    BASE_DIR
    / "data"
    / "state"
)

ANALYSIS_DIR = (
    BASE_DIR
    / "data"
    / "analysis"
)

DRY_RUN_DIR = (
    BASE_DIR
    / "data"
    / "dry_run"
)


def load_json(
    path,
):

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:

        return json.load(
            file
        )


def latest_analysis(
    symbol,
):

    files = list(
        ANALYSIS_DIR.glob(
            f"**/*_{symbol}.json"
        )
    )

    if not files:

        raise FileNotFoundError(
            f"No analysis for {symbol}"
        )

    latest = max(
        files,
        key=lambda path:
            path.stat().st_mtime,
    )

    return (
        load_json(latest),
        latest,
    )


def order_check_to_dict(
    result,
):

    if result is None:
        return None

    return {
        "retcode":
            result.retcode,

        "comment":
            result.comment,

        "balance":
            result.balance,

        "equity":
            result.equity,

        "profit":
            result.profit,

        "margin":
            result.margin,

        "margin_free":
            result.margin_free,

        "margin_level":
            result.margin_level,
    }


def main():

    print("=" * 90)
    print(
        "TRADING AGENT - "
        "FULL DRY RUN PIPELINE"
    )
    print("=" * 90)

    if not mt5.initialize():

        raise RuntimeError(
            f"MT5 init error: "
            f"{mt5.last_error()}"
        )

    try:

        terminal = (
            mt5.terminal_info()
        )

        if terminal is None:

            raise RuntimeError(
                "terminal_info() failed"
            )

        # En esta fase es obligatorio.
        if not terminal.tradeapi_disabled:

            raise RuntimeError(
                "SEGURIDAD: Python trading "
                "debe permanecer deshabilitado."
            )

        runtime = load_json(
            STATE_DIR
            / "runtime.json"
        )

        calendar = load_json(
            STATE_DIR
            / "calendar.json"
        )

        session = (
            build_session_state()
        )

        server_offset = (
            calendar.get(
                "server_utc_offset_seconds"
            )
        )

        if server_offset is None:

            raise RuntimeError(
                "Falta server_utc_offset_seconds "
                "en calendar.json"
            )

        print(
            "Broker UTC offset:",
            server_offset,
            "seconds",
        )

        output = {
            "generated_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "mode":
                "DRY_RUN",

            "python_trading_disabled":
                terminal.tradeapi_disabled,

            "session":
                session,

            "symbols": {},
        }

        for symbol in SYMBOLS:

            print()
            print("=" * 70)
            print(symbol)
            print("=" * 70)

            analysis_document, source = (
                latest_analysis(
                    symbol
                )
            )

            technical = (
                analysis_document[
                    "analysis"
                ]
            )

            candidate = (
                evaluate_candidate(
                    symbol=symbol,

                    technical_analysis=
                        technical,

                    technical_generated_at=
                        analysis_document[
                            "generated_at"
                        ],

                    runtime_state=
                        runtime,

                    calendar_state=
                        calendar,

                    session_state=
                        session,
                )
            )

            symbol_result = {
                "technical_source":
                    str(source),

                "candidate":
                    candidate,

                "trade_plan":
                    None,

                "mt5_request":
                    None,

                "order_check":
                    None,

                "final_validation":
                    None,
            }

            print(
                "Candidate:",
                candidate["status"],
            )

            print(
                "Candidate reasons:",
                candidate[
                    "rejection_reasons"
                ],
            )

            # ==============================================
            # NO CANDIDATE
            # ==============================================

            if not candidate[
                "allowed"
            ]:

                symbol_result[
                    "final_validation"
                ] = {
                    "status":
                        "REJECTED",

                    "approved":
                        False,

                    "errors":
                        candidate[
                            "rejection_reasons"
                        ],
                }

                output[
                    "symbols"
                ][symbol] = (
                    symbol_result
                )

                print(
                    "Final: REJECTED"
                )

                continue

            # ==============================================
            # MARKET STATE
            # ==============================================

            market_state = load_json(
                STATE_DIR
                / f"{symbol}.json"
            )

            # ==============================================
            # TRADE PLAN
            # ==============================================

            plan = (
                build_trade_plan(
                    symbol=symbol,

                    direction=
                        candidate[
                            "direction"
                        ],

                    strategy=
                        candidate[
                            "technical"
                        ][
                            "strategy"
                        ],

                    market_state=
                        market_state,

                    server_utc_offset_seconds=
                        server_offset,
                )
            )

            symbol_result[
                "trade_plan"
            ] = plan

            print(
                "Trade plan:",
                plan[
                    "status"
                ],
            )

            if (
                plan["status"]
                != "VALID"
            ):

                symbol_result[
                    "final_validation"
                ] = {
                    "status":
                        "REJECTED",

                    "approved":
                        False,

                    "errors":
                        plan[
                            "errors"
                        ],
                }

                output[
                    "symbols"
                ][symbol] = (
                    symbol_result
                )

                print(
                    "Final: REJECTED"
                )

                print(
                    "Trade plan errors:",
                    plan.get(
                        "errors",
                        [],
                    ),
                )

                if plan.get(
                    "diagnostics"
                ):

                    print(
                        "Trade plan diagnostics:",
                        plan[
                            "diagnostics"
                        ],
                    )

                continue

            # ==============================================
            # MT5 REQUEST
            # ==============================================

            request, expiration = (
                build_mt5_request(
                    plan
                )
            )

            symbol_result[
                "mt5_request"
            ] = request

            symbol_result[
                "expiration_utc"
            ] = (
                expiration.isoformat()
            )

            # ==============================================
            # ORDER CHECK
            # ==============================================

            check = mt5.order_check(
                request
            )

            symbol_result[
                "order_check"
            ] = (
                order_check_to_dict(
                    check
                )
            )

            # ==============================================
            # FINAL RISK VALIDATOR
            # ==============================================

            final_validation = (
                validate_dry_run(
                    symbol=symbol,

                    candidate=
                        candidate,

                    trade_plan=
                        plan,

                    request=
                        request,

                    order_check_result=
                        check,

                    terminal_info=
                        terminal,
                )
            )

            symbol_result[
                "final_validation"
            ] = (
                final_validation
            )

            output[
                "symbols"
            ][symbol] = (
                symbol_result
            )

            print(
                "Order check:",
                (
                    check.retcode
                    if check
                    else None
                ),
            )

            print(
                "Final:",
                final_validation[
                    "status"
                ],
            )

            print(
                "Errors:",
                final_validation[
                    "errors"
                ],
            )

        # ==================================================
        # SAVE
        # ==================================================

        now = datetime.now(
            timezone.utc
        )

        output_dir = (
            DRY_RUN_DIR
            / now.strftime(
                "%Y-%m-%d"
            )
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = (
            output_dir
            / (
                now.strftime(
                    "%H-%M-%S"
                )
                +
                "_pipeline.json"
            )
        )

        with open(
            path,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                output,
                file,
                indent=2,
                ensure_ascii=False,
            )

        print()
        print(
            "Guardado:",
            path,
        )

        print()
        print(
            "NO se ha llamado "
            "a mt5.order_send()."
        )

    finally:

        mt5.shutdown()


if __name__ == "__main__":
    main()