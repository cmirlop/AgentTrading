import json
from datetime import datetime, timezone
from pathlib import Path

from src.agents.market_analyst import (
    MarketAnalystAgent,
)


BASE_DIR = Path(__file__).resolve().parent

STATE_DIR = (
    BASE_DIR
    / "data"
    / "state"
)

ANALYSIS_DIR = (
    BASE_DIR
    / "data"
    / "analysis"
)

SYMBOLS = [
    "EURUSD",
    "USDCAD",
    "AUDUSD",
]


def load_state(symbol):

    path = (
        STATE_DIR
        / f"{symbol}.json"
    )

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:

        return json.load(file)


def save_analysis(
    symbol,
    analysis,
    metrics,
):

    now = datetime.now(
        timezone.utc
    )

    day = now.strftime(
        "%Y-%m-%d"
    )

    directory = (
        ANALYSIS_DIR
        / day
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = now.strftime(
        "%H-%M-%S"
    )

    path = (
        directory
        / f"{timestamp}_{symbol}.json"
    )

    content = {
        "generated_at":
            now.isoformat(),

        "symbol":
            symbol,

        "analysis":
            analysis.model_dump(),

        "performance":
            metrics,
    }

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            content,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return path


def print_analysis(
    analysis,
    metrics,
):

    print()
    print("=" * 80)
    print(
        analysis.symbol
    )
    print("=" * 80)

    print(
        "Decision:",
        analysis.decision,
    )

    print(
        "Estrategia:",
        analysis.strategy,
    )

    print(
        "Score global:",
        analysis.overall_score,
    )

    print(
        "Scores TF:",
        analysis.timeframe_scores,
    )

    print(
        "Bias:",
        analysis.bias,
    )

    print(
        "Regimen:",
        analysis.market_regime,
    )

    print(
        "Confianza decision:",
        round(
            analysis.decision_confidence,
            2,
        ),
    )

    print(
        "Validacion LLM:",
        (
            "OK"
            if analysis.validation_ok
            else "FAIL"
        ),
    )

    if analysis.validation_errors:

        for error in (
            analysis.validation_errors
        ):

            print(
                " ERROR:",
                error,
            )

    print(
        "Noticias necesarias:",
        analysis.news_check_required,
    )

    print(
        "Setup quality:",
        analysis.setup_quality,
        "/ 100",
    )

    print(
        "Alineacion HTF:",
        analysis.higher_timeframe_alignment,
    )

    print()
    print(
        "Resumen:",
        analysis.summary,
    )

    print()
    print(
        "Caution flags:",
        analysis.caution_flags,
    )

    print()
    print("Evidencia a favor:")

    for factor in (
        analysis.supporting_evidence
    ):

        print(
            " +",
            factor,
        )

    print()
    print("Evidencia en conflicto:")

    for factor in (
        analysis.conflicting_evidence
    ):

        print(
            " -",
            factor,
        )

    print()
    print(
        "Tiempo:",
        round(
            metrics[
                "elapsed_seconds"
            ],
            2,
        ),
        "segundos",
    )

    if (
        "tokens_per_second"
        in metrics
    ):

        print(
            "Velocidad:",
            round(
                metrics[
                    "tokens_per_second"
                ],
                2,
            ),
            "tokens/s",
        )


def main():

    print("=" * 80)
    print(
        "TRADING AGENT - "
        "MARKET ANALYST"
    )
    print("=" * 80)

    agent = (
        MarketAnalystAgent()
    )

    for symbol in SYMBOLS:

        print()
        print(
            f"Analizando {symbol}..."
        )

        state = load_state(
            symbol
        )

        analysis, metrics = (
            agent.analyze(
                state
            )
        )

        path = save_analysis(
            symbol,
            analysis,
            metrics,
        )

        print_analysis(
            analysis,
            metrics,
        )

        print(
            "\nGuardado:",
            path,
        )


if __name__ == "__main__":
    main()