from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from config.market_config import (
    SYMBOLS,
    TIMEFRAMES,
)

from config.backtest_config import (
    BACKTEST_EVALUATION_DAYS,
    BACKTEST_WARMUP_DAYS,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

MARKET_DIR = (
    BASE_DIR
    / "data"
    / "market"
)


TF_MAP = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


TF_MINUTES = {
    "M5": 5,
    "M15": 15,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def load_existing(
    path,
):

    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(
        path
    )

    df["time"] = pd.to_datetime(
        df["time"],
        utc=True,
    )

    return df


def download_history(
    symbol,
    timeframe,
):

    now = datetime.now(
        timezone.utc
    )

    total_days = (
        BACKTEST_EVALUATION_DAYS
        +
        BACKTEST_WARMUP_DAYS[
            timeframe
        ]
    )

    date_from = (
        now
        -
        timedelta(
            days=total_days
        )
    )

    rates = mt5.copy_rates_range(
        symbol,
        TF_MAP[timeframe],
        date_from,
        now,
    )

    if rates is None:

        raise RuntimeError(
            f"{symbol} {timeframe}: "
            f"{mt5.last_error()}"
        )

    df = pd.DataFrame(
        rates
    )

    if df.empty:
        return df

    df["time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True,
    )

    # Eliminar vela todavía abierta.
    duration = pd.Timedelta(
        minutes=
            TF_MINUTES[
                timeframe
            ]
    )

    now_ts = pd.Timestamp.now(
        tz="UTC"
    )

    df = df[
        (
            df["time"]
            + duration
        )
        <= now_ts
    ].copy()

    return df


def merge_save(
    existing,
    downloaded,
    path,
):

    if existing.empty:

        merged = downloaded.copy()

    elif downloaded.empty:

        merged = existing.copy()

    else:

        merged = pd.concat(
            [
                existing,
                downloaded,
            ],
            ignore_index=True,
        )

    merged = (
        merged
        .drop_duplicates(
            subset=["time"],
            keep="last",
        )
        .sort_values("time")
        .reset_index(drop=True)
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    merged.to_csv(
        path,
        index=False,
    )

    return merged


def main():

    print("=" * 90)
    print(
        "TRADING AGENT - "
        "BACKTEST HISTORY BACKFILL"
    )
    print("=" * 90)

    if not mt5.initialize():

        raise RuntimeError(
            f"MT5 initialize: "
            f"{mt5.last_error()}"
        )

    try:

        terminal = (
            mt5.terminal_info()
        )

        print()
        print(
            "Python trading deshabilitado:",
            terminal.tradeapi_disabled,
        )

        for symbol in SYMBOLS:

            print()
            print("=" * 70)
            print(symbol)
            print("=" * 70)

            for timeframe in TIMEFRAMES:

                path = (
                    MARKET_DIR
                    / symbol
                    / f"{timeframe}.csv"
                )

                existing = (
                    load_existing(
                        path
                    )
                )

                downloaded = (
                    download_history(
                        symbol,
                        timeframe,
                    )
                )

                merged = merge_save(
                    existing,
                    downloaded,
                    path,
                )

                if merged.empty:

                    print(
                        timeframe,
                        "SIN DATOS",
                    )

                    continue

                print(
                    f"{timeframe:<3}",
                    "| bars:",
                    f"{len(merged):,}",
                    "| first:",
                    merged[
                        "time"
                    ].iloc[0],
                    "| last:",
                    merged[
                        "time"
                    ].iloc[-1],
                )

    finally:

        mt5.shutdown()


if __name__ == "__main__":
    main()