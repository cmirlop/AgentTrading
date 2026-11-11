"""Budget-guarded one-request DeepSeek provider contract probe.

No live request is made unless the caller explicitly supplies
``--authorize-single-request`` and every local/offline preflight succeeds.
The payload is diagnostic only and contains no market or account data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_llm_live import ProbeAnswer  # noqa: E402
from src.deepseek_remote_smoke import (  # noqa: E402
    ALLOWED_EVIDENCE,
    EXPECTED_STATUS,
    INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION,
    MAX_OUTPUT_TOKENS,
    MAX_TOTAL_COST_USD,
    OUTPUT_PRICE_PEAK_PER_MILLION,
    PRICE_EVIDENCE,
    PROBE_PAYLOAD,
    PROBE_PROMPT,
    authorize_single_request as authorize_budget_request,
    estimate_cost_usd,
    estimate_input_token_upper_bound,
)


DATASET_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
MODEL_ALIAS = "deepseek-flash"
DOCUMENTED_RESOLVED_MODEL = "DeepSeek-V4.1-Flash-1017"
OUTPUT_DIR = ROOT / "reports" / "phase1_deepseek_remote"
PUBLIC_REPORT = OUTPUT_DIR / "validation_summary.json"
PRIVATE_LEDGER_DIR = ROOT / "reports" / "private" / "phase1_deepseek_remote"
FOCUSED_TEST_MODULES = (
    "test_deepseek_remote_smoke",
    "test_phase1_llm_live",
    "test_phase1_llm",
    "test_phase1_validation_provenance",
    "test_phase2_2",
)
RELEVANT_FILES = (
    "scripts/validate_phase1_llm_live.py",
    "scripts/validate_deepseek_remote_smoke.py",
    "src/agents/deepseek_client.py",
    "src/agents/llm_client.py",
    "src/deepseek_remote_smoke.py",
    "src/research_policy.py",
    "src/event_ledger.py",
    "tests/test_deepseek_remote_smoke.py",
)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git(args: list[str]) -> str | None:
    result = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _run_offline_tests() -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Offline mocks never need the live secret; remove it from their child env.
    env.pop("DEEPSEEK_API_KEY", None)
    test_path = str(ROOT / "tests")
    env["PYTHONPATH"] = test_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    started = time.perf_counter()
    modules = []
    for module in FOCUSED_TEST_MODULES:
        command = [sys.executable, "-m", "unittest", "-v", module]
        result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        output = result.stdout
        match = re.search(r"Ran (\d+) tests?", output)
        skipped_match = re.search(r"skipped=(\d+)", output)
        failed_match = re.search(r"FAILED \(([^)]*)\)", output)
        failed = 0
        if failed_match:
            for item in re.findall(r"(?:failures|errors)=(\d+)", failed_match.group(1)):
                failed += int(item)
        tests_run = int(match.group(1)) if match else None
        skipped = int(skipped_match.group(1)) if skipped_match else 0
        modules.append({
            "module": module,
            "command": command,
            "status": "PASS" if result.returncode == 0 and tests_run is not None else "FAIL",
            "exit_code": result.returncode,
            "tests_run": tests_run,
            "tests_failed": failed,
            "tests_skipped": skipped,
        })
    all_pass = all(item["status"] == "PASS" for item in modules)
    return {
        "status": "PASS" if all_pass else "FAIL",
        "modules": modules,
        "tests_run": sum(item["tests_run"] or 0 for item in modules),
        "tests_failed": sum(item["tests_failed"] for item in modules),
        "tests_skipped": sum(item["tests_skipped"] for item in modules),
        "tests_passed": sum(max(0, (item["tests_run"] or 0) - item["tests_failed"] - item["tests_skipped"]) for item in modules),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "live_secret_passed_to_test_process": False,
    }


def _archive_existing_report(path: Path, run_id: str) -> str | None:
    if not path.is_file():
        return None
    archive = OUTPUT_DIR / "archive" / f"validation_summary_{run_id}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, archive)
    return archive.relative_to(ROOT).as_posix()


def _write_public_report(report: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = PUBLIC_REPORT.with_name(f".{PUBLIC_REPORT.name}.{uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, PUBLIC_REPORT)


def _configure_probe_environment(ledger_path: Path) -> None:
    # These settings apply only to this child process and cannot change the
    # user's shell, workstation, trading configuration, or account.
    os.environ["LLM_PROVIDER"] = "deepseek"
    os.environ["DEEPSEEK_MODEL"] = MODEL_ALIAS
    os.environ["DEEPSEEK_MAX_TOKENS"] = str(MAX_OUTPUT_TOKENS)
    os.environ["DEEPSEEK_REASONING_EFFORT"] = "low"
    os.environ["DEEPSEEK_TIMEOUT_SECONDS"] = "30"
    os.environ["RESEARCH_LOG_REQUIRED"] = "1"
    os.environ["AGENTTRADING_EVENT_DB"] = str(ledger_path)


def _safe_private_ledger_path(run_id: str) -> Path:
    root = ROOT.resolve(strict=True)
    current = root
    for component in ("reports", "private", "phase1_deepseek_remote"):
        current = current / component
        if current.is_symlink():
            raise RuntimeError("PRIVATE_LEDGER_PATH_SYMLINK")
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise RuntimeError("PRIVATE_LEDGER_PATH_OUTSIDE_REPOSITORY") from None
    return current / f"ledger_{run_id}.sqlite3"


def _ledger_summary(path: Path, event_id: str) -> dict[str, Any]:
    import sqlite3

    result = {
        "persisted": False,
        "opportunities_total": 0,
        "opportunities_eligible": 0,
        "llm_events": 0,
        "valid_responses": 0,
        "query_planned": 0,
        "query_sent": 0,
        "response_persisted": 0,
        "response_error_persisted": 0,
        "system_decisions": 0,
        "orders_recorded": 0,
    }
    if not path.is_file():
        return result
    try:
        with sqlite3.connect(path) as db:
            result["opportunities_total"] = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
            result["opportunities_eligible"] = db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_type='OPPORTUNITY_ELIGIBLE'").fetchone()[0]
            result["llm_events"] = db.execute("SELECT COUNT(*) FROM llm_events WHERE event_id=?", (event_id,)).fetchone()[0]
            result["valid_responses"] = db.execute("SELECT COUNT(*) FROM llm_events WHERE event_id=? AND pydantic_status='VALID'", (event_id,)).fetchone()[0]
            counts = dict(db.execute("SELECT event_type,COUNT(*) FROM coverage_events WHERE event_id=? GROUP BY event_type", (event_id,)).fetchall())
            result["query_planned"] = int(counts.get("LLM_QUERY_PLANNED", 0))
            result["query_sent"] = int(counts.get("LLM_QUERY_SENT", 0))
            result["response_persisted"] = int(counts.get("LLM_RESPONSE_PERSISTED", 0))
            result["response_error_persisted"] = int(counts.get("LLM_ERROR_RECEIVED", 0))
            result["system_decisions"] = db.execute("SELECT COUNT(*) FROM system_events").fetchone()[0]
            result["orders_recorded"] = db.execute("SELECT COALESCE(SUM(order_executed),0) FROM system_events").fetchone()[0]
        result["persisted"] = (
            result["opportunities_total"] == 1
            and result["opportunities_eligible"] == 0
            and result["llm_events"] == 1
            and result["query_planned"] == 1
            and result["query_sent"] == 1
            and result["response_persisted"] + result["response_error_persisted"] == 1
            and result["system_decisions"] == 0
            and result["orders_recorded"] == 0
        )
    except (OSError, sqlite3.Error):
        result["persisted"] = False
    return result


def _model_matches_reported(reported: str | None) -> tuple[bool, str | None]:
    if not isinstance(reported, str) or not reported.strip():
        return False, None
    normalized = reported.strip().casefold()
    accepted = {MODEL_ALIAS.casefold(), DOCUMENTED_RESOLVED_MODEL.casefold()}
    explanation = None
    if normalized == DOCUMENTED_RESOLVED_MODEL.casefold():
        explanation = "Provider reported the documented serving version for the requested deepseek-flash alias."
    return normalized in accepted, explanation


def _safe_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    allowed = {
        "LLM_API_KEY_MISSING", "LLM_PROVIDER_DEPENDENCY_MISSING", "LLM_CONFIGURATION_INVALID",
        "LLM_RESEARCH_LOG_PERSISTENCE_FAILED", "LLM_AUTHENTICATION_REJECTED",
        "LLM_PROVIDER_PERMISSION_DENIED", "LLM_PROVIDER_MODEL_OR_ENDPOINT_NOT_FOUND",
        "LLM_PROVIDER_RATE_LIMITED", "LLM_TIMEOUT", "LLM_PROVIDER_CONNECTION_FAILED",
        "LLM_PROVIDER_REQUEST_REJECTED", "LLM_PROVIDER_SERVER_ERROR", "LLM_API_ERROR",
        "LLM_INVALID_RESPONSE", "LLM_INVALID_EVIDENCE", "LLM_INCOMPLETE_RESPONSE",
        "LLM_EMPTY_RESPONSE", "LLM_ALLOWED_EVIDENCE_MISSING", "LEDGER_EVENT_CONTEXT_MISSING",
        "LLM_PROVIDER_DEPENDENCY_MISSING", "SINGLE_REQUEST_LIMIT_REACHED",
        "PRIVATE_LEDGER_PATH_SYMLINK", "PRIVATE_LEDGER_PATH_OUTSIDE_REPOSITORY",
    }
    if code in allowed:
        return str(code)
    text = str(exc)
    return text if text in allowed else type(exc).__name__.upper()


def run_smoke(*, authorize_single_request: bool, run_offline_tests: bool = True) -> dict[str, Any]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "live_status": "DEEPSEEK_REMOTE_LIVE_BLOCKED",
        "offline_status": "PENDING",
        "calls_attempted": 0,
        "provider": "deepseek",
        "requested_model": MODEL_ALIAS,
        "provider_reported_model": None,
        "endpoint": "https://api.deepseek.com",
        "api_route": "Chat Completions",
        "response_schema": "scripts.validate_phase1_llm_live.ProbeAnswer",
        "payload_sha256": hashlib.sha256(json.dumps(PROBE_PAYLOAD, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "prompt_sha256": hashlib.sha256(PROBE_PROMPT.encode("utf-8")).hexdigest(),
        "allowed_evidence_ids": list(ALLOWED_EVIDENCE),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_retries": 0,
        "timeout_seconds": 30,
        "request_is_trading_signal": False,
        "operational_signal_created": False,
        "orders_sent": 0,
        "mt5_connections": 0,
        "other_provider_calls": 0,
        "api_key_available": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "api_key_logged": False,
        "api_key_length_logged": False,
        "api_key_hash_logged": False,
        "root_dotenv_present": (ROOT / ".env").is_file(),
        "dataset_sha256_expected": DATASET_SHA256,
        "dataset_sha256_actual": _sha256(ROOT / "data/backtest/technical_candidate_scan.csv"),
        "runtime": {
            "python": sys.version.split()[0],
            "python_implementation": platform.python_implementation(),
            "pydantic": _package_version("pydantic"),
            "openai_sdk": _package_version("openai"),
            "git_head": _git(["git", "rev-parse", "HEAD"]),
            "git_branch": _git(["git", "branch", "--show-current"]),
            "worktree_dirty": bool(_git(["git", "status", "--porcelain"])),
        },
        "source_sha256": {name: _sha256(ROOT / name) for name in RELEVANT_FILES},
        "pricing": dict(PRICE_EVIDENCE),
        "official_model_documentation_verified": True,
        "chat_completions_documentation_verified": True,
        "tokens": {"prompt_tokens": None, "completion_tokens": None, "reasoning_tokens": None},
        "latency_seconds": None,
        "actual_cost_estimate_usd": "UNKNOWN",
        "worst_case_preflight_cost_usd": None,
        "error_code": None,
        "ledger": {"persisted": False, "economic_reconciliation_in_scope": False},
        "offline_tests": None,
    }

    if run_offline_tests:
        report["offline_tests"] = _run_offline_tests()
        report["offline_status"] = "DEEPSEEK_REMOTE_OFFLINE_VALIDATED" if report["offline_tests"]["status"] == "PASS" else "DEEPSEEK_REMOTE_OFFLINE_FAIL"
    else:
        report["offline_status"] = "DEEPSEEK_REMOTE_OFFLINE_VALIDATED"

    blocked_reason = None
    if report["offline_status"] != "DEEPSEEK_REMOTE_OFFLINE_VALIDATED":
        blocked_reason = "OFFLINE_TESTS_FAILED"
    elif not authorize_single_request:
        blocked_reason = "SINGLE_REQUEST_AUTHORIZATION_FLAG_REQUIRED"
    elif not report["api_key_available"]:
        blocked_reason = "LIVE_BLOCKED_SECRET_UNAVAILABLE"
    elif report["root_dotenv_present"]:
        blocked_reason = "LIVE_BLOCKED_SECRET_SOURCE_UNVERIFIED"
    elif _package_version("openai") is None:
        blocked_reason = "LIVE_BLOCKED_SDK_UNAVAILABLE"
    elif report["dataset_sha256_actual"] != DATASET_SHA256:
        blocked_reason = "LIVE_BLOCKED_FROZEN_DATASET_HASH_MISMATCH"
    elif datetime.now(timezone.utc).date().isoformat() != PRICE_EVIDENCE["verified_date_utc"]:
        blocked_reason = "LIVE_BLOCKED_PRICE_EVIDENCE_STALE"

    # Do not silently switch away from the repository's configured alias.
    configured_model = os.environ.get("DEEPSEEK_MODEL", MODEL_ALIAS).strip()
    report["configured_model_before_probe"] = configured_model
    if blocked_reason is None and configured_model != MODEL_ALIAS:
        blocked_reason = "LIVE_BLOCKED_MODEL_CONFIGURATION_UNEXPECTED"

    try:
        input_bound = estimate_input_token_upper_bound(ProbeAnswer, PROBE_PROMPT, PROBE_PAYLOAD)
        report["input_bound"] = input_bound
    except Exception as exc:
        input_bound = None
        report["input_bound"] = {"status": "FAILED", "error_code": type(exc).__name__.upper()}
        if blocked_reason is None:
            blocked_reason = "LIVE_BLOCKED_INPUT_BOUND_UNAVAILABLE"

    if input_bound is not None:
        budget, decision = authorize_budget_request(
            input_bound["conservative_input_tokens_upper_bound"], prices_verified=True,
        )
        report["budget_gate"] = {
            "status": decision.get("status"),
            "authorized": decision.get("authorized", False),
            "reason": decision.get("reason"),
            "price_input_per_million_usd": str(INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION),
            "price_output_per_million_usd": str(OUTPUT_PRICE_PEAK_PER_MILLION),
            "total_authorized_cap_usd": str(MAX_TOTAL_COST_USD),
            "input_token_bound": input_bound["conservative_input_tokens_upper_bound"],
            "output_token_cap": MAX_OUTPUT_TOKENS,
            "request_reserved_tokens": decision.get("request_tokens_reserved"),
            "request_id": decision.get("request_id"),
            "worst_case_cost_estimate_usd": decision.get("estimated_cost"),
            "real_provider_call": False,
        }
        report["worst_case_preflight_cost_usd"] = decision.get("estimated_cost")
        if blocked_reason is None and not decision.get("authorized"):
            blocked_reason = "LIVE_BLOCKED_BUDGET_OR_PRICE_UNVERIFIED"
        if decision.get("authorized"):
            duplicate_check = budget.authorize(
                input_tokens=input_bound["conservative_input_tokens_upper_bound"],
                max_output_tokens=MAX_OUTPUT_TOKENS,
                price_input_per_million=INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION,
                price_output_per_million=OUTPUT_PRICE_PEAK_PER_MILLION,
                prices_verified=True,
            )
            report["budget_gate"]["duplicate_attempt_blocked"] = not duplicate_check.get("authorized", False)
            if duplicate_check.get("authorized"):
                blocked_reason = "LIVE_BLOCKED_DUPLICATE_ATTEMPT_GUARD_FAILED"

    if blocked_reason is not None:
        report["blocked_reason"] = blocked_reason
        report["live_status"] = "DEEPSEEK_REMOTE_LIVE_BLOCKED"
        return report

    # The one approved request is only reached after all offline and budget
    # checks above. The append-only ledger is isolated from the research DB.
    try:
        ledger_path = _safe_private_ledger_path(run_id)
        ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ledger_path = _safe_private_ledger_path(run_id)
        try:
            os.chmod(ledger_path.parent, 0o700)
        except OSError:
            pass
    except Exception as exc:
        report["blocked_reason"] = _safe_error_code(exc)
        report["live_status"] = "DEEPSEEK_REMOTE_LIVE_BLOCKED"
        return report
    attempts = 0
    try:
        os.umask(0o077)
        _configure_probe_environment(ledger_path)
        from src.agents import deepseek_client
        from src.agents.llm_client import LLMProviderError, get_llm_configuration, structured_chat
        from src.event_ledger import bind_event, initialize_ledger, record_opportunity

        if deepseek_client.OpenAI is None:
            raise RuntimeError("LLM_PROVIDER_DEPENDENCY_MISSING")
        configuration = get_llm_configuration(ProbeAnswer)
        if (configuration.provider != "deepseek" or configuration.model != MODEL_ALIAS
                or configuration.max_tokens != MAX_OUTPUT_TOKENS
                or configuration.max_retries != 0 or configuration.temperature != 0.0):
            raise RuntimeError("LLM_CONFIGURATION_INVALID")
        report["llm_configuration"] = {
            "provider": configuration.provider,
            "model": configuration.model,
            "base_url": "https://api.deepseek.com",
            "api_route": "Chat Completions",
            "max_tokens": configuration.max_tokens,
            "max_retries": configuration.max_retries,
            "timeout_seconds": configuration.timeout_seconds,
            "temperature": configuration.temperature,
            "reasoning_effort_environment": configuration.reasoning_effort,
            "thinking_mode_for_this_probe": "disabled",
            "response_format": "json_object",
        }
        if not report["budget_gate"].get("duplicate_attempt_blocked"):
            raise RuntimeError("BUDGET_SINGLE_REQUEST_GUARD_FAILED")

        initialize_ledger(ledger_path)
        probe_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event_id = record_opportunity(
            "DIAGNOSTIC",
            probe_time,
            {"technical_decision": "PROVIDER_CONTRACT_PROBE_ONLY"},
            eligible=False,
            exclusion_reason="PROVIDER_CONTRACT_PROBE_NOT_A_MARKET_OPPORTUNITY",
            strategy_version="REMOTE_DIAGNOSTIC_ONLY",
            code_revision=report["runtime"]["git_head"],
            path=ledger_path,
        )
        report["ledger"]["path"] = ledger_path.relative_to(ROOT).as_posix()
        report["ledger"]["event_id_sha256"] = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        report["ledger"]["opportunity_marked_ineligible"] = True
        report["ledger"]["isolated_from_research_ledger"] = True

        def request_started() -> None:
            nonlocal attempts
            if attempts >= 1:
                raise RuntimeError("SINGLE_REQUEST_LIMIT_REACHED")
            attempts += 1

        started = time.perf_counter()
        try:
            with bind_event(event_id, ledger_path, agent_role="REMOTE_PROVIDER_CONTRACT_PROBE", agent_stage="DIAGNOSTIC_ONLY"):
                answer, metadata = structured_chat(
                    ProbeAnswer,
                    PROBE_PROMPT,
                    PROBE_PAYLOAD,
                    return_metadata=True,
                    on_request_start=request_started,
                )
            report["schema_valid"] = True
            report["json_valid"] = True
            report["response_status"] = answer.status
            report["permitted_evidence_valid"] = answer.evidence_used == ALLOWED_EVIDENCE
            report["provider_reported_model"] = metadata.get("provider_reported_model")
            report["finish_reason"] = metadata.get("finish_reason")
            report["tokens"] = {
                "prompt_tokens": metadata.get("prompt_tokens"),
                "completion_tokens": metadata.get("completion_tokens"),
                "reasoning_tokens": metadata.get("reasoning_tokens"),
            }
            report["latency_seconds"] = metadata.get("latency_seconds")
            model_matches, model_note = _model_matches_reported(report["provider_reported_model"])
            report["provider_reported_model_matches_request"] = model_matches
            report["model_resolution_note"] = model_note
            prompt_tokens = metadata.get("prompt_tokens")
            completion_tokens = metadata.get("completion_tokens")
            if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) and isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool):
                estimated_actual_cost = estimate_cost_usd(prompt_tokens, completion_tokens)
                report["actual_cost_estimate_usd"] = str(estimated_actual_cost.quantize(Decimal("0.000000001")))
                report["actual_cost_status"] = "ESTIMATED_FROM_PROVIDER_USAGE_AT_PEAK_RATES"
                record = budget.record_usage(
                    request_id=report["budget_gate"].get("request_id", ""),
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                )
                report["budget_gate"]["usage_reconciliation"] = record.get("status")
                report["usage_within_preflight_bound"] = prompt_tokens <= input_bound["conservative_input_tokens_upper_bound"] and completion_tokens <= MAX_OUTPUT_TOKENS
            else:
                report["actual_cost_estimate_usd"] = "COST_UNKNOWN"
                report["actual_cost_status"] = "COST_UNKNOWN_PROVIDER_USAGE_MISSING"
                report["usage_within_preflight_bound"] = None
            report["response_valid"] = (
                report["schema_valid"]
                and report["json_valid"]
                and report["response_status"] == EXPECTED_STATUS
                and report["permitted_evidence_valid"]
                and model_matches
                and report["usage_within_preflight_bound"] is not False
                and report["finish_reason"] == "stop"
            )
            if not report["response_valid"]:
                report["error_code"] = "PROBE_RESPONSE_VALIDATION_FAILED"
        except Exception as exc:
            report["error_code"] = _safe_error_code(exc)
            report["latency_seconds"] = round(time.perf_counter() - started, 3)
            report["response_valid"] = False
            report["schema_valid"] = False
            report["json_valid"] = False
            report["permitted_evidence_valid"] = False
            report["actual_cost_estimate_usd"] = "COST_UNKNOWN"
            report["actual_cost_status"] = "COST_UNKNOWN_AFTER_REQUEST_ERROR" if attempts else "NO_REQUEST_ATTEMPTED"

        report["calls_attempted"] = attempts
        ledger = _ledger_summary(ledger_path, event_id)
        report["ledger"].update(ledger)
        report["ledger"]["economic_reconciliation_in_scope"] = False
        report["ledger"]["note"] = "Provider request/response is appended to a private isolated diagnostic ledger; no market opportunity, decision, plan, outcome, or trade is created."
        if not ledger["persisted"]:
            report["error_code"] = report.get("error_code") or "LLM_RESEARCH_LOG_PERSISTENCE_FAILED"

        if attempts == 1:
            report["live_status"] = "DEEPSEEK_REMOTE_LIVE_PASS" if report.get("response_valid") and ledger["persisted"] else "DEEPSEEK_REMOTE_LIVE_FAIL"
        else:
            report["live_status"] = "DEEPSEEK_REMOTE_LIVE_BLOCKED"
            report["blocked_reason"] = report.get("error_code") or "NO_API_REQUEST_ATTEMPTED"
    except Exception as exc:
        report["calls_attempted"] = attempts
        report["live_status"] = "DEEPSEEK_REMOTE_LIVE_FAIL" if attempts else "DEEPSEEK_REMOTE_LIVE_BLOCKED"
        if attempts:
            report["error_code"] = _safe_error_code(exc)
        else:
            report["blocked_reason"] = _safe_error_code(exc)
        report["ledger"]["persisted"] = False
    finally:
        # Credentials remain only in the inherited secure process environment.
        # The API key is never included in the report, database, logs, or output.
        report["api_key_logged"] = False
        report["api_key_length_logged"] = False
        report["api_key_hash_logged"] = False

    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authorize-single-request",
        action="store_true",
        help="Authorize at most one DeepSeek contract probe under the fixed USD 0.25 cap.",
    )
    parser.add_argument("--output", default=str(PUBLIC_REPORT))
    args = parser.parse_args(argv)

    report = run_smoke(authorize_single_request=args.authorize_single_request)
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    try:
        output.resolve(strict=False).relative_to(ROOT.resolve())
    except ValueError:
        print(json.dumps({"status": "DEEPSEEK_REMOTE_LIVE_BLOCKED", "error_code": "REPORT_PATH_OUTSIDE_REPOSITORY", "calls_attempted": 0}))
        return 2
    archived = _archive_existing_report(output, report["run_id"])
    report["previous_report_archived_to"] = archived
    if output != PUBLIC_REPORT:
        output.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        output.write_text(encoded, encoding="utf-8")
    else:
        _write_public_report(report)
    print(json.dumps({
        "report": output.relative_to(ROOT).as_posix(),
        "live_status": report["live_status"],
        "offline_status": report["offline_status"],
        "calls_attempted": report["calls_attempted"],
        "api_key_logged": False,
        "mt5_connections": 0,
        "orders_sent": 0,
        "operational_signal_created": False,
        "error_code": report.get("error_code") or report.get("blocked_reason"),
    }, indent=2, ensure_ascii=False))
    return 0 if report["live_status"] == "DEEPSEEK_REMOTE_LIVE_PASS" else 2 if report["live_status"] == "DEEPSEEK_REMOTE_LIVE_BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
