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

    context = (
        orchestrator.run(
            symbol=
                symbol,

            market_state=
                market_state,

            runtime_state=
                runtime_state,

            calendar_state=
                calendar_state,
        )
    )

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
            context.to_dict(),
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