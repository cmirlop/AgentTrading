import MetaTrader5 as mt5

from src.order_request import (
    build_mt5_request,
)

from src.risk_validator import (
    validate_dry_run,
)


def serialize_order_check(
    result,
):

    if result is None:
        return None

    return {
        "retcode":
            getattr(
                result,
                "retcode",
                None,
            ),

        "comment":
            getattr(
                result,
                "comment",
                None,
            ),

        "balance":
            getattr(
                result,
                "balance",
                None,
            ),

        "equity":
            getattr(
                result,
                "equity",
                None,
            ),

        "profit":
            getattr(
                result,
                "profit",
                None,
            ),

        "margin":
            getattr(
                result,
                "margin",
                None,
            ),

        "margin_free":
            getattr(
                result,
                "margin_free",
                None,
            ),
    }


class RiskAgent:

    name = "risk_agent"

    def run(
        self,
        context,
        bus,
    ):

        plan = (
            bus.require(
                "TRADE_PLAN"
            )[
                "payload"
            ]
        )

        strategy = (
            bus.require(
                "STRATEGY_PROPOSAL"
            )[
                "payload"
            ]
        )

        macro = (
            bus.require(
                "MACRO_ASSESSMENT"
            )[
                "payload"
            ]
        )

        portfolio = (
            bus.require(
                "PORTFOLIO_ASSESSMENT"
            )[
                "payload"
            ]
        )

        if (
            plan.get(
                "status"
            )
            != "VALID"
        ):

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "RISK_ASSESSMENT",

                payload={
                    "status":
                        "REJECTED",

                    "approved":
                        False,

                    "errors":
                        (
                            [
                                "TRADE_PLAN_NOT_VALID"
                            ]
                            +
                            plan.get(
                                "errors",
                                [],
                            )
                        ),

                    "request":
                        None,

                    "order_check":
                        None,

                    "source":
                        "DETERMINISTIC_PYTHON",
                },
            )

        direction = (
            strategy.get(
                "direction"
            )
        )

        if direction not in {
            "BUY",
            "SELL",
        }:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "RISK_ASSESSMENT",

                payload={
                    "status":
                        "REJECTED",

                    "approved":
                        False,

                    "errors":
                        [
                            "INVALID_DIRECTION"
                        ],

                    "request":
                        None,

                    "order_check":
                        None,

                    "source":
                        "DETERMINISTIC_PYTHON",
                },
            )

        candidate = {
            "allowed":
                bool(
                    macro.get(
                        "approved",
                        False,
                    )
                    and
                    portfolio.get(
                        "approved",
                        False,
                    )
                    and
                    strategy.get(
                        "valid",
                        False,
                    )
                    and
                    strategy.get(
                        "action"
                    )
                    == "PROPOSE_TRADE"
                ),

            "status":
                (
                    "CANDIDATE_LONG"
                    if direction
                    == "BUY"
                    else
                    "CANDIDATE_SHORT"
                ),

            "macro": {
                "new_trades_blocked":
                    macro.get(
                        "new_trades_blocked",
                        True,
                    ),

                "upcoming_high_events":
                    macro.get(
                        "upcoming_high_events",
                        [],
                    ),

                "upcoming_critical_events":
                    macro.get(
                        "upcoming_critical_events",
                        [],
                    ),
            },
        }

        try:

            request, expiration = (
                build_mt5_request(
                    plan
                )
            )

        except Exception as exc:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "RISK_ASSESSMENT",

                payload={
                    "status":
                        "REJECTED",

                    "approved":
                        False,

                    "errors": [
                        (
                            "REQUEST_BUILD_ERROR: "
                            + str(exc)
                        )
                    ],

                    "request":
                        None,

                    "order_check":
                        None,

                    "source":
                        "DETERMINISTIC_PYTHON",
                },
            )

        terminal_info = (
            mt5.terminal_info()
        )

        try:

            order_check_result = (
                mt5.order_check(
                    request
                )
            )

            order_check_exception = (
                None
            )

        except Exception as exc:

            order_check_result = None

            order_check_exception = (
                str(exc)
            )

        validation = (
            validate_dry_run(
                symbol=
                    context.symbol,

                candidate=
                    candidate,

                trade_plan=
                    plan,

                request=
                    request,

                order_check_result=
                    order_check_result,

                terminal_info=
                    terminal_info,
            )
        )

        if order_check_exception:

            validation[
                "errors"
            ] = list(
                dict.fromkeys(
                    validation.get(
                        "errors",
                        [],
                    )
                    +
                    [
                        (
                            "ORDER_CHECK_EXCEPTION: "
                            + order_check_exception
                        )
                    ]
                )
            )

            validation[
                "approved"
            ] = False

            validation[
                "status"
            ] = "REJECTED"

        return bus.publish(
            agent=
                self.name,

            message_type=
                "RISK_ASSESSMENT",

            payload={
                **validation,

                "request":
                    request,

                "order_check":
                    serialize_order_check(
                        order_check_result
                    ),

                "candidate":
                    candidate,

                "source":
                    "DETERMINISTIC_PYTHON",
            },
        )