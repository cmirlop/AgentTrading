import MetaTrader5 as mt5

from src.agents.planner_agent import (
    PlannerAgent,
)

from src.agents.risk_agent import (
    RiskAgent,
)

from src.agents.critic_agent import (
    CriticAgent,
)

from src.agents.decision_agent import (
    DecisionAgent,
)


from src.market_snapshot import (
    build_compact_snapshot,
)

from src.agents.technical_agent import (
    TechnicalAgent,
)

from src.agents.regime_agent import (
    RegimeAgent,
)

from src.agents.strategy_agent import (
    StrategyAgent,
)

from src.orchestration.decision_context import (
    DecisionContext,
)

from src.orchestration.agent_bus import (
    AgentBus,
)

from src.agents.session_agent import (
    SessionAgent,
)

from datetime import (
    datetime,
    timezone,
)

from config.operational_config import (
    MAX_TECHNICAL_AGE_SECONDS,
)


from src.agents.macro_agent import (
    MacroAgent,
)

from src.agents.portfolio_agent import (
    PortfolioAgent,
)

from config.agent_config import (
    TECHNICAL_PREFILTER_QUALITY,
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

class MultiAgentOrchestrator:

    

    def __init__(
        self,
    ): pass

        

    

    def run(
        self,
        symbol,
        market_state,
        runtime_state,
        calendar_state,
    ):

        session_agent = (
            SessionAgent()
        )
        technical_agent = TechnicalAgent()
        macro_agent = MacroAgent()
        portfolio_agent = PortfolioAgent()
    
        regime_agent = RegimeAgent()
        strategy_agent = StrategyAgent()

        planner_agent = PlannerAgent()
        risk_agent = RiskAgent()
        critic_agent = CriticAgent()
        decision_agent = DecisionAgent()

        # snapshot = (
        #     build_compact_snapshot(
        #         market_state,
        #         candles_per_timeframe=4,
        #     )
        # )

        context = DecisionContext(
            symbol=symbol,
            market_state=market_state,
            snapshot={},
            analysis_time=
                market_state.get(
                    "analysis_time"
                ),
            runtime_state=
                runtime_state,

            calendar_state=
                calendar_state,
        )

        bus = AgentBus(
            context
        )


        now_utc = datetime.now(
            timezone.utc
        )

        # ==================================================
        # MARKET AVAILABILITY
        # ==================================================

        weekend = (
            now_utc.weekday()
            in {
                5,  # Saturday
                6,  # Sunday
            }
        )

        if weekend:

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "MARKET_AVAILABILITY",

                payload={
                    "status":
                        "CLOSED",

                    "reason":
                        "WEEKEND",

                    "weekday":
                        now_utc.strftime(
                            "%A"
                        ),

                    "checked_at":
                        now_utc.isoformat(),

                    "last_analysis_time":
                        market_state.get(
                            "analysis_time"
                        ),

                    "trade_allowed":
                        False,
                },
            )

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        "MARKET_CLOSED",

                    "reason":
                        "WEEKEND",

                    "regime":
                        "NOT_EVALUATED",

                    "strategy":
                        "NONE",

                    "direction":
                        "NONE",

                    "trade_authorized":
                        False,
                },
            )

            return context

        errors = []

        if (
            market_state.get(
                "symbol"
            )
            != symbol
        ):

            errors.append(
                "MARKET_STATE_SYMBOL_MISMATCH"
            )

        analysis_time = parse_utc(
            market_state.get(
                "analysis_time"
            )
        )

        if analysis_time is None:

            errors.append(
                "MARKET_STATE_TIME_MISSING"
            )

            state_age_seconds = None

        else:

            state_age_seconds = (
                datetime.now(
                    timezone.utc
                )
                - analysis_time
            ).total_seconds()

            if state_age_seconds < -60:

                errors.append(
                    "MARKET_STATE_FROM_FUTURE"
                )

            elif (
                state_age_seconds
                >
                MAX_TECHNICAL_AGE_SECONDS
            ):

                errors.append(
                    "MARKET_STATE_STALE"
                )
        bus.publish(
            agent=
                "orchestrator",

            message_type=
                "MARKET_FRESHNESS",

            payload={
                "valid":
                    not errors,

                "age_seconds":
                    (
                        round(
                            state_age_seconds,
                            1,
                        )
                        if state_age_seconds
                        is not None
                        else None
                    ),

                "max_age_seconds":
                    MAX_TECHNICAL_AGE_SECONDS,

                "errors":
                    errors,
            },
        )

        if errors:

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        "WAIT",

                    "reason":
                        "MARKET_STATE_INVALID",

                    "errors":
                        errors,

                    "trade_authorized":
                        False,
                },
            )

            return context

                # ==================================================
        
        # SESSION GATE
        # ==================================================

        session_agent.run(
            context,
            bus,
        )

        session = (
            bus.require(
                "SESSION_ASSESSMENT"
            )[
                "payload"
            ]
        )

        if not session.get(
            "approved",
            False,
        ):

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "SESSION_GATE_RESULT",

                payload={
                    "approved":
                        False,

                    "sessions_open":
                        session.get(
                            "sessions_open",
                            [],
                        ),

                    "errors":
                        session.get(
                            "errors",
                            [],
                        ),
                },
            )

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        "WAIT",

                    "reason":
                        "OUTSIDE_ALLOWED_SESSION",

                    "sessions_open":
                        session.get(
                            "sessions_open",
                            [],
                        ),

                    "regime":
                        "NOT_EVALUATED",

                    "strategy":
                        "NONE",

                    "direction":
                        "NONE",

                    "trade_authorized":
                        False,
                },
            )

            return context

        bus.publish(
            agent=
                "orchestrator",

            message_type=
                "SESSION_GATE_RESULT",

            payload={
                "approved":
                    True,

                "sessions_open":
                    session.get(
                        "sessions_open",
                        [],
                    ),

                "errors":
                    [],
            },
        )
        
        snapshot = (
            build_compact_snapshot(
                market_state,
                candles_per_timeframe=4,
            )
        )

        context.snapshot = snapshot

        for agent in [
            technical_agent,
            macro_agent,
            portfolio_agent,
        ]:

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "AGENT_STARTED",

                payload={
                    "agent":
                        agent.name,
                },
            )

            agent.run(
                context,
                bus,
            )

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "AGENT_COMPLETED",

                payload={
                    "agent":
                        agent.name,
                },
            )

        technical = (
            bus.require(
                "TECHNICAL_ASSESSMENT"
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

        hard_gate_errors = []

        if (
            technical[
                "bias"
            ]
            == "NEUTRAL"
        ):

            hard_gate_errors.append(
                "TECHNICAL_BIAS_NEUTRAL"
            )

        if (
            technical[
                "alignment"
            ]
            == "CONFLICT"
        ):

            hard_gate_errors.append(
                "TECHNICAL_ALIGNMENT_CONFLICT"
            )

        if (
            technical[
                "setup_quality"
            ]
            <
            TECHNICAL_PREFILTER_QUALITY
        ):

            hard_gate_errors.append(
                "TECHNICAL_QUALITY_BELOW_PREFILTER"
            )

        if not macro.get(
            "approved",
            False,
        ):

            hard_gate_errors.append(
                "MACRO_NOT_APPROVED"
            )

        if not portfolio.get(
            "approved",
            False,
        ):

            hard_gate_errors.append(
                "PORTFOLIO_NOT_APPROVED"
            )

       

        if hard_gate_errors:

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "HARD_GATE_RESULT",

                payload={
                    "approved":
                        False,

                    "errors":
                        hard_gate_errors,
                },
            )

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        "WAIT",

                    "technical_bias":
                        technical[
                            "bias"
                        ],

                    "setup_quality":
                        technical[
                            "setup_quality"
                        ],

                    "macro_approved":
                        macro[
                            "approved"
                        ],

                    "portfolio_approved":
                        portfolio[
                            "approved"
                        ],

                    "hard_gate_errors":
                        hard_gate_errors,

                    "regime":
                        "NOT_EVALUATED",

                    "strategy":
                        "NONE",

                    "direction":
                        "NONE",

                    "trade_authorized":
                        False,
                },
            )

            return context

        bus.publish(
            agent=
                "orchestrator",

            message_type=
                "HARD_GATE_RESULT",

            payload={
                "approved":
                    True,

                "errors":
                    [],
            },
        )

        regime_agent.run(
            context,
            bus,
        )

        strategy_agent.run(
            context,
            bus,
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

        # ==================================================
        # STRATEGY GATE
        # ==================================================

        if (
            strategy.get(
                "action"
            )
            != "PROPOSE_TRADE"
            or
            not strategy.get(
                "valid",
                False,
            )
        ):

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        "WAIT",

                    "technical_bias":
                        technical[
                            "bias"
                        ],

                    "setup_quality":
                        technical[
                            "setup_quality"
                        ],

                    "macro_approved":
                        macro[
                            "approved"
                        ],

                    "portfolio_approved":
                        portfolio[
                            "approved"
                        ],

                    "regime":
                        regime.get(
                            "regime"
                        ),

                    "regime_valid":
                        regime.get(
                            "valid",
                            False,
                        ),

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

                    "trade_authorized":
                        False,
                },
            )

            return context

        bus.publish(
            agent=
                "orchestrator",

            message_type=
                "PLANNING_GATE_RESULT",

            payload={
                "approved":
                    True,

                "status":
                    "READY_FOR_PLANNING",
            },
        )

        # ==================================================
        # MT5 DRY-RUN CONTEXT
        # ==================================================

        if not mt5.initialize():

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        "WAIT",

                    "reason":
                        "MT5_INITIALIZE_FAILED",

                    "trade_authorized":
                        False,
                },
            )

            return context

        try:

            terminal_info = (
                mt5.terminal_info()
            )

            if terminal_info is None:

                bus.publish(
                    agent=
                        "orchestrator",

                    message_type=
                        "ORCHESTRATION_RESULT",

                    payload={
                        "status":
                            "WAIT",

                        "reason":
                            "TERMINAL_INFO_MISSING",

                        "trade_authorized":
                            False,
                    },
                )

                return context

            if not terminal_info.tradeapi_disabled:

                bus.publish(
                    agent=
                        "orchestrator",

                    message_type=
                        "ORCHESTRATION_RESULT",

                    payload={
                        "status":
                            "WAIT",

                        "reason":
                            "PYTHON_TRADING_MUST_REMAIN_DISABLED",

                        "trade_authorized":
                            False,
                    },
                )

                return context

            # ==============================================
            # PLANNER AGENT
            # ==============================================

            planner_agent.run(
                context,
                bus,
            )

            # ==============================================
            # RISK AGENT
            # ==============================================

            risk_agent.run(
                context,
                bus,
            )

            risk = (
                bus.require(
                    "RISK_ASSESSMENT"
                )[
                    "payload"
                ]
            )

            # ==============================================
            # CRITIC AGENT
            #
            # Solo gastamos LLM si Risk ya aprobó.
            # ==============================================

            if risk.get(
                "approved",
                False,
            ):

                critic_agent.run(
                    context,
                    bus,
                )

            # ==============================================
            # FINAL DECISION
            # ==============================================

            decision_agent.run(
                context,
                bus,
            )

            final = (
                bus.require(
                    "FINAL_DECISION"
                )[
                    "payload"
                ]
            )

            bus.publish(
                agent=
                    "orchestrator",

                message_type=
                    "ORCHESTRATION_RESULT",

                payload={
                    "status":
                        final[
                            "status"
                        ],

                    "final_approved":
                        final[
                            "approved"
                        ],

                    "technical_bias":
                        technical[
                            "bias"
                        ],

                    "setup_quality":
                        technical[
                            "setup_quality"
                        ],

                    "regime":
                        regime.get(
                            "regime"
                        ),

                    "strategy":
                        strategy.get(
                            "strategy"
                        ),

                    "direction":
                        strategy.get(
                            "direction"
                        ),

                    "final_errors":
                        final.get(
                            "errors",
                            [],
                        ),

                    "trade_authorized":
                        False,
                },
            )

            return context

        finally:

            mt5.shutdown()