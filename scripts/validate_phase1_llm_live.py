#!/usr/bin/env python3
"""One-request, read-only live check for Ollama or explicitly authorized DeepSeek."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

DEEPSEEK_PREFLIGHT_LIMIT_USD = Decimal("0.25")
DEEPSEEK_PEAK_INPUT_USD_PER_MILLION = Decimal("0.30")
DEEPSEEK_PEAK_OUTPUT_USD_PER_MILLION = Decimal("1.20")
DEEPSEEK_MAX_OUTPUT_TOKENS = 256
DEEPSEEK_REQUEST_TIMEOUT_SECONDS = 30.0


class ProbeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^PASS$")
    evidence_used: list[str]


def _write(report, path):
    from scripts.validate_phase1 import _sanitize_public_report

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_report = _sanitize_public_report(report)
    path.write_text(json.dumps(safe_report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _traceability(provider):
    from scripts.validate_phase1 import _external_scope_fingerprint, _external_scope_snapshot, _source_snapshot

    snapshot = _source_snapshot()
    return {
        "validation_fingerprint": snapshot,
        "external_scope": _external_scope_snapshot(provider, snapshot),
        "external_scope_fingerprint": _external_scope_fingerprint(provider, snapshot),
    }


def _structured_chat_with_temporary_ledger(schema, prompt, payload, *, on_request_start=None):
    """Run one probe against an isolated ledger, never the operational ledger."""
    from src.event_ledger import bind_event, initialize_ledger, record_opportunity
    from src.agents.llm_client import structured_chat

    with tempfile.TemporaryDirectory(prefix="agenttrading-provider-probe-") as directory:
        ledger_path = Path(directory) / "probe.sqlite3"
        initialize_ledger(ledger_path)
        event_id = record_opportunity(
            "DIAGNOSTIC",
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            {"technical_decision": "PROVIDER_CONTRACT_PROBE_ONLY"},
            eligible=False,
            exclusion_reason="PROVIDER_CONTRACT_PROBE_NOT_A_MARKET_OPPORTUNITY",
            strategy_version="LIVE_DIAGNOSTIC_ONLY",
            path=ledger_path,
        )
        prior_log_required = os.environ.get("RESEARCH_LOG_REQUIRED")
        # Unregistered diagnostic schemas must remain ineligible by ledger policy.
        # Persistence is checked from the isolated DB below, so strict market-role
        # registration is neither needed nor appropriate for this probe.
        os.environ["RESEARCH_LOG_REQUIRED"] = "0"
        try:
            with bind_event(
                event_id,
                ledger_path,
                agent_role="UNREGISTERED_SCHEMA",
                agent_stage="DIAGNOSTIC_ONLY",
            ):
                call_options = {"return_metadata": True}
                if on_request_start is not None:
                    call_options["on_request_start"] = on_request_start
                answer, metadata = structured_chat(schema, prompt, payload, **call_options)
        finally:
            if prior_log_required is None:
                os.environ.pop("RESEARCH_LOG_REQUIRED", None)
            else:
                os.environ["RESEARCH_LOG_REQUIRED"] = prior_log_required

        database = sqlite3.connect(ledger_path)
        try:
            persisted = database.execute(
                "SELECT COUNT(*) FROM llm_events WHERE event_id=? AND pydantic_status='VALID'",
                (event_id,),
            ).fetchone()[0]
            stages = dict(database.execute(
                "SELECT event_type,COUNT(*) FROM coverage_events WHERE event_id=? GROUP BY event_type",
                (event_id,),
            ).fetchall())
            eligible = database.execute(
                "SELECT COUNT(*) FROM coverage_events WHERE event_id=? AND eligible=1",
                (event_id,),
            ).fetchone()[0]
            orders = database.execute(
                "SELECT COALESCE(SUM(order_executed),0) FROM llm_events WHERE event_id=?",
                (event_id,),
            ).fetchone()[0]
        finally:
            database.close()
        ledger_result = {
            "status": "PASS" if persisted == 1 and stages.get("LLM_QUERY_PLANNED") == 1
                      and stages.get("LLM_QUERY_SENT") == 1 and stages.get("LLM_RESPONSE_PERSISTED") == 1
                      and eligible == 0 and orders == 0 else "FAIL",
            "isolated_temporary_ledger": True,
            "valid_response_events": int(persisted),
            "planned_queries": int(stages.get("LLM_QUERY_PLANNED", 0)),
            "sent_queries": int(stages.get("LLM_QUERY_SENT", 0)),
            "persisted_responses": int(stages.get("LLM_RESPONSE_PERSISTED", 0)),
            "eligible_events": int(eligible),
            "orders_executed": int(orders),
        }
        return answer, metadata, ledger_result


def _deepseek_preflight(schema, prompt, payload, *, model, max_output_tokens, timeout_seconds):
    """Bound the exact synthetic messages and embedded output schema before one request."""
    from src.agents.deepseek_client import _schema_with_allowed_evidence

    output_schema, _, _ = _schema_with_allowed_evidence(schema, payload)
    instructions = (
        prompt.rstrip()
        + "\n\nReturn ONLY a valid JSON object. No Markdown. "
        + "Follow this JSON Schema exactly, including all required fields:\n"
        + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
    )
    messages = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    serialized_messages = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    message_utf8_bytes = len(serialized_messages.encode("utf-8"))
    framing_allowance_tokens = 64
    input_token_upper_bound = message_utf8_bytes + framing_allowance_tokens
    max_output_tokens = int(max_output_tokens)
    cost_bound = (
        Decimal(input_token_upper_bound) * DEEPSEEK_PEAK_INPUT_USD_PER_MILLION
        + Decimal(max_output_tokens) * DEEPSEEK_PEAK_OUTPUT_USD_PER_MILLION
    ) / Decimal(1_000_000)
    model_verified = model == "deepseek-flash"
    timeouts_safe = 0 < float(timeout_seconds) <= DEEPSEEK_REQUEST_TIMEOUT_SECONDS
    output_limit_safe = 0 < max_output_tokens <= DEEPSEEK_MAX_OUTPUT_TOKENS
    within_budget = cost_bound <= DEEPSEEK_PREFLIGHT_LIMIT_USD
    return {
        "status": "PASS" if model_verified and timeouts_safe and output_limit_safe and within_budget else "PENDING",
        "model": model,
        "model_alias_verified": model_verified,
        "message_utf8_bytes_including_schema": message_utf8_bytes,
        "framing_allowance_tokens": framing_allowance_tokens,
        "input_token_upper_bound": input_token_upper_bound,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": float(timeout_seconds),
        "max_retries": 0,
        "thinking": "disabled",
        "temperature": 0.0,
        "input_price_usd_per_million_peak_uncached": str(DEEPSEEK_PEAK_INPUT_USD_PER_MILLION),
        "output_price_usd_per_million_peak": str(DEEPSEEK_PEAK_OUTPUT_USD_PER_MILLION),
        "maximum_cost_estimate_usd": f"{cost_bound:.9f}",
        "authorization_cap_usd": str(DEEPSEEK_PREFLIGHT_LIMIT_USD),
        "within_budget": within_budget,
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "model_source": "https://api-docs.deepseek.com/api/list-models/",
        "endpoint_source": "https://api-docs.deepseek.com/api/create-chat-completion/",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _usage_cost_estimate(metadata):
    prompt_tokens = metadata.get("prompt_tokens")
    completion_tokens = metadata.get("completion_tokens")
    try:
        prompt_tokens = int(prompt_tokens)
        completion_tokens = int(completion_tokens)
        estimate = (
            Decimal(prompt_tokens) * DEEPSEEK_PEAK_INPUT_USD_PER_MILLION
            + Decimal(completion_tokens) * DEEPSEEK_PEAK_OUTPUT_USD_PER_MILLION
        ) / Decimal(1_000_000)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return {
        "basis": "provider-reported tokens at peak uncached list prices",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": f"{estimate:.9f}",
    }


def _archive_existing_output(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return None
    archive_dir = ROOT / "reports" / "private" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archived = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, archived)
    return str(archived.relative_to(ROOT)).replace("\\", "/")


def _invalid_response_guard_check():
    schema_rejected = False
    try:
        ProbeAnswer.model_validate({"status": "BUY", "evidence_used": ["FORGED_EVIDENCE"]})
    except Exception:
        schema_rejected = True
    evidence_rejected = False
    try:
        from src.agents.llm_client import LLMProviderError, _validate_evidence

        valid_shape = ProbeAnswer.model_validate({"status": "PASS", "evidence_used": ["FORGED_EVIDENCE"]})
        _validate_evidence(valid_shape, ["evidence_used"], ["LIVE_PROBE"])
    except LLMProviderError as exc:
        evidence_rejected = exc.code == "LLM_INVALID_EVIDENCE"
    except Exception:
        evidence_rejected = False
    return {
        "status": "PASS" if schema_rejected and evidence_rejected else "FAIL",
        "invalid_schema_rejected": schema_rejected,
        "invalid_evidence_rejected": evidence_rejected,
        "operational_signal_created": False,
    }


def validate(provider, *, authorize_deepseek=False):
    provider = provider.lower()
    report = {
        "phase": "FASE 1.1 LLM LIVE",
        "provider": provider,
        "status": "PENDING",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "calls_made": 0,
        "api_key_logged": False,
        "market_order_sent": False,
        "operational_signal_created": False,
        "research_ledger": {"status": "PENDING"},
        "invalid_response_guard": _invalid_response_guard_check(),
        "traceability": _traceability(provider),
    }
    if report["invalid_response_guard"]["status"] != "PASS":
        report["status"] = "FAIL"
        report["reason"] = "INVALID_RESPONSE_GUARD_FAILED"
        return report

    if provider == "deepseek":
        if not authorize_deepseek:
            report["reason"] = "DeepSeek call requires --authorize-deepseek for this invocation."
            return report
        if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            report["reason"] = "DEEPSEEK_API_KEY is unavailable in the process environment."
            return report
        os.environ["DEEPSEEK_MAX_TOKENS"] = str(DEEPSEEK_MAX_OUTPUT_TOKENS)
        os.environ["DEEPSEEK_REASONING_EFFORT"] = "low"
        os.environ["DEEPSEEK_TIMEOUT_SECONDS"] = str(DEEPSEEK_REQUEST_TIMEOUT_SECONDS)
    os.environ["LLM_PROVIDER"] = provider

    from src.agents.llm_client import LLMProviderError, get_llm_configuration, structured_chat

    try:
        configuration = get_llm_configuration(ProbeAnswer)
    except LLMProviderError as exc:
        report["status"] = "FAIL"
        report["reason"] = exc.code
        return report
    report["configuration"] = configuration.public_dict()

    if provider == "deepseek":
        report["configuration"]["max_retries"] = 0
        report["configuration"]["thinking"] = "disabled"
        preflight = _deepseek_preflight(
            ProbeAnswer,
            (
                "This is a read-only provider integration diagnostic, not a trading request. "
                "Return status PASS and evidence_used containing only LIVE_PROBE. "
                "Do not return a trading recommendation."
            ),
            {
                "request_type": "provider_contract_probe",
                "allowed_evidence_ids": ["LIVE_PROBE"],
                "expected_status": "PASS",
            },
            model=configuration.model,
            max_output_tokens=configuration.max_tokens,
            timeout_seconds=configuration.timeout_seconds,
        )
        report["preflight"] = preflight
        if preflight["status"] != "PASS":
            report["status"] = "PENDING"
            report["reason"] = (
                "PENDING_PRICE_UNVERIFIED" if not preflight["model_alias_verified"]
                else "DEEPSEEK_PREFLIGHT_LIMIT_OR_TIMEOUT_FAILED"
            )
            return report

    if provider == "ollama":
        try:
            from src.agents.llm_client import _ollama_client

            client = _ollama_client(os.environ.get("OLLAMA_HOST") or None, configuration.timeout_seconds)
            listing = client.list()
            models = listing.get("models", []) if isinstance(listing, dict) else getattr(listing, "models", [])
            installed = {
                str(item.get("model") or item.get("name") or "") if isinstance(item, dict)
                else str(getattr(item, "model", None) or getattr(item, "name", ""))
                for item in models
            }
            report["model_available"] = configuration.model in installed
            if not report["model_available"]:
                report["status"] = "FAIL"
                report["reason"] = "Configured Ollama model is not available locally."
                report["installed_models"] = sorted(name for name in installed if name)
                return report
        except Exception as exc:
            report["reason"] = f"Ollama service/model listing unavailable ({type(exc).__name__})."
            return report

    payload = {
        "request_type": "provider_contract_probe",
        "allowed_evidence_ids": ["LIVE_PROBE"],
        "expected_status": "PASS",
    }
    prompt = (
        "This is a read-only provider integration diagnostic, not a trading request. "
        "Return status PASS and evidence_used containing only LIVE_PROBE. "
        "Do not return a trading recommendation."
    )
    attempts = {"count": 0}

    def mark_provider_request_start():
        attempts["count"] += 1

    try:
        answer, metadata, ledger_result = _structured_chat_with_temporary_ledger(
            ProbeAnswer,
            prompt,
            payload,
            on_request_start=mark_provider_request_start if provider == "deepseek" else None,
        )
        report["research_ledger"] = ledger_result
        report["calls_made"] = attempts["count"] if provider == "deepseek" else 1
        report["response"] = {
            "status": answer.status,
            "evidence_used": answer.evidence_used,
        }
        report["provider_metadata"] = metadata
        reported_model = metadata.get("provider_reported_model")
        model_matches = reported_model == configuration.model
        report["model_matches_configuration"] = model_matches
        finish_reason_ok = provider != "deepseek" or metadata.get("finish_reason") == "stop"
        if provider == "deepseek":
            report["usage_cost_estimate"] = _usage_cost_estimate(metadata)
        report["status"] = "PASS" if model_matches and answer.status == "PASS" and ledger_result.get("status") == "PASS" and finish_reason_ok else "FAIL"
        if not model_matches:
            report["reason"] = "Provider did not report the configured model exactly."
        elif answer.evidence_used != ["LIVE_PROBE"]:
            report["status"] = "FAIL"
            report["reason"] = "Response evidence does not match the permitted probe ID."
        elif not finish_reason_ok:
            report["status"] = "FAIL"
            report["reason"] = "Provider response did not end with finish_reason=stop."
    except LLMProviderError as exc:
        report["calls_made"] = attempts["count"] if provider == "deepseek" else 1
        report["status"] = "FAIL"
        report["reason"] = exc.code
        report["research_ledger"] = {"status": "FAIL", "reason": exc.code}
    except Exception as exc:
        report["calls_made"] = attempts["count"] if provider == "deepseek" else 1
        report["status"] = "FAIL"
        report["reason"] = f"Provider request failed ({type(exc).__name__}); no retry was attempted."
        report["research_ledger"] = {"status": "FAIL", "reason": "PROBE_LEDGER_OR_VALIDATION_ERROR"}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ollama", "deepseek"), required=True)
    parser.add_argument(
        "--authorize-deepseek",
        action="store_true",
        help="Explicitly authorize one low-token DeepSeek API probe for this command only.",
    )
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    report = validate(args.provider, authorize_deepseek=args.authorize_deepseek)
    default_output = ROOT / "reports" / f"phase1_llm_live_{args.provider}.json"
    output = args.output or default_output
    command = [sys.executable, "scripts/validate_phase1_llm_live.py", "--provider", args.provider]
    if args.authorize_deepseek:
        command.append("--authorize-deepseek")
    if args.output:
        command += ["--output", args.output]
    report["command_executed"] = command
    report["previous_report_archive"] = _archive_existing_output(output)
    _write(report, output)
    print(json.dumps({
        "report": str(output),
        "provider": args.provider,
        "status": report["status"],
        "calls_made": report["calls_made"],
        "api_key_logged": False,
        "market_order_sent": False,
    }, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2 if report["status"] == "PENDING" else 1


if __name__ == "__main__":
    raise SystemExit(main())
