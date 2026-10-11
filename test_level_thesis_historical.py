import pprint

from src.historical_state import (
    prepare_symbol_history,
    build_historical_market_state,
)

from src.market_snapshot import (
    build_compact_snapshot,
)

from src.support_resistance import (
    detect_support_resistance,
)

from src.level_evidence import (
    build_level_evidence,
)

from src.agents.technical_agent import (
    TechnicalAgent,
)

from src.agents.regime_agent import (
    RegimeAgent,
)

from src.agents.level_thesis_agent import (
    LevelThesisAgent,
)

from src.orchestration.decision_context import (
    DecisionContext,
)

from src.orchestration.agent_bus import (
    AgentBus,
)


SYMBOL = "USDCAD"

ANALYSIS_TIME = (
    "2026-04-08T18:00:00+00:00"
)


history = (
    prepare_symbol_history(
        SYMBOL
    )
)


market_state = (
    build_historical_market_state(
        symbol=
            SYMBOL,

        analysis_time=
            ANALYSIS_TIME,

        prepared_history=
            history,
    )
)


snapshot = (
    build_compact_snapshot(
        market_state,
        candles_per_timeframe=4,
    )
)


context = (
    DecisionContext(
        symbol=
            SYMBOL,

        market_state=
            market_state,

        snapshot=
            snapshot,

        analysis_time=
            ANALYSIS_TIME,

        runtime_state=
            {},

        calendar_state=
            {},
    )
)


bus = AgentBus(
    context
)


# ==========================================================
# MACRO HISTORICO: EXPLICITAMENTE NO DISPONIBLE
# ==========================================================

bus.publish(
    agent=
        "macro_agent",

    message_type=
        "MACRO_ASSESSMENT",

    payload={
        "approved":
            True,

        "macro_risk":
            "NOT_AVAILABLE_HISTORICALLY",

        "moderate_warnings":
            [],

        "historical_data_available":
            False,

        "source":
            "HISTORICAL_REPLAY",
    },
)


# ==========================================================
# TECHNICAL
# ==========================================================

TechnicalAgent().run(
    context,
    bus,
)


# ==========================================================
# REGIME
# ==========================================================

RegimeAgent().run(
    context,
    bus,
)


# ==========================================================
# SUPPORT / RESISTANCE
# ==========================================================

levels = (
    detect_support_resistance(
        symbol=
            SYMBOL,

        analysis_time=
            ANALYSIS_TIME,

        prepared_history=
            history,
    )
)


level_evidence = (
    build_level_evidence(
        levels
    )
)


bus.publish(
    agent=
        "level_agent",

    message_type=
        "LEVEL_EVIDENCE",

    payload=
        level_evidence,
)


# ==========================================================
# LEVEL THESIS
# ==========================================================

LevelThesisAgent().run(
    context,
    bus,
)


technical = (
    bus.require(
        "TECHNICAL_ASSESSMENT"
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


thesis = (
    bus.require(
        "LEVEL_THESIS"
    )[
        "payload"
    ]
)


print(
    "\n"
    + "=" * 80
)

print(
    "TECHNICAL"
)

print(
    "=" * 80
)

pprint.pp(
    technical
)


print(
    "\n"
    + "=" * 80
)

print(
    "REGIME"
)

print(
    "=" * 80
)

pprint.pp(
    regime
)


print(
    "\n"
    + "=" * 80
)

print(
    "LEVEL STRUCTURE"
)

print(
    "=" * 80
)

pprint.pp(
    level_evidence
)


print(
    "\n"
    + "=" * 80
)

print(
    "LEVEL THESIS"
)

print(
    "=" * 80
)

pprint.pp(
    thesis
)