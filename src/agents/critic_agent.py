from typing import (
    Literal,
)

from pydantic import (
    BaseModel,
    Field,
)

from src.agents.llm_client import (
    structured_chat,
)

from src.agents.normalization import (
    unique_list,
)


class CriticAssessment(
    BaseModel
):

    verdict: Literal[
        "NO_OBJECTION",
        "VETO",
    ]

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    evidence_used: list[
        str
    ] = Field(
        default_factory=list
    )

    reason_codes: list[
        Literal[
            "NO_MATERIAL_OBJECTION",
            "TECHNICAL_CONFLICT",
            "REGIME_STRATEGY_MISMATCH",
            "STRATEGY_COHERENCE_WEAK",
            "EVIDENCE_INCONSISTENT",
            "RISK_TOO_CLOSE_TO_LIMIT",
            "MARGIN_HEADROOM_LOW",
            "INSUFFICIENT_EVIDENCE",
        ]
    ] = Field(
        default_factory=list
    )


class CriticAgent:

    name = "critic_agent"

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

        if not risk.get(
            "approved",
            False,
        ):

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "CRITIC_ASSESSMENT",

                payload={
                    "verdict":
                        "VETO",

                    "confidence":
                        1.0,

                    "evidence_used":
                        [],

                    "reason_codes":
                        [
                            "INSUFFICIENT_EVIDENCE"
                        ],

                    "valid":
                        True,

                    "validation_errors":
                        [],

                    "source":
                        "DETERMINISTIC_GATE",
                },
            )

        allowed_evidence = sorted(
            set(
                context.snapshot.get(
                    "available_evidence_ids",
                    [],
                )
            )
            |
            set(
                technical.get(
                    "supporting_evidence",
                    [],
                )
            )
            |
            set(
                technical.get(
                    "conflicting_evidence",
                    [],
                )
            )
            |
            set(
                regime.get(
                    "evidence_used",
                    [],
                )
            )
            |
            set(
                strategy.get(
                    "evidence_used",
                    [],
                )
            )
        )

        payload = {
            "symbol":
                context.symbol,

            "technical": {
                "bias":
                    technical.get(
                        "bias"
                    ),

                "alignment":
                    technical.get(
                        "alignment"
                    ),

                "setup_quality":
                    technical.get(
                        "setup_quality"
                    ),

                "conflicting_evidence":
                    technical.get(
                        "conflicting_evidence",
                        [],
                    ),
            },

            "regime": {
                "regime":
                    regime.get(
                        "regime"
                    ),

                "confidence":
                    regime.get(
                        "confidence"
                    ),

                "reason_codes":
                    regime.get(
                        "reason_codes",
                        [],
                    ),
            },

            "strategy": {
                "strategy":
                    strategy.get(
                        "strategy"
                    ),

                "direction":
                    strategy.get(
                        "direction"
                    ),

                "confidence":
                    strategy.get(
                        "confidence"
                    ),

                "reason_codes":
                    strategy.get(
                        "reason_codes",
                        [],
                    ),
            },

            "trade_plan": {
                "order_type":
                    plan.get(
                        "order_type"
                    ),

                "entry":
                    plan.get(
                        "entry"
                    ),

                "stop_loss":
                    plan.get(
                        "stop_loss"
                    ),

                "take_profit":
                    plan.get(
                        "take_profit"
                    ),

                "estimated_loss_eur":
                    plan.get(
                        "estimated_loss_eur"
                    ),

                "risk_reward":
                    plan.get(
                        "risk_reward"
                    ),

                "spread_points":
                    plan.get(
                        "spread_points"
                    ),

                "margin_required_eur":
                    plan.get(
                        "margin_required_eur"
                    ),

                "free_margin_eur":
                    plan.get(
                        "free_margin_eur"
                    ),
            },

            "risk_approved":
                risk.get(
                    "approved"
                ),

            "allowed_evidence_ids":
                allowed_evidence,
        }

        system_prompt = """
You are the Critic Agent in a Forex multi-agent system.

The deterministic risk engine has already approved the
trade mechanically.

Your role is adversarial:
look for a material contradiction between the technical
assessment, regime, strategy and proposed trade.

You CANNOT:
- change BUY to SELL
- calculate a new entry
- calculate a new SL or TP
- change volume
- override deterministic risk checks
- execute orders
- invent evidence
- write free-form explanations

NO_OBJECTION does NOT authorize a trade.
It only means that you found no material qualitative objection.

If uncertain, choose VETO.

evidence_used may contain ONLY exact IDs from
allowed_evidence_ids.

Return only:
- verdict
- confidence
- evidence_used
- reason_codes
"""

        try:

            assessment = (
                structured_chat(
                    schema=
                        CriticAssessment,

                    system_prompt=
                        system_prompt,

                    payload=
                        payload,
                )
            )

        except Exception as exc:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "CRITIC_ASSESSMENT",

                payload={
                    "verdict":
                        "VETO",

                    "confidence":
                        1.0,

                    "evidence_used":
                        [],

                    "reason_codes":
                        [
                            "INSUFFICIENT_EVIDENCE"
                        ],

                    "valid":
                        False,

                    "validation_errors":
                        [
                            (
                                "LLM_ERROR: "
                                + str(exc)
                            )
                        ],

                    "source":
                        "LLM",
                },
            )

        assessment.evidence_used = (
            unique_list(
                assessment.evidence_used,
                max_items=12,
            )
        )

        assessment.reason_codes = (
            unique_list(
                assessment.reason_codes,
            )
        )

        invalid_evidence = [
            item

            for item
            in assessment.evidence_used

            if item
            not in allowed_evidence
        ]

        if invalid_evidence:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "CRITIC_ASSESSMENT",

                payload={
                    "verdict":
                        "VETO",

                    "confidence":
                        1.0,

                    "evidence_used":
                        [],

                    "reason_codes":
                        [
                            "EVIDENCE_INCONSISTENT"
                        ],

                    "valid":
                        False,

                    "validation_errors":
                        [
                            (
                                "INVALID_EVIDENCE: "
                                + item
                            )

                            for item
                            in invalid_evidence
                        ],

                    "source":
                        "LLM",
                },
            )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "CRITIC_ASSESSMENT",

            payload={
                **assessment.model_dump(),

                "valid":
                    True,

                "validation_errors":
                    [],

                "source":
                    "LLM",
            },
        )