from datetime import (
    datetime,
    timezone,
)

from config.operational_config import (
    MAX_TECHNICAL_AGE_SECONDS,
    MAX_RUNTIME_AGE_SECONDS,
    MAX_CALENDAR_STATE_AGE_SECONDS,
    REQUIRE_ALLOWED_SESSION,
)

from config.execution_config import (
    PENDING_ORDER_EXPIRATION_MINUTES,
)

from config.calendar_config import (
    HIGH_IMPACT_BLOCK_BEFORE_MINUTES,
    CRITICAL_BLOCK_BEFORE_MINUTES,
)


# ==========================================================
# TIME
# ==========================================================

def _parse_datetime(
    value,
):

    if not value:
        return None

    value = value.replace(
        "Z",
        "+00:00",
    )

    dt = datetime.fromisoformat(
        value
    )

    if dt.tzinfo is None:

        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(
        timezone.utc
    )


def _age_seconds(
    timestamp,
    now_utc,
):

    dt = _parse_datetime(
        timestamp
    )

    if dt is None:
        return None

    return (
        now_utc - dt
    ).total_seconds()


# ==========================================================
# CANDIDATE
# ==========================================================

def evaluate_candidate(
    symbol,
    technical_analysis,
    technical_generated_at,
    runtime_state,
    calendar_state,
    session_state,
):

    now_utc = datetime.now(
        timezone.utc
    )

    reasons = []

    # ======================================================
    # 1. FRESHNESS - ANALISIS
    # ======================================================

    technical_age = _age_seconds(
        technical_generated_at,
        now_utc,
    )

    if technical_age is None:

        reasons.append(
            "TECHNICAL_TIMESTAMP_MISSING"
        )

    elif (
        technical_age
        > MAX_TECHNICAL_AGE_SECONDS
    ):

        reasons.append(
            "TECHNICAL_ANALYSIS_STALE"
        )

    # ======================================================
    # 2. FRESHNESS - RUNTIME
    # ======================================================

    runtime_age = _age_seconds(
        runtime_state.get(
            "generated_at"
        ),
        now_utc,
    )

    if runtime_age is None:

        reasons.append(
            "RUNTIME_TIMESTAMP_MISSING"
        )

    elif (
        runtime_age
        > MAX_RUNTIME_AGE_SECONDS
    ):

        reasons.append(
            "RUNTIME_STATE_STALE"
        )

    # ======================================================
    # 3. FRESHNESS - CALENDAR STATE
    # ======================================================

    calendar_age = _age_seconds(
        calendar_state.get(
            "generated_at"
        ),
        now_utc,
    )

    if calendar_age is None:

        reasons.append(
            "CALENDAR_STATE_TIMESTAMP_MISSING"
        )

    elif (
        calendar_age
        > MAX_CALENDAR_STATE_AGE_SECONDS
    ):

        reasons.append(
            "CALENDAR_STATE_STALE"
        )

    if not calendar_state.get(
        "calendar_fresh",
        False,
    ):

        reasons.append(
            "CALENDAR_FEED_STALE"
        )

    # ======================================================
    # 4. SESIONES
    # ======================================================

    if (
        REQUIRE_ALLOWED_SESSION
        and
        not session_state[
            "trading_session_allowed"
        ]
    ):

        reasons.append(
            "OUTSIDE_ALLOWED_SESSION"
        )

    # ======================================================
    # 5. VALIDACION TECNICA
    # ======================================================

    if not technical_analysis.get(
        "validation_ok",
        False,
    ):

        reasons.append(
            "TECHNICAL_ANALYSIS_INVALID"
        )

    decision = (
        technical_analysis.get(
            "decision"
        )
    )

    if decision not in {
        "WATCH_LONG",
        "WATCH_SHORT",
    }:

        reasons.append(
            "NO_TECHNICAL_SETUP"
        )

    # ======================================================
    # 6. CALIDAD
    # ======================================================

    setup_quality = (
        technical_analysis.get(
            "setup_quality",
            0,
        )
    )

    if setup_quality < 65:

        reasons.append(
            "SETUP_QUALITY_TOO_LOW"
        )

    # ======================================================
    # 7. TIMEFRAME ALIGNMENT
    # ======================================================

    alignment = (
        technical_analysis.get(
            "higher_timeframe_alignment"
        )
    )

    if alignment == "CONFLICT":

        reasons.append(
            "TIMEFRAME_CONFLICT"
        )

    # ======================================================
    # 8. CUENTA
    # ======================================================

    risk_status = (
        runtime_state[
            "risk_status"
        ]
    )

    if not risk_status[
        "can_open_more_positions"
    ]:

        reasons.append(
            "MAX_OPEN_POSITIONS_REACHED"
        )

    # ======================================================
    # 9. POSICION YA ABIERTA
    # ======================================================

    symbol_positions = (
        runtime_state[
            "positions_by_symbol"
        ].get(
            symbol,
            [],
        )
    )

    if symbol_positions:

        reasons.append(
            "POSITION_ALREADY_OPEN"
        )

    # ======================================================
    # 10. ORDEN PENDIENTE YA EXISTENTE
    # ======================================================

    pending_for_symbol = [
        order

        for order
        in runtime_state[
            "pending_orders"
        ]

        if order[
            "symbol"
        ] == symbol
    ]

    if pending_for_symbol:

        reasons.append(
            "PENDING_ORDER_ALREADY_EXISTS"
        )

    # ======================================================
    # 11. CALENDARIO DEL SIMBOLO
    # ======================================================

    symbol_calendar = (
        calendar_state[
            "symbols"
        ].get(
            symbol
        )
    )

    if symbol_calendar is None:

        reasons.append(
            "CALENDAR_SYMBOL_MISSING"
        )

    else:

        if symbol_calendar[
            "new_trades_blocked"
        ]:

            reasons.extend(
                symbol_calendar[
                    "blocking_reasons"
                ]
            )
    # ==================================================
    # ORDEN PENDIENTE VS PROXIMA NOTICIA HIGH
    # ==================================================

    high_overlap_horizon = (
        PENDING_ORDER_EXPIRATION_MINUTES
        +
        HIGH_IMPACT_BLOCK_BEFORE_MINUTES
    )

    if not symbol_calendar[
        "new_trades_blocked"
    ]:

        for event in (
            symbol_calendar.get(
                "upcoming_high_events",
                [],
            )
        ):

            minutes_until = (
                event.get(
                    "minutes_until_event"
                )
            )

            if minutes_until is None:
                continue

            if (
                0
                <= minutes_until
                <= high_overlap_horizon
            ):

                reasons.append(
                    "PENDING_ORDER_COULD_OVERLAP_HIGH_IMPACT_WINDOW"
                )

                break
        critical_overlap_horizon = (
            PENDING_ORDER_EXPIRATION_MINUTES
            +
            CRITICAL_BLOCK_BEFORE_MINUTES
        )

        if not symbol_calendar[
            "new_trades_blocked"
        ]:

            for event in (
                symbol_calendar.get(
                    "upcoming_critical_events",
                    [],
                )
            ):

                minutes_until = (
                    event.get(
                        "minutes_until_event"
                    )
                )

                if minutes_until is None:
                    continue

                if (
                    0
                    <= minutes_until
                    <= critical_overlap_horizon
                ):

                    reasons.append(
                        "PENDING_ORDER_COULD_OVERLAP_CRITICAL_EVENT"
                    )

                    break
    # ======================================================
    # RESULTADO
    # ======================================================

    reasons = list(
        dict.fromkeys(
            reasons
        )
    )

    allowed = (
        len(reasons) == 0
    )

    if not allowed:

        status = "REJECTED"
        direction = None

    elif decision == "WATCH_LONG":

        status = (
            "CANDIDATE_LONG"
        )

        direction = "BUY"

    else:

        status = (
            "CANDIDATE_SHORT"
        )

        direction = "SELL"

    return {
        "symbol":
            symbol,

        "status":
            status,

        "direction":
            direction,

        "allowed":
            allowed,

        "rejection_reasons":
            reasons,

        "freshness": {
            "technical_age_seconds":
                (
                    round(
                        technical_age,
                        1,
                    )
                    if technical_age
                    is not None
                    else None
                ),

            "runtime_age_seconds":
                (
                    round(
                        runtime_age,
                        1,
                    )
                    if runtime_age
                    is not None
                    else None
                ),

            "calendar_state_age_seconds":
                (
                    round(
                        calendar_age,
                        1,
                    )
                    if calendar_age
                    is not None
                    else None
                ),
        },

        "session":
            session_state,

        "technical": {
            "decision":
                decision,

            "strategy":
                technical_analysis.get(
                    "strategy"
                ),

            "bias":
                technical_analysis.get(
                    "bias"
                ),

            "setup_quality":
                setup_quality,

            "overall_score":
                technical_analysis.get(
                    "overall_score"
                ),

            "alignment":
                alignment,
        },

        "macro":
            (
                symbol_calendar
                if symbol_calendar
                else None
            ),
    }