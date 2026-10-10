from config.operational_config import (
    REQUIRE_ALLOWED_SESSION,
)

from src.session_gate import (
    build_session_state,
)


class SessionAgent:

    name = "session_agent"

    def run(
        self,
        context,
        bus,
    ):

        session_state = (
            build_session_state()
        )

        trading_session_allowed = (
            session_state.get(
                "trading_session_allowed",
                False,
            )
        )

        approved = (
            (
                not REQUIRE_ALLOWED_SESSION
            )
            or
            trading_session_allowed
        )

        errors = []

        if (
            REQUIRE_ALLOWED_SESSION
            and
            not trading_session_allowed
        ):

            errors.append(
                "OUTSIDE_ALLOWED_SESSION"
            )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "SESSION_ASSESSMENT",

            payload={
                "approved":
                    approved,

                "required":
                    REQUIRE_ALLOWED_SESSION,

                "trading_session_allowed":
                    trading_session_allowed,

                "sessions_open":
                    session_state.get(
                        "sessions_open",
                        [],
                    ),

                "london":
                    session_state.get(
                        "london",
                        {},
                    ),

                "new_york":
                    session_state.get(
                        "new_york",
                        {},
                    ),

                "generated_at":
                    session_state.get(
                        "generated_at"
                    ),

                "errors":
                    errors,

                "source":
                    "DETERMINISTIC_PYTHON",
            },
        )