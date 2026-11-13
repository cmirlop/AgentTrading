import os
import sys
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_llm_live import (
    ProbeAnswer,
    _deepseek_preflight,
    _invalid_response_guard_check,
    _structured_chat_with_temporary_ledger,
    validate,
)
from src.agents.deepseek_client import _api_failure_code
from src.agents.llm_client import LLMProviderError


class LiveProviderValidatorOfflineTests(unittest.TestCase):
    def test_ollama_http_500_acl_and_host_failures_are_safe_pending(self):
        class OllamaHTTP500(Exception):
            status_code = 500

        cases = (
            OllamaHTTP500("manifest access denied at C:\\private\\models\\manifests\\qwen3"),
            PermissionError("access denied at C:\\private\\models\\manifests\\qwen3"),
            ConnectionError("cannot connect to http://private-host:11434/api/tags?token=secret"),
        )
        configuration = SimpleNamespace(
            model="qwen3:4b",
            timeout_seconds=5,
            public_dict=lambda: {"provider": "ollama", "model": "qwen3:4b"},
        )

        for error in cases:
            with self.subTest(error=type(error).__name__):
                class BrokenClient:
                    def list(self):
                        raise error

                with patch.dict(os.environ, {"OLLAMA_MODEL": "qwen3:4b", "OLLAMA_HOST": "http://127.0.0.1:11434"}):
                    with patch("src.agents.llm_client.get_llm_configuration", return_value=configuration):
                        with patch("src.agents.llm_client._ollama_client", return_value=BrokenClient()):
                            report = validate("ollama")
                serialized = json.dumps(report, ensure_ascii=False)
                self.assertEqual(report["status"], "PENDING")
                self.assertEqual(report["calls_made"], 0)
                self.assertFalse(report["operational_signal_created"])
                self.assertFalse(report["market_order_sent"])
                self.assertNotIn(str(error), serialized)
                self.assertNotIn("private-host", serialized)
                self.assertNotIn("secret", serialized)

    def test_api_errors_are_classified_without_provider_body(self):
        class AuthenticationError(Exception):
            status_code = 401

        class APIConnectionError(Exception):
            pass

        class RateLimitError(Exception):
            status_code = 429

        self.assertEqual(_api_failure_code(AuthenticationError("private body")), "LLM_AUTHENTICATION_REJECTED")
        self.assertEqual(_api_failure_code(APIConnectionError("private body")), "LLM_PROVIDER_CONNECTION_FAILED")
        self.assertEqual(_api_failure_code(RateLimitError("private body")), "LLM_PROVIDER_RATE_LIMITED")

    def test_invalid_probe_schema_and_evidence_are_rejected_without_signal(self):
        result = _invalid_response_guard_check()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["invalid_schema_rejected"])
        self.assertTrue(result["invalid_evidence_rejected"])
        self.assertFalse(result["operational_signal_created"])

    def test_deepseek_is_pending_without_explicit_authorization_and_makes_no_call(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-test-only"}):
            report = validate("deepseek", authorize_deepseek=False)
        self.assertEqual(report["status"], "PENDING")
        self.assertEqual(report["calls_made"], 0)
        self.assertFalse(report["api_key_logged"])
        self.assertFalse(report["market_order_sent"])

    def test_explicitly_authorized_deepseek_runs_one_validated_request(self):
        answer = ProbeAnswer(status="PASS", evidence_used=["LIVE_PROBE"])
        metadata = {
            "provider": "deepseek",
            "configured_model": "deepseek-flash",
            "provider_reported_model": "deepseek-flash",
            "finish_reason": "stop",
            "latency_seconds": 0.1,
            "prompt_tokens": 10,
            "completion_tokens": 4,
        }

        def one_request(*args, **kwargs):
            callback = kwargs.get("on_request_start")
            if callback:
                callback()
            return answer, metadata, {"status": "PASS", "isolated_temporary_ledger": True}

        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "unit-test-only",
            "DEEPSEEK_MODEL": "deepseek-flash",
        }):
            with patch(
                "scripts.validate_phase1_llm_live._structured_chat_with_temporary_ledger",
                side_effect=one_request,
            ) as request:
                report = validate("deepseek", authorize_deepseek=True)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["calls_made"], 1)
        self.assertEqual(report["provider_metadata"]["completion_tokens"], 4)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], ProbeAnswer)
        self.assertFalse(report["api_key_logged"])
        self.assertFalse(report["market_order_sent"])

    def test_deepseek_preflight_enforces_model_timeout_output_and_cost_cap(self):
        prompt = "Synthetic integration diagnostic."
        payload = {"allowed_evidence_ids": ["LIVE_PROBE"]}
        passing = _deepseek_preflight(
            ProbeAnswer, prompt, payload, model="deepseek-flash",
            max_output_tokens=256, timeout_seconds=30,
        )
        self.assertEqual(passing["status"], "PASS")
        self.assertLessEqual(float(passing["maximum_cost_estimate_usd"]), 0.25)

        wrong_model = _deepseek_preflight(
            ProbeAnswer, prompt, payload, model="another-model",
            max_output_tokens=256, timeout_seconds=30,
        )
        too_long = _deepseek_preflight(
            ProbeAnswer, prompt, payload, model="deepseek-flash",
            max_output_tokens=257, timeout_seconds=30,
        )
        too_slow = _deepseek_preflight(
            ProbeAnswer, prompt, payload, model="deepseek-flash",
            max_output_tokens=256, timeout_seconds=30.01,
        )
        self.assertEqual(wrong_model["status"], "PENDING")
        self.assertEqual(too_long["status"], "PENDING")
        self.assertEqual(too_slow["status"], "PENDING")

    def test_invalid_provider_response_never_becomes_signal(self):
        def invalid_response(*args, **kwargs):
            callback = kwargs.get("on_request_start")
            if callback:
                callback()
            raise LLMProviderError("LLM_INVALID_EVIDENCE")

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-test-only"}):
            with patch("scripts.validate_phase1_llm_live._structured_chat_with_temporary_ledger", side_effect=invalid_response):
                report = validate("deepseek", authorize_deepseek=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["calls_made"], 1)
        self.assertFalse(report["operational_signal_created"])

    def test_probe_ledger_is_temporary_ineligible_and_persists_valid_response(self):
        from src.event_ledger import record_llm_call, record_llm_query_started

        answer = ProbeAnswer(status="PASS", evidence_used=["LIVE_PROBE"])

        def fake_structured_chat(schema, prompt, payload, *, return_metadata):
            self.assertTrue(return_metadata)
            record_llm_query_started(role="LIVE_PROVIDER_CONTRACT_PROBE", policy_id=None, payload=payload)
            record_llm_call(
                schema_name=schema.__name__,
                provider="ollama",
                model="qwen3:4b",
                prompt=prompt,
                payload=payload,
                allowed_evidence=["LIVE_PROBE"],
                requested_at=datetime.now(timezone.utc).isoformat(),
                result=answer,
                metadata={"provider_reported_model": "qwen3:4b"},
                agent_role="UNREGISTERED_SCHEMA",
                agent_stage="DIAGNOSTIC_ONLY",
            )
            return answer, {"provider_reported_model": "qwen3:4b"}

        with patch("src.agents.llm_client.structured_chat", side_effect=fake_structured_chat):
            response, metadata, ledger = _structured_chat_with_temporary_ledger(
                ProbeAnswer,
                "diagnostic only",
                {"allowed_evidence_ids": ["LIVE_PROBE"]},
            )
        self.assertEqual(response.status, "PASS")
        self.assertEqual(metadata["provider_reported_model"], "qwen3:4b")
        self.assertEqual(ledger["status"], "PASS")
        self.assertTrue(ledger["isolated_temporary_ledger"])
        self.assertEqual(ledger["valid_response_events"], 1)
        self.assertEqual(ledger["eligible_events"], 0)
        self.assertEqual(ledger["orders_executed"], 0)


if __name__ == "__main__":
    unittest.main()
