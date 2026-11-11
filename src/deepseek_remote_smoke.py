"""Preflight helpers for the one-request DeepSeek contract smoke test."""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any

from src.research_policy import BudgetGate, BudgetLimits


REQUEST_TYPE = "provider_contract_probe"
ALLOWED_EVIDENCE = ["LIVE_PROBE"]
EXPECTED_STATUS = "PASS"
MAX_OUTPUT_TOKENS = 256
MAX_INPUT_TOKEN_BOUND = 65_536
MAX_TOTAL_COST_USD = Decimal("0.25")
INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION = Decimal("0.30")
OUTPUT_PRICE_PEAK_PER_MILLION = Decimal("1.20")

PROBE_PAYLOAD: dict[str, Any] = {
    "request_type": REQUEST_TYPE,
    "allowed_evidence_ids": ALLOWED_EVIDENCE,
    "expected_status": EXPECTED_STATUS,
}
PROBE_PROMPT = (
    "Provider contract diagnostic only; this is not a trading request. "
    "Return exactly a JSON object with status PASS and evidence_used containing only LIVE_PROBE. "
    "Do not provide a market view, trading recommendation, signal, or order."
)

PRICE_EVIDENCE = {
    "model": "deepseek-flash",
    "resolved_model_documented": "DeepSeek-V4.1-Flash-1017",
    "currency": "USD",
    "billing_unit": "1M tokens",
    "input_tier": "peak cache miss",
    "input_price_per_million": "0.30",
    "output_tier": "peak",
    "output_price_per_million": "1.20",
    "pricing_url": "https://api-docs.deepseek.com/quick_start/pricing/",
    "models_url": "https://api-docs.deepseek.com/quick_start/model_list",
    "chat_completions_url": "https://api-docs.deepseek.com/api/create-chat-completion",
    "verified_date_utc": "2026-11-11",
    "checked_at_utc": "2026-11-11T15:46:44Z",
    "basis": "Published official DeepSeek pricing; worst published peak tier and uncached input assumed.",
}


def estimate_input_token_upper_bound(schema: type, system_prompt: str, payload: dict[str, Any]) -> dict[str, int]:
    """Bound full Chat Completions content, including generated JSON Schema.

    The request adapter uses UTF-8 text and a byte-fallback tokenizer. Four
    tokens per UTF-8 byte plus 64 chat framing tokens is deliberately looser
    than one token per byte and includes both complete messages, not just the
    payload. The caller blocks when this bound exceeds the explicit cap.
    """
    from src.agents.deepseek_client import _schema_with_allowed_evidence

    output_schema, _allowed_ids, _evidence_fields = _schema_with_allowed_evidence(schema, payload)
    system_content = (
        system_prompt.rstrip()
        + "\n\nReturn ONLY a valid JSON object. No Markdown. "
        + "Follow this JSON Schema exactly, including all required fields:\n"
        + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
    )
    user_content = json.dumps(payload, ensure_ascii=False)
    system_bytes = len(system_content.encode("utf-8"))
    user_bytes = len(user_content.encode("utf-8"))
    text_bytes = system_bytes + user_bytes
    bound = 4 * text_bytes + 64
    return {
        "system_message_utf8_bytes": system_bytes,
        "user_message_utf8_bytes": user_bytes,
        "all_message_utf8_bytes": text_bytes,
        "chat_framing_tokens_allowance": 64,
        "conservative_input_tokens_upper_bound": bound,
    }


def make_single_request_budget_gate() -> BudgetGate:
    return BudgetGate(BudgetLimits(
        max_requests=1,
        max_tokens_per_request=MAX_OUTPUT_TOKENS,
        max_total_tokens=MAX_INPUT_TOKEN_BOUND + MAX_OUTPUT_TOKENS,
        max_estimated_cost=MAX_TOTAL_COST_USD,
        authorized_total=MAX_TOTAL_COST_USD,
    ))


def authorize_single_request(input_tokens: int, *, prices_verified: bool) -> tuple[BudgetGate, dict[str, Any]]:
    gate = make_single_request_budget_gate()
    if input_tokens > MAX_INPUT_TOKEN_BOUND:
        result = {
            "authorized": False,
            "status": "BLOCKED",
            "reason": "INPUT_TOKEN_BOUND_EXCEEDED",
            "real_provider_call": False,
        }
        gate.events.append(result.copy())
        return gate, result
    decision = gate.authorize(
        input_tokens=input_tokens,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        price_input_per_million=INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION if prices_verified else None,
        price_output_per_million=OUTPUT_PRICE_PEAK_PER_MILLION if prices_verified else None,
        prices_verified=prices_verified,
    )
    return gate, decision


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> Decimal:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("TOKEN_USAGE_INVALID")
    return (
        Decimal(input_tokens) * INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION
        + Decimal(output_tokens) * OUTPUT_PRICE_PEAK_PER_MILLION
    ) / Decimal(1_000_000)
