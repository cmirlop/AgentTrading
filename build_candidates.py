import json
from pathlib import Path

from config.market_config import SYMBOLS

from src.candidate_gate import (
    evaluate_candidate,
)

from datetime import (
    datetime,
    timezone,
)

from src.session_gate import (
    build_session_state,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

ANALYSIS_DIR = (
    BASE_DIR
    / "data"
    / "analysis"
)

STATE_DIR = (
    BASE_DIR
    / "data"
    / "state"
)


def load_json(path):

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:

        return json.load(file)


def find_latest_analysis(
    symbol,
):

    files = list(
        ANALYSIS_DIR.glob(
            f"**/*_{symbol}.json"
        )
    )

    if not files:

        raise FileNotFoundError(
            f"No hay analisis para {symbol}"
        )

    latest = max(
        files,
        key=lambda path:
            path.stat().st_mtime,
    )

    content = load_json(
        latest
    )

    return (
        content,
        latest,
    )


def save_candidates(
    state,
):

    path = (
        STATE_DIR
        / "candidates.json"
    )

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return path


def main():

    print("=" * 80)

    print(
        "TRADING AGENT - "
        "CANDIDATE GATE"
    )

    print("=" * 80)

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

    print()
    print("--- SESSION GATE ---")

    print(
        "London:",
        session[
            "london"
        ][
            "local_time"
        ],
        "| open:",
        session[
            "london"
        ][
            "is_open"
        ],
    )

    print(
        "New York:",
        session[
            "new_york"
        ][
            "local_time"
        ],
        "| open:",
        session[
            "new_york"
        ][
            "is_open"
        ],
    )

    print(
        "Allowed:",
        session[
            "trading_session_allowed"
        ],
    )

    output = {
        "symbols": {}
    }

    for symbol in SYMBOLS:

        technical_document, source = (
            find_latest_analysis(
                symbol
            )
        )

        technical = (
            technical_document[
                "analysis"
            ]
        )

        technical_generated_at = (
            technical_document[
                "generated_at"
            ]
        )

        candidate = (
            evaluate_candidate(
                symbol=symbol,

                technical_analysis=
                    technical,

                technical_generated_at=
                    technical_generated_at,

                runtime_state=
                    runtime,

                calendar_state=
                    calendar,

                session_state=
                    session,
            )
        )

        candidate[
            "technical_source"
        ] = str(source)

        output = {
            "generated_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "session":
                session,

            "symbols": {},
        }

        print()
        print("=" * 60)
        print(symbol)
        print("=" * 60)

        print(
            "Status:",
            candidate["status"],
        )

        print(
            "Direction:",
            candidate["direction"],
        )

        print(
            "Allowed:",
            candidate["allowed"],
        )

        print(
            "Setup quality:",
            candidate[
                "technical"
            ][
                "setup_quality"
            ],
        )

        print(
            "Strategy:",
            candidate[
                "technical"
            ][
                "strategy"
            ],
        )

        print(
            "Macro risk:",
            candidate[
                "macro"
            ][
                "macro_risk"
            ]
            if candidate["macro"]
            else None,
        )

        print(
            "Reasons:",
            candidate[
                "rejection_reasons"
            ],
        )

    path = save_candidates(
        output
    )

    print()
    print(
        "Guardado:",
        path,
    )


if __name__ == "__main__":
    main()