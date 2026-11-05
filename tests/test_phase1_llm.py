import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents import llm_client
from src.agents.llm_client import LLMProviderError, structured_chat


class Answer(BaseModel):
    decision: str
    evidence_used: list[str]


def _response(content, done_reason="stop"):
    return SimpleNamespace(
        message=SimpleNamespace(content=content),
        done_reason=done_reason,
    )


class FakeOllama:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    def chat(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


class LLMProviderTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"available_evidence_ids": ["EVIDENCE_A"]}
        self.valid_json = json.dumps({"decision": "WAIT", "evidence_used": ["EVIDENCE_A"]})

    def test_ollama_selected_and_schema_constrains_evidence(self):
        client = FakeOllama(_response(self.valid_json))
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "unit-qwen"}):
            with patch.object(llm_client, "_ollama_client", return_value=client):
                result = structured_chat(Answer, "system", self.payload)
        self.assertEqual(result.decision, "WAIT")
        self.assertEqual(client.kwargs["model"], "unit-qwen")
        self.assertEqual(
            client.kwargs["format"]["properties"]["evidence_used"]["items"]["enum"],
            ["EVIDENCE_A"],
        )

    def test_deepseek_selected_with_single_no_retry_request(self):
        expected = Answer(decision="WAIT", evidence_used=["EVIDENCE_A"])
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "unit-test-only",
            "DEEPSEEK_MODEL": "deepseek-unit",
            "DEEPSEEK_REASONING_EFFORT": "high",
        }):
            with patch("src.agents.deepseek_client.deepseek_structured_chat", return_value=expected) as call:
                result = structured_chat(Answer, "system", self.payload)
        self.assertEqual(result, expected)
        self.assertEqual(call.call_args.kwargs["model"], "deepseek-unit")
        self.assertEqual(call.call_args.kwargs["max_retries"] if "max_retries" in call.call_args.kwargs else 0, 0)

    def test_unknown_provider_fails_with_safe_code(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "unknown-provider"}):
            with self.assertRaises(LLMProviderError) as raised:
                structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_PROVIDER_UNSUPPORTED")

    def test_non_finite_timeout_is_rejected_as_configuration_error(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "OLLAMA_TIMEOUT_SECONDS": "nan"}):
            with self.assertRaises(LLMProviderError) as raised:
                structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_CONFIGURATION_INVALID")

    def test_missing_api_key_fails_without_constructing_client(self):
        env = {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": ""}
        with patch.dict(os.environ, env):
            from src.agents.deepseek_client import deepseek_structured_chat
            with self.assertRaises(RuntimeError) as raised:
                deepseek_structured_chat(Answer, "system", self.payload)
        self.assertEqual(str(raised.exception), "LLM_API_KEY_MISSING")

    def test_missing_deepseek_sdk_fails_closed(self):
        with patch.dict(os.environ, {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "unit-test-only",
        }):
            with patch.object(llm_client.deepseek_client, "OpenAI", None):
                with self.assertRaises(LLMProviderError) as raised:
                    structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_PROVIDER_DEPENDENCY_MISSING")

    def test_invalid_json_is_rejected(self):
        client = FakeOllama(_response("not-json"))
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama"}):
            with patch.object(llm_client, "_ollama_client", return_value=client):
                with self.assertRaises(LLMProviderError) as raised:
                    structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_INVALID_RESPONSE")

    def test_ollama_truncated_dict_response_is_rejected(self):
        client = FakeOllama({"message": {"content": self.valid_json}, "done_reason": "length"})
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama"}):
            with patch.object(llm_client, "_ollama_client", return_value=client):
                with self.assertRaises(LLMProviderError) as raised:
                    structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_INCOMPLETE_RESPONSE")

    def test_invented_evidence_is_rejected(self):
        client = FakeOllama(_response(json.dumps({"decision": "WAIT", "evidence_used": ["INVENTED"]})))
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama"}):
            with patch.object(llm_client, "_ollama_client", return_value=client):
                with self.assertRaises(LLMProviderError) as raised:
                    structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_INVALID_EVIDENCE")

    def test_ollama_timeout_is_controlled(self):
        client = FakeOllama(error=TimeoutError("do not echo transport details"))
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama"}):
            with patch.object(llm_client, "_ollama_client", return_value=client):
                with self.assertRaises(LLMProviderError) as raised:
                    structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_TIMEOUT")

    def test_deepseek_incomplete_response_is_rejected(self):
        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=self.valid_json),
                )])

            valid_json = json.dumps({"decision": "WAIT", "evidence_used": ["EVIDENCE_A"]})

        with patch.dict(os.environ, {
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "unit-test-only",
        }):
            with patch("src.agents.deepseek_client.OpenAI", FakeOpenAI):
                with self.assertRaises(LLMProviderError) as raised:
                    structured_chat(Answer, "system", self.payload)
        self.assertEqual(raised.exception.code, "LLM_INCOMPLETE_RESPONSE")

    def test_explicit_session_environment_wins_over_dotenv(self):
        from dotenv import load_dotenv

        old = os.environ.get("LLM_PROVIDER")
        try:
            os.environ["LLM_PROVIDER"] = "deepseek"
            with tempfile.TemporaryDirectory() as directory:
                env_file = Path(directory) / ".env"
                env_file.write_text("LLM_PROVIDER=ollama\n", encoding="utf-8")
                load_dotenv(env_file, override=False)
            self.assertEqual(os.environ["LLM_PROVIDER"], "deepseek")
        finally:
            if old is None:
                os.environ.pop("LLM_PROVIDER", None)
            else:
                os.environ["LLM_PROVIDER"] = old

    def test_market_analyst_provider_failure_returns_no_trade(self):
        from src.agents.market_analyst import MarketAnalystAgent

        snapshot = {
            "symbol": "EURUSD",
            "analysis_time": "2026-01-01T00:15:00+00:00",
            "available_evidence_ids": ["EVIDENCE_A"],
            "timeframes": {},
        }
        technical = {
            "bias": "BULLISH",
            "overall_score": 42.0,
            "timeframe_scores": {"M15": 42.0},
            "alignment": "ALIGNED",
            "setup_quality": 80,
            "supporting_evidence": ["EVIDENCE_A"],
            "conflicting_evidence": [],
        }
        with patch("src.agents.market_analyst.build_compact_snapshot", return_value=snapshot):
            with patch("src.agents.market_analyst.evaluate_technical_state", return_value=technical):
                with patch("src.agents.market_analyst.structured_chat", side_effect=LLMProviderError("LLM_TIMEOUT")):
                    result, metrics = MarketAnalystAgent().analyze({"symbol": "EURUSD"})
        self.assertEqual(result.decision, "NO_TRADE")
        self.assertFalse(result.validation_ok)
        self.assertEqual(result.validation_errors, ["LLM_TIMEOUT"])
        self.assertEqual(metrics["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()
