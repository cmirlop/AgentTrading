def _relation(value, reference):
    if value is None or reference is None:
        return "UNKNOWN"

    if value > reference:
        return "ABOVE"

    if value < reference:
        return "BELOW"

    return "EQUAL"


def _rsi_zone(rsi):
    if rsi is None:
        return "UNKNOWN"

    if rsi < 30:
        return "OVERSOLD"

    if rsi < 45:
        return "WEAK"

    if rsi <= 55:
        return "NEUTRAL"

    if rsi <= 70:
        return "STRONG"

    return "OVERBOUGHT"


def _ema_structure(ema20, ema50, ema200):

    if ema20 is None or ema50 is None:
        return "UNKNOWN"

    if ema200 is None:

        if ema20 > ema50:
            return "PARTIAL_BULLISH"

        if ema20 < ema50:
            return "PARTIAL_BEARISH"

        return "MIXED"

    if ema20 > ema50 > ema200:
        return "BULLISH"

    if ema20 < ema50 < ema200:
        return "BEARISH"

    return "MIXED"


def _range_zone(position):

    if position is None:
        return "UNKNOWN"

    if position >= 0.70:
        return "UPPER"

    if position <= 0.30:
        return "LOWER"

    return "MIDDLE"


def _volume_zone(ratio):

    if ratio is None:
        return "UNKNOWN"

    if ratio >= 1.20:
        return "HIGH"

    if ratio <= 0.80:
        return "LOW"

    return "NORMAL"


def build_compact_snapshot(
    market_state,
    candles_per_timeframe=4,
):

    result = {
        "symbol": market_state["symbol"],
        "analysis_time": market_state["analysis_time"],
        "data_quality": market_state["data_quality"],
        "timeframes": {},
        "available_evidence_ids": [],
    }

    evidence = result["available_evidence_ids"]

    for timeframe, state in market_state["timeframes"].items():

        indicators = state["indicators"]

        candles = state["recent_candles"]

        last_candle = candles[-1]

        close = last_candle["close"]

        ema20 = indicators["ema20"]
        ema50 = indicators["ema50"]
        ema200 = indicators["ema200"]

        rsi = indicators["rsi14"]

        price_ema20 = _relation(
            close,
            ema20,
        )

        price_ema50 = _relation(
            close,
            ema50,
        )

        price_ema200 = _relation(
            close,
            ema200,
        )

        ema_structure = _ema_structure(
            ema20,
            ema50,
            ema200,
        )

        rsi_zone = _rsi_zone(rsi)

        range_zone = _range_zone(
            indicators["range_position_20"]
        )

        volume_zone = _volume_zone(
            indicators["volume_ratio"]
        )

        return_5 = indicators["return_5"]

        if return_5 is None:
            return5_direction = "UNKNOWN"

        elif return_5 > 0:
            return5_direction = "POSITIVE"

        elif return_5 < 0:
            return5_direction = "NEGATIVE"

        else:
            return5_direction = "FLAT"

        tf_snapshot = {
            "close": close,

            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,

            "price_vs_ema20":
                price_ema20,

            "price_vs_ema50":
                price_ema50,

            "price_vs_ema200":
                price_ema200,

            "ema_structure":
                ema_structure,

            "rsi14": rsi,
            "rsi_zone": rsi_zone,

            "atr14":
                indicators["atr14"],

            "range_position_20":
                indicators[
                    "range_position_20"
                ],

            "range_zone":
                range_zone,

            "volume_ratio":
                indicators["volume_ratio"],

            "volume_zone":
                volume_zone,

            "return_1":
                indicators["return_1"],

            "return_5":
                return_5,

            "return5_direction":
                return5_direction,

            "recent_candles":
                candles[
                    -candles_per_timeframe:
                ],
        }

        result[
            "timeframes"
        ][timeframe] = tf_snapshot

        # --------------------------------------------------
        # HECHOS VERIFICADOS POR PYTHON
        # --------------------------------------------------

        evidence.extend(
            [
                f"{timeframe}_PRICE_{price_ema20}_EMA20",
                f"{timeframe}_PRICE_{price_ema50}_EMA50",
                f"{timeframe}_PRICE_{price_ema200}_EMA200",
                f"{timeframe}_EMA_STRUCTURE_{ema_structure}",
                f"{timeframe}_RSI_{rsi_zone}",
                f"{timeframe}_RANGE_{range_zone}",
                f"{timeframe}_VOLUME_{volume_zone}",
                f"{timeframe}_RETURN5_{return5_direction}",
            ]
        )

    return result