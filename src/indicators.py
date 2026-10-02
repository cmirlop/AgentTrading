import math

import numpy as np
import pandas as pd


# ==========================================================
# INDICADORES
# ==========================================================

def add_indicators(df):
    """
    Añade indicadores técnicos al DataFrame.

    La función NO modifica el DataFrame original.
    """

    data = df.copy()

    data = (
        data
        .sort_values("time")
        .reset_index(drop=True)
    )

    close = data["close"]
    high = data["high"]
    low = data["low"]

    # ------------------------------------------------------
    # EMA
    # ------------------------------------------------------

    data["ema20"] = (
        close
        .ewm(
            span=20,
            adjust=False,
            min_periods=20,
        )
        .mean()
    )

    data["ema50"] = (
        close
        .ewm(
            span=50,
            adjust=False,
            min_periods=50,
        )
        .mean()
    )

    data["ema200"] = (
        close
        .ewm(
            span=200,
            adjust=False,
            min_periods=200,
        )
        .mean()
    )

    # ------------------------------------------------------
    # RSI 14 - Wilder
    # ------------------------------------------------------

    delta = close.diff()

    gains = delta.clip(lower=0)

    losses = (
        -delta.clip(upper=0)
    )

    average_gain = gains.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    average_loss = losses.ewm(
        alpha=1 / 14,
        adjust=False,
        min_periods=14,
    ).mean()

    rs = (
        average_gain
        / average_loss.replace(0, np.nan)
    )

    data["rsi14"] = (
        100
        - (100 / (1 + rs))
    )

    # Caso especial:
    # ganancias pero ninguna perdida.
    data.loc[
        (average_loss == 0)
        & (average_gain > 0),
        "rsi14",
    ] = 100

    # ------------------------------------------------------
    # ATR 14 - Wilder
    # ------------------------------------------------------

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    data["atr14"] = (
        true_range
        .ewm(
            alpha=1 / 14,
            adjust=False,
            min_periods=14,
        )
        .mean()
    )

    # ------------------------------------------------------
    # ESTRUCTURA 20 VELAS
    # ------------------------------------------------------

    data["high20"] = (
        high
        .rolling(
            window=20,
            min_periods=20,
        )
        .max()
    )

    data["low20"] = (
        low
        .rolling(
            window=20,
            min_periods=20,
        )
        .min()
    )

    range20 = (
        data["high20"]
        - data["low20"]
    )

    data["range_position_20"] = (
        (close - data["low20"])
        / range20.replace(0, np.nan)
    )

    # ------------------------------------------------------
    # VOLUMEN
    # ------------------------------------------------------

    data["volume_sma20"] = (
        data["tick_volume"]
        .rolling(
            window=20,
            min_periods=20,
        )
        .mean()
    )

    data["volume_ratio"] = (
        data["tick_volume"]
        / data["volume_sma20"]
        .replace(0, np.nan)
    )

    # ------------------------------------------------------
    # RETORNOS
    # ------------------------------------------------------

    data["return_1"] = (
        close.pct_change(1)
    )

    data["return_5"] = (
        close.pct_change(5)
    )

    # ------------------------------------------------------
    # CARACTERISTICAS VELA
    # ------------------------------------------------------

    data["candle_range"] = (
        high - low
    )

    data["candle_body"] = (
        (close - data["open"]).abs()
    )

    data["range_atr_ratio"] = (
        data["candle_range"]
        / data["atr14"]
        .replace(0, np.nan)
    )

    return data


# ==========================================================
# CONVERSION JSON
# ==========================================================

def safe_number(value):
    """
    Convierte numpy/pandas a valores compatibles con JSON.
    """

    if value is None:
        return None

    try:

        if pd.isna(value):
            return None

    except TypeError:
        pass

    if isinstance(
        value,
        (
            np.integer,
            int,
        ),
    ):
        return int(value)

    if isinstance(
        value,
        (
            np.floating,
            float,
        ),
    ):

        value = float(value)

        if not math.isfinite(value):
            return None

        return value

    return value


def get_indicator_snapshot(df):
    """
    Obtiene únicamente los indicadores
    correspondientes a la última vela.
    """

    if df.empty:
        return {}

    row = df.iloc[-1]

    fields = [
        "ema20",
        "ema50",
        "ema200",
        "rsi14",
        "atr14",
        "high20",
        "low20",
        "range_position_20",
        "volume_sma20",
        "volume_ratio",
        "return_1",
        "return_5",
        "candle_range",
        "candle_body",
        "range_atr_ratio",
    ]

    return {
        field: safe_number(row[field])
        for field in fields
    }