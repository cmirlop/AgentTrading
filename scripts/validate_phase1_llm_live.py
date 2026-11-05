#!/usr/bin/env python3
"""One-request, read-only live check for Ollama or explicitly authorized DeepSeek."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ProbeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern="^PASS$")
    evidence_used: list[str]


def _write(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _traceability():
    from scripts.validate_phase1 import _source_snapshot

    return {"validation_fingerprint": _source_snapshot()}


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
        "invalid_response_guard": _invalid_response_guard_check(),
        "traceability": _traceability(),
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
        os.environ["DEEPSEEK_MAX_TOKENS"] = "256"
        os.environ["DEEPSEEK_REASONING_EFFORT"] = "low"
    os.environ["LLM_PROVIDER"] = provider

    from src.agents.llm_client import LLMProviderError, get_llm_configuration, structured_chat

    try:
        configuration = get_llm_configuration(ProbeAnswer)
    except LLMProviderError as exc:
        report["status"] = "FAIL"
        report["reason"] = exc.code
        return report
    report["configuration"] = configuration.public_dict()

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
    try:
        answer, metadata = structured_chat(
            ProbeAnswer,
            prompt,
            payload,
            return_metadata=True,
        )
        report["calls_made"] = 1
        report["response"] = {
            "status": answer.status,
            "evidence_used": answer.evidence_used,
        }
        report["provider_metadata"] = metadata
        reported_model = metadata.get("provider_reported_model")
        model_matches = reported_model == configuration.model
        report["model_matches_configuration"] = model_matches
        report["status"] = "PASS" if model_matches and answer.status == "PASS" else "FAIL"
        if not model_matches:
            report["reason"] = "Provider did not report the configured model exactly."
        elif answer.evidence_used != ["LIVE_PROBE"]:
            report["status"] = "FAIL"
            report["reason"] = "Response evidence does not match the permitted probe ID."
    except LLMProviderError as exc:
        report["calls_made"] = 1
        report["status"] = "FAIL"
        report["reason"] = exc.code
    except Exception as exc:
        report["calls_made"] = 1
        report["status"] = "FAIL"
        report["reason"] = f"Provider request failed ({type(exc).__name__}); no retry was attempted."
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
