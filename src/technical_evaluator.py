def _sign(value, threshold=15):

    if value >= threshold:
        return 1

    if value <= -threshold:
        return -1

    return 0


def evaluate_timeframe(
    timeframe,
    tf,
):

    score = 0.0
    maximum = 0.0

    bullish = []
    bearish = []

    # ======================================================
    # PRECIO VS EMA
    # ======================================================

    for ema in [
        "ema20",
        "ema50",
        "ema200",
    ]:

        key = (
            f"price_vs_{ema}"
        )

        relation = tf[key]

        evidence_id = (
            f"{timeframe}_PRICE_"
            f"{relation}_{ema.upper()}"
        )

        if relation == "ABOVE":

            score += 1
            maximum += 1

            bullish.append(
                evidence_id
            )

        elif relation == "BELOW":

            score -= 1
            maximum += 1

            bearish.append(
                evidence_id
            )

        elif relation != "UNKNOWN":

            maximum += 1

    # ======================================================
    # ESTRUCTURA EMAS
    # ======================================================

    structure = (
        tf["ema_structure"]
    )

    structure_id = (
        f"{timeframe}_EMA_STRUCTURE_"
        f"{structure}"
    )

    if structure == "BULLISH":

        score += 2
        maximum += 2
        bullish.append(structure_id)

    elif structure == "BEARISH":

        score -= 2
        maximum += 2
        bearish.append(structure_id)

    elif structure == "PARTIAL_BULLISH":

        score += 1
        maximum += 2
        bullish.append(structure_id)

    elif structure == "PARTIAL_BEARISH":

        score -= 1
        maximum += 2
        bearish.append(structure_id)

    else:

        maximum += 2

    # ======================================================
    # RSI
    # ======================================================

    rsi_zone = (
        tf["rsi_zone"]
    )

    rsi_id = (
        f"{timeframe}_RSI_"
        f"{rsi_zone}"
    )

    maximum += 1

    if rsi_zone == "STRONG":

        score += 1
        bullish.append(rsi_id)

    elif rsi_zone == "OVERBOUGHT":

        score += 0.5
        bullish.append(rsi_id)

    elif rsi_zone == "WEAK":

        score -= 1
        bearish.append(rsi_id)

    elif rsi_zone == "OVERSOLD":

        score -= 0.5
        bearish.append(rsi_id)

    # ======================================================
    # RETURN 5
    # ======================================================

    direction = (
        tf["return5_direction"]
    )

    return_id = (
        f"{timeframe}_RETURN5_"
        f"{direction}"
    )

    maximum += 1

    if direction == "POSITIVE":

        score += 1
        bullish.append(return_id)

    elif direction == "NEGATIVE":

        score -= 1
        bearish.append(return_id)

    # ======================================================
    # NORMALIZAR
    # ======================================================

    normalized = 0.0

    if maximum > 0:

        normalized = (
            score
            / maximum
            * 100
        )

    return {
        "score": round(
            normalized,
            2,
        ),

        "bullish_evidence":
            bullish,

        "bearish_evidence":
            bearish,
    }


def evaluate_technical_state(
    snapshot,
):

    tf_results = {}

    for timeframe, tf in (
        snapshot[
            "timeframes"
        ].items()
    ):

        tf_results[
            timeframe
        ] = evaluate_timeframe(
            timeframe,
            tf,
        )

    scores = {
        timeframe:
            result["score"]

        for timeframe, result
        in tf_results.items()
    }

    # ======================================================
    # PESOS INTRADIA
    # ======================================================

    weights = {
        "M5": 0.10,
        "M15": 0.30,
        "H1": 0.30,
        "H4": 0.20,
        "D1": 0.10,
    }

    overall_score = sum(
        scores[tf]
        * weights[tf]

        for tf in weights
    )

    overall_score = round(
        overall_score,
        2,
    )

    # ======================================================
    # BIAS
    # ======================================================

    if overall_score >= 15:

        bias = "BULLISH"

    elif overall_score <= -15:

        bias = "BEARISH"

    else:

        bias = "NEUTRAL"

    # ======================================================
    # ALINEACION
    # ======================================================

    m15_sign = _sign(
        scores["M15"]
    )

    h1_sign = _sign(
        scores["H1"]
    )

    h4_sign = _sign(
        scores["H4"]
    )

    core = [
        m15_sign,
        h1_sign,
        h4_sign,
    ]

    non_zero = [
        x
        for x in core
        if x != 0
    ]

    if (
        len(non_zero) == 3
        and
        len(set(non_zero)) == 1
    ):

        alignment = "ALIGNED"

    elif (
        m15_sign != 0
        and
        h1_sign != 0
        and
        m15_sign != h1_sign
    ):

        alignment = "CONFLICT"

    elif (
        m15_sign != 0
        and
        h4_sign != 0
        and
        m15_sign != h4_sign
        and
        abs(scores["H4"]) >= 30
    ):

        alignment = "CONFLICT"

    else:

        alignment = "PARTIAL"

    # ======================================================
    # CALIDAD DEL SETUP
    # ======================================================

    quality = (
        abs(overall_score) * 0.35
        +
        abs(scores["M15"]) * 0.25
        +
        abs(scores["H1"]) * 0.20
        +
        abs(scores["H4"]) * 0.10
    )

    if alignment == "ALIGNED":

        quality += 15

    elif alignment == "PARTIAL":

        quality += 5

    else:

        quality -= 20

    # Penalizar extensión extrema
    d1_rsi = (
        snapshot[
            "timeframes"
        ]["D1"]["rsi_zone"]
    )

    m15_rsi = (
        snapshot[
            "timeframes"
        ]["M15"]["rsi_zone"]
    )

    if d1_rsi in {
        "OVERBOUGHT",
        "OVERSOLD",
    }:

        quality -= 5

    if m15_rsi in {
        "OVERBOUGHT",
        "OVERSOLD",
    }:

        quality -= 10

    quality = int(
        max(
            0,
            min(
                100,
                round(quality),
            ),
        )
    )

    # ======================================================
    # EVIDENCIAS SEGUN SESGO
    # ======================================================

    bullish = []

    bearish = []

    for result in (
        tf_results.values()
    ):

        bullish.extend(
            result[
                "bullish_evidence"
            ]
        )

        bearish.extend(
            result[
                "bearish_evidence"
            ]
        )

    if bias == "BULLISH":

        supporting = bullish
        conflicting = bearish

    elif bias == "BEARISH":

        supporting = bearish
        conflicting = bullish

    else:

        supporting = []
        conflicting = (
            bullish
            +
            bearish
        )

    return {
        "bias": bias,

        "overall_score":
            overall_score,

        "alignment":
            alignment,

        "setup_quality":
            quality,

        "timeframe_scores":
            scores,

        "supporting_evidence":
            supporting,

        "conflicting_evidence":
            conflicting,
    }