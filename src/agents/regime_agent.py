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


class RegimeAssessment(
    BaseModel
):

    regime: Literal[
        "TREND",
        "RANGE",
        "BREAKOUT",
        "REVERSAL",
        "UNCLEAR",
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

    caution_flags: list[
        Literal[
            "MIXED_TIMEFRAMES",
            "WEAK_MOMENTUM",
            "EXTREME_RSI",
            "LOW_VOLUME",
            "HIGH_VOLATILITY",
            "CONFLICTING_RETURNS",
        ]
    ] = Field(
        default_factory=list
    )

    reason_codes: list[
        Literal[
            "STRONG_TREND_STRUCTURE",
            "MIXED_TIMEFRAMES",
            "BREAKOUT_CHARACTERISTICS",
            "RANGE_CHARACTERISTICS",
            "REVERSAL_CHARACTERISTICS",
            "MOMENTUM_CONFLICT",
            "VOLUME_CONFIRMATION",
            "VOLUME_NOT_CONFIRMING",
            "INSUFFICIENT_EVIDENCE",
        ]
    ] = Field(
        default_factory=list
    )


class RegimeAgent:

    name = "regime_agent"

    def run(
        self,
        context,
        bus,
    ):

        technical_message = (
            bus.require(
                "TECHNICAL_ASSESSMENT"
            )
        )

        technical = (
            technical_message[
                "payload"
            ]
        )

        snapshot_evidence = set(
            context.snapshot.get(
                "available_evidence_ids",
                [],
            )
        )

        technical_evidence = set(
            technical.get(
                "supporting_evidence",
                [],
            )
        )

        technical_evidence.update(
            technical.get(
                "conflicting_evidence",
                [],
            )
        )

        available_evidence = sorted(
            snapshot_evidence
            |
            technical_evidence
        )

        payload = {
            "symbol":
                context.symbol,

            "analysis_time":
                context.analysis_time,

            "technical_summary": {
                "bias":
                    technical.get(
                        "bias"
                    ),

                "overall_score":
                    technical.get(
                        "overall_score"
                    ),

                "alignment":
                    technical.get(
                        "alignment"
                    ),

                "setup_quality":
                    technical.get(
                        "setup_quality"
                    ),
            },

            "technical_supporting_evidence":
                technical.get(
                    "supporting_evidence",
                    [],
                ),

            "technical_conflicting_evidence":
                technical.get(
                    "conflicting_evidence",
                    [],
                ),

            "allowed_evidence_ids":
                available_evidence,
        }

        system_prompt = """
You are the Regime Agent in a Forex multi-agent system.

Your ONLY task is to classify the current market regime.

Valid regimes:
TREND
RANGE
BREAKOUT
REVERSAL
UNCLEAR

The Technical Agent and deterministic Python have already
calculated the market facts.

You MUST NOT:
- calculate indicators
- invent facts
- invent evidence IDs
- change timeframe names
- use timeframes not present in allowed_evidence_ids
- choose BUY or SELL
- calculate entry, SL, TP or risk
- execute orders

IMPORTANT EVIDENCE RULES:

If regime is:
TREND
RANGE
BREAKOUT
REVERSAL

then evidence_used MUST contain at least 3 and at most 12
EXACT evidence IDs copied from allowed_evidence_ids.

Do not modify, abbreviate or create evidence IDs.

If there is not enough valid evidence to cite at least
3 evidence IDs, return:

regime = UNCLEAR

For UNCLEAR, evidence_used may be empty.

Do not write free-form explanations.

Return only:
- regime
- confidence
- evidence_used
- caution_flags
- reason_codes

Every factual conclusion must be grounded in evidence_used.
""" 
        def normalize_assessment(
            item,
        ):

            item.evidence_used = (
                unique_list(
                    item.evidence_used,
                    max_items=12,
                )
            )

            item.caution_flags = (
                unique_list(
                    item.caution_flags,
                )
            )

            item.reason_codes = (
                unique_list(
                    item.reason_codes,
                )
            )

            return item


        def validate_assessment(
            item,
        ):

            errors = []

            invalid_evidence = [
                evidence

                for evidence
                in item.evidence_used

                if evidence
                not in available_evidence
            ]

            for evidence in invalid_evidence:

                errors.append(
                    (
                        "INVALID_EVIDENCE: "
                        + evidence
                    )
                )

            if (
                item.regime
                != "UNCLEAR"
                and
                len(
                    item.evidence_used
                )
                < 3
            ):

                errors.append(
                    "REGIME_INSUFFICIENT_EVIDENCE"
                )

            if (
                item.regime
                == "UNCLEAR"
                and
                item.confidence
                > 0.50
            ):

                errors.append(
                    "UNCLEAR_WITH_HIGH_CONFIDENCE"
                )

            return list(
                dict.fromkeys(
                    errors
                )
            )


        # ==================================================
        # FIRST LLM ATTEMPT
        # ==================================================

        try:

            assessment = (
                structured_chat(
                    schema=
                        RegimeAssessment,

                    system_prompt=
                        system_prompt,

                    payload=
                        payload,
                )
            )

            assessment = (
                normalize_assessment(
                    assessment
                )
            )

            validation_errors = (
                validate_assessment(
                    assessment
                )
            )

        except Exception as exc:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "REGIME_ASSESSMENT",

                payload={
                    "regime":
                        "UNCLEAR",

                    "confidence":
                        0.0,

                    "evidence_used":
                        [],

                    "caution_flags":
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


        # ==================================================
        # ONE CORRECTION ATTEMPT
        # ==================================================

        if validation_errors:

            retry_payload = {
                **payload,

                "previous_invalid_output":
                    assessment.model_dump(),

                "previous_validation_errors":
                    validation_errors,

                "correction_instruction":
                    (
                        "Correct the previous output. "
                        "Use ONLY exact evidence IDs "
                        "from allowed_evidence_ids. "
                        "A non-UNCLEAR regime requires "
                        "at least 3 valid evidence IDs. "
                        "If that is not possible, "
                        "return UNCLEAR."
                    ),
            }

            try:

                assessment = (
                    structured_chat(
                        schema=
                            RegimeAssessment,

                        system_prompt=
                            system_prompt,

                        payload=
                            retry_payload,
                    )
                )

                assessment = (
                    normalize_assessment(
                        assessment
                    )
                )

                validation_errors = (
                    validate_assessment(
                        assessment
                    )
                )

            except Exception as exc:

                return bus.publish(
                    agent=
                        self.name,

                    message_type=
                        "REGIME_ASSESSMENT",

                    payload={
                        "regime":
                            "UNCLEAR",

                        "confidence":
                            0.0,

                        "evidence_used":
                            [],

                        "caution_flags":
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
                                    "LLM_RETRY_ERROR: "
                                    + str(exc)
                                )
                            ],

                        "source":
                            "LLM",
                    },
                )


        # ==================================================
        # FAIL CLOSED AFTER RETRY
        # ==================================================

        if validation_errors:

            return bus.publish(
                agent=
                    self.name,

                message_type=
                    "REGIME_ASSESSMENT",

                payload={
                    "regime":
                        "UNCLEAR",

                    "confidence":
                        0.0,

                    "evidence_used":
                        [],

                    "caution_flags":
                        [],

                    "reason_codes":
                        [
                            "INSUFFICIENT_EVIDENCE"
                        ],

                    "valid":
                        False,

                    "validation_errors":
                        validation_errors,

                    "source":
                        "LLM",
                },
            )


        # ==================================================
        # VALID RESULT
        # ==================================================

        return bus.publish(
            agent=
                self.name,

            message_type=
                "REGIME_ASSESSMENT",

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