from typing import (
    Literal,
)

from pydantic import (
    BaseModel,
    Field,
)

from config.agent_config import (
    TECHNICAL_PREFILTER_QUALITY,
    PLANNER_SUPPORTED_STRATEGIES,
)

from src.agents.llm_client import (
    structured_chat,
)


from src.agents.normalization import (
    unique_list,
)


class StrategyAssessment(
    BaseModel
):

    action: Literal[
        "PROPOSE_TRADE",
        "WAIT",
    ]

    strategy: Literal[
        "TREND_CONTINUATION",
        "PULLBACK",
        "BREAKOUT",
        "NONE",
    ]

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    evidence_used: list[
        str
    ] = []

    reason_codes: list[
        Literal[
            "REGIME_SUPPORTS_STRATEGY",
            "TECHNICAL_ALIGNMENT_STRONG",
            "MOMENTUM_CONFLICT",
            "HIGHER_TIMEFRAME_CONFLICT",
            "BREAKOUT_NOT_CONFIRMED",
            "PULLBACK_NOT_CONFIRMED",
            "TREND_CONTINUATION_NOT_CONFIRMED",
            "INSUFFICIENT_COHERENCE",
        ]
    ]


class StrategyAgent:

    name = "strategy_agent"

    def deterministic_wait(
        self,
        context,
        bus,
        reason_code,
    ):

        return bus.publish(
            agent=
                self.name,

            message_type=
                "STRATEGY_PROPOSAL",

            payload={
                "action":
                    "WAIT",

                "strategy":
                    "NONE",

                "direction":
                    "NONE",

                "confidence":
                    0.0,

                "evidence_used":
                    [],

                "reason_codes":
                    [
                        reason_code
                    ],

                "valid":
                    True,

                "validation_errors":
                    [],

                "source":
                    "DETERMINISTIC_GATE",
            },
        )

    def run(
        self,
        context,
        bus,
    ):

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

        macro = (
            bus.require(
                "MACRO_ASSESSMENT"
            )[
                "payload"
            ]
        )

        bias = technical[
            "bias"
        ]

        quality = technical[
            "setup_quality"
        ]

        alignment = technical[
            "alignment"
        ]

        # ==================================================
        # PREFILTRO DETERMINISTA
        # ==================================================
        if not macro.get(
            "approved",
            False,
        ):

            return self.deterministic_wait(
                context,
                bus,
                "MACRO_BLOCKED",
            )
        
        if bias == "NEUTRAL":

            return self.deterministic_wait(
                context,
                bus,
                "TECHNICAL_BIAS_NEUTRAL",
            )

        if alignment == "CONFLICT":

            return self.deterministic_wait(
                context,
                bus,
                "HIGHER_TIMEFRAME_CONFLICT",
            )

        if (
            quality
            <
            TECHNICAL_PREFILTER_QUALITY
        ):

            return self.deterministic_wait(
                context,
                bus,
                (
                    "Technical quality below "
                    f"{TECHNICAL_PREFILTER_QUALITY}."
                ),
            )

        if not regime.get(
            "valid",
            False,
        ):

            return self.deterministic_wait(
                context,
                bus,
                "REGIME_ASSESSMENT_INVALID",
            )

        # Dirección NO la decide el LLM.
        direction = (
            "BUY"
            if bias
            == "BULLISH"
            else "SELL"
        )

        available_evidence = (
            context.snapshot.get(
                "available_evidence_ids",
                [],
            )
        )

        payload = {
            "symbol":
                context.symbol,

            "direction_fixed_by_python":
                direction,

            "technical":
                technical,

            "regime":
                regime,

            "allowed_strategies":
                sorted(
                    PLANNER_SUPPORTED_STRATEGIES
                ),

            "allowed_evidence_ids":
                available_evidence,

            "macro":
                {
                    "macro_risk":
                        macro.get(
                            "macro_risk"
                        ),

                    "approved":
                        macro.get(
                            "approved"
                        ),

                    "moderate_warnings":
                        macro.get(
                            "moderate_warnings",
                            [],
                        ),
                },
        }

        system_prompt = """
You are the Strategy Agent in a Forex multi-agent system.

Technical direction has already been fixed by deterministic Python.

You CANNOT change direction.

Your task is only to decide whether one of the supported
strategy families is appropriate now.

Supported strategies:
TREND_CONTINUATION
PULLBACK
BREAKOUT
NONE

You do NOT:
- calculate indicators
- calculate entry price
- calculate SL
- calculate TP
- calculate risk
- change BUY to SELL or SELL to BUY
- execute orders
- invent evidence

Use only the Technical Agent and Regime Agent outputs.

evidence_used may contain ONLY IDs from allowed_evidence_ids.

If there is not enough coherence, choose:
action = WAIT
strategy = NONE

Return structured output only.
"""

        try:

            assessment = (
                structured_chat(
                    schema=
                        StrategyAssessment,

                    system_prompt=
                        system_prompt,

                    payload=
                        payload,
                )
            )

            assessment.evidence_used = (
                unique_list(
                    assessment.evidence_used,
                    max_items=20,
                )
            )

            assessment.caution_flags = (
                unique_list(
                    assessment.caution_flags,
                )
            )

            assessment.reason_codes = (
                unique_list(
                    assessment.reason_codes,
                )
            )

        except Exception as exc:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "STRATEGY_PROPOSAL",

                payload={
                    "action":
                        "WAIT",

                    "strategy":
                        "NONE",

                    "direction":
                        direction,

                    "confidence":
                        0.0,

                    "evidence_used":
                        [],

                    "rationale":
                        "Strategy Agent failed.",

                    "valid":
                        False,

                    "validation_errors":
                        [
                            str(exc)
                        ],

                    "source":
                        "LLM",
                },
            )

        invalid_evidence = [
            evidence

            for evidence
            in assessment.evidence_used

            if evidence
            not in available_evidence
        ]

        errors = []

        if invalid_evidence:

            errors.extend(
                [
                    (
                        "INVALID_EVIDENCE: "
                        + evidence
                    )

                    for evidence
                    in invalid_evidence
                ]
            )

        if (
            assessment.strategy
            != "NONE"
            and
            assessment.strategy
            not in
            PLANNER_SUPPORTED_STRATEGIES
        ):

            errors.append(
                "UNSUPPORTED_STRATEGY"
            )

        if (
            assessment.action
            == "PROPOSE_TRADE"
            and
            assessment.strategy
            == "NONE"
        ):

            errors.append(
                "TRADE_WITHOUT_STRATEGY"
            )

        if errors:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "STRATEGY_PROPOSAL",

                payload={
                    "action":
                        "WAIT",

                    "strategy":
                        "NONE",

                    "direction":
                        direction,

                    "confidence":
                        0.0,

                    "evidence_used":
                        [],

                    "rationale":
                        "Strategy output failed validation.",

                    "valid":
                        False,

                    "validation_errors":
                        errors,

                    "source":
                        "LLM",
                },
            )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "STRATEGY_PROPOSAL",

            payload={
                **assessment.model_dump(),

                "direction":
                    direction,

                "valid":
                    True,

                "validation_errors":
                    [],

                "source":
                    "LLM",
            },
        )