import pandas as pd

from config.calendar_config import (
    SYMBOL_CURRENCIES,
    MAX_CALENDAR_AGE_SECONDS,
    HIGH_IMPACT_BLOCK_BEFORE_MINUTES,
    HIGH_IMPACT_BLOCK_AFTER_MINUTES,
    EVENT_LOOKAHEAD_HOURS,
    EVENT_LOOKBACK_HOURS,
    MODERATE_WARNING_MINUTES,
    CRITICAL_EVENT_CODES,
    CRITICAL_BLOCK_BEFORE_MINUTES,
    CRITICAL_BLOCK_AFTER_MINUTES,
)

from src.calendar_reader import (
    event_to_dict,
)


# ==========================================================
# BUILD CALENDAR STATE
# ==========================================================

def build_calendar_state(
    df,
):

    now_utc = pd.Timestamp.now(
        tz="UTC"
    )

    generated_utc = (
        df[
            "generated_utc"
        ].max()
    )

    latest_rows = df[
        df["generated_utc"]
        == generated_utc
    ]

    if latest_rows.empty:

        raise RuntimeError(
            "No se pudo determinar "
            "el offset horario del broker"
        )

    offset_value = (
        latest_rows.iloc[0][
            "server_utc_offset_seconds"
        ]
    )

    if pd.isna(offset_value):

        raise RuntimeError(
            "server_utc_offset_seconds "
            "esta vacio en el calendario"
        )

    server_utc_offset_seconds = int(
        offset_value
    )

    age_seconds = (
        now_utc
        - generated_utc
    ).total_seconds()

    calendar_fresh = (
        age_seconds
        <= MAX_CALENDAR_AGE_SECONDS
    )

    result = {
        "generated_at":
            now_utc.isoformat(),

        "calendar_generated_utc":
            generated_utc.isoformat(),

        "calendar_age_seconds":
            round(
                age_seconds,
                1,
            ),

        "calendar_fresh":
            calendar_fresh,

        "server_utc_offset_seconds":
            server_utc_offset_seconds,

        "symbols": {},
    }

    # ======================================================
    # CADA PAR
    # ======================================================

    for (
        symbol,
        currencies,
    ) in SYMBOL_CURRENCIES.items():

        relevant = df[
            df[
                "currency"
            ].isin(
                currencies
            )
        ].copy()

        # --------------------------------------------------
        # VENTANA DE EVENTOS QUE GUARDAMOS
        # --------------------------------------------------

        window_start = (
            now_utc
            - pd.Timedelta(
                hours=
                    EVENT_LOOKBACK_HOURS
            )
        )

        window_end = (
            now_utc
            + pd.Timedelta(
                hours=
                    EVENT_LOOKAHEAD_HOURS
            )
        )

        relevant = relevant[
            (
                relevant[
                    "event_time_utc"
                ]
                >= window_start
            )
            &
            (
                relevant[
                    "event_time_utc"
                ]
                <= window_end
            )
        ].copy()

        # --------------------------------------------------
        # EVENTOS HIGH
        # --------------------------------------------------

        high_events = relevant[
            relevant[
                "importance"
            ]
            == "HIGH"
        ].copy()

        moderate_events = relevant[
            relevant[
                "importance"
            ]
            == "MODERATE"
        ].copy()


        # --------------------------------------------------
        # EVENTOS CRITICOS INDEPENDIENTES DE IMPORTANCE
        # --------------------------------------------------

        critical_events = relevant[
            relevant[
                "event_code"
            ].isin(
                CRITICAL_EVENT_CODES
            )
        ].copy()

        blocking_events = []

        upcoming_high = []

        moderate_warnings = []

        critical_blocking_events = []
        upcoming_critical = []

        # --------------------------------------------------
        # BLOQUEO HIGH
        # --------------------------------------------------

        for _, row in (
            high_events.iterrows()
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

            # Entre -15 y +30 minutos:
            # bloqueado.

            if (
                -HIGH_IMPACT_BLOCK_AFTER_MINUTES
                <= minutes_until
                <= HIGH_IMPACT_BLOCK_BEFORE_MINUTES
            ):

                blocking_events.append(
                    event_to_dict(
                        row,
                        now_utc,
                    )
                )

            # Guardamos también HIGH próximos.

            if minutes_until >= 0:

                upcoming_high.append(
                    event_to_dict(
                        row,
                        now_utc,
                    )
                )

        # --------------------------------------------------
        # AVISO MODERATE
        # --------------------------------------------------

        for _, row in (
            moderate_events.iterrows()
        ):

            minutes_until = (
                (
                    row[
                        "event_time_utc"
                    ]
                    - now_utc
                )
                .total_seconds()
                / 60
            )

            if (
                0
                <= minutes_until
                <= MODERATE_WARNING_MINUTES
            ):

                moderate_warnings.append(
                    event_to_dict(
                        row,
                        now_utc,
                    )
                )


        # --------------------------------------------------
        # BLOQUEO CRITICAL
        # --------------------------------------------------

        for _, row in (
            critical_events.iterrows()
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

            if (
                -CRITICAL_BLOCK_AFTER_MINUTES
                <= minutes_until
                <= CRITICAL_BLOCK_BEFORE_MINUTES
            ):

                critical_blocking_events.append(
                    event_to_dict(
                        row,
                        now_utc,
                    )
                )

            if minutes_until >= 0:

                upcoming_critical.append(
                    event_to_dict(
                        row,
                        now_utc,
                    )
                )

        # --------------------------------------------------
        # DECISION MACRO
        # --------------------------------------------------

        reasons = []

        if not calendar_fresh:

            reasons.append(
                "CALENDAR_STALE"
            )

        if blocking_events:

            reasons.append(
                "HIGH_IMPACT_EVENT_WINDOW"
            )

        if critical_blocking_events:

            reasons.append(
                "CRITICAL_EVENT_WINDOW"
            )

        new_trades_blocked = (
            len(reasons) > 0
        )

        # --------------------------------------------------
        # RIESGO MACRO
        # --------------------------------------------------

        if not calendar_fresh:

            macro_risk = "UNKNOWN"

        elif (
            blocking_events
            or
            critical_blocking_events
        ):

            macro_risk = "BLOCKED"

        elif (
            upcoming_critical
            and
            upcoming_critical[0][
                "minutes_until_event"
            ]
            <= 120
        ):

            macro_risk = "ELEVATED"

        elif (
            upcoming_high
            and
            upcoming_high[0][
                "minutes_until_event"
            ]
            <= 120
        ):

            macro_risk = "ELEVATED"

        elif moderate_warnings:

            macro_risk = "CAUTION"

        else:

            macro_risk = "NORMAL"

        result[
            "symbols"
        ][symbol] = {
            "currencies":
                currencies,

            "macro_risk":
                macro_risk,

            "new_trades_blocked":
                new_trades_blocked,

            "blocking_reasons":
                reasons,

            "blocking_events":
                blocking_events,

            "upcoming_high_events":
                upcoming_high,

            "moderate_warnings":
                moderate_warnings,

            "server_utc_offset_seconds":
                server_utc_offset_seconds,

            "critical_blocking_events":
                critical_blocking_events,

            "upcoming_critical_events":
                upcoming_critical,
        }

    return result