"""DeepSeek adapter used only by the unified LLM client."""

from __future__ import annotations

import json
import logging
import os
from time import perf_counter

try:
    from openai import OpenAI
except ImportError:  # Offline validation can import this adapter without the SDK.
    OpenAI = None


LOGGER = logging.getLogger("agenttrading.llm")
_ALLOWED_EFFORTS = {"low", "high", "max"}
_EVIDENCE_FIELDS = {"evidence_used", "hold_evidence_used", "break_evidence_used"}


def _schema_with_allowed_evidence(schema, payload):
    output_schema = schema.model_json_schema()
    fields = [
        field
        for field in sorted(_EVIDENCE_FIELDS)
        if field in output_schema.get("properties", {})
    ]
    if not fields:
        return output_schema, [], fields

    ids = payload.get("allowed_evidence_ids")
    if ids is None:
        ids = payload.get("available_evidence_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
        raise ValueError("LLM_ALLOWED_EVIDENCE_MISSING")
    ids = list(dict.fromkeys(ids))
    for field in fields:
        output_schema["properties"][field].setdefault("items", {})
        output_schema["properties"][field]["items"] = {
            "type": "string",
            "enum": ids,
        }
    output_schema["required"] = list(dict.fromkeys(output_schema.get("required", []) + fields))
    return output_schema, ids, fields


def _api_failure_code(exc):
    """Classify API failures without exposing provider text or credentials."""
    name = type(exc).__name__
    raw_status = getattr(exc, "status_code", None)
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None

    if status == 401 or name == "AuthenticationError":
        return "LLM_AUTHENTICATION_REJECTED"
    if status == 403 or name == "PermissionDeniedError":
        return "LLM_PROVIDER_PERMISSION_DENIED"
    if status == 404 or name == "NotFoundError":
        return "LLM_PROVIDER_MODEL_OR_ENDPOINT_NOT_FOUND"
    if status == 429 or name == "RateLimitError":
        return "LLM_PROVIDER_RATE_LIMITED"
    if name == "APITimeoutError":
        return "LLM_TIMEOUT"
    if name == "APIConnectionError":
        return "LLM_PROVIDER_CONNECTION_FAILED"
    if status is not None and 400 <= status < 500:
        return "LLM_PROVIDER_REQUEST_REJECTED"
    if status is not None and status >= 500:
        return "LLM_PROVIDER_SERVER_ERROR"
    return "LLM_API_ERROR"


def deepseek_structured_chat(
    schema,
    system_prompt,
    payload,
    *,
    model=None,
    timeout_seconds=None,
    max_tokens=None,
    return_metadata=False,
    on_request_start=None,
):
    """Generate and validate one structured response; retries remain disabled."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("LLM_API_KEY_MISSING")

    model = (model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")).strip()
    effort = os.environ.get("DEEPSEEK_REASONING_EFFORT", "low").strip().lower()
    if not model or effort not in _ALLOWED_EFFORTS:
        raise ValueError("LLM_CONFIGURATION_INVALID")

    is_comparison = schema.__name__ == "LevelHypothesisComparison"
    max_output = int(
        max_tokens
        if max_tokens is not None
        else os.environ.get("DEEPSEEK_MAX_TOKENS", "8192" if is_comparison else "2048")
    )
    if not 1 <= max_output <= 65536:
        raise ValueError("LLM_CONFIGURATION_INVALID")

    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "180"))
    if timeout_seconds <= 0 or timeout_seconds > 1800:
        raise ValueError("LLM_CONFIGURATION_INVALID")

    output_schema, allowed_ids, evidence_fields = _schema_with_allowed_evidence(schema, payload)
    instructions = (
        system_prompt.rstrip()
        + "\n\nReturn ONLY a valid JSON object. No Markdown. "
        + "Follow this JSON Schema exactly, including all required fields:\n"
        + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
    )
    if OpenAI is None:
        raise RuntimeError("LLM_PROVIDER_DEPENDENCY_MISSING")
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=timeout_seconds,
        max_retries=0,
    )
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": max_output,
    }
    if is_comparison:
        kwargs["reasoning_effort"] = effort
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = 0.0

    started = perf_counter()
    if on_request_start is not None:
        # The remote smoke runner uses this local callback to count an attempt
        # at the exact point immediately before the sole HTTP request.
        on_request_start()
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        error_code = _api_failure_code(exc)
        status = getattr(exc, "status_code", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        LOGGER.warning(
            "DeepSeek provider request failed error=%s http_status=%s",
            error_code,
            status,
        )
        raise RuntimeError(error_code) from None
    if not response.choices:
        raise RuntimeError("LLM_EMPTY_RESPONSE")
    choice = response.choices[0]
    if choice.finish_reason != "stop":
        raise RuntimeError("LLM_INCOMPLETE_RESPONSE")
    content = choice.message.content or ""
    if not content.strip():
        raise RuntimeError("LLM_EMPTY_RESPONSE")

    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM_INVALID_RESPONSE")
    for field in evidence_fields:
        values = parsed.get(field)
        if not isinstance(values, list) or any(
            not isinstance(item, str) or item not in allowed_ids
            for item in values
        ):
            raise ValueError("LLM_INVALID_EVIDENCE")

    result = schema.model_validate_json(content)
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    metadata = {
        "provider_reported_model": getattr(response, "model", None),
        "finish_reason": choice.finish_reason,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
    }
    LOGGER.info(
        "DeepSeek response model=%s schema=%s prompt_tokens=%s completion_tokens=%s "
        "reasoning_tokens=%s elapsed_seconds=%.3f",
        model,
        schema.__name__,
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        getattr(details, "reasoning_tokens", None),
        perf_counter() - started,
    )
    return (result, metadata) if return_metadata else result
