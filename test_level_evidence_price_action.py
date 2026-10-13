from pprint import pprint

import pandas as pd

from src.historical_state import (
    prepare_symbol_history,
)

from src.support_resistance import (
    detect_support_resistance,
)

from src.level_evidence import (
    build_level_evidence,
)

from src.level_price_action import (
    build_level_price_action,
    build_level_price_action_evidence,
)


SYMBOL = "USDCAD"

TIMES = [
    "2026-04-08T07:30:00+00:00",
    "2026-04-08T08:00:00+00:00",
    "2026-04-08T09:45:00+00:00",
    "2026-04-08T15:00:00+00:00",
]


history = prepare_symbol_history(
    SYMBOL
)


for time_text in TIMES:

    analysis_time = pd.Timestamp(
        time_text
    )

    levels = detect_support_resistance(
        symbol=
            SYMBOL,

        analysis_time=
            analysis_time,

        prepared_history=
            history,
    )

    level_evidence = (
        build_level_evidence(
            levels
        )
    )

    market_structure = (
        level_evidence.get(
            "market_structure"
        )
    )

    if market_structure == "AT_SUPPORT":

        active_level = "SUPPORT"

    elif market_structure == "AT_RESISTANCE":

        active_level = "RESISTANCE"

    else:

        active_level = None


    if active_level is not None:

        price_action = (
            build_level_price_action(
                prepared_history=
                    history,

                analysis_time=
                    analysis_time,

                level_facts=
                    level_evidence.get(
                        "facts",
                        {},
                    ),

                active_level=
                    active_level,
            )
        )

        price_action_evidence = (
            build_level_price_action_evidence(
                price_action
            )
        )

    else:

        price_action = {}

        price_action_evidence = []


    level_evidence[
        "price_action"
    ] = price_action

    level_evidence[
        "price_action_evidence"
    ] = price_action_evidence

    level_evidence[
        "available_evidence_ids"
    ] = list(
        dict.fromkeys(
            level_evidence.get(
                "available_evidence_ids",
                [],
            )
            +
            price_action_evidence
        )
    )


    print(
        "\n"
        + "=" * 100
    )

    print(
        "TIME:",
        time_text
    )

    print(
        "MARKET STRUCTURE:",
        market_structure
    )

    print(
        "ACTIVE LEVEL:",
        active_level
    )

    print(
        "\nPRICE ACTION:"
    )

    pprint(
        price_action,
        sort_dicts=False,
    )

    print(
        "\nPRICE ACTION EVIDENCE:"
    )

    pprint(
        price_action_evidence,
        sort_dicts=False,
    )

    print(
        "\nIDS PRESENT IN AVAILABLE_EVIDENCE:"
    )

    pprint(
        [
            item
            for item in level_evidence.get(
                "available_evidence_ids",
                [],
            )
            if "_LEVEL_" in item
        ],
        sort_dicts=False,
    )