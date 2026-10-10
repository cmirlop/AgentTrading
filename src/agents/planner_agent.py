from src.trade_planner import (
    build_trade_plan,
)


def get_server_utc_offset(
    calendar_state,
):

    calendar_state = (
        calendar_state
        or {}
    )

    for key in (
        "broker_utc_offset_seconds",
        "server_utc_offset_seconds",
        "utc_offset_seconds",
    ):

        value = (
            calendar_state.get(
                key
            )
        )

        if value is not None:
            return int(
                value
            )

    metadata = (
        calendar_state.get(
            "metadata",
            {},
        )
        or {}
    )

    for key in (
        "broker_utc_offset_seconds",
        "server_utc_offset_seconds",
        "utc_offset_seconds",
    ):

        value = (
            metadata.get(
                key
            )
        )

        if value is not None:
            return int(
                value
            )

    return None


class PlannerAgent:

    name = "planner_agent"

    def run(
        self,
        context,
        bus,
    ):

        strategy = (
            bus.require(
                "STRATEGY_PROPOSAL"
            )[
                "payload"
            ]
        )

        if (
            not strategy.get(
                "valid",
                False,
            )
            or
            strategy.get(
                "action"
            )
            != "PROPOSE_TRADE"
        ):

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "TRADE_PLAN",

                payload={
                    "symbol":
                        context.symbol,

                    "status":
                        "REJECTED",

                    "errors":
                        [
                            "STRATEGY_NOT_PROPOSED"
                        ],

                    "source":
                        "DETERMINISTIC_PYTHON",
                },
            )

        server_offset = (
            get_server_utc_offset(
                context.calendar_state
            )
        )

        if server_offset is None:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "TRADE_PLAN",

                payload={
                    "symbol":
                        context.symbol,

                    "status":
                        "REJECTED",

                    "errors":
                        [
                            "SERVER_UTC_OFFSET_MISSING"
                        ],

                    "source":
                        "DETERMINISTIC_PYTHON",
                },
            )

        plan = (
            build_trade_plan(
                symbol=
                    context.symbol,

                direction=
                    strategy[
                        "direction"
                    ],

                strategy=
                    strategy[
                        "strategy"
                    ],

                market_state=
                    context.market_state,

                server_utc_offset_seconds=
                    server_offset,
            )
        )

        plan[
            "source"
        ] = (
            "DETERMINISTIC_PYTHON"
        )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "TRADE_PLAN",

            payload=
                plan,
        )