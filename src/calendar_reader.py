import time
from pathlib import Path

import pandas as pd


# ==========================================================
# COLUMNAS DE FECHA
# ==========================================================

DATE_COLUMNS = [
    "generated_server_time",
    "generated_utc",
    "event_time_server",
    "event_time_utc",
]


# ==========================================================
# UTILIDADES
# ==========================================================

def _parse_utc(series):
    """
    Convierte fechas MT5:

    2026.10.07 14:30:00

    a timestamps UTC.
    """

    return pd.to_datetime(
        series,
        format="%Y.%m.%d %H:%M:%S",
        utc=True,
        errors="coerce",
    )


def _safe_value(value):

    if pd.isna(value):
        return None

    if isinstance(
        value,
        (
            int,
            float,
        ),
    ):
        return float(value)

    value = str(value).strip()

    if not value:
        return None

    return value


# ==========================================================
# LECTURA ROBUSTA
# ==========================================================

def load_calendar_csv(
    file_path: Path,
    attempts=3,
):

    if not file_path.exists():

        raise FileNotFoundError(
            f"No existe el calendario: "
            f"{file_path}"
        )

    last_error = None

    # El EA sobrescribe el CSV cada minuto.
    # Si Python intenta leer justo mientras se escribe,
    # hacemos varios intentos.

    for attempt in range(
        attempts
    ):

        try:

            df = pd.read_csv(
                file_path,
                sep=";",
                encoding="utf-8-sig",
            )

            if df.empty:

                raise ValueError(
                    "CSV calendario vacio"
                )

            required = {
                "generated_utc",
                "currency",
                "event_id",
                "event_code",
                "event_name",
                "importance",
                "event_time_utc",
            }

            missing = (
                required
                - set(df.columns)
            )

            if missing:

                raise ValueError(
                    "Columnas faltantes: "
                    + str(
                        sorted(missing)
                    )
                )

            # ----------------------------------------------
            # FECHAS UTC
            # ----------------------------------------------

            df[
                "generated_utc"
            ] = _parse_utc(
                df[
                    "generated_utc"
                ]
            )

            df[
                "event_time_utc"
            ] = _parse_utc(
                df[
                    "event_time_utc"
                ]
            )

            # Fechas servidor se conservan como texto.
            # No las usaremos para lógica temporal.

            # ----------------------------------------------
            # NORMALIZACION
            # ----------------------------------------------

            df[
                "currency"
            ] = (
                df["currency"]
                .astype(str)
                .str.upper()
                .str.strip()
            )

            df[
                "importance"
            ] = (
                df["importance"]
                .astype(str)
                .str.upper()
                .str.strip()
            )

            # ----------------------------------------------
            # QUITAR FILAS INVALIDAS
            # ----------------------------------------------

            df = df[
                df[
                    "event_time_utc"
                ].notna()
            ].copy()

            df = df[
                df[
                    "generated_utc"
                ].notna()
            ].copy()

            # ----------------------------------------------
            # DUPLICADOS
            # ----------------------------------------------

            df = (
                df
                .drop_duplicates(
                    subset=[
                        "event_id",
                        "event_time_utc",
                    ],
                    keep="last",
                )
                .sort_values(
                    "event_time_utc"
                )
                .reset_index(
                    drop=True
                )
            )

            return df

        except Exception as exc:

            last_error = exc

            time.sleep(
                0.25
            )

    raise RuntimeError(
        "No se pudo leer calendario "
        f"despues de {attempts} intentos: "
        f"{last_error}"
    )


# ==========================================================
# EVENTO -> JSON
# ==========================================================

def event_to_dict(
    row,
    now_utc,
):

    event_time = (
        row[
            "event_time_utc"
        ]
    )

    minutes_until = (
        event_time
        - now_utc
    ).total_seconds() / 60

    return {
        "currency":
            row["currency"],

        "event_id":
            int(
                row["event_id"]
            ),

        "event_code":
            _safe_value(
                row.get(
                    "event_code"
                )
            ),

        "event_name":
            _safe_value(
                row.get(
                    "event_name"
                )
            ),

        "importance":
            row["importance"],

        "event_time_utc":
            event_time.isoformat(),

        "minutes_until_event":
            round(
                minutes_until,
                1,
            ),

        "actual":
            _safe_value(
                row.get(
                    "actual"
                )
            ),

        "forecast":
            _safe_value(
                row.get(
                    "forecast"
                )
            ),

        "previous":
            _safe_value(
                row.get(
                    "previous"
                )
            ),

        "revised":
            _safe_value(
                row.get(
                    "revised"
                )
            ),

        "impact":
            _safe_value(
                row.get(
                    "impact"
                )
            ),

        "unit":
            _safe_value(
                row.get(
                    "unit"
                )
            ),

        "multiplier":
            _safe_value(
                row.get(
                    "multiplier"
                )
            ),

        "source_url":
            _safe_value(
                row.get(
                    "source_url"
                )
            ),
    }