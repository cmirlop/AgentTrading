import pandas as pd

from config.risk_config import (
    LOT_SIZE,
)

from src.historical_trade_planner import (
    SYMBOL_POINT,
)


# Contrato estándar Forex de estos tres símbolos.
# Después podemos guardar estas especificaciones
# directamente desde MT5 para hacer el backtest
# completamente reproducible.
CONTRACT_SIZE = {
    "EURUSD": 100_000,
    "USDCAD": 100_000,
    "AUDUSD": 100_000,
}


def historical_mid_price(
    prepared_history,
    symbol,
    analysis_time,
):

    df = prepared_history[
        symbol
    ]["M5"]

    analysis_time = pd.Timestamp(
        analysis_time
    )

    end_index = (
        df["bar_close"]
        .searchsorted(
            analysis_time,
            side="right",
        )
    )

    if end_index <= 0:

        raise RuntimeError(
            f"No historical price for "
            f"{symbol} at {analysis_time}"
        )

    row = df.iloc[
        end_index - 1
    ]

    point = SYMBOL_POINT[
        symbol
    ]

    bid = float(
        row["close"]
    )

    spread_price = (
        float(
            row.get(
                "spread",
                0,
            )
            or 0
        )
        * point
    )

    ask = (
        bid
        + spread_price
    )

    return (
        bid + ask
    ) / 2.0


def estimate_loss_eur(
    plan,
    analysis_time,
    prepared_history,
):

    symbol = plan[
        "symbol"
    ]

    entry = float(
        plan["entry"]
    )

    stop_loss = float(
        plan["stop_loss"]
    )

    price_distance = abs(
        entry
        - stop_loss
    )

    # P&L inicialmente expresado
    # en la divisa cotizada.
    quote_loss = (
        CONTRACT_SIZE[
            symbol
        ]
        * LOT_SIZE
        * price_distance
    )

    eurusd = (
        historical_mid_price(
            prepared_history,
            "EURUSD",
            analysis_time,
        )
    )

    if eurusd <= 0:

        raise RuntimeError(
            "Invalid EURUSD conversion"
        )

    # EURUSD y AUDUSD generan P&L
    # inicialmente en USD.
    if symbol in {
        "EURUSD",
        "AUDUSD",
    }:

        loss_eur = (
            quote_loss
            / eurusd
        )

    # USDCAD genera P&L en CAD.
    elif symbol == "USDCAD":

        usdcad = (
            historical_mid_price(
                prepared_history,
                "USDCAD",
                analysis_time,
            )
        )

        if usdcad <= 0:

            raise RuntimeError(
                "Invalid USDCAD conversion"
            )

        loss_usd = (
            quote_loss
            / usdcad
        )

        loss_eur = (
            loss_usd
            / eurusd
        )

    else:

        raise ValueError(
            f"Unsupported symbol: "
            f"{symbol}"
        )

    return round(
        loss_eur,
        4,
    )