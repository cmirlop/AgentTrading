from typing import Literal

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


class LevelThesisAssessment(
    BaseModel
):

    thesis: Literal[
        "SUPPORT_HOLD",
        "SUPPORT_BREAK",
        "RESISTANCE_HOLD",
        "RESISTANCE_BREAK",
        "UNCLEAR",
    ]

    target_level: Literal[
        "SUPPORT",
        "RESISTANCE",
        "NONE",
    ]

    operation: Literal[
        "BUY",
        "SELL",
        "NONE",
    ]

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    evidence_used: list[str] = []

    reason_codes: list[
        Literal[
            "LEVEL_STRUCTURE_SUPPORTS_HOLD",
            "LEVEL_STRUCTURE_SUPPORTS_BREAK",
            "TREND_SUPPORTS_HOLD",
            "TREND_SUPPORTS_BREAK",
            "MOMENTUM_SUPPORTS_HOLD",
            "MOMENTUM_SUPPORTS_BREAK",
            "REGIME_SUPPORTS_HOLD",
            "REGIME_SUPPORTS_BREAK",
            "MACRO_SUPPORTS_HOLD",
            "MACRO_SUPPORTS_BREAK",
            "CONFLICTING_SIGNALS",
            "INSUFFICIENT_EVIDENCE",
        ]
    ]


THESIS_OPERATION = {
    "SUPPORT_HOLD":
        "BUY",

    "SUPPORT_BREAK":
        "SELL",

    "RESISTANCE_HOLD":
        "SELL",

    "RESISTANCE_BREAK":
        "BUY",

    "UNCLEAR":
        "NONE",
}


THESIS_LEVEL = {
    "SUPPORT_HOLD":
        "SUPPORT",

    "SUPPORT_BREAK":
        "SUPPORT",

    "RESISTANCE_HOLD":
        "RESISTANCE",

    "RESISTANCE_BREAK":
        "RESISTANCE",

    "UNCLEAR":
        "NONE",
}


