from pathlib import Path
import json

from time import perf_counter

import pandas as pd

from src.historical_state import (
    prepare_symbol_history,
    build_historical_market_state,
)

from src.market_snapshot import (
    build_compact_snapshot,
)

from src.session_gate import (
    build_session_state,
)

from src.support_resistance import (
    detect_support_resistance,
)

from src.level_evidence import (
    build_level_evidence,
)

from src.level_price_action import (
    build_level_price_action,
    build_level_price_action_evidence,
)

from src.historical_level_outcome import (
    evaluate_level_thesis,
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

START = pd.Timestamp(
    "2026-04-08T00:00:00+00:00"
)

END = pd.Timestamp(
    "2026-04-09T00:00:00+00:00"
)

HORIZON_MINUTES = 180


BASE_DIR = Path(
    __file__
).resolve().parent


OUTPUT_DIR = (
    BASE_DIR
    / "data"
    / "backtest"
    / "level_thesis"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


print("=" * 100)
print("LEVEL THESIS HISTORICAL BATCH")
print("=" * 100)

print(
    f"Symbol: {SYMBOL}"
)

print(
    f"Periodo: {START} -> {END}"
)


# ==========================================================
# PREPARAR HISTORIA
# ==========================================================

history = (
    prepare_symbol_history(
        SYMBOL
    )
)


m15 = history[
    "M15"
]


decision_times = (
    m15.loc[
        (
            m15["bar_close"]
            >= START
        )
        &
        (
            m15["bar_close"]
            < END
        ),
        "bar_close",
    ]
    .tolist()
)

decision_times = [
    pd.Timestamp(
        "2026-04-08T07:30:00+00:00"
    ),
    pd.Timestamp(
        "2026-04-08T08:00:00+00:00"
    ),
    pd.Timestamp(
        "2026-04-08T09:45:00+00:00"
    ),
    pd.Timestamp(
        "2026-04-08T15:00:00+00:00"
    ),
]


stats = {
    "m15_evaluated":
        0,

    "outside_session":
        0,

    "open_space":
        0,

    "level_candidates":
        0,

    "regime_invalid":
        0,

    "thesis_invalid":
        0,

    "thesis_unclear":
        0,

    "thesis_operational":
        0,

    "correct":
        0,

    "wrong":
        0,

    "ambiguous":
        0,

    "unresolved":
        0,
}


rows = []


# ==========================================================
# REPLAY
# ==========================================================

for analysis_time in decision_times:

    stats[
        "m15_evaluated"
    ] += 1


    # ------------------------------------------------------
    # SESSION GATE HISTORICO
    # ------------------------------------------------------

    session = (
        build_session_state(
            now_utc=
                analysis_time.to_pydatetime()
        )
    )


    if not session[
        "trading_session_allowed"
    ]:

        stats[
            "outside_session"
        ] += 1

        continue


    # ------------------------------------------------------
    # MARKET STATE SIN LOOKAHEAD
    # ------------------------------------------------------

    market_state = (
        build_historical_market_state(
            symbol=
                SYMBOL,

            analysis_time=
                analysis_time,

            prepared_history=
                history,
        )
    )


    # ------------------------------------------------------
    # SUPPORT / RESISTANCE
    # ------------------------------------------------------

    levels = (
        detect_support_resistance(
            symbol=
                SYMBOL,

            analysis_time=
                analysis_time,

            prepared_history=
                history,
        )
    )


    level_evidence = (
        build_level_evidence(
            levels
        )
    )


    if (
        level_evidence.get(
            "status"
        )
        != "OK"
    ):

        continue


    market_structure = (
        level_evidence.get(
            "market_structure"
        )
    )

    if market_structure == "AT_SUPPORT":

        active_level = "SUPPORT"

    elif market_structure == "AT_RESISTANCE":

        active_level = "RESISTANCE"

    else:

        active_level = None


    if active_level is not None:

        price_action = (
            build_level_price_action(
                prepared_history=
                    history,

                analysis_time=
                    analysis_time,

                level_facts=
                    level_evidence.get(
                        "facts",
                        {},
                    ),

                active_level=
                    active_level,
            )
        )

        price_action_evidence = (
            build_level_price_action_evidence(
                price_action
            )
        )

    else:

        price_action = {}

        price_action_evidence = []


    level_evidence[
        "price_action"
    ] = price_action

    level_evidence[
        "price_action_evidence"
    ] = price_action_evidence

    level_evidence[
        "available_evidence_ids"
    ] = list(
        dict.fromkeys(
            level_evidence.get(
                "available_evidence_ids",
                [],
            )
            +
            price_action_evidence
        )
    )


    # Solo preguntamos al LLM cuando el precio está
    # realmente interactuando con un nivel.
    if (
        market_structure
        == "OPEN_SPACE"
    ):

        stats[
            "open_space"
        ] += 1

        continue


    stats[
        "level_candidates"
    ] += 1

    level_started_at = perf_counter()


    # ------------------------------------------------------
    # SNAPSHOT
    # ------------------------------------------------------

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
                analysis_time.isoformat(),

            runtime_state=
                {},

            calendar_state=
                {},
        )
    )


    bus = AgentBus(
        context
    )


    # ------------------------------------------------------
    # HISTORICAL MACRO
    #
    # IMPORTANTE:
    # No inventamos noticias de abril.
    # ------------------------------------------------------

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


    # ------------------------------------------------------
    # TECHNICAL
    # ------------------------------------------------------

    TechnicalAgent().run(
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


    # ------------------------------------------------------
    # REGIME
    # ------------------------------------------------------

    RegimeAgent().run(
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


    # ------------------------------------------------------
    # LEVEL EVIDENCE
    # ------------------------------------------------------

    bus.publish(
        agent=
            "level_agent",

        message_type=
            "LEVEL_EVIDENCE",

        payload=
            level_evidence,
    )


    # ------------------------------------------------------
    # LEVEL THESIS
    # ------------------------------------------------------

    thesis_started_at = perf_counter()

    LevelThesisAgent().run(
        context,
        bus,
    )

    thesis_seconds = (
        perf_counter() - thesis_started_at
    )

    total_seconds = (
        perf_counter() - level_started_at
    )

    print(
        "\n[LEVEL TIMING]",
        f"time={analysis_time.isoformat()}",
        f"level={active_level}",
        f"total_seconds={total_seconds:.2f}",
        f"thesis_seconds={thesis_seconds:.2f}",
    )


    thesis = (
        bus.require(
            "LEVEL_THESIS"
        )[
            "payload"
        ]
    )


    if not regime.get(
        "valid",
        False,
    ):

        stats[
            "regime_invalid"
        ] += 1


    if not thesis.get(
        "valid",
        False,
    ):

        stats[
            "thesis_invalid"
        ] += 1


    elif (
        thesis.get(
            "thesis"
        )
        == "UNCLEAR"
    ):

        stats[
            "thesis_unclear"
        ] += 1


    else:

        stats[
            "thesis_operational"
        ] += 1


    # ------------------------------------------------------
    # FUTURE OUTCOME
    # ------------------------------------------------------

    outcome = (
        evaluate_level_thesis(
            thesis=
                thesis,

            levels=
                levels,

            prepared_history=
                history,

            horizon_minutes=
                HORIZON_MINUTES,
        )
    )


    actual_outcome = (
        outcome.get(
            "actual_outcome"
        )
    )


    correct = outcome.get(
        "correct"
    )


    if correct is True:

        stats[
            "correct"
        ] += 1


    elif correct is False:

        stats[
            "wrong"
        ] += 1


    elif (
        actual_outcome
        == "AMBIGUOUS"
    ):

        stats[
            "ambiguous"
        ] += 1


    elif (
        actual_outcome
        == "UNRESOLVED"
    ):

        stats[
            "unresolved"
        ] += 1


    # ------------------------------------------------------
    # DATOS DEL NIVEL
    # ------------------------------------------------------

    facts = (
        level_evidence.get(
            "facts",
            {},
        )
    )


    support = (
        facts.get(
            "support"
        )
        or {}
    )


    resistance = (
        facts.get(
            "resistance"
        )
        or {}
    )


    row = {
        "analysis_time":
            analysis_time.isoformat(),

        "market_structure":
            market_structure,

        "technical_bias":
            technical.get(
                "bias"
            ),

        "technical_score":
            technical.get(
                "overall_score"
            ),

        "setup_quality":
            technical.get(
                "setup_quality"
            ),

        "alignment":
            technical.get(
                "alignment"
            ),

        "regime":
            regime.get(
                "regime"
            ),

        "regime_confidence":
            regime.get(
                "confidence"
            ),

        "regime_valid":
            regime.get(
                "valid"
            ),

        "support_strength":
            support.get(
                "strength"
            ),

        "support_distance_atr":
            support.get(
                "distance_atr"
            ),

        "support_touches":
            support.get(
                "touches"
            ),

        "resistance_strength":
            resistance.get(
                "strength"
            ),

        "resistance_distance_atr":
            resistance.get(
                "distance_atr"
            ),

        "resistance_touches":
            resistance.get(
                "touches"
            ),

        "thesis":
            thesis.get(
                "thesis"
            ),

        "operation":
            thesis.get(
                "operation"
            ),

        "thesis_confidence":
            thesis.get(
                "confidence"
            ),

        "thesis_valid":
            thesis.get(
                "valid"
            ),

        "reason_codes":
            "|".join(
                thesis.get(
                    "reason_codes",
                    [],
                )
            ),

        "validation_errors":
            "|".join(
                thesis.get(
                    "validation_errors",
                    [],
                )
            ),

        "expected_outcome":
            outcome.get(
                "expected_outcome"
            ),

        "actual_outcome":
            actual_outcome,

        "correct":
            correct,

        "event_time":
            outcome.get(
                "event_time"
            ),

        "future_min_low":
            outcome.get(
                "future_min_low"
            ),

        "future_max_high":
            outcome.get(
                "future_max_high"
            ),
    }


    rows.append(
        row
    )


    print(
        analysis_time.isoformat(),
        "|",
        market_structure,
        "|",
        thesis.get(
            "thesis"
        ),
        "| conf=",
        thesis.get(
            "confidence"
        ),
        "| outcome=",
        actual_outcome,
        "| correct=",
        correct,
    )


# ==========================================================
# RESULTADOS
# ==========================================================

df = pd.DataFrame(
    rows
)


resolved = (
    stats["correct"]
    +
    stats["wrong"]
)

actual_hold = 0
actual_break = 0

if not df.empty:

    actual_hold = int(
        (
            df["actual_outcome"]
            == "HOLD"
        ).sum()
    )

    actual_break = int(
        (
            df["actual_outcome"]
            == "BREAK"
        ).sum()
    )


always_hold_accuracy = (
    actual_hold
    /
    (
        actual_hold
        +
        actual_break
    )
    if (
        actual_hold
        +
        actual_break
    )
    else None
)


always_break_accuracy = (
    actual_break
    /
    (
        actual_hold
        +
        actual_break
    )
    if (
        actual_hold
        +
        actual_break
    )
    else None
)


accuracy = (
    stats["correct"]
    /
    resolved
    if resolved
    else None
)


summary = {
    "symbol":
        SYMBOL,

    "start":
        START.isoformat(),

    "end":
        END.isoformat(),

    "scope":
        "LEVEL_THESIS_TECHNICAL_ONLY",

    "horizon_minutes":
        HORIZON_MINUTES,

    "funnel":
        stats,

    "resolved":
        resolved,

    "actual_outcomes": {
        "hold":
            actual_hold,

        "break":
            actual_break,
    },

    "baseline_always_hold_accuracy":
        (
            round(
                always_hold_accuracy,
                4,
            )
            if always_hold_accuracy
            is not None
            else None
        ),

    "baseline_always_break_accuracy":
        (
            round(
                always_break_accuracy,
                4,
            )
            if always_break_accuracy
            is not None
            else None
        ),

    "model_edge_over_always_hold":
        (
            round(
                accuracy
                -
                always_hold_accuracy,
                4,
            )
            if (
                accuracy
                is not None
                and
                always_hold_accuracy
                is not None
            )
            else None
        ),

    "accuracy_resolved":
        (
            round(
                accuracy,
                4,
            )
            if accuracy
            is not None
            else None
        ),

    "limitations": [
        "NO_HISTORICAL_NEWS",
        "NO_HISTORICAL_MACRO",
        "DIAGNOSTIC_NOT_PROFITABILITY_BACKTEST",
        "OVERLAPPING_SIGNALS_NOT_DEDUPLICATED",
    ],
}


csv_path = (
    OUTPUT_DIR
    /
    "20260408_USDCAD_level_thesis.csv"
)


json_path = (
    OUTPUT_DIR
    /
    "20260408_USDCAD_level_thesis_summary.json"
)


df.to_csv(
    csv_path,
    index=False,
)


with open(
    json_path,
    "w",
    encoding="utf-8",
) as handle:

    json.dump(
        summary,
        handle,
        indent=2,
    )


print(
    "\n"
    + "=" * 100
)

print(
    "RESULTADO"
)

print(
    "=" * 100
)


print(
    json.dumps(
        summary,
        indent=2,
    )
)


print(
    "\nCSV:",
    csv_path,
)

print(
    "Summary:",
    json_path,
)