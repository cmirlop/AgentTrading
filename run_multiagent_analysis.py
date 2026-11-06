import argparse
import json

from datetime import (
    datetime,
    timezone,
)

from pathlib import Path

from config.market_config import (
    SYMBOLS,
)

from src.orchestration.orchestrator import (
    MultiAgentOrchestrator,
)
from src.event_ledger import bind_event, record_opportunity, record_system_decision, research_log_required
from src.market_snapshot import build_compact_snapshot
from src.technical_evaluator import evaluate_technical_state


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

DECISION_DIR = (
    BASE_DIR
    / "data"
    / "decisions"
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


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "symbol",
        choices=
            SYMBOLS,
    )

    args = parser.parse_args()

    symbol = (
        args.symbol
    )

    state_path = (
        STATE_DIR
        / f"{symbol}.json"
    )

    market_state = (
        load_json(
            state_path
        )
    )


    runtime_state = load_json(
        STATE_DIR
        / "runtime.json"
    )

    calendar_state = load_json(
        STATE_DIR
        / "calendar.json"
    )

    print("=" * 90)
    print(
        "MULTI-AGENT ANALYSIS"
    )
    print("=" * 90)

    print(
        "Symbol:",
        symbol,
    )

    print(
        "Market state:",
        market_state[
            "analysis_time"
        ],
    )

    print()

    orchestrator = (
        MultiAgentOrchestrator()
    )
    event_id = None
    try:
        timestamp = market_state.get("analysis_time") or market_state.get("timestamp")
        if timestamp:
            technical = evaluate_technical_state(build_compact_snapshot(market_state, candles_per_timeframe=4))
            technical_decision = "NO_TRADE"
            if technical["setup_quality"] >= 65 and technical["alignment"] != "CONFLICT" and technical["bias"] != "NEUTRAL":
                technical_decision = "WATCH_LONG" if technical["bias"] == "BULLISH" else "WATCH_SHORT"
            snapshot = {
                **market_state,
                "technical_decision": technical_decision,
                "technical_evaluation": {key: technical[key] for key in ("bias", "overall_score", "setup_quality", "alignment", "timeframe_scores")},
            }
            event_id = record_opportunity(
                symbol, timestamp, snapshot,
                eligible=technical_decision != "NO_TRADE",
                exclusion_reason="NO_TECHNICAL_CANDIDATE" if technical_decision == "NO_TRADE" else None,
            )
    except Exception:
        if research_log_required():
            raise
        event_id = None
    call_args = dict(symbol=symbol, market_state=market_state, runtime_state=runtime_state, calendar_state=calendar_state)
    if event_id:
        with bind_event(event_id):
            context = orchestrator.run(**call_args)
    else:
        context = orchestrator.run(**call_args)
    if event_id:
        try:
            result_message = next((message for message in reversed(context.messages) if message.get("type") == "ORCHESTRATION_RESULT"), None)
            result_payload = (result_message or {}).get("payload", {})
            final_decision = result_payload.get("direction") if result_payload.get("final_approved") else "NO_TRADE"
            record_system_decision(
                event_id,
                final_decision or "NO_TRADE",
                decision_stage="MARKET_ANALYSIS",
                validation_status="VALID" if result_payload.get("final_approved") else "REJECTED",
                rejection_code=";".join(result_payload.get("final_errors", [])) or None,
                agent_role="MULTIAGENT_DETERMINISTIC_FINAL",
            )
        except Exception:
            if research_log_required():
                raise

    print()
    print("=" * 90)
    print("AGENT BUS")
    print("=" * 90)

    for message in (
        context.messages
    ):

        if message[
            "type"
        ] in {
            "AGENT_STARTED",
            "AGENT_COMPLETED",
        }:

            continue

        print()
        print(
            f"[{message['sequence']}] "
            f"{message['agent']} "
            f"-> "
            f"{message['type']}"
        )

        print(
            json.dumps(
                message[
                    "payload"
                ],
                indent=2,
                ensure_ascii=False,
            )
        )

    now = datetime.now(
        timezone.utc
    )

    day_dir = (
        DECISION_DIR
        / now.strftime(
            "%Y-%m-%d"
        )
    )

    day_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        day_dir
        / (
            now.strftime(
                "%H-%M-%S"
            )
            +
            f"_{symbol}.json"
        )
    )

    with open(
        output_path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
        {**context.to_dict(), "event_id": event_id},
            file,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("=" * 90)
    print(
        "Guardado:",
        output_path,
    )

    print(
        "NO hay Trade Planner."
    )

    print(
        "NO hay order_check()."
    )

    print(
        "NO hay order_send()."
    )


if __name__ == "__main__":
    main()
