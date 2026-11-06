import time
from typing import Literal

from pydantic import BaseModel, Field

from src.agents.llm_client import (
    LLMProviderError,
    get_llm_configuration,
    structured_chat,
)

from src.market_snapshot import (
    build_compact_snapshot,
)

from src.technical_evaluator import (
    evaluate_technical_state,
)
from src.research_policy import MARKET_ANALYST_FILTER_PROMPT


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

    llm_provider: str

    llm_model: str

    llm_timeout_seconds: float


class MarketAnalystAgent:

    def __init__(
        self,
        model=None,
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

        system_prompt = MARKET_ANALYST_FILTER_PROMPT

        start = (
            time.perf_counter()
        )

        try:
            llm_config = get_llm_configuration(StrategyAssessment)
            assessment = structured_chat(
                StrategyAssessment,
                system_prompt,
                model_input,
                model_override=self.model,
            )
            llm_error = None
        except Exception as exc:
            elapsed = time.perf_counter() - start
            try:
                llm_config = get_llm_configuration(StrategyAssessment)
            except Exception:
                llm_config = None
            llm_error = (
                exc.code
                if isinstance(exc, LLMProviderError)
                else "LLM_CALL_FAILED"
            )
            result = MarketAnalysis(
                symbol=market_state["symbol"],
                decision="NO_TRADE",
                strategy="NONE",
                market_regime="UNCLEAR",
                bias=technical["bias"],
                overall_score=technical["overall_score"],
                timeframe_scores=technical["timeframe_scores"],
                higher_timeframe_alignment=technical["alignment"],
                setup_quality=technical["setup_quality"],
                decision_confidence=0.0,
                supporting_evidence=technical["supporting_evidence"],
                conflicting_evidence=technical["conflicting_evidence"],
                evidence_used=[],
                caution_flags=[],
                summary="LLM provider failed; no trade decision was produced.",
                validation_ok=False,
                validation_errors=[llm_error],
                news_check_required=False,
                llm_provider=(llm_config.provider if llm_config else "unavailable"),
                llm_model=(llm_config.model if llm_config else "unavailable"),
                llm_timeout_seconds=(llm_config.timeout_seconds if llm_config else 0.0),
            )
            return result, {
                "elapsed_seconds": elapsed,
                "provider": result.llm_provider,
                "model": result.llm_model,
                "timeout_seconds": result.llm_timeout_seconds,
                "max_retries": 0,
                "error": llm_error,
            }

        elapsed = time.perf_counter() - start

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

                caution_flags=list(
                    dict.fromkeys(
                        assessment.caution_flags
                    )
                ),

                summary=(
                    f"Bias tecnico={technical['bias']}; "
                    f"score={technical['overall_score']}; "
                    f"alineacion={technical['alignment']}; "
                    f"setup_quality={technical['setup_quality']}; "
                    f"estrategia propuesta={assessment.strategy}; "
                    f"regimen propuesto={assessment.market_regime}."
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

                llm_provider=llm_config.provider,

                llm_model=llm_config.model,

                llm_timeout_seconds=llm_config.timeout_seconds,
            )
        )

        metrics = {
            "elapsed_seconds":
                elapsed,

            "provider":
                llm_config.provider,

            "model":
                llm_config.model,

            "timeout_seconds":
                llm_config.timeout_seconds,

            "max_retries":
                0,
        }

        return (
            result,
            metrics,
        )
