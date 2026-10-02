from pathlib import Path

import pandas as pd

from config.market_config import SYMBOLS, TIMEFRAMES


# ==========================================================
# CONFIGURACION
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "market"

EXPECTED_COLUMNS = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
]

TIMEFRAME_SECONDS = {
    "M5": 5 * 60,
    "M15": 15 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}


# ==========================================================
# UTILIDADES
# ==========================================================

def crosses_weekend(previous_time, current_time):
    """
    Devuelve True si el hueco temporal atraviesa
    un sabado o domingo.
    """

    days = pd.date_range(
        start=previous_time.normalize(),
        end=current_time.normalize(),
        freq="D",
    )

    return any(day.weekday() >= 5 for day in days)


def check_timestamp_alignment(series, timeframe):
    """
    Comprueba que las velas empiecen en el momento
    correcto para su temporalidad.
    """

    if timeframe == "M5":
        valid = (
            (series.dt.minute % 5 == 0)
            & (series.dt.second == 0)
        )

    elif timeframe == "M15":
        valid = (
            (series.dt.minute % 15 == 0)
            & (series.dt.second == 0)
        )

    elif timeframe == "H1":
        valid = (
            (series.dt.minute == 0)
            & (series.dt.second == 0)
        )

    elif timeframe == "H4":
        valid = (
            (series.dt.hour % 4 == 0)
            & (series.dt.minute == 0)
            & (series.dt.second == 0)
        )

    elif timeframe == "D1":
        valid = (
            (series.dt.hour == 0)
            & (series.dt.minute == 0)
            & (series.dt.second == 0)
        )

    else:
        return pd.Series(
            True,
            index=series.index,
        )

    return valid


# ==========================================================
# VALIDACION
# ==========================================================

