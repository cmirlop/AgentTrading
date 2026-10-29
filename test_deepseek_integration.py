"""Prueba SIN RED del cliente DeepSeek y su ruta opcional.

Ejecutar desde la raíz de AgentTrading:
    python test_deepseek_integration.py
"""
import json
import os
import importlib
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel, Field

from src.agents.llm_client import structured_chat


class LevelHypothesisComparison(BaseModel):
    hold_score: float = Field(ge=0, le=1)
    break_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    hold_evidence_used: list[str]
    break_evidence_used: list[str]


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        assert kwargs["base_url"] == "https://api.deepseek.com"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        FakeClient.calls.append(kwargs)
        assert kwargs["model"] == "deepseek-flash"
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["max_tokens"] == 8192
        assert "M5_RETURN5_NEGATIVE" in kwargs["messages"][0]["content"]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=json.dumps({
                    "hold_score": 0.2,
                    "break_score": 0.8,
                    "confidence": 0.7,
                    "hold_evidence_used": ["SUPPORT_MEDIUM"],
                    "break_evidence_used": ["M5_RETURN5_NEGATIVE"],
                }))
            )],
            usage=SimpleNamespace(
                prompt_tokens=650,
                completion_tokens=120,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=80),
            ),
        )


if __name__ == "__main__":
    original = {key: os.environ.get(key) for key in (
        "DEEPSEEK_API_KEY", "LLM_PROVIDER", "DEEPSEEK_REASONING_EFFORT",
        "DEEPSEEK_MAX_TOKENS", "DEEPSEEK_MODEL",
    )}
    try:
        os.environ.update({
            "DEEPSEEK_API_KEY": "testing-only-not-a-real-key",
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_REASONING_EFFORT": "low",
            "DEEPSEEK_MODEL": "deepseek-flash",
        })
        os.environ.pop("DEEPSEEK_MAX_TOKENS", None)
        deepseek_module = importlib.import_module("src.agents.deepseek_client")
        with patch.object(deepseek_module, "OpenAI", FakeClient):
            result = structured_chat(
                LevelHypothesisComparison,
                "Compare a support level. Return JSON.",
                {"allowed_evidence_ids": [
                    "SUPPORT_MEDIUM", "M5_RETURN5_NEGATIVE",
                ]},
            )
        assert result.break_score == 0.8
        assert result.break_evidence_used == ["M5_RETURN5_NEGATIVE"]
        assert len(FakeClient.calls) == 1
        print("OK: conmutador y DeepSeek Flash validados sin red ni coste API")
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
