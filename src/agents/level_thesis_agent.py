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

    # reason_codes: list[
    #     Literal[
    #         "LEVEL_STRUCTURE_SUPPORTS_HOLD",
    #         "LEVEL_STRUCTURE_SUPPORTS_BREAK",
    #         "TREND_SUPPORTS_HOLD",
    #         "TREND_SUPPORTS_BREAK",
    #         "MOMENTUM_SUPPORTS_HOLD",
    #         "MOMENTUM_SUPPORTS_BREAK",
    #         "REGIME_SUPPORTS_HOLD",
    #         "REGIME_SUPPORTS_BREAK",
    #         "MACRO_SUPPORTS_HOLD",
    #         "MACRO_SUPPORTS_BREAK",
    #         "CONFLICTING_SIGNALS",
    #         "INSUFFICIENT_EVIDENCE",
    #     ]
    # ]



class LevelHypothesisComparison(
    BaseModel
):

    hold_score: float = Field(
        ge=0.0,
        le=1.0,
    )

    break_score: float = Field(
        ge=0.0,
        le=1.0,
    )

    hold_evidence_used: list[
        str
    ] = Field(
        default_factory=list
    )

    break_evidence_used: list[
        str
    ] = Field(
        default_factory=list
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )
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


        price_action = (
            level_evidence.get(
                "price_action",
                {},
            )
        )

        price_action_evidence = (
            level_evidence.get(
                "price_action_evidence",
                [],
            )
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

            "level_price_action":
                price_action,

            "price_action_evidence":
                price_action_evidence,

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

You must evaluate the level price is currently interacting with.

If market_structure = AT_SUPPORT:
you may choose only SUPPORT_HOLD, SUPPORT_BREAK or UNCLEAR.

If market_structure = AT_RESISTANCE:
you may choose only RESISTANCE_HOLD, RESISTANCE_BREAK or UNCLEAR.

If market_structure = COMPRESSED_BETWEEN_LEVELS:
you may evaluate either the support or resistance.

Do not choose a support thesis when price is currently
testing resistance, and do not choose a resistance thesis
when price is currently testing support.

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

For every non-UNCLEAR thesis:

- evidence_used MUST contain at least one LEVEL evidence ID.
- evidence_used MUST contain at least one TECHNICAL evidence ID.
- Technical evidence may SUPPORT or CONFLICT with the thesis.
- Do not omit conflicting technical evidence just because the
  level structure is strong.

Examples of TECHNICAL evidence IDs include:
- *_PRICE_ABOVE_EMA*
- *_PRICE_BELOW_EMA*
- *_EMA_STRUCTURE_BULLISH
- *_EMA_STRUCTURE_BEARISH
- *_RSI_STRONG
- *_RSI_WEAK
- *_RETURN5_POSITIVE
- *_RETURN5_NEGATIVE

If you cannot cite both level evidence and technical evidence,
return UNCLEAR.

Technical evidence can SUPPORT or CONFLICT with the thesis.

Never claim that bearish trend or bearish momentum supports
a bullish thesis.

Never claim that bullish trend or bullish momentum supports
a bearish thesis.

Bullish theses are:
- SUPPORT_HOLD
- RESISTANCE_BREAK

Bearish theses are:
- RESISTANCE_HOLD
- SUPPORT_BREAK

Conflicting technical evidence does not automatically require
UNCLEAR. It may be used as conflicting evidence while the
level structure supports the final thesis.

Do not choose WAIT merely because some conflicting
evidence exists. Markets normally contain conflicting
evidence. Choose UNCLEAR only when the evidence is
genuinely insufficient to distinguish a hold from a break.

Return structured output only.
"""
        market_structure = (
            level_evidence.get(
                "market_structure"
            )
        )

        if (
            market_structure
            == "AT_SUPPORT"
        ):

            active_level = (
                "SUPPORT"
            )

        elif (
            market_structure
            == "AT_RESISTANCE"
        ):

            active_level = (
                "RESISTANCE"
            )

        else:

            # COMPRESSED_BETWEEN_LEVELS se evaluará
            # más adelante comparando ambos niveles.
            # Por ahora: fail closed.
            active_level = None


        comparison_prompt = """
You are the Level Hypothesis Comparison Agent
inside a Forex trading system.

Python has already selected the active support or
resistance level.

You do NOT choose:
- trade direction
- BUY or SELL
- final thesis
- entry
- stop loss
- take profit
- risk
- execution

Your ONLY task is to compare two competing hypotheses:

1. HOLD
2. BREAK

The active level is supplied as active_level.

If active_level = SUPPORT:

HOLD means:
price rejects support and moves upward.

BREAK means:
price breaks downward through support.

If active_level = RESISTANCE:

HOLD means:
price rejects resistance and moves downward.

BREAK means:
price breaks upward through resistance.


IMPORTANT:

Do NOT assume that a support or resistance will hold
just because the level exists.

HOLD and BREAK must be evaluated as competing hypotheses.

Consider:
- level strength
- number of touches
- multi-timeframe presence
- distance to level
- trend
- momentum
- EMA structure
- RSI
- recent returns
- regime
- macro information only when actually available

A strong level may support HOLD.

Strong directional momentum INTO a level may support BREAK.

LEVEL PRICE ACTION:

The payload may include:
- level_price_action
- price_action_evidence

These describe recent interaction with the ACTIVE level.

Interpret them as follows:

APPROACH_STRONG or APPROACH_MODERATE:
- price has moved materially toward the active level.
- this alone does NOT imply BREAK.

APPROACH_WITH_REPEATED_TESTS:
- price is pressing toward a level that has already been tested repeatedly.
- this may support BREAK pressure.
- it is stronger evidence when both M5 and M15 agree.
- it is NOT automatic proof of BREAK.

APPROACH_UNTESTED:
- price is approaching the level, but the recent window has not yet tested the zone.
- do not treat this as established rejection or established breakout pressure.

REPEATED_TESTS:
- the level has been interacted with multiple times.
- repeated tests can mean persistent pressure OR repeated rejection.
- use current trajectory and other price-action evidence to disambiguate.

CLOSE_INSIDE_ZONE:
- price has closed inside the level zone.
- this indicates interaction, not confirmed BREAK.

CLOSE_THROUGH_LEVEL and MULTIPLE_CLOSES_THROUGH_LEVEL:
- historical closes have occurred beyond the level.
- these do NOT automatically mean the current hypothesis is BREAK.
- if current price action is moving back toward the pre-break side or moving away from the level,
  prior closes through the level may instead describe a failed breakout or rejection.

MEANINGFUL_BREACH:
- price previously penetrated materially beyond the level.
- this is historical interaction evidence, not automatic proof that BREAK is currently favored.

MOVING_AWAY:
- recent net trajectory is away from the active level.

MOVING_AWAY_AFTER_INTERACTION:
- price interacted with the level and is now moving away from it.
- this may support HOLD or failed-break behavior.
- give this substantial weight when it conflicts with older breach evidence.

CURRENTLY_PRE_BREAK_SIDE:
- current price is on the non-break side of the active level.
- for SUPPORT, this means price is currently above the support zone.
- for RESISTANCE, this means price is currently below the resistance zone.
- do not interpret historical closes through the level as an active breakout if price has returned to the pre-break side.

When level_price_action numeric fields are available:
- approach_move_atr > 0 means net movement toward the active level.
- approach_move_atr < 0 means net movement away from the active level.
- zone_touches counts recent interactions with the zone.
- closes_through_level counts historical closes beyond the level.
- max_breach_atr measures the largest historical penetration beyond the level.
- recent evidence and current trajectory should generally be weighted more than older interaction counts.

Do not infer HOLD or BREAK from one price-action feature alone.
Evaluate the complete interaction sequence.

Technical evidence may disagree with level structure.
That conflict must be represented instead of ignored.


SCORING:

hold_score:
0.0 = no support for HOLD
1.0 = extremely strong support for HOLD

break_score:
0.0 = no support for BREAK
1.0 = extremely strong support for BREAK

Score HOLD and BREAK independently.

Do not force the two scores to sum to 1.0.


EVIDENCE:

hold_evidence_used must contain only evidence that
actually supports HOLD.

break_evidence_used must contain only evidence that
actually supports BREAK.

Do not put the same evidence ID into both lists.

Use ONLY evidence IDs explicitly available in the payload.

Do not invent indicators, news, macro events or evidence IDs.

If historical macro data is unavailable, macro must not
influence either hypothesis.


CONFIDENCE:

confidence measures how reliable you believe the
comparison itself is.

Low confidence is appropriate when evidence is sparse,
mixed or contradictory.

Return structured output only.
"""


        try:

            if active_level is None:

                assessment = (
                    LevelThesisAssessment(
                        thesis=
                            "UNCLEAR",

                        target_level=
                            "NONE",

                        operation=
                            "NONE",

                        confidence=
                            0.0,

                        evidence_used=
                            [],
                    )
                )

            else:

                comparison_payload = {
                    **payload,

                    "active_level":
                        active_level,
                }

                if (
                    context.analysis_time
                    in {
                        "2026-04-08T09:45:00+00:00",
                        "2026-04-08T15:00:00+00:00",
                    }
                ):

                    import pprint

                    print(
                        "\n"
                        + "=" * 100
                    )

                    print(
                        "PAYLOAD ENVIADO A QWEN"
                    )

                    print(
                        "=" * 100
                    )

                    pprint.pp(
                        comparison_payload,
                        width=120,
                    )


                comparison = (
                    structured_chat(
                        schema=
                            LevelHypothesisComparison,

                        system_prompt=
                            comparison_prompt,

                        payload=
                            comparison_payload,
                    )
                )


                comparison.hold_evidence_used = (
                    unique_list(
                        comparison.hold_evidence_used,
                        max_items=12,
                    )
                )


                comparison.break_evidence_used = (
                    unique_list(
                        comparison.break_evidence_used,
                        max_items=12,
                    )
                )


                evidence_used = (
                    unique_list(
                        (
                            comparison.hold_evidence_used
                            +
                            comparison.break_evidence_used
                        ),
                        max_items=16,
                    )
                )


                hold_score = float(
                    comparison.hold_score
                )

                break_score = float(
                    comparison.break_score
                )

                comparison_confidence = float(
                    comparison.confidence
                )

                print(
                    "\n[LEVEL HYPOTHESIS COMPARISON]"
                )

                print(
                    "Time:",
                    context.analysis_time,
                )

                print(
                    "Active level:",
                    active_level,
                )

                print(
                    "HOLD score:",
                    comparison.hold_score,
                )

                print(
                    "BREAK score:",
                    comparison.break_score,
                )

                print(
                    "Confidence:",
                    comparison.confidence,
                )

                print(
                    "HOLD evidence:",
                    comparison.hold_evidence_used,
                )

                print(
                    "BREAK evidence:",
                    comparison.break_evidence_used,
                )


                score_margin = abs(
                    hold_score
                    -
                    break_score
                )

                max_score = max(
                    hold_score,
                    break_score,
                )


                # Valores iniciales congelados.
                # No optimizarlos con este único día.
                minimum_score = 0.60
                minimum_margin = 0.15
                minimum_confidence = 0.60


                if (
                    max_score
                    < minimum_score
                    or
                    score_margin
                    < minimum_margin
                    or
                    comparison_confidence
                    < minimum_confidence
                ):

                    assessment = (
                        LevelThesisAssessment(
                            thesis=
                                "UNCLEAR",

                            target_level=
                                "NONE",

                            operation=
                                "NONE",

                            confidence=
                                0.0,

                            evidence_used=
                                evidence_used,
                        )
                    )

                else:

                    hypothesis = (
                        "HOLD"
                        if hold_score
                        >
                        break_score
                        else "BREAK"
                    )


                    if (
                        active_level
                        == "SUPPORT"
                    ):

                        if hypothesis == "HOLD":

                            thesis = (
                                "SUPPORT_HOLD"
                            )

                            operation = (
                                "BUY"
                            )

                        else:

                            thesis = (
                                "SUPPORT_BREAK"
                            )

                            operation = (
                                "SELL"
                            )

                    else:

                        if hypothesis == "HOLD":

                            thesis = (
                                "RESISTANCE_HOLD"
                            )

                            operation = (
                                "SELL"
                            )

                        else:

                            thesis = (
                                "RESISTANCE_BREAK"
                            )

                            operation = (
                                "BUY"
                            )


                    final_confidence = min(
                        max_score,
                        comparison_confidence,
                    )


                    assessment = (
                        LevelThesisAssessment(
                            thesis=
                                thesis,

                            target_level=
                                active_level,

                            operation=
                                operation,

                            confidence=
                                final_confidence,

                            evidence_used=
                                evidence_used,
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
        # LA TESIS DEBE REFERIRSE AL NIVEL QUE EL PRECIO
        # ESTA TESTEANDO AHORA
        # ==================================================

        market_structure = (
            level_evidence.get(
                "market_structure"
            )
        )

        if (
            assessment.thesis
            != "UNCLEAR"
        ):

            if (
                market_structure
                == "AT_SUPPORT"
                and
                assessment.target_level
                != "SUPPORT"
            ):

                errors.append(
                    "THESIS_NOT_AT_ACTIVE_LEVEL"
                )

            elif (
                market_structure
                == "AT_RESISTANCE"
                and
                assessment.target_level
                != "RESISTANCE"
            ):

                errors.append(
                    "THESIS_NOT_AT_ACTIVE_LEVEL"
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

        # if not historical_macro_available:

        #     forbidden_macro_reasons = {
        #         "MACRO_SUPPORTS_HOLD",
        #         "MACRO_SUPPORTS_BREAK",
        #     }

        #     if any(
        #         item
        #         in forbidden_macro_reasons

        #         # for item
        #         # in assessment.reason_codes
        #     ):

        #         errors.append(
        #             "MACRO_REASON_WITHOUT_HISTORICAL_DATA"
        #         )
        # # ==================================================
        # # COHERENCIA SEMANTICA DEL RAZONAMIENTO TECNICO
        # # ==================================================

        # bullish_technical = {
        #     item
        #     for item
        #     in assessment.evidence_used
        #     if (
        #         "_ABOVE_EMA" in item
        #         or
        #         "_STRUCTURE_BULLISH" in item
        #         or
        #         "_PARTIAL_BULLISH" in item
        #         or
        #         "_RSI_STRONG" in item
        #         or
        #         "_RETURN5_POSITIVE" in item
        #     )
        # }

        # bearish_technical = {
        #     item
        #     for item
        #     in assessment.evidence_used
        #     if (
        #         "_BELOW_EMA" in item
        #         or
        #         "_STRUCTURE_BEARISH" in item
        #         or
        #         "_PARTIAL_BEARISH" in item
        #         or
        #         "_RSI_WEAK" in item
        #         or
        #         "_RETURN5_NEGATIVE" in item
        #     )
        # }

        # bullish_thesis = (
        #     assessment.thesis
        #     in {
        #         "SUPPORT_HOLD",
        #         "RESISTANCE_BREAK",
        #     }
        # )

        # bearish_thesis = (
        #     assessment.thesis
        #     in {
        #         "RESISTANCE_HOLD",
        #         "SUPPORT_BREAK",
        #     }
        # )

        # if (
        #     "TREND_SUPPORTS_HOLD"
        #     in assessment.reason_codes
        #     or
        #     "TREND_SUPPORTS_BREAK"
        #     in assessment.reason_codes
        # ):

        #     if (
        #         bullish_thesis
        #         and
        #         not bullish_technical
        #     ):

        #         errors.append(
        #             "TECHNICAL_REASON_DIRECTION_MISMATCH"
        #         )

        #     if (
        #         bearish_thesis
        #         and
        #         not bearish_technical
        #     ):

        #         errors.append(
        #             "TECHNICAL_REASON_DIRECTION_MISMATCH"
        #         )

        # if (
        #     "MOMENTUM_SUPPORTS_HOLD"
        #     in assessment.reason_codes
        #     or
        #     "MOMENTUM_SUPPORTS_BREAK"
        #     in assessment.reason_codes
        # ):

        #     bullish_momentum = {
        #         item
        #         for item
        #         in assessment.evidence_used
        #         if (
        #             "_RSI_STRONG"
        #             in item
        #             or
        #             "_RETURN5_POSITIVE"
        #             in item
        #         )
        #     }

        #     bearish_momentum = {
        #         item
        #         for item
        #         in assessment.evidence_used
        #         if (
        #             "_RSI_WEAK"
        #             in item
        #             or
        #             "_RETURN5_NEGATIVE"
        #             in item
        #         )
        #     }

        #     if (
        #         bullish_thesis
        #         and
        #         not bullish_momentum
        #     ):

        #         errors.append(
        #             "MOMENTUM_REASON_DIRECTION_MISMATCH"
        #         )

        #     if (
        #         bearish_thesis
        #         and
        #         not bearish_momentum
        #     ):

        #         errors.append(
        #             "MOMENTUM_REASON_DIRECTION_MISMATCH"
        #         )

        if errors:

            print(
                "\n[LEVEL THESIS INVALID RAW]"
            )

            print(
                "Time:",
                context.analysis_time,
            )

            print(
                "Market structure:",
                level_evidence.get(
                    "market_structure"
                ),
            )

            print(
                "Raw assessment:",
                assessment.model_dump(),
            )

            print(
                "Validation errors:",
                list(
                    dict.fromkeys(
                        errors
                    )
                ),
            )

            return self._invalid(
                bus,
                list(
                    dict.fromkeys(
                        errors
                    )
                ),
            )
        # ==================================================
        # CLASIFICACION DETERMINISTA DE LA EVIDENCIA
        # ==================================================

        bullish_thesis = (
            assessment.thesis
            in {
                "SUPPORT_HOLD",
                "RESISTANCE_BREAK",
            }
        )

        bearish_thesis = (
            assessment.thesis
            in {
                "RESISTANCE_HOLD",
                "SUPPORT_BREAK",
            }
        )

        bullish_technical = [
            item
            for item
            in assessment.evidence_used
            if (
                "_ABOVE_EMA" in item
                or
                "_STRUCTURE_BULLISH" in item
                or
                "_PARTIAL_BULLISH" in item
                or
                "_RSI_STRONG" in item
                or
                "_RETURN5_POSITIVE" in item
            )
        ]

        bearish_technical = [
            item
            for item
            in assessment.evidence_used
            if (
                "_BELOW_EMA" in item
                or
                "_STRUCTURE_BEARISH" in item
                or
                "_PARTIAL_BEARISH" in item
                or
                "_RSI_WEAK" in item
                or
                "_RETURN5_NEGATIVE" in item
            )
        ]

        reason_codes = []

        if assessment.thesis == "UNCLEAR":

            reason_codes.append(
                "INSUFFICIENT_EVIDENCE"
            )

        else:

            reason_codes.append(
                "LEVEL_STRUCTURE_SUPPORTS_THESIS"
            )

            if bullish_thesis:

                if bullish_technical:

                    reason_codes.append(
                        "TECHNICAL_SUPPORTS_THESIS"
                    )

                if bearish_technical:

                    reason_codes.append(
                        "TECHNICAL_CONFLICTS_WITH_THESIS"
                    )

            elif bearish_thesis:

                if bearish_technical:

                    reason_codes.append(
                        "TECHNICAL_SUPPORTS_THESIS"
                    )

                if bullish_technical:

                    reason_codes.append(
                        "TECHNICAL_CONFLICTS_WITH_THESIS"
                    )

        return bus.publish(
            agent=
                self.name,

            message_type=
                "LEVEL_THESIS",

            payload={
                **assessment.model_dump(),

                "reason_codes":
                    reason_codes,

                "valid":
                    True,

                "validation_errors":
                    [],

                "source":
                    "LLM",
            },
        )


class SupportLevelThesisAssessment(
    LevelThesisAssessment
):

    thesis: Literal[
        "SUPPORT_HOLD",
        "SUPPORT_BREAK",
        "UNCLEAR",
    ]

    target_level: Literal[
        "SUPPORT",
        "NONE",
    ]


class ResistanceLevelThesisAssessment(
    LevelThesisAssessment
):

    thesis: Literal[
        "RESISTANCE_HOLD",
        "RESISTANCE_BREAK",
        "UNCLEAR",
    ]

    target_level: Literal[
        "RESISTANCE",
        "NONE",
    ]



