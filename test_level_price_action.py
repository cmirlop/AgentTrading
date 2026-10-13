from pprint import pprint

from src.historical_state import (
    prepare_symbol_history,
)

from src.level_price_action import (
    build_level_price_action,
    build_level_price_action_evidence,
)


SYMBOL = "USDCAD"


CASES = [
    {
        "analysis_time":
            "2026-04-08T07:30:00+00:00",

        "actual_outcome":
            "BREAK",

        "active_level":
            "SUPPORT",

        "level_facts": {
            "reference_atr_m15":
                0.000646,

            "support": {
                "low":
                    1.38314,

                "high":
                    1.38356,
            },
        },
    },

    {
        "analysis_time":
            "2026-04-08T08:00:00+00:00",

        "actual_outcome":
            "HOLD",

        "active_level":
            "SUPPORT",

        "level_facts": {
            "reference_atr_m15":
                0.000600,

            "support": {
                "low":
                    1.38315,

                "high":
                    1.38333,
            },
        },
    },

    {
        "analysis_time":
            "2026-04-08T09:45:00+00:00",

        "actual_outcome":
            "BREAK",

        "active_level":
            "RESISTANCE",

        "level_facts": {
            "reference_atr_m15":
                0.000675,

            "resistance": {
                "low":
                    1.38413,

                "high":
                    1.38443,
            },
        },
    },

    {
        "analysis_time":
            "2026-04-08T15:00:00+00:00",

        "actual_outcome":
            "HOLD",

        "active_level":
            "RESISTANCE",

        "level_facts": {
            "reference_atr_m15":
                0.000723,

            "resistance": {
                "low":
                    1.38647,

                "high":
                    1.38669,
            },
        },
    },
]


history = prepare_symbol_history(
    SYMBOL
)


for case in CASES:

    price_action = build_level_price_action(
        prepared_history=
            history,

        analysis_time=
            case["analysis_time"],

        level_facts=
            case["level_facts"],

        active_level=
            case["active_level"],
    )

    evidence = (
        build_level_price_action_evidence(
            price_action
        )
    )

    print(
        "\n"
        + "=" * 100
    )

    print(
        "TIME:",
        case["analysis_time"],
    )

    print(
        "ACTIVE LEVEL:",
        case["active_level"],
    )

    print(
        "ACTUAL OUTCOME:",
        case["actual_outcome"],
    )

    print(
        "=" * 100
    )

    pprint(
        price_action,
        sort_dicts=False,
    )

    print(
        "\nPRICE ACTION EVIDENCE:"
    )

    for item in evidence:

        print(
            " -",
            item,
        )