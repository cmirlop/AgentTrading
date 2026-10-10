import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from config.agent_config import (
    OLLAMA_MODEL,
    TECHNICAL_PREFILTER_QUALITY,
)

from config.risk_config import (
    LOT_SIZE,
    MAX_LOSS_PER_TRADE_EUR,
    MAX_SPREAD_POINTS_BY_SYMBOL,
    MAX_OPEN_POSITIONS,
)

from config.trade_planner_config import (
    MIN_RISK_REWARD,
)

from src.historical_state import (
    prepare_symbol_history,
    build_historical_market_state,
)

from src.historical_trade_planner import (
    build_historical_trade_plan,
)

from src.historical_execution import (
    simulate_trade,
)

from src.historical_risk import (
    estimate_loss_eur,
)

from src.market_snapshot import (
    build_compact_snapshot,
)

from src.session_gate import (
    build_session_state,
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

from src.agents.critic_agent import (
    CriticAgent,
)

from src.agents.decision_agent import (
    DecisionAgent,
)

from src.orchestration.decision_context import (
    DecisionContext,
)

from src.orchestration.agent_bus import (
    AgentBus,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

OUTPUT_DIR = (
    BASE_DIR
    / "data"
    / "backtest"
    / "multiagent_replay"
)

CACHE_DIR = (
    BASE_DIR
    / "data"
    / "backtest"
    / "multiagent_cache"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

CACHE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# Cambiar este valor si modificamos prompts,
# schemas o reglas que puedan cambiar la salida LLM.
CACHE_VERSION = (
    "MULTIAGENT_REPLAY_V3_2026_10_10"
)


STRATEGY_TO_MODE = {
    "TREND_CONTINUATION":
        "LIMIT",

    "PULLBACK":
        "LIMIT",

    "BREAKOUT":
        "STOP",
}


# ==========================================================
# CACHE LLM
# ==========================================================

class ReplayCache:

    def __init__(
        self,
        path,
    ):

        self.path = Path(
            path
        )

        self.data = {}

        if self.path.exists():

            with self.path.open(
                "r",
                encoding="utf-8",
            ) as handle:

                for line in handle:

                    line = line.strip()

                    if not line:
                        continue

                    try:
                        item = json.loads(
                            line
                        )

                    except Exception:
                        continue

                    key = item.get(
                        "key"
                    )

                    payload = item.get(
                        "payload"
                    )

                    if key and payload is not None:

                        self.data[
                            key
                        ] = payload

    def make_key(
        self,
        agent_name,
        symbol,
        analysis_time,
    ):

        return (
            f"{CACHE_VERSION}|"
            f"{OLLAMA_MODEL}|"
            f"{agent_name}|"
            f"{symbol}|"
            f"{pd.Timestamp(analysis_time).isoformat()}"
        )

    def get(
        self,
        key,
    ):

        return self.data.get(
            key
        )

    def put(
        self,
        key,
        payload,
    ):

        self.data[
            key
        ] = payload

        with self.path.open(
            "a",
            encoding="utf-8",
        ) as handle:

            handle.write(
                json.dumps(
                    {
                        "key":
                            key,

                        "payload":
                            payload,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )


# ==========================================================
# HELPERS
# ==========================================================

def utc_timestamp(
    value,
):

    value = pd.Timestamp(
        value
    )

    if value.tzinfo is None:

        return value.tz_localize(
            "UTC"
        )

    return value.tz_convert(
        "UTC"
    )


def publish(
    bus,
    agent,
    message_type,
    payload,
):

    return bus.publish(
        agent=agent,
        message_type=message_type,
        payload=payload,
    )


def run_cached_agent(
    agent,
    message_type,
    context,
    bus,
    cache,
):

    key = cache.make_key(
        agent_name=
            agent.name,

        symbol=
            context.symbol,

        analysis_time=
            context.analysis_time,
    )

    cached = cache.get(
        key
    )

    if cached is not None:

        publish(
            bus,
            agent.name,
            message_type,
            cached,
        )

        return (
            cached,
            True,
        )

    agent.run(
        context,
        bus,
    )

    payload = (
        bus.require(
            message_type
        )[
            "payload"
        ]
    )

    if payload.get(
        "valid",
        True,
    ):

        cache.put(
            key,
            payload,
        )

    return (
        payload,
        False,
    )


def historical_macro_assessment():

    # IMPORTANTE:
    # No estamos fingiendo que no existían noticias.
    #
    # Simplemente no tenemos histórico macro fiable.
    # Por tanto este replay se etiqueta TECHNICAL_ONLY.

    return {
        "approved":
            True,

        "macro_risk":
            "NOT_AVAILABLE_HISTORICALLY",

        "new_trades_blocked":
            False,

        "blocking_reasons":
            [],

        "upcoming_high_events":
            [],

        "upcoming_critical_events":
            [],

        "historical_data_available":
            False,

        "approval_basis":
            "TECHNICAL_ONLY_REPLAY_BYPASS",

        "source":
            "HISTORICAL_REPLAY",
    }


def historical_portfolio_assessment(
    symbol,
    active_reservations,
):

    symbol_reservations = [
        item

        for item
        in active_reservations

        if item[
            "symbol"
        ]
        == symbol
    ]

    reservation_count = len(
        active_reservations
    )

    free_slots = max(
        0,
        MAX_OPEN_POSITIONS
        - reservation_count,
    )

    errors = []

    if reservation_count >= MAX_OPEN_POSITIONS:

        errors.append(
            "NO_GLOBAL_SLOT_AVAILABLE"
        )

    if symbol_reservations:

        errors.append(
            "SYMBOL_ALREADY_RESERVED"
        )

    return {
        "approved":
            not errors,

        "open_positions":
            None,

        "pending_orders":
            None,

        "reservations":
            reservation_count,

        "max_reservations":
            MAX_OPEN_POSITIONS,

        "free_slots":
            free_slots,

        "symbol_has_reservation":
            bool(
                symbol_reservations
            ),

        "errors":
            errors,

        "source":
            "HISTORICAL_VIRTUAL_PORTFOLIO",
    }


def historical_risk_assessment(
    plan,
    analysis_time,
    prepared_history,
):

    errors = []

    try:

        estimated_loss_eur = (
            estimate_loss_eur(
                plan=
                    plan,

                analysis_time=
                    analysis_time,

                prepared_history=
                    prepared_history,
            )
        )

    except Exception as exc:

        return (
            {
                **plan,

                "status":
                    "REJECTED",

                "errors":
                    [
                        (
                            "HISTORICAL_RISK_ERROR: "
                            + str(exc)
                        )
                    ],
            },
            {
                "status":
                    "REJECTED",

                "approved":
                    False,

                "errors":
                    [
                        "HISTORICAL_RISK_ERROR"
                    ],

                "request":
                    None,

                "source":
                    "HISTORICAL_DETERMINISTIC_PYTHON",
            },
        )

    risk_reward = float(
        plan.get(
            "target_rr",
            0.0,
        )
        or 0.0
    )

    estimated_profit_eur = (
        estimated_loss_eur
        * risk_reward
    )

    spread_points = float(
        plan.get(
            "spread_points_at_signal",
            0.0,
        )
        or 0.0
    )

    max_spread = (
        MAX_SPREAD_POINTS_BY_SYMBOL
        .get(
            plan[
                "symbol"
            ]
        )
    )

    if estimated_loss_eur <= 0:

        errors.append(
            "INVALID_ESTIMATED_LOSS"
        )

    elif (
        estimated_loss_eur
        >
        MAX_LOSS_PER_TRADE_EUR
    ):

        errors.append(
            "MAX_LOSS_EXCEEDED"
        )

    if (
        risk_reward
        <
        MIN_RISK_REWARD
    ):

        errors.append(
            "RISK_REWARD_TOO_LOW"
        )

    if max_spread is None:

        errors.append(
            "SPREAD_LIMIT_NOT_CONFIGURED"
        )

    elif (
        spread_points
        >
        max_spread
    ):

        errors.append(
            "SPREAD_TOO_HIGH"
        )

    enriched_plan = {
        **plan,

        "volume":
            LOT_SIZE,

        "estimated_loss_eur":
            round(
                estimated_loss_eur,
                4,
            ),

        "estimated_profit_eur":
            round(
                estimated_profit_eur,
                4,
            ),

        "risk_reward":
            round(
                risk_reward,
                4,
            ),

        "spread_points":
            spread_points,

        # Todavía no tenemos un modelo histórico
        # de margen/equity completo.
        "margin_required_eur":
            None,

        "free_margin_eur":
            None,

        "historical_replay":
            True,
    }

    approved = (
        len(errors)
        == 0
    )

    historical_request = None

    if approved:

        historical_request = {
            "historical_simulation":
                True,

            "symbol":
                plan[
                    "symbol"
                ],

            "order_type":
                plan[
                    "order_type"
                ],

            "volume":
                LOT_SIZE,

            "price":
                plan[
                    "entry"
                ],

            "sl":
                plan[
                    "stop_loss"
                ],

            "tp":
                plan[
                    "take_profit"
                ],
        }

    risk = {
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

        "request":
            historical_request,

        "risk": {
            "volume":
                LOT_SIZE,

            "estimated_loss_eur":
                estimated_loss_eur,

            "max_loss_eur":
                MAX_LOSS_PER_TRADE_EUR,

            "risk_reward":
                risk_reward,

            "minimum_risk_reward":
                MIN_RISK_REWARD,

            "spread_points":
                spread_points,

            "max_spread_points":
                max_spread,
        },

        "limitations": [
            "HISTORICAL_MACRO_NOT_AVAILABLE",
            "MARGIN_NOT_MODELED",
            "COMMISSION_NOT_MODELED",
            "SLIPPAGE_NOT_MODELED",
            "SWAP_NOT_MODELED",
        ],

        "source":
            "HISTORICAL_DETERMINISTIC_PYTHON",
    }

    return (
        enriched_plan,
        risk,
    )


# ==========================================================
# MAIN REPLAY
# ==========================================================

def run_replay(
    symbols,
    start,
    end,
):

    start = utc_timestamp(
        start
    )

    end = utc_timestamp(
        end
    )

    print(
        "=" * 100
    )

    print(
        "MULTI-AGENT HISTORICAL REPLAY"
    )

    print(
        "=" * 100
    )

    print(
        f"Periodo: {start} -> {end}"
    )

    print(
        f"Symbols: {symbols}"
    )

    print(
        "Scope: TECHNICAL_ONLY "
        "(sin histórico macro)"
    )

    print()

    prepared = {}

    for symbol in symbols:

        print(
            f"Preparando historia: "
            f"{symbol}..."
        )

        prepared[
            symbol
        ] = (
            prepare_symbol_history(
                symbol
            )
        )

    # Para convertir USDCAD/AUDUSD a EUR,
    # historical_risk necesita EURUSD.
    if (
        "EURUSD"
        not in prepared
    ):

        print(
            "Cargando EURUSD adicional "
            "para conversion de riesgo..."
        )

        prepared[
            "EURUSD"
        ] = (
            prepare_symbol_history(
                "EURUSD"
            )
        )

    symbol_times = {}

    timeline = set()

    for symbol in symbols:

        m15 = (
            prepared[
                symbol
            ][
                "M15"
            ]
        )

        mask = (
            (
                m15[
                    "bar_close"
                ]
                >= start
            )
            &
            (
                m15[
                    "bar_close"
                ]
                <= end
            )
        )

        times = set(
            m15.loc[
                mask,
                "bar_close",
            ].tolist()
        )

        symbol_times[
            symbol
        ] = times

        timeline.update(
            times
        )

    timeline = sorted(
        timeline
    )

    cache = ReplayCache(
        CACHE_DIR
        / "multiagent_replay.jsonl"
    )

    reservations = []

    events = []

    funnel = Counter()

    technical_agent = (
        TechnicalAgent()
    )

    regime_agent = (
        RegimeAgent()
    )

    strategy_agent = (
        StrategyAgent()
    )

    critic_agent = (
        CriticAgent()
    )

    decision_agent = (
        DecisionAgent()
    )

    for time_index, analysis_time in enumerate(
        timeline,
        start=1,
    ):

        # Liberar posiciones/pendientes que ya
        # habrían terminado en este instante.
        reservations = [
            item

            for item
            in reservations

            if utc_timestamp(
                item[
                    "release_time"
                ]
            )
            > analysis_time
        ]

        for symbol in symbols:

            if (
                analysis_time
                not in symbol_times[
                    symbol
                ]
            ):

                continue

            funnel[
                "m15_evaluated"
            ] += 1

            # ==============================================
            # SESSION
            # ==============================================

            session_state = (
                build_session_state(
                    analysis_time
                    .to_pydatetime()
                )
            )

            if not session_state[
                "trading_session_allowed"
            ]:

                funnel[
                    "outside_session"
                ] += 1

                continue

            funnel[
                "inside_session"
            ] += 1

            # ==============================================
            # MARKET STATE
            # ==============================================

            market_state = (
                build_historical_market_state(
                    symbol=
                        symbol,

                    analysis_time=
                        analysis_time,

                    prepared_history=
                        prepared[
                            symbol
                        ],
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
                        symbol,

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

            publish(
                bus,
                "session_agent",
                "SESSION_ASSESSMENT",
                {
                    "approved":
                        True,

                    "required":
                        True,

                    "trading_session_allowed":
                        True,

                    "sessions_open":
                        session_state[
                            "sessions_open"
                        ],

                    "london":
                        session_state[
                            "london"
                        ],

                    "new_york":
                        session_state[
                            "new_york"
                        ],

                    "generated_at":
                        session_state[
                            "generated_at"
                        ],

                    "errors":
                        [],

                    "source":
                        "HISTORICAL_SESSION",
                },
            )

            # ==============================================
            # MACRO - EXPLICIT TECHNICAL-ONLY BYPASS
            # ==============================================

            publish(
                bus,
                "macro_agent",
                "MACRO_ASSESSMENT",
                historical_macro_assessment(),
            )

            # ==============================================
            # PORTFOLIO
            # ==============================================

            portfolio = (
                historical_portfolio_assessment(
                    symbol=
                        symbol,

                    active_reservations=
                        reservations,
                )
            )

            publish(
                bus,
                "portfolio_agent",
                "PORTFOLIO_ASSESSMENT",
                portfolio,
            )

            if not portfolio[
                "approved"
            ]:

                funnel[
                    "portfolio_blocked"
                ] += 1

                continue

            # ==============================================
            # TECHNICAL
            # ==============================================

            technical_agent.run(
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

            hard_errors = []

            if (
                technical[
                    "bias"
                ]
                == "NEUTRAL"
            ):

                hard_errors.append(
                    "TECHNICAL_BIAS_NEUTRAL"
                )

            if (
                technical[
                    "alignment"
                ]
                == "CONFLICT"
            ):

                hard_errors.append(
                    "TECHNICAL_ALIGNMENT_CONFLICT"
                )

            if (
                technical[
                    "setup_quality"
                ]
                <
                TECHNICAL_PREFILTER_QUALITY
            ):

                hard_errors.append(
                    "TECHNICAL_QUALITY_BELOW_PREFILTER"
                )

            if hard_errors:

                funnel[
                    "technical_hard_gate_rejected"
                ] += 1

                continue

            funnel[
                "hard_gate_pass"
            ] += 1

            row = {
                "analysis_time":
                    analysis_time.isoformat(),

                "symbol":
                    symbol,

                "technical_bias":
                    technical[
                        "bias"
                    ],

                "setup_quality":
                    technical[
                        "setup_quality"
                    ],

                "technical_alignment":
                    technical[
                        "alignment"
                    ],

                "sessions_open":
                    ",".join(
                        session_state[
                            "sessions_open"
                        ]
                    ),

                "stage":
                    "HARD_GATE_PASS",
            }

            # ==============================================
            # REGIME
            # ==============================================

            (
                regime,
                regime_cache_hit,
            ) = run_cached_agent(
                agent=
                    regime_agent,

                message_type=
                    "REGIME_ASSESSMENT",

                context=
                    context,

                bus=
                    bus,

                cache=
                    cache,
            )

            row[
                "regime_cache_hit"
            ] = regime_cache_hit

            row[
                "regime"
            ] = regime.get(
                "regime"
            )

            row[
                "regime_valid"
            ] = regime.get(
                "valid",
                False,
            )

            row[
                "regime_confidence"
            ] = regime.get(
                "confidence"
            )

            row[
                "regime_evidence_count"
            ] = len(
                regime.get(
                    "evidence_used",
                    [],
                )
            )

            row[
                "regime_evidence_used"
            ] = "|".join(
                regime.get(
                    "evidence_used",
                    [],
                )
            )

            row[
                "regime_reason_codes"
            ] = "|".join(
                regime.get(
                    "reason_codes",
                    [],
                )
            )

            row[
                "regime_validation_errors"
            ] = "|".join(
                regime.get(
                    "validation_errors",
                    [],
                )
            )

            if not regime.get(
                "valid",
                False,
            ):

                funnel[
                    "regime_invalid"
                ] += 1

                row[
                    "stage"
                ] = "REGIME_INVALID"

                events.append(
                    row
                )


                print(
                    "\n[REGIME INVALID]"
                )

                print(
                    f"Time: {analysis_time}"
                )

                print(
                    f"Symbol: {symbol}"
                )

                print(
                    "Regime:",
                    regime.get(
                        "regime"
                    ),
                )

                print(
                    "Confidence:",
                    regime.get(
                        "confidence"
                    ),
                )

                print(
                    "Evidence:",
                    regime.get(
                        "evidence_used",
                        [],
                    ),
                )

                print(
                    "Reason codes:",
                    regime.get(
                        "reason_codes",
                        [],
                    ),
                )

                print(
                    "Validation errors:",
                    regime.get(
                        "validation_errors",
                        [],
                    ),
                )

                continue

            funnel[
                "regime_valid"
            ] += 1

            # ==============================================
            # STRATEGY
            # ==============================================

            (
                strategy,
                strategy_cache_hit,
            ) = run_cached_agent(
                agent=
                    strategy_agent,

                message_type=
                    "STRATEGY_PROPOSAL",

                context=
                    context,

                bus=
                    bus,

                cache=
                    cache,
            )

            row[
                "strategy_valid"
            ] = strategy.get(
                "valid",
                False,
            )

            row[
                "strategy_confidence"
            ] = strategy.get(
                "confidence"
            )

            row[
                "strategy_evidence_count"
            ] = len(
                strategy.get(
                    "evidence_used",
                    [],
                )
            )

            row[
                "strategy_evidence_used"
            ] = "|".join(
                strategy.get(
                    "evidence_used",
                    [],
                )
            )

            row[
                "strategy_reason_codes"
            ] = "|".join(
                strategy.get(
                    "reason_codes",
                    [],
                )
            )

            row[
                "strategy_validation_errors"
            ] = "|".join(
                strategy.get(
                    "validation_errors",
                    [],
                )
            )

            row[
                "strategy_source"
            ] = strategy.get(
                "source"
            )

            row[
                "strategy_cache_hit"
            ] = strategy_cache_hit

            row[
                "strategy_action"
            ] = strategy.get(
                "action"
            )

            row[
                "strategy"
            ] = strategy.get(
                "strategy"
            )

            row[
                "direction"
            ] = strategy.get(
                "direction"
            )

            if not strategy.get(
                "valid",
                False,
            ):

                funnel[
                    "strategy_invalid"
                ] += 1

                row[
                    "stage"
                ] = "STRATEGY_INVALID"

                events.append(
                    row
                )

                continue

            if (
                strategy.get(
                    "action"
                )
                != "PROPOSE_TRADE"
            ):

                funnel[
                    "strategy_wait"
                ] += 1

                print(
                    "\n[STRATEGY WAIT]"
                )

                print(
                    f"Time: {analysis_time}"
                )

                print(
                    f"Symbol: {symbol}"
                )

                print(
                    "Technical bias:",
                    technical.get(
                        "bias"
                    ),
                )

                print(
                    "Setup quality:",
                    technical.get(
                        "setup_quality"
                    ),
                )

                print(
                    "Regime:",
                    regime.get(
                        "regime"
                    ),
                )

                print(
                    "Regime confidence:",
                    regime.get(
                        "confidence"
                    ),
                )

                print(
                    "Regime reason codes:",
                    regime.get(
                        "reason_codes",
                        [],
                    ),
                )

                print(
                    "Action:",
                    strategy.get(
                        "action"
                    ),
                )

                print(
                    "Strategy:",
                    strategy.get(
                        "strategy"
                    ),
                )

                print(
                    "Direction:",
                    strategy.get(
                        "direction"
                    ),
                )

                print(
                    "Confidence:",
                    strategy.get(
                        "confidence"
                    ),
                )

                print(
                    "Evidence:",
                    strategy.get(
                        "evidence_used",
                        [],
                    ),
                )

                print(
                    "Reason codes:",
                    strategy.get(
                        "reason_codes",
                        [],
                    ),
                )

                print(
                    "Validation errors:",
                    strategy.get(
                        "validation_errors",
                        [],
                    ),
                )

                print(
                    "Source:",
                    strategy.get(
                        "source"
                    ),
                )

                row[
                    "stage"
                ] = "STRATEGY_WAIT"

                events.append(
                    row
                )

                continue

            funnel[
                "strategy_proposed"
            ] += 1

            mode = (
                STRATEGY_TO_MODE.get(
                    strategy[
                        "strategy"
                    ]
                )
            )

            if mode is None:

                funnel[
                    "unsupported_strategy"
                ] += 1

                row[
                    "stage"
                ] = "UNSUPPORTED_STRATEGY"

                events.append(
                    row
                )

                continue

            # ==============================================
            # HISTORICAL PLANNER
            # ==============================================

            plan = (
                build_historical_trade_plan(
                    symbol=
                        symbol,

                    direction=
                        strategy[
                            "direction"
                        ],

                    mode=
                        mode,

                    market_state=
                        market_state,
                )
            )

            if (
                plan.get(
                    "status"
                )
                != "VALID"
            ):

                funnel[
                    "planner_rejected"
                ] += 1

                row[
                    "stage"
                ] = "PLANNER_REJECTED"

                row[
                    "planner_error"
                ] = plan.get(
                    "error"
                )

                events.append(
                    row
                )

                continue

            funnel[
                "planner_valid"
            ] += 1

            # ==============================================
            # HISTORICAL RISK
            # ==============================================

            (
                plan,
                risk,
            ) = historical_risk_assessment(
                plan=
                    plan,

                analysis_time=
                    analysis_time,

                prepared_history=
                    prepared,
            )

            publish(
                bus,
                "planner_agent",
                "TRADE_PLAN",
                plan,
            )

            publish(
                bus,
                "risk_agent",
                "RISK_ASSESSMENT",
                risk,
            )

            row[
                "order_type"
            ] = plan.get(
                "order_type"
            )

            row[
                "entry"
            ] = plan.get(
                "entry"
            )

            row[
                "stop_loss"
            ] = plan.get(
                "stop_loss"
            )

            row[
                "take_profit"
            ] = plan.get(
                "take_profit"
            )

            row[
                "estimated_loss_eur"
            ] = plan.get(
                "estimated_loss_eur"
            )

            row[
                "risk_reward"
            ] = plan.get(
                "risk_reward"
            )

            row[
                "spread_points"
            ] = plan.get(
                "spread_points"
            )

            if not risk.get(
                "approved",
                False,
            ):

                funnel[
                    "risk_rejected"
                ] += 1

                row[
                    "stage"
                ] = "RISK_REJECTED"

                row[
                    "risk_errors"
                ] = "|".join(
                    risk.get(
                        "errors",
                        [],
                    )
                )

                events.append(
                    row
                )

                continue

            funnel[
                "risk_approved"
            ] += 1

            # ==============================================
            # CRITIC
            # ==============================================

            (
                critic,
                critic_cache_hit,
            ) = run_cached_agent(
                agent=
                    critic_agent,

                message_type=
                    "CRITIC_ASSESSMENT",

                context=
                    context,

                bus=
                    bus,

                cache=
                    cache,
            )

            row[
                "critic_cache_hit"
            ] = critic_cache_hit

            row[
                "critic_verdict"
            ] = critic.get(
                "verdict"
            )

            if (
                not critic.get(
                    "valid",
                    False,
                )
                or
                critic.get(
                    "verdict"
                )
                != "NO_OBJECTION"
            ):

                funnel[
                    "critic_veto"
                ] += 1

            else:

                funnel[
                    "critic_no_objection"
                ] += 1

            # ==============================================
            # DECISION AGENT
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

            row[
                "final_status"
            ] = final.get(
                "status"
            )

            row[
                "final_approved"
            ] = final.get(
                "approved",
                False,
            )

            if not final.get(
                "approved",
                False,
            ):

                funnel[
                    "final_rejected"
                ] += 1

                row[
                    "stage"
                ] = "FINAL_REJECTED"

                row[
                    "final_errors"
                ] = "|".join(
                    final.get(
                        "errors",
                        [],
                    )
                )

                events.append(
                    row
                )

                continue

            funnel[
                "final_approved"
            ] += 1

            # ==============================================
            # EXECUTION SIMULATION
            # ==============================================

            execution = (
                simulate_trade(
                    plan=
                        plan,

                    signal_time=
                        analysis_time,

                    m5_history=
                        prepared[
                            symbol
                        ][
                            "M5"
                        ],
                )
            )

            release_time = utc_timestamp(
                execution[
                    "release_time"
                ]
            )

            reservations.append(
                {
                    "symbol":
                        symbol,

                    "signal_time":
                        analysis_time,

                    "release_time":
                        release_time,

                    "execution_result":
                        execution[
                            "result"
                        ],
                }
            )

            result_r = (
                execution.get(
                    "r"
                )
            )

            pnl_eur_approx = None

            if result_r is not None:

                pnl_eur_approx = (
                    float(
                        result_r
                    )
                    *
                    float(
                        plan[
                            "estimated_loss_eur"
                        ]
                    )
                )

            row.update(
                {
                    "stage":
                        "EXECUTED_HISTORICALLY",

                    "execution_result":
                        execution.get(
                            "result"
                        ),

                    "r":
                        result_r,

                    "fill_time":
                        (
                            str(
                                execution.get(
                                    "fill_time"
                                )
                            )
                            if execution.get(
                                "fill_time"
                            )
                            is not None
                            else None
                        ),

                    "exit_time":
                        str(
                            execution.get(
                                "exit_time"
                            )
                        ),

                    "release_time":
                        str(
                            execution.get(
                                "release_time"
                            )
                        ),

                    "pnl_eur_approx":
                        (
                            round(
                                pnl_eur_approx,
                                4,
                            )
                            if pnl_eur_approx
                            is not None
                            else None
                        ),
                }
            )

            events.append(
                row
            )

        if (
            time_index % 250
            == 0
        ):

            print(
                f"[{time_index}/{len(timeline)}] "
                f"{analysis_time} | "
                f"hard_pass="
                f"{funnel['hard_gate_pass']} | "
                f"final="
                f"{funnel['final_approved']}"
            )

    # ======================================================
    # OUTPUT
    # ======================================================

    dataframe = pd.DataFrame(
        events
    )

    start_name = (
        start.strftime(
            "%Y%m%d"
        )
    )

    end_name = (
        end.strftime(
            "%Y%m%d"
        )
    )

    symbols_name = "_".join(
        symbols
    )

    prefix = (
        f"{start_name}_"
        f"{end_name}_"
        f"{symbols_name}"
    )

    csv_path = (
        OUTPUT_DIR
        / f"{prefix}_events.csv"
    )

    summary_path = (
        OUTPUT_DIR
        / f"{prefix}_summary.json"
    )

    dataframe.to_csv(
        csv_path,
        index=False,
    )

    executed = (
        dataframe[
            dataframe[
                "stage"
            ]
            == "EXECUTED_HISTORICALLY"
        ]
        if (
            not dataframe.empty
            and
            "stage"
            in dataframe.columns
        )
        else pd.DataFrame()
    )

    resolved = (
        executed[
            executed[
                "execution_result"
            ].isin(
                [
                    "WIN",
                    "LOSS",
                ]
            )
        ]
        if (
            not executed.empty
            and
            "execution_result"
            in executed.columns
        )
        else pd.DataFrame()
    )

    wins = 0
    losses = 0
    pnl = 0.0
    win_rate = None
    profit_factor = None

    if not resolved.empty:

        wins = int(
            (
                resolved[
                    "execution_result"
                ]
                == "WIN"
            ).sum()
        )

        losses = int(
            (
                resolved[
                    "execution_result"
                ]
                == "LOSS"
            ).sum()
        )

        pnl = float(
            resolved[
                "pnl_eur_approx"
            ].fillna(
                0.0
            ).sum()
        )

        win_rate = (
            wins
            /
            len(
                resolved
            )
        )

        positive = float(
            resolved.loc[
                resolved[
                    "pnl_eur_approx"
                ]
                > 0,
                "pnl_eur_approx",
            ].sum()
        )

        negative = abs(
            float(
                resolved.loc[
                    resolved[
                        "pnl_eur_approx"
                    ]
                    < 0,
                    "pnl_eur_approx",
                ].sum()
            )
        )

        if negative > 0:

            profit_factor = (
                positive
                / negative
            )

    summary = {
        "scope":
            "MULTIAGENT_TECHNICAL_ONLY",

        "start":
            start.isoformat(),

        "end":
            end.isoformat(),

        "symbols":
            symbols,

        "llm_model":
            OLLAMA_MODEL,

        "cache_version":
            CACHE_VERSION,

        "funnel":
            dict(
                funnel
            ),

        "results": {
            "executed":
                int(
                    len(
                        executed
                    )
                ),

            "resolved_win_loss":
                int(
                    len(
                        resolved
                    )
                ),

            "wins":
                wins,

            "losses":
                losses,

            "win_rate":
                (
                    round(
                        win_rate,
                        4,
                    )
                    if win_rate
                    is not None
                    else None
                ),

            "pnl_eur_approx":
                round(
                    pnl,
                    2,
                ),

            "profit_factor_approx":
                (
                    round(
                        profit_factor,
                        4,
                    )
                    if profit_factor
                    is not None
                    else None
                ),
        },

        "limitations": [
            "NO_HISTORICAL_MACRO",
            "NO_HISTORICAL_MARGIN_MODEL",
            "NO_COMMISSION",
            "NO_SLIPPAGE",
            "NO_SWAP",
            "M5_INTRABAR_AMBIGUITY_EXCLUDED",
            "STOP_GAP_FILL_MODEL_NOT_HARDENED",
            "APRIL_OCTOBER_PERIOD_IS_EXPLORATORY",
        ],
    }

    with summary_path.open(
        "w",
        encoding="utf-8",
    ) as handle:

        json.dump(
            summary,
            handle,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print(
        "=" * 100
    )

    print(
        "REPLAY TERMINADO"
    )

    print(
        "=" * 100
    )

    print(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        )
    )

    print()
    print(
        f"Eventos: {csv_path}"
    )

    print(
        f"Resumen: {summary_path}"
    )


# ==========================================================
# CLI
# ==========================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        default=
            "2026-04-08T00:00:00+00:00",
    )

    parser.add_argument(
        "--end",
        default=
            "2026-04-09T00:00:00+00:00",
    )

    parser.add_argument(
        "--symbols",
        nargs="+",
        default=[
            "USDCAD",
        ],
    )

    args = parser.parse_args()

    run_replay(
        symbols=
            args.symbols,

        start=
            args.start,

        end=
            args.end,
    )


if __name__ == "__main__":
    main()