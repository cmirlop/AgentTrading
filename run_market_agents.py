import json
from datetime import datetime, timezone
from pathlib import Path

from src.event_ledger import (
    LedgerPersistenceError, bind_event, record_opportunity, record_system_decision,
    record_technical_plan, research_log_required,
)
from src.research_policy import CANONICAL_FILTER_ROLE, POLICY_VERSION
from src.market_snapshot import build_compact_snapshot
from src.technical_evaluator import evaluate_technical_state
from src.technical_reference import build_technical_reference_plan

from src.agents.market_analyst import (
    MarketAnalystAgent,
)


BASE_DIR = Path(__file__).resolve().parent

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

SYMBOLS = [
    "EURUSD",
    "USDCAD",
    "AUDUSD",
]


def load_state(symbol):

    path = (
        STATE_DIR
        / f"{symbol}.json"
    )

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:

        return json.load(file)


def save_analysis(
    symbol,
    analysis,
    metrics,
    event_id=None,
    decision_time_utc=None,
):

    now = datetime.now(
        timezone.utc
    )

    day = now.strftime(
        "%Y-%m-%d"
    )

    directory = (
        ANALYSIS_DIR
        / day
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = now.strftime(
        "%H-%M-%S"
    )

    path = (
        directory
        / f"{timestamp}_{symbol}.json"
    )

    content = {
        "generated_at":
            now.isoformat(),

        "symbol":
            symbol,

        "event_id": event_id,

        "decision_time_utc": decision_time_utc,

        "analysis":
            analysis.model_dump(),

        "performance":
            metrics,
    }

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            content,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return path


def print_analysis(
    analysis,
    metrics,
):

    print()
    print("=" * 80)
    print(
        analysis.symbol
    )
    print("=" * 80)

    print(
        "Decision:",
        analysis.decision,
    )

    print(
        "Estrategia:",
        analysis.strategy,
    )

    print(
        "Score global:",
        analysis.overall_score,
    )

    print(
        "Scores TF:",
        analysis.timeframe_scores,
    )

    print(
        "Bias:",
        analysis.bias,
    )

    print(
        "Regimen:",
        analysis.market_regime,
    )

    print(
        "Confianza decision:",
        round(
            analysis.decision_confidence,
            2,
        ),
    )

    print(
        "Validacion LLM:",
        (
            "OK"
            if analysis.validation_ok
            else "FAIL"
        ),
    )

    if analysis.validation_errors:

        for error in (
            analysis.validation_errors
        ):

            print(
                " ERROR:",
                error,
            )

    print(
        "Noticias necesarias:",
        analysis.news_check_required,
    )

    print(
        "Setup quality:",
        analysis.setup_quality,
        "/ 100",
    )

    print(
        "Alineacion HTF:",
        analysis.higher_timeframe_alignment,
    )

    print()
    print(
        "Resumen:",
        analysis.summary,
    )

    print()
    print(
        "Caution flags:",
        analysis.caution_flags,
    )

    print()
    print("Evidencia a favor:")

    for factor in (
        analysis.supporting_evidence
    ):

        print(
            " +",
            factor,
        )

    print()
    print("Evidencia en conflicto:")

    for factor in (
        analysis.conflicting_evidence
    ):

        print(
            " -",
            factor,
        )

    print()
    print(
        "Tiempo:",
        round(
            metrics[
                "elapsed_seconds"
            ],
            2,
        ),
        "segundos",
    )

    if (
        "tokens_per_second"
        in metrics
    ):

        print(
            "Velocidad:",
            round(
                metrics[
                    "tokens_per_second"
                ],
                2,
            ),
            "tokens/s",
        )


def main():

    print("=" * 80)
    print(
        "TRADING AGENT - "
        "MARKET ANALYST"
    )
    print("=" * 80)

    agent = (
        MarketAnalystAgent()
    )

    for symbol in SYMBOLS:

        print()
        print(
            f"Analizando {symbol}..."
        )

        state = load_state(
            symbol
        )
        event_id = None
        timestamp = None
        try:
            timestamp = state.get("analysis_time") or state.get("timestamp")
            if timestamp:
                technical_decision = "NO_TRADE"
                try:
                    technical = evaluate_technical_state(build_compact_snapshot(state, candles_per_timeframe=4))
                    if technical["setup_quality"] >= 65 and technical["alignment"] != "CONFLICT" and technical["bias"] != "NEUTRAL":
                        technical_decision = "WATCH_LONG" if technical["bias"] == "BULLISH" else "WATCH_SHORT"
                    safe_technical = {key: technical[key] for key in ("bias", "overall_score", "setup_quality", "alignment", "timeframe_scores")}
                except Exception as exc:
                    technical = None
                    safe_technical = {"validation_error_code": f"TECHNICAL_EVALUATION_{type(exc).__name__.upper()}"}
                reference = build_technical_reference_plan(
                    symbol, state, timestamp,
                    constructed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    technical_evaluation=technical,
                )
                opportunity_snapshot = {
                    **state,
                    "technical_decision": technical_decision,
                    "technical_evaluation": safe_technical,
                    "technical_plan_status": reference["status"],
                    "technical_plan_sha256": reference["plan_sha256"],
                    "technical_policy_version": reference["policy_version"],
                }
                event_id = record_opportunity(
                    symbol, timestamp, opportunity_snapshot,
                    eligible=technical_decision != "NO_TRADE",
                    exclusion_reason="NO_TECHNICAL_CANDIDATE" if technical_decision == "NO_TRADE" else None,
                )
                plan_write = record_technical_plan(event_id, reference)
                if research_log_required() and not plan_write.persisted:
                    raise LedgerPersistenceError(plan_write.error_code or "TECHNICAL_PLAN_NOT_PERSISTED")
        except Exception:
            if research_log_required():
                raise
            event_id = None
        if event_id:
            with bind_event(event_id, agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION):
                analysis, metrics = agent.analyze(state)
                try:
                    record_system_decision(
                        event_id, analysis.decision,
                        validation_status="VALID" if analysis.validation_ok else "INVALID",
                        decision_stage="MARKET_ANALYSIS",
                        rejection_code=None if analysis.validation_ok else "ANALYSIS_VALIDATION_FAILED",
                        agent_role=CANONICAL_FILTER_ROLE,
                        decision_policy_id=POLICY_VERSION if analysis.llm_provider == "deepseek" else None,
                    )
                except Exception:
                    if research_log_required():
                        raise
        else:
            analysis, metrics = agent.analyze(state)

        path = save_analysis(
            symbol,
            analysis,
            metrics,
            event_id=event_id,
            decision_time_utc=timestamp,
        )

        print_analysis(
            analysis,
            metrics,
        )

        print(
            "\nGuardado:",
            path,
        )


if __name__ == "__main__":
    main()
