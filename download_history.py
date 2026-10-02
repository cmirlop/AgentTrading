from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from config.market_config import SYMBOLS, HISTORY_DAYS, TIMEFRAMES


# ==========================================================
# CONFIGURACION DE TIMEFRAMES
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


def connect_mt5():
    """
    Conecta con el terminal MetaTrader 5 ya abierto.
    """

    if not mt5.initialize():
        raise RuntimeError(
            f"No se pudo conectar con MT5: {mt5.last_error()}"
        )

    print("[OK] Conexion con MetaTrader 5")


def get_closed_bars(symbol, timeframe_name, date_from, date_to):
    """
    Descarga velas desde MT5.

    Solo devuelve velas que consideramos cerradas.
    """

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
        print(
            f"[AVISO] {symbol} {timeframe_name}: "
            "MT5 no devolvio datos"
        )
        return None

    df = pd.DataFrame(rates)

    # MT5 entrega las fechas en UTC
    df["time"] = pd.to_datetime(
        df["time"],
        unit="s",
        utc=True,
    )

    # Orden cronologico
    df = df.sort_values("time")

    # Eliminar posibles duplicados
    df = df.drop_duplicates(
        subset=["time"],
        keep="last",
    )

    # ------------------------------------------------------
    # EVITAR GUARDAR LA VELA QUE TODAVIA ESTA ABIERTA
    # ------------------------------------------------------

    timeframe_seconds = TIMEFRAME_SECONDS[timeframe_name]

    now_utc = datetime.now(timezone.utc)

    last_closed_limit = (
        pd.Timestamp(now_utc)
        - pd.Timedelta(seconds=timeframe_seconds)
    )

    df = df[
        df["time"] <= last_closed_limit
    ].copy()

    return df


def save_bars(symbol, timeframe_name, df):
    """
    Guarda las velas en CSV.
    """

    symbol_dir = DATA_DIR / symbol

    symbol_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_path = (
        symbol_dir
        / f"{timeframe_name}.csv"
    )

    df.to_csv(
        file_path,
        index=False,
    )

    return file_path


def print_summary(symbol, timeframe_name, df, file_path):

    if df is None or df.empty:
        return

    first_date = df["time"].iloc[0]
    last_date = df["time"].iloc[-1]

    print()
    print(
        f"[OK] {symbol} {timeframe_name}"
    )

    print(
        f"     Velas: {len(df):,}"
    )

    print(
        f"     Primera: {first_date}"
    )

    print(
        f"     Ultima:  {last_date}"
    )

    print(
        f"     Archivo: {file_path}"
    )


def main():

    print("=" * 70)
    print("TRADING AGENT - DESCARGA HISTORICA MT5")
    print("=" * 70)

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

        now_utc = datetime.now(timezone.utc)

        date_from = (
            now_utc
            - timedelta(days=HISTORY_DAYS)
        )

        print()
        print("--- PERIODO ---")
        print(
            "Desde:",
            date_from.isoformat()
        )

        print(
            "Hasta:",
            now_utc.isoformat()
        )

        print()

        # ==================================================
        # DESCARGA
        # ==================================================

        for symbol in SYMBOLS:

            print("=" * 70)
            print(f"SIMBOLO: {symbol}")
            print("=" * 70)

            if not mt5.symbol_select(
                symbol,
                True
            ):

                print(
                    f"[ERROR] No se pudo seleccionar {symbol}"
                )

                continue

            for timeframe_name in TIMEFRAMES:

                df = get_closed_bars(
                    symbol=symbol,
                    timeframe_name=timeframe_name,
                    date_from=date_from,
                    date_to=now_utc,
                )

                if df is None:
                    continue

                file_path = save_bars(
                    symbol,
                    timeframe_name,
                    df,
                )

                print_summary(
                    symbol,
                    timeframe_name,
                    df,
                    file_path,
                )

    finally:

        mt5.shutdown()

        print()
        print(
            "[OK] Conexion MT5 cerrada"
        )

        print("=" * 70)


if __name__ == "__main__":
    main()