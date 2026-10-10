from datetime import (
    datetime,
    timezone,
)

from config.operational_config import (
    MAX_CALENDAR_STATE_AGE_SECONDS,
)


def parse_utc(
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


class MacroAgent:

    name = "macro_agent"

    def run(
        self,
        context,
        bus,
    ):

        calendar = (
            context.calendar_state
        )

        if not calendar:

            return bus.publish(
                agent=self.name,
                message_type=
                    "MACRO_ASSESSMENT",
                payload={
                    "approved":
                        False,

                    "macro_risk":
                        "UNKNOWN",

                    "fresh":
                        False,

                    "blocking_reasons":
                        [
                            "CALENDAR_STATE_MISSING"
                        ],
                },
            )

        generated_at = parse_utc(
            calendar.get(
                "generated_at"
            )
        )

        if generated_at is None:

            age_seconds = None
            state_fresh = False

        else:

            age_seconds = (
                datetime.now(
                    timezone.utc
                )
                - generated_at
            ).total_seconds()

            state_fresh = (
                0
                <= age_seconds
                <= MAX_CALENDAR_STATE_AGE_SECONDS
            )

        symbol_state = (
            calendar.get(
                "symbols",
                {},
            )
            .get(
                context.symbol
            )
        )

        if not symbol_state:

            return bus.publish(
                agent=self.name,
                message_type=
                    "MACRO_ASSESSMENT",
                payload={
                    "approved":
                        False,

                    "macro_risk":
                        "UNKNOWN",

                    "fresh":
                        state_fresh,

                    "age_seconds":
                        (
                            round(
                                age_seconds,
                                1,
                            )
                            if age_seconds
                            is not None
                            else None
                        ),

                    "blocking_reasons":
                        [
                            "SYMBOL_CALENDAR_STATE_MISSING"
                        ],
                },
            )

        calendar_feed_fresh = (
            calendar.get(
                "calendar_fresh",
                False,
            )
        )

        new_trades_blocked = (
            symbol_state.get(
                "new_trades_blocked",
                True,
            )
        )

        reasons = list(
            symbol_state.get(
                "blocking_reasons",
                [],
            )
        )

        if not state_fresh:

            reasons.append(
                "CALENDAR_STATE_STALE"
            )

        if not calendar_feed_fresh:

            reasons.append(
                "CALENDAR_FEED_STALE"
            )

        reasons = list(
            dict.fromkeys(
                reasons
            )
        )

        approved = (
            state_fresh
            and calendar_feed_fresh
            and not new_trades_blocked
        )

        return bus.publish(
            agent=self.name,

            message_type=
                "MACRO_ASSESSMENT",

            payload={
                "approved":
                    approved,

                "macro_risk":
                    symbol_state.get(
                        "macro_risk",
                        "UNKNOWN",
                    ),

                "fresh":
                    (
                        state_fresh
                        and calendar_feed_fresh
                    ),

                "age_seconds":
                    (
                        round(
                            age_seconds,
                            1,
                        )
                        if age_seconds
                        is not None
                        else None
                    ),

                "new_trades_blocked":
                    new_trades_blocked,

                "blocking_reasons":
                    reasons,

                "critical_events":
                    symbol_state.get(
                        "critical_blocking_events",
                        [],
                    ),

                "upcoming_critical_events":
                    symbol_state.get(
                        "upcoming_critical_events",
                        [],
                    ),

                "upcoming_high_events":
                    symbol_state.get(
                        "upcoming_high_events",
                        [],
                    ),

                "moderate_warnings":
                    symbol_state.get(
                        "moderate_warnings",
                        [],
                    ),

                "source":
                    "DETERMINISTIC_PYTHON",
            },
        )