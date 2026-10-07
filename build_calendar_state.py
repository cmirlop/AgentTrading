import json
from pathlib import Path

import MetaTrader5 as mt5

from src.calendar_reader import (
    load_calendar_csv,
)

from src.calendar_gate import (
    build_calendar_state,
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


def get_calendar_path():

    if not mt5.initialize():

        raise RuntimeError(
            "No se pudo conectar con MT5: "
            f"{mt5.last_error()}"
        )

    try:

        terminal = (
            mt5.terminal_info()
        )

        if terminal is None:

            raise RuntimeError(
                "terminal_info() devolvio None"
            )

        common_path = Path(
            terminal.commondata_path
        )

        return (
            common_path
            / "Files"
            / "AgentTrading"
            / "economic_calendar.csv"
        )

    finally:

        mt5.shutdown()


def save_state(
    state,
):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = (
        STATE_DIR
        / "calendar.json"
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


def print_event(
    event,
):

    print(
        event[
            "currency"
        ],
        "|",
        event[
            "event_time_utc"
        ],
        "|",
        event[
            "importance"
        ],
        "|",
        event[
            "event_code"
        ],
        "| en",
        event[
            "minutes_until_event"
        ],
        "min",
    )


def main():

    print("=" * 90)

    print(
        "TRADING AGENT - "
        "ECONOMIC CALENDAR STATE"
    )

    print("=" * 90)

    path = (
        get_calendar_path()
    )

    print()
    print(
        "CSV:",
        path,
    )

    df = load_calendar_csv(
        path
    )

    state = (
        build_calendar_state(
            df
        )
    )

    print()
    print(
        "Calendar fresh:",
        state[
            "calendar_fresh"
        ],
    )

    print(
        "Age:",
        state[
            "calendar_age_seconds"
        ],
        "seconds",
    )

    # ======================================================
    # SIMBOLOS
    # ======================================================

    for symbol, data in (
        state[
            "symbols"
        ].items()
    ):

        print()
        print("=" * 70)
        print(symbol)
        print("=" * 70)

        print(
            "Currencies:",
            data[
                "currencies"
            ],
        )

        print(
            "Macro risk:",
            data[
                "macro_risk"
            ],
        )

        print(
            "New trades blocked:",
            data[
                "new_trades_blocked"
            ],
        )

        print(
            "Blocking reasons:",
            data[
                "blocking_reasons"
            ],
        )

        print()
        print(
            "Blocking events:"
        )

        if not data[
            "blocking_events"
        ]:

            print(
                "  Ninguno"
            )

        else:

            for event in (
                data[
                    "blocking_events"
                ]
            ):

                print(
                    " ",
                    end="",
                )

                print_event(
                    event
                )

        print()
        print(
            "Proximos HIGH:"
        )

        if not data[
            "upcoming_high_events"
        ]:

            print(
                "  Ninguno"
            )

        else:

            for event in (
                data[
                    "upcoming_high_events"
                ][:5]
            ):

                print(
                    " ",
                    end="",
                )

                print_event(
                    event
                )

        print()
        print(
            "Avisos MODERATE:"
        )

        if not data[
            "moderate_warnings"
        ]:

            print(
                "  Ninguno"
            )

        else:

            for event in (
                data[
                    "moderate_warnings"
                ]
            ):

                print(
                    " ",
                    end="",
                )

                print_event(
                    event
                )

    file_path = (
        save_state(
            state
        )
    )

    print()
    print(
        "Guardado:",
        file_path,
    )


if __name__ == "__main__":
    main()