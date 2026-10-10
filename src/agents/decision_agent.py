class DecisionAgent:

    name = "decision_agent"

    def run(
        self,
        context,
        bus,
    ):

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

        regime = (
            bus.require(
                "REGIME_ASSESSMENT"
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

        plan = (
            bus.require(
                "TRADE_PLAN"
            )[
                "payload"
            ]
        )

        risk = (
            bus.require(
                "RISK_ASSESSMENT"
            )[
                "payload"
            ]
        )

        critic_message = (
            bus.latest(
                "CRITIC_ASSESSMENT"
            )
        )

        critic = (
            critic_message[
                "payload"
            ]
            if critic_message
            else None
        )

        errors = []

        if not macro.get(
            "approved",
            False,
        ):

            errors.append(
                "MACRO_NOT_APPROVED"
            )

        if not portfolio.get(
            "approved",
            False,
        ):

            errors.append(
                "PORTFOLIO_NOT_APPROVED"
            )

        if not regime.get(
            "valid",
            False,
        ):

            errors.append(
                "REGIME_NOT_VALID"
            )

        if (
            strategy.get(
                "action"
            )
            != "PROPOSE_TRADE"
        ):

            errors.append(
                "STRATEGY_NOT_PROPOSED"
            )

        if (
            plan.get(
                "status"
            )
            != "VALID"
        ):

            errors.append(
                "TRADE_PLAN_NOT_VALID"
            )

        if not risk.get(
            "approved",
            False,
        ):

            errors.append(
                "RISK_NOT_APPROVED"
            )

        if risk.get(
            "approved",
            False,
        ):

            if critic is None:

                errors.append(
                    "CRITIC_ASSESSMENT_MISSING"
                )

            elif not critic.get(
                "valid",
                False,
            ):

                errors.append(
                    "CRITIC_ASSESSMENT_INVALID"
                )

            elif (
                critic.get(
                    "verdict"
                )
                != "NO_OBJECTION"
            ):

                errors.append(
                    "CRITIC_VETO"
                )

        # Todavía no hemos integrado el SessionAgent
        # en esta arquitectura.
        #
        # Por seguridad, una decisión no puede considerarse
        # completa hasta que exista SESSION_ASSESSMENT.

        session_message = (
            bus.latest(
                "SESSION_ASSESSMENT"
            )
        )

        if session_message is None:

            errors.append(
                "SESSION_ASSESSMENT_MISSING"
            )

        else:

            session = (
                session_message[
                    "payload"
                ]
            )

            if not session.get(
                "approved",
                False,
            ):

                errors.append(
                    "SESSION_NOT_APPROVED"
                )

        errors = list(
            dict.fromkeys(
                errors
            )
        )

        approved = (
            len(errors)
            == 0
        )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "FINAL_DECISION",

            payload={
                "status":
                    (
                        "DRY_RUN_APPROVED"
                        if approved
                        else "REJECTED"
                    ),

                "approved":
                    approved,

                "errors":
                    errors,

                "symbol":
                    context.symbol,

                "strategy":
                    strategy.get(
                        "strategy",
                        "NONE",
                    ),

                "direction":
                    strategy.get(
                        "direction",
                        "NONE",
                    ),

                "order_request":
                    (
                        risk.get(
                            "request"
                        )
                        if approved
                        else None
                    ),

                # MUY IMPORTANTE:
                # todavía no autoriza order_send().
                "trade_authorized":
                    False,

                "source":
                    "DETERMINISTIC_PYTHON",
            },
        )