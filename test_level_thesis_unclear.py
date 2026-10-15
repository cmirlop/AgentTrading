"""Prueba aislada: UNCLEAR valido no es error de validacion; sin Ollama."""

from types import SimpleNamespace
from unittest.mock import patch

from src.agents.level_thesis_agent import LevelThesisAgent, LevelHypothesisComparison


class FakeBus:
    def __init__(self):
        self.messages = {
            "TECHNICAL_ASSESSMENT": {
                "bias": "BEARISH",
                "timeframe_scores": {"M5": -80, "M15": -70},
            },
            "REGIME_ASSESSMENT": {"valid": True, "regime": "TREND"},
            "MACRO_ASSESSMENT": {"historical_data_available": False},
            "LEVEL_EVIDENCE": {
                "status": "OK",
                "market_structure": "AT_SUPPORT",
                "facts": {
                    "support": {
                        "strength": 60,
                        "touches": 2,
                        "distance_atr": 0.2,
                        "timeframes": ["H1"],
                    }
                },
                "price_action": {"M5": {}, "M15": {}},
                "available_evidence_ids": ["SUPPORT_MEDIUM"],
            },
        }

    def require(self, message_type):
        return {"payload": self.messages[message_type]}

    def publish(self, *, agent, message_type, payload):
        self.messages[message_type] = payload
        return payload


bus = FakeBus()
context = SimpleNamespace(
    symbol="USDCAD",
    analysis_time="TEST_UNCLEAR",
    snapshot={"available_evidence_ids": ["M5_RETURN5_NEGATIVE"]},
)

comparison = LevelHypothesisComparison(
    hold_score=0.7,
    break_score=0.3,
    confidence=0.3,  # Bajo el umbral: debe abstenerse.
    hold_evidence_used=["SUPPORT_MEDIUM"],
    break_evidence_used=["M5_RETURN5_NEGATIVE"],
)

with patch(
    "src.agents.level_thesis_agent.structured_chat",
    return_value=comparison,
):
    result = LevelThesisAgent().run(context, bus)

assert result["thesis"] == "UNCLEAR", result
assert result["valid"] is True, result
assert result["operation"] == "NONE", result
assert result["target_level"] == "NONE", result
assert result["evidence_used"] == [], result
assert result["validation_errors"] == [], result

print("OK: UNCLEAR es una abstencion valida, sin operacion ni evidencias de tesis.")
