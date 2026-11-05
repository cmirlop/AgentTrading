import MetaTrader5 as mt5

from src.order_request import (
    build_mt5_request,
)

from src.risk_validator import (
    validate_dry_run,
)
from src.risk_preflight import evaluate_plan_risk, release_plan_risk_reservation


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

        if candidate["allowed"]:
            portfolio_risk = evaluate_plan_risk(
                plan,
                context.runtime_state,
            )
        else:
            portfolio_risk = {
                "approved": False,
                "errors": ["CANDIDATE_NOT_ALLOWED"],
            }
        if not portfolio_risk.get("approved"):
            return bus.publish(
                agent=self.name,
                message_type="RISK_ASSESSMENT",
                payload={
                    "status": "REJECTED",
                    "approved": False,
                    "errors": portfolio_risk.get("errors", ["PORTFOLIO_RISK_CHECK_REJECTED"]),
                    "portfolio_risk": portfolio_risk,
                    "request": None,
                    "order_check": None,
                    "source": "DETERMINISTIC_PYTHON",
                },
            )

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

                portfolio_risk=
                    portfolio_risk,
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

        if not validation.get("approved") and portfolio_risk.get("reservation_id"):
            release_plan_risk_reservation(plan)

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

                "portfolio_risk":
                    portfolio_risk,

                "source":
                    "DETERMINISTIC_PYTHON",
            },
        )
