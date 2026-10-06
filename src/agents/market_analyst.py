import json
import time
from typing import Literal

from ollama import chat
from pydantic import BaseModel, Field

from src.market_snapshot import (
    build_compact_snapshot,
)

from src.technical_evaluator import (
    evaluate_technical_state,
)


MODEL = "qwen3:4b"


# ==========================================================
# RESPUESTA LLM
# ==========================================================

class StrategyAssessment(
    BaseModel
):

    strategy: Literal[
        "TREND_CONTINUATION",
        "PULLBACK",
        "BREAKOUT",
        "REVERSAL",
        "RANGE_FADE",
        "NONE",
    ]

    market_regime: Literal[
        "TREND",
        "RANGE",
        "BREAKOUT",
        "REVERSAL",
        "UNCLEAR",
    ]

    evidence_used: list[str]

    caution_flags: list[
        Literal[
            "OVEREXTENDED",
            "LOW_VOLUME",
            "HIGH_VOLATILITY",
            "LOW_VOLATILITY",
            "MIXED_TIMEFRAMES",
            "WEAK_MOMENTUM",
            "NONE",
        ]
    ]

    summary: str = Field(
        max_length=500,
    )


# ==========================================================
# RESULTADO FINAL
# ==========================================================

class MarketAnalysis(
    BaseModel
):

    symbol: str

    decision: Literal[
        "NO_TRADE",
        "WATCH_LONG",
        "WATCH_SHORT",
    ]

    strategy: str

    market_regime: str

    bias: str

    overall_score: float

    timeframe_scores: dict[
        str,
        float,
    ]

    higher_timeframe_alignment: str

    setup_quality: int

    decision_confidence: float

    supporting_evidence: list[str]

    conflicting_evidence: list[str]

    evidence_used: list[str]

    caution_flags: list[str]

    summary: str

    validation_ok: bool

    validation_errors: list[str]

    news_check_required: bool


