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
from src.risk_preflight import evaluate_plan_risk, release_plan_risk_reservation
from src.event_ledger import (
    LedgerPersistenceError, record_system_decision, record_trade_context,
    research_log_required,
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


def record_research_decision(event_id, decision, status, errors=None):
    """Append-only research observability; never authorizes order execution."""
    if not event_id:
        if research_log_required():
            raise LedgerPersistenceError("LEDGER_EVENT_CONTEXT_MISSING")
        return
    try:
        result = record_system_decision(
            event_id,
            str(decision),
            validation_status=str(status),
            decision_stage="DRY_RUN_VALIDATION",
            rejection_code=";".join(str(item) for item in (errors or []))[:240] or None,
        )
        if research_log_required() and not result.persisted:
            raise LedgerPersistenceError(result.error_code or "LEDGER_SYSTEM_DECISION_NOT_PERSISTED")
    except Exception:
        if research_log_required():
            raise


def _research_market_risk_context(runtime_state, account_state):
    """Return market inputs for offline sizing without account/position data."""
    snapshot = runtime_state.get("risk_snapshot") or {}
    return {
        "account_currency": (account_state or {}).get("currency"),
        "symbol_metadata": snapshot.get("symbol_metadata"),
        "conversion_rates": snapshot.get("conversion_rates"),
    }


def record_research_trade_context(event_id, context, *, context_type="TRADE_PLAN_RISK_INPUT"):
    if not event_id:
        if research_log_required():
            raise LedgerPersistenceError("LEDGER_EVENT_CONTEXT_MISSING")
        return
    result = record_trade_context(event_id, context, context_type=context_type)
    if research_log_required() and not result.persisted:
        raise LedgerPersistenceError(result.error_code or "LEDGER_TRADE_CONTEXT_NOT_PERSISTED")


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
            event_id = analysis_document.get("event_id")

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
                "event_id": event_id,
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

                record_research_trade_context(event_id, {
                    "candidate_status": candidate.get("status"),
                    "candidate_rejection_codes": candidate.get("rejection_reasons", []),
                    "plan_status": "NOT_BUILT",
                    "plan_strategy_source": "NO_PLAN_AVAILABLE",
                    "plan": None,
                    "market_data_time_utc": analysis_document.get("decision_time_utc"),
                    "context_known_at_utc": datetime.now(timezone.utc).isoformat(),
                    "risk_context_known_at_utc": runtime.get("generated_at"),
                    "risk_context": _research_market_risk_context(runtime, runtime.get("account")),
                })

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
                record_research_decision(
                    event_id, candidate.get("direction") or "HOLD", "REJECTED",
                    candidate.get("rejection_reasons", []),
                )

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

            record_research_trade_context(event_id, {
                "candidate_status": candidate.get("status"),
                "candidate_rejection_codes": candidate.get("rejection_reasons", []),
                "plan_status": plan.get("status"),
                "plan_strategy_source": "MODEL_SELECTED_STRATEGY_FAMILY",
                "plan": plan,
                "market_data_time_utc": market_state.get("analysis_time"),
                "context_known_at_utc": datetime.now(timezone.utc).isoformat(),
                "risk_context_known_at_utc": runtime.get("generated_at"),
                "risk_context": _research_market_risk_context(runtime, runtime.get("account")),
                "market_data_quality": {
                    "spread_status": (market_state.get("spread_quality") or {}).get("status", "UNVERIFIED")
                    if isinstance(market_state.get("spread_quality"), dict) else "UNVERIFIED",
                },
            })

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
                record_research_decision(event_id, candidate.get("direction") or "HOLD", "REJECTED", plan.get("errors", []))

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

            portfolio_risk = evaluate_plan_risk(
                plan,
                runtime,
            )
            record_research_trade_context(event_id, {
                "risk_approved": bool(portfolio_risk.get("approved")),
                "risk_rejection_codes": portfolio_risk.get("errors", []),
            }, context_type="RISK_PREFLIGHT_RESULT")
            symbol_result["portfolio_risk"] = portfolio_risk
            if not portfolio_risk.get("approved"):
                symbol_result["final_validation"] = {
                    "status": "REJECTED",
                    "approved": False,
                    "errors": portfolio_risk.get("errors", ["PORTFOLIO_RISK_CHECK_REJECTED"]),
                }
                record_research_decision(event_id, candidate.get("direction") or "HOLD", "REJECTED", portfolio_risk.get("errors", []))
                output["symbols"][symbol] = symbol_result
                print("Final: REJECTED by portfolio/daily risk")
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

                    portfolio_risk=
                        portfolio_risk,
                )
            )

            if not final_validation.get("approved") and portfolio_risk.get("reservation_id"):
                release_plan_risk_reservation(
                    plan,
                    reservation_token=portfolio_risk.get("reservation_token"),
                )

            symbol_result[
                "final_validation"
            ] = (
                final_validation
            )
            record_research_decision(
                event_id, candidate.get("direction") or "HOLD",
                "VALID" if final_validation.get("approved") else "REJECTED",
                final_validation.get("errors", []),
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
