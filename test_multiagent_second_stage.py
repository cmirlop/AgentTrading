from types import SimpleNamespace

from src.orchestration.agent_bus import (
    AgentBus,
)

from src.orchestration.decision_context import (
    DecisionContext,
)

from src.agents.decision_agent import (
    DecisionAgent,
)


def publish(
    bus,
    agent,
    message_type,
    payload,
):

    bus.publish(
        agent=agent,
        message_type=message_type,
        payload=payload,
    )


context = DecisionContext(
    symbol="USDCAD",
    market_state={},
    snapshot={},
    analysis_time=
        "2026-10-09T15:00:00+00:00",
    runtime_state={},
    calendar_state={},
)

bus = AgentBus(
    context
)


# ==========================================================
# ESTADOS PREVIOS YA APROBADOS
# ==========================================================

publish(
    bus,
    "session_agent",
    "SESSION_ASSESSMENT",
    {
        "approved": True,
        "sessions_open": [
            "LONDON",
            "NEW_YORK",
        ],
        "source":
            "TEST_FIXTURE",
    },
)

publish(
    bus,
    "macro_agent",
    "MACRO_ASSESSMENT",
    {
        "approved": True,
        "new_trades_blocked":
            False,
        "source":
            "TEST_FIXTURE",
    },
)

publish(
    bus,
    "portfolio_agent",
    "PORTFOLIO_ASSESSMENT",
    {
        "approved": True,
        "free_slots": 2,
        "source":
            "TEST_FIXTURE",
    },
)

publish(
    bus,
    "regime_agent",
    "REGIME_ASSESSMENT",
    {
        "regime": "TREND",
        "confidence": 0.90,
        "valid": True,
        "evidence_used": [
            "H1_EMA_STRUCTURE_BULLISH",
            "H4_EMA_STRUCTURE_BULLISH",
            "D1_EMA_STRUCTURE_BULLISH",
        ],
        "source":
            "TEST_FIXTURE",
    },
)

publish(
    bus,
    "strategy_agent",
    "STRATEGY_PROPOSAL",
    {
        "action":
            "PROPOSE_TRADE",

        "strategy":
            "PULLBACK",

        "direction":
            "BUY",

        "confidence":
            0.90,

        "valid":
            True,

        "source":
            "TEST_FIXTURE",
    },
)


# ==========================================================
# PLAN DETERMINISTA SIMULADO
# ==========================================================

publish(
    bus,
    "planner_agent",
    "TRADE_PLAN",
    {
        "symbol":
            "USDCAD",

        "status":
            "VALID",

        "direction":
            "BUY",

        "strategy":
            "PULLBACK",

        "order_type":
            "BUY_LIMIT",

        "volume":
            0.01,

        "entry":
            1.42000,

        "stop_loss":
            1.41800,

        "take_profit":
            1.42420,

        "estimated_loss_eur":
            1.30,

        "estimated_profit_eur":
            2.73,

        "risk_reward":
            2.10,

        "source":
            "TEST_FIXTURE",
    },
)


# ==========================================================
# RISK APPROVED SIMULADO
# ==========================================================

publish(
    bus,
    "risk_agent",
    "RISK_ASSESSMENT",
    {
        "status":
            "DRY_RUN_APPROVED",

        "approved":
            True,

        "errors":
            [],

        "request": {
            "symbol":
                "USDCAD",

            "volume":
                0.01,

            "price":
                1.42000,

            "sl":
                1.41800,

            "tp":
                1.42420,
        },

        "source":
            "TEST_FIXTURE",
    },
)


# ==========================================================
# CRITIC SIN OBJECIONES
# ==========================================================

publish(
    bus,
    "critic_agent",
    "CRITIC_ASSESSMENT",
    {
        "verdict":
            "VETO",

        "confidence":
            0.90,

        "evidence_used":
            [],

        "reason_codes": [
            "NO_MATERIAL_OBJECTION"
        ],

        "valid":
            True,

        "validation_errors":
            [],

        "source":
            "TEST_FIXTURE",
    },
)


# ==========================================================
# DECISION FINAL
# ==========================================================

decision_agent = (
    DecisionAgent()
)

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

print(
    "\nFINAL DECISION\n"
)

print(
    final
)


assert (
    final[
        "status"
    ]
    == "DRY_RUN_APPROVED"
)

assert (
    final[
        "approved"
    ]
    is True
)

assert (
    final[
        "trade_authorized"
    ]
    is False
)

print(
    "\n[OK] SECOND STAGE TEST PASSED"
)