class MarketAnalystAgent:

    def __init__(
        self,
        model=MODEL,
    ):

        self.model = model

    def analyze(
        self,
        market_state,
    ):

        snapshot = (
            build_compact_snapshot(
                market_state,
                candles_per_timeframe=4,
            )
        )

        technical = (
            evaluate_technical_state(
                snapshot
            )
        )

        allowed_evidence = set(
            snapshot[
                "available_evidence_ids"
            ]
        )

        model_input = {
            "symbol":
                snapshot["symbol"],

            "analysis_time":
                snapshot[
                    "analysis_time"
                ],

            "technical_evaluation":
                technical,

            "timeframes":
                snapshot[
                    "timeframes"
                ],

            "available_evidence_ids":
                snapshot[
                    "available_evidence_ids"
                ],
        }

        system_prompt = """
Eres especialista en patrones y estrategias
de trading Forex.

Python YA ha calculado:

- dirección técnica
- score de cada timeframe
- alineación temporal
- setup_quality
- evidencias alcistas/bajistas

NO debes recalcular esos valores.

Tu única función es clasificar qué estrategia
describe mejor la situación.

Estrategias permitidas:

TREND_CONTINUATION
PULLBACK
BREAKOUT
REVERSAL
RANGE_FADE
NONE

La temporalidad principal es M15.

NO inventes noticias.
NO inventes gaps.
NO inventes indicadores.

evidence_used SOLO puede contener identificadores
exactos presentes en available_evidence_ids.

Si no hay una configuración clara:
strategy = NONE.

Devuelve exclusivamente el JSON requerido.
"""

        user_prompt = (
            "Analiza este estado:\n\n"
            +
            json.dumps(
                model_input,
                indent=2,
                ensure_ascii=False,
            )
        )

        start = (
            time.perf_counter()
        )

        response = chat(
            model=self.model,

            messages=[
                {
                    "role":
                        "system",

                    "content":
                        system_prompt,
                },
                {
                    "role":
                        "user",

                    "content":
                        user_prompt,
                },
            ],

            format=(
                StrategyAssessment
                .model_json_schema()
            ),

            options={
                "temperature": 0,
                "num_ctx": 4096,
            },

            think=False,
        )

        elapsed = (
            time.perf_counter()
            -
            start
        )

        assessment = (
            StrategyAssessment
            .model_validate_json(
                response.message.content
            )
        )

        # ==================================================
        # VALIDACION
        # ==================================================

        validation_errors = []

        unknown_evidence = [
            evidence

            for evidence
            in assessment.evidence_used

            if evidence
            not in allowed_evidence
        ]

        if unknown_evidence:

            validation_errors.append(
                "UNKNOWN_EVIDENCE: "
                +
                ", ".join(
                    unknown_evidence
                )
            )

        if (
            market_state[
                "data_quality"
            ]["status"]
            != "OK"
        ):

            validation_errors.append(
                "MARKET_DATA_NOT_OK"
            )

        validation_ok = (
            len(
                validation_errors
            )
            == 0
        )

        # ==================================================
        # DECISION FINAL
        # ==================================================

        decision = "NO_TRADE"

        eligible = (
            validation_ok
            and
            technical[
                "setup_quality"
            ] >= 65
            and
            technical[
                "alignment"
            ] != "CONFLICT"
            and
            technical[
                "bias"
            ] != "NEUTRAL"
            and
            assessment.strategy
            != "NONE"
        )

        if eligible:

            if (
                technical["bias"]
                == "BULLISH"
            ):

                decision = (
                    "WATCH_LONG"
                )

            elif (
                technical["bias"]
                == "BEARISH"
            ):

                decision = (
                    "WATCH_SHORT"
                )

        # No es probabilidad de ganar.
        decision_confidence = (
            technical[
                "setup_quality"
            ]
            / 100
        )

        result = (
            MarketAnalysis(
                symbol=(
                    market_state[
                        "symbol"
                    ]
                ),

                decision=
                    decision,

                strategy=
                    assessment.strategy,

                market_regime=(
                    assessment
                    .market_regime
                ),

                bias=(
                    technical[
                        "bias"
                    ]
                ),

                overall_score=(
                    technical[
                        "overall_score"
                    ]
                ),

                timeframe_scores=(
                    technical[
                        "timeframe_scores"
                    ]
                ),

                higher_timeframe_alignment=(
                    technical[
                        "alignment"
                    ]
                ),

                setup_quality=(
                    technical[
                        "setup_quality"
                    ]
                ),

                decision_confidence=(
                    decision_confidence
                ),

                supporting_evidence=(
                    technical[
                        "supporting_evidence"
                    ]
                ),

                conflicting_evidence=(
                    technical[
                        "conflicting_evidence"
                    ]
                ),

                evidence_used=(
                    assessment
                    .evidence_used
                ),

                caution_flags=(
                    assessment
                    .caution_flags
                ),

                summary=(
                    assessment.summary
                ),

                validation_ok=(
                    validation_ok
                ),

                validation_errors=(
                    validation_errors
                ),

                news_check_required=(
                    decision
                    != "NO_TRADE"
                ),
            )
        )

        metrics = {
            "elapsed_seconds":
                elapsed,

            "prompt_eval_count":
                getattr(
                    response,
                    "prompt_eval_count",
                    None,
                ),

            "eval_count":
                getattr(
                    response,
                    "eval_count",
                    None,
                ),

            "eval_duration":
                getattr(
                    response,
                    "eval_duration",
                    None,
                ),
        }

        if (
            metrics[
                "eval_count"
            ]
            and
            metrics[
                "eval_duration"
            ]
        ):

            seconds = (
                metrics[
                    "eval_duration"
                ]
                / 1_000_000_000
            )

            if seconds > 0:

                metrics[
                    "tokens_per_second"
                ] = (
                    metrics[
                        "eval_count"
                    ]
                    / seconds
                )

        return (
            result,
            metrics,
        )