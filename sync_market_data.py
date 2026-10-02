from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from config.market_config import SYMBOLS, HISTORY_DAYS, TIMEFRAMES


# ==========================================================
# CONFIGURACION
# ==========================================================

MT5_TIMEFRAMES = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

TIMEFRAME_SECONDS = {
    "M5": 5 * 60,
    "M15": 15 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "market"


# ==========================================================
# MT5
# ==========================================================

def connect_mt5():

    if not mt5.initialize():
        raise RuntimeError(
            f"No se pudo conectar con MT5: {mt5.last_error()}"
        )

    print("[OK] Conexion con MetaTrader 5")


# ==========================================================
# DATOS
# ==========================================================

def download_bars(symbol, timeframe_name, date_from, date_to):

    timeframe = MT5_TIMEFRAMES[timeframe_name]

    rates = mt5.copy_rates_range(
        symbol,
        timeframe,
        date_from,
        date_to,
    )

    if rates is None:
        print(
            f"[ERROR] {symbol} {timeframe_name}: "
            f"{mt5.last_error()}"
        )
        return None

    if len(rates) == 0:
        return pd.DataFrame()

    df = pd.DataFrame(rates)

    df["time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True,
    )

    return df


def remove_open_bars(df, timeframe_name):
    """
    Conservamos solamente velas completamente cerradas.

    time representa la hora de apertura de la vela.
    """

    if df.empty:
        return df

    now = pd.Timestamp.now(tz="UTC")

    duration = pd.Timedelta(
        seconds=TIMEFRAME_SECONDS[timeframe_name]
    )

    # Una vela está cerrada si:
    #
    # apertura + duración <= ahora
    #
    closed = (
        df["time"] + duration <= now
    )

    return df.loc[closed].copy()


# ==========================================================
# ARCHIVOS LOCALES
# ==========================================================

def load_local_data(file_path):

    if not file_path.exists():
        return None

    df = pd.read_csv(
        file_path,
        parse_dates=["time"],
    )

    df["time"] = pd.to_datetime(
        df["time"],
        utc=True,
    )

    return df


def merge_data(old_df, new_df):

    if old_df is None or old_df.empty:
        merged = new_df.copy()

    elif new_df is None or new_df.empty:
        merged = old_df.copy()

    else:
        merged = pd.concat(
            [old_df, new_df],
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

    return merged


# ==========================================================
# SINCRONIZACION
# ==========================================================

def sync_symbol_timeframe(symbol, timeframe_name):

    symbol_dir = DATA_DIR / symbol

    symbol_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_path = (
        symbol_dir
        / f"{timeframe_name}.csv"
    )

    old_df = load_local_data(file_path)

    now_utc = datetime.now(timezone.utc)

    # ------------------------------------------------------
    # SI YA TENEMOS DATOS
    # ------------------------------------------------------

    if old_df is not None and not old_df.empty:

        previous_rows = len(old_df)

        last_local_time = old_df["time"].max()

        # Descargamos algunas velas anteriores también.
        # Esto permite corregir posibles diferencias o huecos.
        overlap_seconds = (
            TIMEFRAME_SECONDS[timeframe_name]
            * 3
        )

        date_from = (
            last_local_time.to_pydatetime()
            - timedelta(seconds=overlap_seconds)
        )

    # ------------------------------------------------------
    # SI TODAVIA NO HAY ARCHIVO
    # ------------------------------------------------------

    else:

        previous_rows = 0

        date_from = (
            now_utc
            - timedelta(days=HISTORY_DAYS)
        )

    new_df = download_bars(
        symbol=symbol,
        timeframe_name=timeframe_name,
        date_from=date_from,
        date_to=now_utc,
    )

    if new_df is None:
        return

    new_df = remove_open_bars(
        new_df,
        timeframe_name,
    )

    merged = merge_data(
        old_df,
        new_df,
    )

    merged = remove_open_bars(
        merged,
        timeframe_name,
    )

    # ------------------------------------------------------
    # VALIDACIONES BASICAS
    # ------------------------------------------------------

    if merged.empty:

        print(
            f"[AVISO] {symbol} {timeframe_name}: "
            "sin datos"
        )

        return

    merged.to_csv(
        file_path,
        index=False,
    )

    total_rows = len(merged)

    added_rows = (
        total_rows - previous_rows
    )

    first_time = merged["time"].iloc[0]
    last_time = merged["time"].iloc[-1]

    print(
        f"[OK] {symbol:<7} "
        f"{timeframe_name:<3} | "
        f"total={total_rows:<7,} | "
        f"nuevas={added_rows:<4} | "
        f"ultima={last_time}"
    )


# ==========================================================
# MAIN
# ==========================================================

def main():

    print("=" * 100)
    print(
        "TRADING AGENT - SINCRONIZACION "
        "INCREMENTAL DE MERCADO"
    )
    print("=" * 100)

    connect_mt5()

    try:

        terminal = mt5.terminal_info()

        if terminal is not None:

            print()
            print("--- SEGURIDAD ---")

            print(
                "Trading terminal permitido:",
                terminal.trade_allowed
            )

            print(
                "Python trading deshabilitado:",
                terminal.tradeapi_disabled
            )

        print()
        print("--- SINCRONIZACION ---")
        print()

        for symbol in SYMBOLS:

            if not mt5.symbol_select(
                symbol,
                True,
            ):

                print(
                    f"[ERROR] No se pudo "
                    f"seleccionar {symbol}"
                )

                continue

            for timeframe_name in TIMEFRAMES:

                sync_symbol_timeframe(
                    symbol,
                    timeframe_name,
                )

    finally:

        mt5.shutdown()

        print()
        print(
            "[OK] Conexion MT5 cerrada"
        )

        print("=" * 100)


if __name__ == "__main__":
    main()