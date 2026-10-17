"""Prueba determinista de coherencia direccional en LevelThesisAgent.

Ejecutar desde la raiz de AgentTrading:
    python test_level_thesis_direction.py

No llama a Ollama ni envia ordenes.
"""

from types import SimpleNamespace
from unittest.mock import patch

from src.agents.level_thesis_agent import LevelHypothesisComparison, LevelThesisAgent


TECHNICAL_IDS = ["M5_RETURN5_POSITIVE", "M5_RETURN5_NEGATIVE"]
LEVEL_IDS = {
    "SUPPORT": ["SUPPORT_MEDIUM", "M5_LEVEL_REPEATED_TESTS"],
    "RESISTANCE": ["RESISTANCE_MEDIUM", "M5_LEVEL_REPEATED_TESTS"],
}


class FakeBus:
    def __init__(self, level):
        facts = {
            level.lower(): {
                "strength": 60,
                "touches": 2,
                "distance_atr": 0.2,
                "timeframes": ["H1"],
            }
        }
        self.messages = {
            "TECHNICAL_ASSESSMENT": {
                "bias": "MIXED",
                "timeframe_scores": {"M5": 0, "M15": 0},
            },
            "REGIME_ASSESSMENT": {"valid": True, "regime": "RANGE"},
            "MACRO_ASSESSMENT": {"historical_data_available": False},
            "LEVEL_EVIDENCE": {
                "status": "OK",
                "market_structure": f"AT_{level}",
                "facts": facts,
                "price_action": {"M5": {}, "M15": {}},
                "available_evidence_ids": LEVEL_IDS[level],
            },
        }

    def require(self, message_type):
        return {"payload": self.messages[message_type]}

    def publish(self, *, agent, message_type, payload):
        self.messages[message_type] = payload
        return payload


def check(level, hypothesis, technical_id, should_pass):
    level_ids = LEVEL_IDS[level]
    winner_ids = level_ids + [technical_id]
    loser_ids = ["M5_RETURN5_NEGATIVE" if technical_id.endswith("POSITIVE") else "M5_RETURN5_POSITIVE"]

    comparison = LevelHypothesisComparison(
        hold_score=0.8 if hypothesis == "HOLD" else 0.2,
        break_score=0.2 if hypothesis == "HOLD" else 0.8,
        confidence=0.8,
        hold_evidence_used=winner_ids if hypothesis == "HOLD" else loser_ids,
        break_evidence_used=winner_ids if hypothesis == "BREAK" else loser_ids,
    )
    context = SimpleNamespace(
        symbol="USDCAD",
        analysis_time="TEST_DIRECTION",
        snapshot={"available_evidence_ids": TECHNICAL_IDS},
    )
    bus = FakeBus(level)
    with patch(
        "src.agents.level_thesis_agent.structured_chat",
        return_value=comparison,
    ):
        result = LevelThesisAgent().run(context, bus)

    case = f"{level}_{hypothesis} / {technical_id}"
    if should_pass:
        assert result["valid"] is True, (case, result)
        assert result["thesis"] == f"{level}_{hypothesis}", (case, result)
        assert result["operation"] != "NONE", (case, result)
        assert result["evidence_used"] == winner_ids, (
            case,
            "Las evidencias ganadoras se mezclaron con las perdedoras",
            result,
        )
        print(f"OK ACEPTADA: {case}")
    else:
        assert result["valid"] is False, (case, result)
        assert "THESIS_WITHOUT_TECHNICAL_EVIDENCE" in result["validation_errors"], (
            case,
            result,
        )
        assert result["operation"] == "NONE", (case, result)
        print(f"OK RECHAZADA: {case}")


for level, hypothesis, favorable, contrary in (
    ("SUPPORT", "HOLD", "M5_RETURN5_POSITIVE", "M5_RETURN5_NEGATIVE"),
    ("SUPPORT", "BREAK", "M5_RETURN5_NEGATIVE", "M5_RETURN5_POSITIVE"),
    ("RESISTANCE", "HOLD", "M5_RETURN5_NEGATIVE", "M5_RETURN5_POSITIVE"),
    ("RESISTANCE", "BREAK", "M5_RETURN5_POSITIVE", "M5_RETURN5_NEGATIVE"),
):
    check(level, hypothesis, favorable, should_pass=True)
    check(level, hypothesis, contrary, should_pass=False)

print("OK: las 8 comprobaciones direccionales han superado la prueba.")
