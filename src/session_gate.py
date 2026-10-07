from datetime import (
    datetime,
    time,
    timezone,
)

from zoneinfo import ZoneInfo

from config.operational_config import (
    LONDON_OPEN_HOUR,
    LONDON_OPEN_MINUTE,
    LONDON_CLOSE_HOUR,
    LONDON_CLOSE_MINUTE,
    NEW_YORK_OPEN_HOUR,
    NEW_YORK_OPEN_MINUTE,
    NEW_YORK_CLOSE_HOUR,
    NEW_YORK_CLOSE_MINUTE,
)


LONDON_TZ = ZoneInfo(
    "Europe/London"
)

NEW_YORK_TZ = ZoneInfo(
    "America/New_York"
)


def _session_is_open(
    local_datetime,
    open_hour,
    open_minute,
    close_hour,
    close_minute,
):

    # Sabado / domingo
    if local_datetime.weekday() >= 5:
        return False

    current = local_datetime.time()

    session_open = time(
        open_hour,
        open_minute,
    )

    session_close = time(
        close_hour,
        close_minute,
    )

    return (
        session_open
        <= current
        < session_close
    )


def build_session_state(
    now_utc=None,
):

    if now_utc is None:

        now_utc = datetime.now(
            timezone.utc
        )

    london_now = (
        now_utc.astimezone(
            LONDON_TZ
        )
    )

    new_york_now = (
        now_utc.astimezone(
            NEW_YORK_TZ
        )
    )

    london_open = (
        _session_is_open(
            london_now,
            LONDON_OPEN_HOUR,
            LONDON_OPEN_MINUTE,
            LONDON_CLOSE_HOUR,
            LONDON_CLOSE_MINUTE,
        )
    )

    new_york_open = (
        _session_is_open(
            new_york_now,
            NEW_YORK_OPEN_HOUR,
            NEW_YORK_OPEN_MINUTE,
            NEW_YORK_CLOSE_HOUR,
            NEW_YORK_CLOSE_MINUTE,
        )
    )

    sessions_open = []

    if london_open:
        sessions_open.append(
            "LONDON"
        )

    if new_york_open:
        sessions_open.append(
            "NEW_YORK"
        )

    return {
        "generated_at":
            now_utc.isoformat(),

        "london": {
            "local_time":
                london_now.isoformat(),

            "is_open":
                london_open,
        },

        "new_york": {
            "local_time":
                new_york_now.isoformat(),

            "is_open":
                new_york_open,
        },

        "sessions_open":
            sessions_open,

        "trading_session_allowed":
            (
                london_open
                or
                new_york_open
            ),
    }