"""Single provider boundary for structured LLM calls.

Agents receive validated Pydantic models. Provider failures are translated to
safe error codes, and no provider response or credential is logged.
"""

from __future__ import annotations

import copy
import json
import logging
from math import isfinite
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any
from datetime import datetime, timezone

from dotenv import load_dotenv
from pydantic import ValidationError

from config.agent_config import OLLAMA_CONTEXT, OLLAMA_KEEP_ALIVE, OLLAMA_MODEL, OLLAMA_TEMPERATURE
from src.agents import deepseek_client
from src.research_policy import CANONICAL_FILTER_ROLE, POLICY_VERSION, schema_role


load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

LOGGER = logging.getLogger("agenttrading.llm")
_PROVIDERS = {"ollama", "deepseek"}
_EVIDENCE_FIELDS = {"evidence_used", "hold_evidence_used", "break_evidence_used"}


class LLMProviderError(RuntimeError):
    """Provider failure with a non-sensitive, stable error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LLMConfiguration:
    provider: str
    model: str
    timeout_seconds: float
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    max_retries: int = 0
    temperature: float | None = None
    context_window: int | None = None
    keep_alive: str | int | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "max_retries": self.max_retries,
            "temperature": self.temperature,
            "context_window": self.context_window,
            "keep_alive": self.keep_alive,
        }


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except (TypeError, ValueError):
        raise LLMProviderError("LLM_CONFIGURATION_INVALID") from None
    if not isfinite(value) or value <= 0 or value > 1800:
        raise LLMProviderError("LLM_CONFIGURATION_INVALID")
    return value


def get_llm_configuration(schema=None) -> LLMConfiguration:
    """Read the effective settings; process environment overrides .env."""
    provider = os.environ.get("LLM_PROVIDER", "ollama").strip().lower()
    if provider not in _PROVIDERS:
        raise LLMProviderError("LLM_PROVIDER_UNSUPPORTED")

    if provider == "deepseek":
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
        effort = os.environ.get("DEEPSEEK_REASONING_EFFORT", "low").strip().lower()
        if effort not in {"low", "high", "max"} or not model:
            raise LLMProviderError("LLM_CONFIGURATION_INVALID")
        is_comparison = getattr(schema, "__name__", "") == "LevelHypothesisComparison"
        try:
            max_tokens = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "8192" if is_comparison else "2048"))
        except (TypeError, ValueError):
            raise LLMProviderError("LLM_CONFIGURATION_INVALID") from None
        if not 1 <= max_tokens <= 65536:
            raise LLMProviderError("LLM_CONFIGURATION_INVALID")
        return LLMConfiguration(
            provider=provider,
            model=model,
            timeout_seconds=_positive_float("DEEPSEEK_TIMEOUT_SECONDS", 180.0),
            reasoning_effort=effort,
            max_tokens=max_tokens,
            temperature=None if is_comparison else 0.0,
        )

    model = os.environ.get("OLLAMA_MODEL", OLLAMA_MODEL).strip()
    if not model:
        raise LLMProviderError("LLM_CONFIGURATION_INVALID")
    return LLMConfiguration(
        provider=provider,
        model=model,
        timeout_seconds=_positive_float("OLLAMA_TIMEOUT_SECONDS", 120.0),
        temperature=OLLAMA_TEMPERATURE,
        context_window=OLLAMA_CONTEXT,
        keep_alive=OLLAMA_KEEP_ALIVE,
    )


@lru_cache(maxsize=8)
def _ollama_client(host: str | None, timeout_seconds: float):
    from ollama import Client

    return Client(host=host, timeout=timeout_seconds)


def _allowed_evidence(payload: dict[str, Any], schema: type) -> tuple[list[str], list[str]]:
    properties = schema.model_json_schema().get("properties", {})
    fields = [field for field in sorted(_EVIDENCE_FIELDS) if field in properties]
    if not fields:
        return [], []

    raw_ids = payload.get("allowed_evidence_ids")
    if raw_ids is None:
        raw_ids = payload.get("available_evidence_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise LLMProviderError("LLM_ALLOWED_EVIDENCE_MISSING")
    if any(not isinstance(item, str) or not item for item in raw_ids):
        raise LLMProviderError("LLM_ALLOWED_EVIDENCE_INVALID")
    return list(dict.fromkeys(raw_ids)), fields


def _schema_with_evidence(schema: type, allowed_ids: list[str], fields: list[str]) -> dict:
    result = copy.deepcopy(schema.model_json_schema())
    for field in fields:
        item_schema = result["properties"][field].setdefault("items", {"type": "string"})
        item_schema.clear()
        item_schema.update({"type": "string", "enum": allowed_ids})
    return result


def _response_content(response: Any) -> str:
    if isinstance(response, dict):
        message = response.get("message") or {}
        return str(message.get("content") or "")
    message = getattr(response, "message", None)
    return str(getattr(message, "content", None) or "")


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, LLMProviderError):
        return exc.code
    safe_codes = {
        "LLM_API_KEY_MISSING",
        "LLM_PROVIDER_DEPENDENCY_MISSING",
        "LLM_AUTHENTICATION_REJECTED",
        "LLM_PROVIDER_PERMISSION_DENIED",
        "LLM_PROVIDER_MODEL_OR_ENDPOINT_NOT_FOUND",
        "LLM_PROVIDER_RATE_LIMITED",
        "LLM_PROVIDER_CONNECTION_FAILED",
        "LLM_PROVIDER_REQUEST_REJECTED",
        "LLM_PROVIDER_SERVER_ERROR",
        "LLM_CONFIGURATION_INVALID",
        "LLM_ALLOWED_EVIDENCE_MISSING",
        "LLM_ALLOWED_EVIDENCE_INVALID",
        "LLM_INVALID_EVIDENCE",
        "LLM_INCOMPLETE_RESPONSE",
        "LLM_EMPTY_RESPONSE",
        "LLM_INVALID_RESPONSE",
    }
    if str(exc) in safe_codes:
        return str(exc)
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name:
        return "LLM_TIMEOUT"
    if isinstance(exc, (ValidationError, json.JSONDecodeError)):
        return "LLM_INVALID_RESPONSE"
    return "LLM_API_ERROR"


def _validate_evidence(result: Any, fields: list[str], allowed_ids: list[str]) -> None:
    allowed = set(allowed_ids)
    for field in fields:
        values = getattr(result, field, None)
        if not isinstance(values, list) or any(value not in allowed for value in values):
            raise LLMProviderError("LLM_INVALID_EVIDENCE")


def structured_chat(schema, system_prompt, payload, *, model_override=None, return_metadata=False):
    """Call the selected provider once and return a validated Pydantic model.

    ``return_metadata`` is intended for explicit live validation. It returns
    only the provider-reported model, latency and token counts, never prompts,
    response bodies, credentials, or internal reasoning.
    """
    started = perf_counter()
    requested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    allowed_ids: list[str] = []
    config = None

    def record_failure(code: str) -> None:
        try:
            from src.event_ledger import record_llm_call, research_log_required
            provider = config.provider if config else os.environ.get("LLM_PROVIDER", "ollama").strip().lower()
            model = config.model if config else os.environ.get("DEEPSEEK_MODEL" if provider == "deepseek" else "OLLAMA_MODEL", "unavailable")
            role, stage, _ = schema_role(schema)
            saved = record_llm_call(
                schema_name=schema.__name__, provider=provider, model=model,
                prompt=system_prompt, payload=payload, allowed_evidence=allowed_ids,
                requested_at=requested_at, error_code=code, rejection_code=code,
                elapsed_seconds=perf_counter() - started,
                metadata={"schema_module": schema.__module__},
                agent_role=role, agent_stage=stage,
                decision_policy_id=POLICY_VERSION if provider == "deepseek" and role == CANONICAL_FILTER_ROLE else None,
            )
            if research_log_required() and not saved.persisted:
                raise LLMProviderError("LLM_RESEARCH_LOG_PERSISTENCE_FAILED") from None
        except Exception as exc:
            if os.environ.get("RESEARCH_LOG_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}:
                raise LLMProviderError("LLM_RESEARCH_LOG_PERSISTENCE_FAILED") from None
            LOGGER.error("research ledger write failed while recording provider error schema=%s", schema.__name__)

    try:
        config = get_llm_configuration(schema)
        if model_override is not None:
            if config.provider != "ollama" or not str(model_override).strip():
                raise LLMProviderError("LLM_MODEL_OVERRIDE_UNSUPPORTED")
            config = replace(config, model=str(model_override).strip())
        if not isinstance(payload, dict):
            raise LLMProviderError("LLM_PAYLOAD_INVALID")
        allowed_ids, evidence_fields = _allowed_evidence(payload, schema)
        output_schema = _schema_with_evidence(schema, allowed_ids, evidence_fields)
    except Exception as exc:
        code = _failure_code(exc)
        record_failure(code)
        raise LLMProviderError(code) from None

    role, role_stage, _is_filter = schema_role(schema)
    policy_id = POLICY_VERSION if config.provider == "deepseek" and role == CANONICAL_FILTER_ROLE else None
    try:
        from src.event_ledger import LedgerPersistenceError, record_llm_query_started
        query_result = record_llm_query_started(
            role=role, policy_id=policy_id,
            payload=payload,
        )
        if os.environ.get("RESEARCH_LOG_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"} and not query_result.persisted:
            raise LedgerPersistenceError(query_result.error_code or "LEDGER_QUERY_START_NOT_PERSISTED")
    except Exception as exc:
        code = "LLM_RESEARCH_LOG_PERSISTENCE_FAILED" if "LedgerPersistenceError" in type(exc).__name__ else _failure_code(exc)
        if code not in {"LLM_RESEARCH_LOG_PERSISTENCE_FAILED", "LLM_API_ERROR"}:
            code = "LLM_RESEARCH_LOG_PERSISTENCE_FAILED"
        record_failure(code)
        raise LLMProviderError(code) from None

    LOGGER.info(
        "LLM call provider=%s model=%s schema=%s timeout_seconds=%s "
        "reasoning_effort=%s max_tokens=%s temperature=%s context_window=%s keep_alive=%s max_retries=0",
        config.provider,
        config.model,
        schema.__name__,
        config.timeout_seconds,
        config.reasoning_effort,
        config.max_tokens,
        config.temperature,
        config.context_window,
        config.keep_alive,
    )

    metadata = {}
    try:
        if config.provider == "deepseek":
            response = deepseek_client.deepseek_structured_chat(
                schema,
                system_prompt,
                payload,
                model=config.model,
                timeout_seconds=config.timeout_seconds,
                max_tokens=config.max_tokens,
                # Metadata comes from the same response; this does not make an
                # extra provider request and lets the append-only ledger retain
                # usage counts even when the caller does not request metadata.
                return_metadata=True,
            )
            if isinstance(response, tuple) and len(response) == 2:
                result, metadata = response
            else:
                # Test doubles and older provider adapters may only return the
                # schema model; preserve compatibility and log missing usage.
                result = response
                metadata = {}
        else:
            host = os.environ.get("OLLAMA_HOST") or None
            client = _ollama_client(host, config.timeout_seconds)
            response = client.chat(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                format=output_schema,
                options={
                    "temperature": OLLAMA_TEMPERATURE,
                    "num_ctx": OLLAMA_CONTEXT,
                },
                think=schema.__name__ == "LevelHypothesisComparison",
                keep_alive=OLLAMA_KEEP_ALIVE,
            )
            done_reason = response.get("done_reason") if isinstance(response, dict) else getattr(response, "done_reason", None)
            if done_reason == "length":
                raise LLMProviderError("LLM_INCOMPLETE_RESPONSE")
            content = _response_content(response)
            if not content.strip():
                raise LLMProviderError("LLM_EMPTY_RESPONSE")
            result = schema.model_validate_json(content)
            metadata = {
                "provider_reported_model": (
                    response.get("model") if isinstance(response, dict)
                    else getattr(response, "model", None)
                ),
                "prompt_tokens": (
                    response.get("prompt_eval_count") if isinstance(response, dict)
                    else getattr(response, "prompt_eval_count", None)
                ),
                "completion_tokens": (
                    response.get("eval_count") if isinstance(response, dict)
                    else getattr(response, "eval_count", None)
                ),
            }

        _validate_evidence(result, evidence_fields, allowed_ids)
    except Exception as exc:
        code = _failure_code(exc)
        record_failure(code)
        LOGGER.warning(
            "LLM call failed provider=%s model=%s schema=%s error=%s",
            config.provider,
            config.model,
            schema.__name__,
            code,
        )
        raise LLMProviderError(code) from None

    elapsed = perf_counter() - started
    try:
        from src.event_ledger import record_llm_call, research_log_required
        saved = record_llm_call(
            schema_name=schema.__name__, provider=config.provider, model=config.model,
            prompt=system_prompt, payload=payload, allowed_evidence=allowed_ids,
            requested_at=requested_at, result=result, elapsed_seconds=elapsed,
            metadata={**metadata, "schema_module": schema.__module__},
            agent_role=role, agent_stage=role_stage, decision_policy_id=policy_id,
        )
        if research_log_required() and not saved.persisted:
            raise LLMProviderError("LLM_RESEARCH_LOG_PERSISTENCE_FAILED")
    except Exception as exc:
        LOGGER.error("research ledger write failed after provider response schema=%s", schema.__name__)
        if os.environ.get("RESEARCH_LOG_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}:
            raise LLMProviderError("LLM_RESEARCH_LOG_PERSISTENCE_FAILED") from None
    LOGGER.info(
        "LLM call complete provider=%s model=%s schema=%s elapsed_seconds=%.3f",
        config.provider,
        config.model,
        schema.__name__,
        elapsed,
    )
    if return_metadata:
        return result, {
            "provider": config.provider,
            "configured_model": config.model,
            "provider_reported_model": metadata.get("provider_reported_model"),
            "latency_seconds": round(elapsed, 3),
            "prompt_tokens": metadata.get("prompt_tokens"),
            "completion_tokens": metadata.get("completion_tokens"),
            "reasoning_tokens": metadata.get("reasoning_tokens"),
            "timeout_seconds": config.timeout_seconds,
            "reasoning_effort": config.reasoning_effort,
            "max_tokens": config.max_tokens,
            "max_retries": config.max_retries,
        }
    return result