def validate_file(symbol, timeframe):

    file_path = (
        DATA_DIR
        / symbol
        / f"{timeframe}.csv"
    )

    print()
    print(
        f"--- {symbol} {timeframe} ---"
    )

    # ------------------------------------------------------
    # ARCHIVO
    # ------------------------------------------------------

    if not file_path.exists():

        print(
            f"[ERROR] Archivo inexistente: "
            f"{file_path}"
        )

        return {
            "status": "FAIL",
            "warnings": 0,
        }

    try:

        df = pd.read_csv(file_path)

    except Exception as exc:

        print(
            f"[ERROR] No se puede leer CSV: {exc}"
        )

        return {
            "status": "FAIL",
            "warnings": 0,
        }

    if df.empty:

        print("[ERROR] Archivo vacio")

        return {
            "status": "FAIL",
            "warnings": 0,
        }

    errors = []
    warnings = []

    # ------------------------------------------------------
    # COLUMNAS
    # ------------------------------------------------------

    missing_columns = [
        column
        for column in EXPECTED_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:

        errors.append(
            f"Columnas faltantes: "
            f"{missing_columns}"
        )

        for error in errors:
            print("[ERROR]", error)

        return {
            "status": "FAIL",
            "warnings": 0,
        }

    # ------------------------------------------------------
    # TIMESTAMP
    # ------------------------------------------------------

    try:

        df["time"] = pd.to_datetime(
            df["time"],
            utc=True,
        )

    except Exception as exc:

        print(
            "[ERROR] Timestamps invalidos:",
            exc,
        )

        return {
            "status": "FAIL",
            "warnings": 0,
        }

    # ------------------------------------------------------
    # ORDEN
    # ------------------------------------------------------

    if not df["time"].is_monotonic_increasing:

        errors.append(
            "Los timestamps no estan "
            "ordenados cronologicamente"
        )

    # ------------------------------------------------------
    # DUPLICADOS
    # ------------------------------------------------------

    duplicate_count = (
        df["time"]
        .duplicated()
        .sum()
    )

    if duplicate_count > 0:

        errors.append(
            f"{duplicate_count} timestamps duplicados"
        )

    # ------------------------------------------------------
    # NULOS
    # ------------------------------------------------------

    null_counts = (
        df[EXPECTED_COLUMNS]
        .isna()
        .sum()
    )

    total_nulls = int(
        null_counts.sum()
    )

    if total_nulls > 0:

        errors.append(
            f"{total_nulls} valores nulos"
        )

    # ------------------------------------------------------
    # PRECIOS POSITIVOS
    # ------------------------------------------------------

    prices = [
        "open",
        "high",
        "low",
        "close",
    ]

    invalid_prices = (
        df[prices] <= 0
    ).any(axis=1)

    invalid_price_count = int(
        invalid_prices.sum()
    )

    if invalid_price_count > 0:

        errors.append(
            f"{invalid_price_count} velas "
            f"con precios <= 0"
        )

    # ------------------------------------------------------
    # ESTRUCTURA OHLC
    # ------------------------------------------------------

    invalid_ohlc = (
        (df["high"] < df["low"])
        |
        (df["high"] < df["open"])
        |
        (df["high"] < df["close"])
        |
        (df["low"] > df["open"])
        |
        (df["low"] > df["close"])
    )

    invalid_ohlc_count = int(
        invalid_ohlc.sum()
    )

    if invalid_ohlc_count > 0:

        errors.append(
            f"{invalid_ohlc_count} velas "
            f"con estructura OHLC imposible"
        )

    # ------------------------------------------------------
    # VOLUMEN / SPREAD
    # ------------------------------------------------------

    negative_tick_volume = int(
        (df["tick_volume"] < 0).sum()
    )

    if negative_tick_volume > 0:

        errors.append(
            f"{negative_tick_volume} valores "
            f"tick_volume negativos"
        )

    negative_real_volume = int(
        (df["real_volume"] < 0).sum()
    )

    if negative_real_volume > 0:

        errors.append(
            f"{negative_real_volume} valores "
            f"real_volume negativos"
        )

    negative_spread = int(
        (df["spread"] < 0).sum()
    )

    if negative_spread > 0:

        errors.append(
            f"{negative_spread} spreads negativos"
        )

    # ------------------------------------------------------
    # ALINEACION TEMPORAL
    # ------------------------------------------------------

    alignment = check_timestamp_alignment(
        df["time"],
        timeframe,
    )

    alignment_errors = int(
        (~alignment).sum()
    )

    if alignment_errors > 0:

        errors.append(
            f"{alignment_errors} velas "
            f"mal alineadas temporalmente"
        )

    # ------------------------------------------------------
    # VELAS ABIERTAS / FUTURO
    # ------------------------------------------------------

    now_utc = pd.Timestamp.now(
        tz="UTC"
    )

    duration = pd.Timedelta(
        seconds=TIMEFRAME_SECONDS[timeframe]
    )

    open_or_future = (
        df["time"] + duration > now_utc
    )

    open_or_future_count = int(
        open_or_future.sum()
    )

    if open_or_future_count > 0:

        errors.append(
            f"{open_or_future_count} velas "
            f"todavia abiertas o futuras"
        )

    # ------------------------------------------------------
    # HUECOS TEMPORALES
    # ------------------------------------------------------

    expected_delta = pd.Timedelta(
        seconds=TIMEFRAME_SECONDS[timeframe]
    )

    differences = df["time"].diff()

    gap_indices = differences[
        differences > expected_delta
    ].index

    weekend_gaps = []

    unexpected_gaps = []

    for index in gap_indices:

        previous_time = df.loc[
            index - 1,
            "time"
        ]

        current_time = df.loc[
            index,
            "time"
        ]

        delta = (
            current_time
            - previous_time
        )

        gap_info = {
            "from": previous_time,
            "to": current_time,
            "duration": delta,
        }

        if crosses_weekend(
            previous_time,
            current_time,
        ):

            weekend_gaps.append(
                gap_info
            )

        else:

            unexpected_gaps.append(
                gap_info
            )

    if unexpected_gaps:

        warnings.append(
            f"{len(unexpected_gaps)} "
            f"huecos fuera de fin de semana"
        )

    # ------------------------------------------------------
    # INFORMACION
    # ------------------------------------------------------

    first_time = df["time"].iloc[0]
    last_time = df["time"].iloc[-1]

    coverage_days = (
        last_time - first_time
    ).total_seconds() / 86400

    print(
        f"Velas:       {len(df):,}"
    )

    print(
        f"Primera:     {first_time}"
    )

    print(
        f"Ultima:      {last_time}"
    )

    print(
        f"Cobertura:   {coverage_days:.1f} dias"
    )

    print(
        f"Weekend gaps: {len(weekend_gaps)}"
    )

    print(
        f"Otros gaps:   {len(unexpected_gaps)}"
    )

    # ------------------------------------------------------
    # MOSTRAR HUECOS
    # ------------------------------------------------------

    if unexpected_gaps:

        print()
        print(
            "Primeros huecos inesperados:"
        )

        for gap in unexpected_gaps[:5]:

            print(
                "  ",
                gap["from"],
                "->",
                gap["to"],
                "|",
                gap["duration"],
            )

    # ------------------------------------------------------
    # RESULTADO
    # ------------------------------------------------------

    if errors:

        for error in errors:

            print(
                "[ERROR]",
                error,
            )

        status = "FAIL"

    elif warnings:

        for warning in warnings:

            print(
                "[AVISO]",
                warning,
            )

        status = "WARN"

    else:

        status = "OK"

    print(
        f"Resultado:    {status}"
    )

    return {
        "status": status,
        "warnings": len(warnings),
    }


# ==========================================================
# MAIN
# ==========================================================

def main():

    print("=" * 90)
    print(
        "TRADING AGENT - VALIDACION "
        "DE DATOS DE MERCADO"
    )
    print("=" * 90)

    total_files = 0
    ok_files = 0
    warning_files = 0
    failed_files = 0

    for symbol in SYMBOLS:

        for timeframe in TIMEFRAMES:

            total_files += 1

            result = validate_file(
                symbol,
                timeframe,
            )

            if result["status"] == "OK":

                ok_files += 1

            elif result["status"] == "WARN":

                warning_files += 1

            else:

                failed_files += 1

    print()
    print("=" * 90)
    print("RESUMEN")
    print("=" * 90)

    print(
        "Archivos analizados:",
        total_files,
    )

    print(
        "Correctos:",
        ok_files,
    )

    print(
        "Con avisos:",
        warning_files,
    )

    print(
        "Con errores:",
        failed_files,
    )

    print("=" * 90)

    # Esto sera util mas adelante:
    # un sistema automatico no continuara
    # si detectamos corrupcion estructural.

    if failed_files > 0:

        raise SystemExit(1)


if __name__ == "__main__":
    main()