class LevelThesisAgent:

    name = "level_thesis_agent"


    def _invalid(
        self,
        bus,
        errors,
    ):

        return bus.publish(
            agent=
                self.name,

            message_type=
                "LEVEL_THESIS",

            payload={
                "thesis":
                    "UNCLEAR",

                "target_level":
                    "NONE",

                "operation":
                    "NONE",

                "confidence":
                    0.0,

                "evidence_used":
                    [],

                "reason_codes":
                    [
                        "INSUFFICIENT_EVIDENCE"
                    ],

                "valid":
                    False,

                "validation_errors":
                    errors,

                "source":
                    "LLM",
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

        level_evidence = (
            bus.require(
                "LEVEL_EVIDENCE"
            )[
                "payload"
            ]
        )

        if (
            level_evidence.get(
                "status"
            )
            != "OK"
        ):

            return self._invalid(
                bus,
                [
                    "LEVEL_EVIDENCE_INVALID"
                ],
            )

        if not regime.get(
            "valid",
            False,
        ):

            return self._invalid(
                bus,
                [
                    "REGIME_INVALID"
                ],
            )

        technical_evidence = (
            context.snapshot.get(
                "available_evidence_ids",
                [],
            )
        )

        level_evidence_ids = (
            level_evidence.get(
                "available_evidence_ids",
                [],
            )
        )

        allowed_evidence = list(
            dict.fromkeys(
                technical_evidence
                +
                level_evidence_ids
            )
        )

        facts = level_evidence.get(
            "facts",
            {},
        )

        support = facts.get(
            "support"
        )

        resistance = facts.get(
            "resistance"
        )

        historical_macro_available = (
            macro.get(
                "historical_data_available",
                True,
            )
        )

        payload = {
            "symbol":
                context.symbol,

            "analysis_time":
                context.analysis_time,

            "technical":
                technical,

            "regime":
                regime,

            "market_structure":
                level_evidence.get(
                    "market_structure"
                ),

            "level_facts":
                facts,

            "allowed_evidence_ids":
                allowed_evidence,

            "macro": {
                "historical_data_available":
                    historical_macro_available,

                "macro_risk":
                    macro.get(
                        "macro_risk"
                    ),

                "moderate_warnings":
                    macro.get(
                        "moderate_warnings",
                        [],
                    ),
            },
        }

        system_prompt = """
You are the Level Thesis Agent in a Forex trading system.

Your task is to evaluate the nearest SUPPORT and RESISTANCE
and decide which market hypothesis is best supported.

Possible theses:

SUPPORT_HOLD
- price is expected to reject or bounce upward from support
- operation must be BUY

SUPPORT_BREAK
- price is expected to break downward through support
- operation must be SELL

RESISTANCE_HOLD
- price is expected to reject or fall from resistance
- operation must be SELL

RESISTANCE_BREAK
- price is expected to break upward through resistance
- operation must be BUY

UNCLEAR
- evidence does not justify one of the four hypotheses
- operation must be NONE
- target_level must be NONE

Use the supplied:
- support/resistance structure
- level strength
- number of touches
- multi-timeframe confirmation
- distance to levels
- technical trend
- momentum
- regime
- macro/news context ONLY when actually available

IMPORTANT:

You do NOT calculate support or resistance.
Python has already calculated the levels.

You do NOT calculate indicators.

You do NOT calculate entry, SL, TP or lot size.

You do NOT execute orders.

You must NOT invent news, events or macro facts.

If historical macro/news data is marked unavailable,
do not use macro/news as evidence and do not make claims
about what news existed at that historical time.

evidence_used may contain ONLY exact IDs from
allowed_evidence_ids.

A non-UNCLEAR thesis should use evidence from the
level structure AND technical/regime context.

Do not choose WAIT merely because some conflicting
evidence exists. Markets normally contain conflicting
evidence. Choose UNCLEAR only when the evidence is
genuinely insufficient to distinguish a hold from a break.

Return structured output only.
"""

        try:

            assessment = (
                structured_chat(
                    schema=
                        LevelThesisAssessment,

                    system_prompt=
                        system_prompt,

                    payload=
                        payload,
                )
            )

            assessment.evidence_used = (
                unique_list(
                    assessment.evidence_used,
                    max_items=16,
                )
            )

            assessment.reason_codes = (
                unique_list(
                    assessment.reason_codes,
                )
            )

        except Exception as exc:

            return self._invalid(
                bus,
                [
                    (
                        "LLM_ERROR: "
                        + str(exc)
                    )
                ],
            )

        errors = []

        # ==================================================
        # EVIDENCIA INVENTADA
        # ==================================================

        invalid_evidence = [
            item

            for item
            in assessment.evidence_used

            if item
            not in allowed_evidence
        ]

        for item in invalid_evidence:

            errors.append(
                (
                    "INVALID_EVIDENCE: "
                    + item
                )
            )

        # ==================================================
        # COHERENCIA TESIS -> NIVEL -> OPERACION
        # ==================================================

        expected_operation = (
            THESIS_OPERATION[
                assessment.thesis
            ]
        )

        expected_level = (
            THESIS_LEVEL[
                assessment.thesis
            ]
        )

        if (
            assessment.operation
            != expected_operation
        ):

            errors.append(
                "THESIS_OPERATION_MISMATCH"
            )

        if (
            assessment.target_level
            != expected_level
        ):

            errors.append(
                "THESIS_LEVEL_MISMATCH"
            )

        # ==================================================
        # EXISTENCIA DEL NIVEL
        # ==================================================

        if (
            assessment.target_level
            == "SUPPORT"
            and
            support is None
        ):

            errors.append(
                "SUPPORT_NOT_AVAILABLE"
            )

        if (
            assessment.target_level
            == "RESISTANCE"
            and
            resistance is None
        ):

            errors.append(
                "RESISTANCE_NOT_AVAILABLE"
            )

        # ==================================================
        # UNA TESIS OPERATIVA NECESITA EVIDENCIA REAL
        # ==================================================

        if (
            assessment.thesis
            != "UNCLEAR"
        ):

            level_ids_used = [
                item

                for item
                in assessment.evidence_used

                if (
                    item.startswith(
                        "SUPPORT_"
                    )
                    or
                    item.startswith(
                        "RESISTANCE_"
                    )
                    or
                    item.startswith(
                        "PRICE_"
                    )
                    or
                    item.startswith(
                        "NEAREST_"
                    )
                )
            ]

            technical_ids_used = [
                item

                for item
                in assessment.evidence_used

                if item
                in technical_evidence
            ]

            if len(
                assessment.evidence_used
            ) < 3:

                errors.append(
                    "INSUFFICIENT_EVIDENCE"
                )

            if not level_ids_used:

                errors.append(
                    "THESIS_WITHOUT_LEVEL_EVIDENCE"
                )

            if not technical_ids_used:

                errors.append(
                    "THESIS_WITHOUT_TECHNICAL_EVIDENCE"
                )

        # ==================================================
        # UNCLEAR NO DEBE TENER OPERACION
        # ==================================================

        if (
            assessment.thesis
            == "UNCLEAR"
        ):

            if (
                assessment.operation
                != "NONE"
            ):

                errors.append(
                    "UNCLEAR_WITH_OPERATION"
                )

            if assessment.confidence > 0.60:

                errors.append(
                    "UNCLEAR_WITH_HIGH_CONFIDENCE"
                )

        # ==================================================
        # NO PERMITIR MACRO INVENTADO EN HISTORICO
        # ==================================================

        if not historical_macro_available:

            forbidden_macro_reasons = {
                "MACRO_SUPPORTS_HOLD",
                "MACRO_SUPPORTS_BREAK",
            }

            if any(
                item
                in forbidden_macro_reasons

                for item
                in assessment.reason_codes
            ):

                errors.append(
                    "MACRO_REASON_WITHOUT_HISTORICAL_DATA"
                )

        if errors:

            return self._invalid(
                bus,
                list(
                    dict.fromkeys(
                        errors
                    )
                ),
            )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "LEVEL_THESIS",

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