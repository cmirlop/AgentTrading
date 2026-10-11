def _strength_label(
    value,
):

    if value >= 80:
        return "STRONG"

    if value >= 60:
        return "MEDIUM"

    return "WEAK"


def _distance_label(
    distance_atr,
):

    if distance_atr <= 0.25:
        return "VERY_NEAR"

    if distance_atr <= 0.75:
        return "NEAR"

    if distance_atr <= 1.50:
        return "MODERATE"

    return "FAR"


def build_level_evidence(
    levels,
):

    if (
        not levels
        or levels.get(
            "status"
        )
        != "OK"
    ):

        return {
            "status":
                "INVALID",

            "available_evidence_ids":
                [],

            "market_structure":
                "UNKNOWN",
        }

    support = levels.get(
        "nearest_support"
    )

    resistance = levels.get(
        "nearest_resistance"
    )

    evidence = []

    facts = {
        "current_price":
            levels.get(
                "current_price"
            ),

        "reference_atr_m15":
            levels.get(
                "reference_atr_m15"
            ),
    }

    # ======================================================
    # SUPPORT
    # ======================================================

    if support:

        support_strength = (
            _strength_label(
                support[
                    "strength"
                ]
            )
        )

        support_distance = (
            _distance_label(
                support[
                    "distance_atr"
                ]
            )
        )

        facts[
            "support"
        ] = {
            **support,

            "strength_label":
                support_strength,

            "distance_label":
                support_distance,
        }

        evidence.append(
            "NEAREST_SUPPORT_PRESENT"
        )

        evidence.append(
            (
                "SUPPORT_"
                + support_strength
            )
        )

        evidence.append(
            (
                "SUPPORT_DISTANCE_"
                + support_distance
            )
        )

        if (
            len(
                support.get(
                    "timeframes",
                    [],
                )
            )
            >= 2
        ):

            evidence.append(
                "SUPPORT_MULTI_TIMEFRAME"
            )

        if (
            support.get(
                "touches",
                0,
            )
            >= 4
        ):

            evidence.append(
                "SUPPORT_MULTIPLE_TOUCHES"
            )

        if (
            support.get(
                "distance_atr",
                999,
            )
            <= 0.25
        ):

            evidence.append(
                "PRICE_AT_SUPPORT"
            )

    # ======================================================
    # RESISTANCE
    # ======================================================

    if resistance:

        resistance_strength = (
            _strength_label(
                resistance[
                    "strength"
                ]
            )
        )

        resistance_distance = (
            _distance_label(
                resistance[
                    "distance_atr"
                ]
            )
        )

        facts[
            "resistance"
        ] = {
            **resistance,

            "strength_label":
                resistance_strength,

            "distance_label":
                resistance_distance,
        }

        evidence.append(
            "NEAREST_RESISTANCE_PRESENT"
        )

        evidence.append(
            (
                "RESISTANCE_"
                + resistance_strength
            )
        )

        evidence.append(
            (
                "RESISTANCE_DISTANCE_"
                + resistance_distance
            )
        )

        if (
            len(
                resistance.get(
                    "timeframes",
                    [],
                )
            )
            >= 2
        ):

            evidence.append(
                "RESISTANCE_MULTI_TIMEFRAME"
            )

        if (
            resistance.get(
                "touches",
                0,
            )
            >= 4
        ):

            evidence.append(
                "RESISTANCE_MULTIPLE_TOUCHES"
            )

        if (
            resistance.get(
                "distance_atr",
                999,
            )
            <= 0.25
        ):

            evidence.append(
                "PRICE_AT_RESISTANCE"
            )

    # ======================================================
    # RELACION ENTRE LOS DOS NIVELES
    # ======================================================

    market_structure = (
        "OPEN_SPACE"
    )

    if (
        support
        and
        resistance
    ):

        support_distance_atr = float(
            support[
                "distance_atr"
            ]
        )

        resistance_distance_atr = float(
            resistance[
                "distance_atr"
            ]
        )

        total_distance_atr = (
            support_distance_atr
            +
            resistance_distance_atr
        )

        facts[
            "distance_between_nearest_levels_atr"
        ] = round(
            total_distance_atr,
            2,
        )

        if (
            support_distance_atr
            <= 0.25
            and
            resistance_distance_atr
            <= 0.25
        ):

            market_structure = (
                "COMPRESSED_BETWEEN_LEVELS"
            )

            evidence.append(
                "PRICE_COMPRESSED_BETWEEN_LEVELS"
            )

        elif (
            support_distance_atr
            <= 0.25
        ):

            market_structure = (
                "AT_SUPPORT"
            )

        elif (
            resistance_distance_atr
            <= 0.25
        ):

            market_structure = (
                "AT_RESISTANCE"
            )

        support_strength_value = int(
            support[
                "strength"
            ]
        )

        resistance_strength_value = int(
            resistance[
                "strength"
            ]
        )

        strength_difference = (
            support_strength_value
            -
            resistance_strength_value
        )

        facts[
            "strength_difference_support_minus_resistance"
        ] = (
            strength_difference
        )

        if strength_difference >= 15:

            evidence.append(
                "SUPPORT_STRONGER_THAN_RESISTANCE"
            )

        elif strength_difference <= -15:

            evidence.append(
                "RESISTANCE_STRONGER_THAN_SUPPORT"
            )

    elif support:

        if (
            support[
                "distance_atr"
            ]
            <= 0.25
        ):

            market_structure = (
                "AT_SUPPORT"
            )

    elif resistance:

        if (
            resistance[
                "distance_atr"
            ]
            <= 0.25
        ):

            market_structure = (
                "AT_RESISTANCE"
            )

    evidence = list(
        dict.fromkeys(
            evidence
        )
    )

    return {
        "status":
            "OK",

        "symbol":
            levels.get(
                "symbol"
            ),

        "analysis_time":
            levels.get(
                "analysis_time"
            ),

        "market_structure":
            market_structure,

        "facts":
            facts,

        "available_evidence_ids":
            evidence,

        "source":
            "DETERMINISTIC_PYTHON",
    }