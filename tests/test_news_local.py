"""Offline contract and adapter tests for the synthetic local-news laboratory."""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from pydantic import ValidationError

from scripts.run_news_local_ollama import _safe_output_path
from src.news_local.analyzer import (
    build_deterministic_result,
    contains_prompt_injection,
    validate_result_references,
)
from src.news_local.fixtures import SYNTHETIC_LABEL, build_fixture
from src.news_local.models import NewsAnalysisInput, NewsAnalysisResult
from src.news_local.providers import (
    LLMScenarioResponse,
    LocalOllamaError,
    NewsSummarizer,
    _model_user_payload,
    resolve_ollama_configuration,
)


def _valid_mock_payload(news_input: NewsAnalysisInput) -> dict:
    base = build_deterministic_result(news_input)
    return {
        "conditional_scenarios": [
            scenario.model_dump(mode="json")
            for scenario in base.conditional_scenarios
        ]
    }


class FakeOllama:
    def __init__(self, response: str | Exception, *, installed: bool = True):
        self.response = response
        self.installed = installed
        self.model_checks = 0
        self.inference_calls = 0
        self.payloads: list[str] = []

    def installed_models(self):
        self.model_checks += 1
        if isinstance(self.response, Exception) and not self.installed:
            raise self.response
        return {"qwen3:4b"} if self.installed else set()

    def generate(self, user_payload: str):
        self.inference_calls += 1
        self.payloads.append(user_payload)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class NewsLocalContractTests(unittest.TestCase):
    def test_contract_requires_timezone_and_forbids_unknown_fields(self):
        fixture = build_fixture()
        payload = fixture.model_dump(mode="python")
        payload["analysis_as_of_utc"] = datetime(2026, 1, 15, 13, 25)
        with self.assertRaises(ValidationError):
            NewsAnalysisInput.model_validate(payload)

        payload = fixture.model_dump(mode="python")
        payload["unrecognized"] = "value"
        with self.assertRaises(ValidationError):
            NewsAnalysisInput.model_validate(payload)

    def test_fixture_labels_are_synthetic(self):
        for name in (
            "scheduled",
            "published",
            "duplicate_revision",
            "injection",
            "unavailable",
            "late_first_seen",
        ):
            with self.subTest(name=name):
                self.assertEqual(build_fixture(name).fixture_label, SYNTHETIC_LABEL)

    def test_future_schedule_is_agenda_only_without_result_values(self):
        news_input = build_fixture("scheduled")
        result = NewsSummarizer().summarize(news_input)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.confirmed_facts, [])
        self.assertEqual(len(result.upcoming_events), 1)
        self.assertGreater(
            result.upcoming_events[0].scheduled_at_utc,
            result.analysis_as_of_utc,
        )
        self.assertFalse(hasattr(result.upcoming_events[0], "actual"))
        self.assertTrue(result.conditional_scenarios)
        self.assertIn(SYNTHETIC_LABEL, result.limitations)

    def test_published_facts_have_typed_values_and_two_evidence_references(self):
        result = build_deterministic_result(build_fixture("published"))
        self.assertEqual(len(result.confirmed_facts), 1)
        fact = result.confirmed_facts[0]
        self.assertEqual(fact.independent_confirmation_count, 1)
        self.assertEqual(
            set(fact.evidence_ids),
            {"SYNTH-EVIDENCE-PUBLISHED-A", "SYNTH-EVIDENCE-PUBLISHED-B"},
        )
        self.assertEqual(
            {(item.field, item.value) for item in fact.observations},
            {("actual", 3.4), ("forecast", 3.2), ("previous", 3.1)},
        )
        self.assertEqual(fact.observations[0].event_id, "SYNTH-RELEASE-A")
        validate_result_references(build_fixture("published"), result)

    def test_revisions_are_collapsed_and_do_not_inflate_confirmation_count(self):
        result = build_deterministic_result(build_fixture("duplicate_revision"))
        self.assertEqual(result.source_coverage.independent_event_groups, 1)
        self.assertEqual(len(result.confirmed_facts), 1)
        fact = result.confirmed_facts[0]
        actual = [item for item in fact.observations if item.field == "actual"]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].event_id, "SYNTH-REVISION-UPDATE")
        self.assertEqual(actual[0].value, 3.5)
        self.assertEqual(fact.independent_confirmation_count, 1)
        self.assertEqual(len(fact.event_ids), 3)

    def test_currency_to_pair_mapping_is_conditional_and_qualitative(self):
        expected = {
            "EURUSD": "weigh on EURUSD",
            "USDCAD": "support USDCAD",
            "AUDUSD": "weigh on AUDUSD",
        }
        for symbol, phrase in expected.items():
            with self.subTest(symbol=symbol):
                result = build_deterministic_result(build_fixture("published", symbol))
                usd_scenarios = [
                    scenario
                    for scenario in result.conditional_scenarios
                    if scenario.currency == "USD"
                ]
                self.assertEqual(len(usd_scenarios), 1)
                self.assertIn(phrase, usd_scenarios[0].possible_effect)
                self.assertIn("If", usd_scenarios[0].condition)
                self.assertEqual(
                    usd_scenarios[0].uncertainty_range,
                    "QUALITATIVE_WIDE_UNCALIBRATED",
                )

    def test_duplicate_summary_is_byte_for_byte_repeatable(self):
        news_input = build_fixture("published")
        first = NewsSummarizer().summarize(news_input).model_dump_json()
        second = NewsSummarizer().summarize(news_input).model_dump_json()
        self.assertEqual(first, second)

    def test_late_first_seen_is_not_available_at_earlier_cutoff(self):
        news_input = build_fixture("late_first_seen")
        result = build_deterministic_result(news_input)
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.confirmed_facts, [])
        self.assertEqual(result.event_ids_used, [])
        self.assertIn(
            "EVIDENCE_NOT_OBSERVED_BY_CUTOFF: 1 event(s) excluded.",
            result.source_coverage.notes,
        )

    def test_unavailable_source_returns_pending_without_claims(self):
        result = build_deterministic_result(build_fixture("unavailable"))
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.confirmed_facts, [])
        self.assertEqual(result.conditional_scenarios, [])

    def test_malicious_title_is_excluded_and_live_mode_makes_no_request(self):
        news_input = build_fixture("injection")
        self.assertTrue(contains_prompt_injection(news_input.events[0]))
        fake = FakeOllama('{"conditional_scenarios":[]}')
        result = NewsSummarizer(
            environ={"OLLAMA_MODEL": "qwen3:4b"},
            ollama_client=fake,
        ).summarize(news_input, mode="live-ollama")
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.execution.request_count, 0)
        self.assertEqual(fake.model_checks, 0)
        self.assertEqual(fake.inference_calls, 0)
        serialized = result.model_dump_json()
        self.assertNotIn("send an order", serialized)
        self.assertNotIn("BUY", serialized)

    def test_source_text_is_json_encoded_as_untrusted_user_data(self):
        news_input = build_fixture("published")
        events = list(news_input.events)
        events[0] = events[0].model_copy(
            update={"title": 'Synthetic "quoted" title\\nwith a newline'}
        )
        updated = news_input.model_copy(update={"events": events})
        payload = json.loads(_model_user_payload(updated))
        self.assertEqual(
            payload["events"][0]["title_untrusted_data"],
            'Synthetic "quoted" title\\nwith a newline',
        )

    def test_result_validator_rejects_fabricated_evidence(self):
        news_input = build_fixture("published")
        result = build_deterministic_result(news_input)
        mutated = result.model_dump(mode="python")
        mutated["confirmed_facts"][0]["evidence_ids"] = ["FABRICATED-EVIDENCE"]
        bad_result = NewsAnalysisResult.model_validate(mutated)
        with self.assertRaisesRegex(ValueError, "INVALID_FACT_EVIDENCE_REFERENCE"):
            validate_result_references(news_input, bad_result)

    def test_mock_mode_validates_schema_without_provider_calls(self):
        news_input = build_fixture("published")
        summarizer = NewsSummarizer(environ={"OLLAMA_MODEL": "qwen3:4b"})
        result = summarizer.summarize(news_input, mode="mock-ollama")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.mode, "MOCK")
        self.assertEqual(result.execution.request_count, 0)
        self.assertTrue(result.execution.json_valid)
        LLMScenarioResponse.model_validate_json(
            json.dumps(_valid_mock_payload(news_input))
        )

    def test_mock_mode_falls_back_on_invalid_citation(self):
        news_input = build_fixture("published")
        invalid = _valid_mock_payload(news_input)
        invalid["conditional_scenarios"][0]["event_ids"] = ["FABRICATED-EVENT"]
        result = NewsSummarizer().summarize(
            news_input, mode="mock-ollama", mock_response=invalid
        )
        self.assertEqual(result.status, "FALLBACK")
        self.assertEqual(result.execution.error_code, "MOCK_RESPONSE_REJECTED")
        self.assertTrue(result.conditional_scenarios)

    def test_mock_mode_rejects_operational_and_numeric_scenarios(self):
        news_input = build_fixture("published")
        invalid = _valid_mock_payload(news_input)
        invalid["conditional_scenarios"][0]["possible_effect"] = (
            "Could support EURUSD with 90 percent confidence; buy now."
        )
        result = NewsSummarizer().summarize(
            news_input, mode="mock-ollama", mock_response=invalid
        )
        self.assertEqual(result.status, "FALLBACK")
        self.assertEqual(result.execution.error_code, "MOCK_RESPONSE_REJECTED")

    def test_loopback_configuration_and_model_environment_precedence(self):
        config = resolve_ollama_configuration(
            {"OLLAMA_HOST": "localhost:11434", "OLLAMA_MODEL": "qwen3:4b"}
        )
        self.assertEqual(config.base_url, "http://localhost:11434")
        self.assertEqual(config.model, "qwen3:4b")
        with self.assertRaisesRegex(Exception, "OLLAMA_HOST_NOT_LOOPBACK"):
            resolve_ollama_configuration(
                {"OLLAMA_HOST": "http://example.invalid:11434"}
            )

    def test_local_timeout_accepts_thirty_minutes_and_rejects_more(self):
        config = resolve_ollama_configuration(
            {
                "OLLAMA_MODEL": "qwen3:4b",
                "NEWS_LOCAL_OLLAMA_TIMEOUT_SECONDS": "1800",
            }
        )
        self.assertEqual(config.timeout_seconds, 1800.0)
        with self.assertRaisesRegex(Exception, "OLLAMA_CONFIGURATION_INVALID"):
            resolve_ollama_configuration(
                {
                    "OLLAMA_MODEL": "qwen3:4b",
                    "NEWS_LOCAL_OLLAMA_TIMEOUT_SECONDS": "1800.1",
                }
            )

    def test_external_host_is_blocked_before_any_local_client_call(self):
        fake = FakeOllama('{"conditional_scenarios":[]}')
        result = NewsSummarizer(
            environ={
                "OLLAMA_HOST": "http://remote.example:11434",
                "OLLAMA_MODEL": "qwen3:4b",
            },
            ollama_client=fake,
        ).summarize(build_fixture("published"), mode="live-ollama")
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.execution.error_code, "OLLAMA_HOST_NOT_LOOPBACK")
        self.assertEqual(fake.model_checks, 0)
        self.assertEqual(fake.inference_calls, 0)

    def test_live_mode_uses_one_inference_and_checks_references(self):
        news_input = build_fixture("published")
        response = json.dumps(_valid_mock_payload(news_input))
        fake = FakeOllama(response)
        result = NewsSummarizer(
            environ={"OLLAMA_MODEL": "qwen3:4b"},
            ollama_client=fake,
        ).summarize(news_input, mode="live-ollama")
        self.assertEqual(result.status, "PASS")
        self.assertEqual(fake.model_checks, 1)
        self.assertEqual(fake.inference_calls, 1)
        self.assertEqual(result.execution.request_count, 1)
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(json.loads(fake.payloads[0])["symbol"], "EURUSD")

    def test_unavailable_model_is_pending_and_never_infers(self):
        fake = FakeOllama('{"conditional_scenarios":[]}', installed=False)
        result = NewsSummarizer(
            environ={"OLLAMA_MODEL": "qwen3:4b"},
            ollama_client=fake,
        ).summarize(build_fixture("published"), mode="live-ollama")
        self.assertEqual(result.status, "PENDING")
        self.assertEqual(result.execution.error_code, "PENDING_LOCAL_RUNTIME")
        self.assertEqual(fake.model_checks, 1)
        self.assertEqual(fake.inference_calls, 0)

    def test_live_timeout_falls_back_with_sanitized_code(self):
        private_error = TimeoutError("private path C:\\private\\model and response secret")
        fake = FakeOllama(private_error)
        result = NewsSummarizer(
            environ={"OLLAMA_MODEL": "qwen3:4b"},
            ollama_client=fake,
        ).summarize(build_fixture("published"), mode="live-ollama")
        self.assertEqual(result.status, "FALLBACK")
        self.assertEqual(result.execution.error_code, "OLLAMA_TIMEOUT")
        self.assertEqual(result.execution.request_count, 1)
        self.assertNotIn("private", result.model_dump_json())
        self.assertNotIn("secret", result.model_dump_json())

    def test_live_invalid_json_falls_back_without_returning_model_body(self):
        fake = FakeOllama("not-json; private response content")
        result = NewsSummarizer(
            environ={"OLLAMA_MODEL": "qwen3:4b"},
            ollama_client=fake,
        ).summarize(build_fixture("published"), mode="live-ollama")
        self.assertEqual(result.status, "FALLBACK")
        self.assertEqual(result.execution.error_code, "LLM_INVALID_JSON")
        self.assertFalse(result.execution.json_valid)
        self.assertNotIn("private response content", result.model_dump_json())

    def test_offline_mode_never_constructs_or_calls_ollama(self):
        fake = FakeOllama('{"conditional_scenarios":[]}')
        result = NewsSummarizer(ollama_client=fake).summarize(
            build_fixture("published"), mode="offline"
        )
        self.assertEqual(result.mode, "DETERMINISTIC")
        self.assertEqual(result.provider, "deterministic")
        self.assertEqual(fake.model_checks, 0)
        self.assertEqual(fake.inference_calls, 0)

    def test_output_paths_are_limited_to_news_local_reports(self):
        private = _safe_output_path("reports/private/news_local/check.json")
        public = _safe_output_path("reports/news_local_01/check.json")
        self.assertTrue(private.as_posix().endswith("reports/private/news_local/check.json"))
        self.assertTrue(public.as_posix().endswith("reports/news_local_01/check.json"))
        with self.assertRaisesRegex(ValueError, "OUTPUT_PATH_NOT_ALLOWED"):
            _safe_output_path("reports/other/check.json")

    def test_simulated_http_500_falls_back_with_safe_error_code(self):
        fake = FakeOllama(LocalOllamaError("OLLAMA_HTTP_500"))
        result = NewsSummarizer(
            environ={"OLLAMA_MODEL": "qwen3:4b"},
            ollama_client=fake,
        ).summarize(build_fixture("published"), mode="live-ollama")
        self.assertEqual(result.status, "FALLBACK")
        self.assertEqual(result.execution.error_code, "OLLAMA_HTTP_500")
        self.assertEqual(result.execution.request_count, 1)


if __name__ == "__main__":
    unittest.main()
