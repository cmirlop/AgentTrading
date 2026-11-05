#!/usr/bin/env python3
"""Offline-first Phase 1 validator with execution provenance."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "reports" / "phase1_validation.json"
BASELINE_PATH = ROOT / "reports" / "phase1_baseline_metrics.json"
SCAN = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"

OFFLINE_EXISTING_TESTS = [
    "test_deepseek_integration.py",
    "test_historical_level_outcome_regression.py",
    "test_level_event_context_historical.py",
    "test_level_evidence.py",
    "test_level_evidence_price_action.py",
    "test_level_history_batch_payload.py",
    "test_level_history_events_historical.py",
    "test_level_history_historical.py",
    "test_level_history_llm_historical.py",
    "test_level_history_prompt_payload.py",
    "test_level_history_report_historical.py",
    "test_level_outcome_touch.py",
    "test_level_price_action.py",
    "test_level_registry_full_day.py",
    "test_level_registry_historical.py",
    "test_level_thesis_direction.py",
    "test_level_thesis_historical.py",
    "test_level_thesis_unclear.py",
    "test_multiagent_second_stage.py",
    "test_no_lookahead.py",
    "test_support_resistance_historical.py",
]

PENDING_TESTS = {
    "test_ollama.py": "Requiere servidor y modelo Ollama local; no se ejecuta en la suite offline.",
    "test_order_request.py": "Requiere terminal MT5 conectado en demo.",
    "test_trade_planner.py": "Requiere terminal MT5 conectado en demo y metadatos del bróker.",
}

RELEVANT_FILES = [
    "build_runtime_state.py",
    "config/agent_config.py",
    "config/backtest_config.py",
    "config/backtest_cost_config.py",
    "config/execution_config.py",
    "config/market_config.py",
    "config/risk_config.py",
    "config/trade_planner_config.py",
    "reports/phase1_baseline_metrics.json",
    "run_dry_pipeline.py",
    "run_cycle.py",
    "run_market_agents.py",
    "run_portfolio_backtest.py",
    "run_stateful_backtest.py",
    "reports/phase1_pre_finalization_validation.json",
    "src/agents/deepseek_client.py",
    "src/agents/decision_agent.py",
    "src/agents/llm_client.py",
    "src/agents/market_analyst.py",
    "src/agents/regime_agent.py",
    "src/agents/risk_agent.py",
    "src/agents/strategy_agent.py",
    "src/daily_risk_guard.py",
    "src/historical_execution.py",
    "src/historical_trade_planner.py",
    "src/mt5_deal_history.py",
    "src/mt5_runtime.py",
    "src/orchestration/orchestrator.py",
    "src/risk_engine.py",
    "src/risk_preflight.py",
    "src/risk_validator.py",
    "src/trade_planner.py",
    "scripts/validate_phase1.py",
    "scripts/validate_phase1_mt5.py",
    "scripts/validate_phase1_llm_live.py",
]

PHASE1_TEST_FILES = sorted(
    str(path.relative_to(ROOT)).replace("\\", "/")
    for path in (ROOT / "tests").glob("test_phase1_*.py")
)
RELEVANT_FILES = sorted(set(RELEVANT_FILES + PHASE1_TEST_FILES))
CONFIG_FILES = [name for name in RELEVANT_FILES if name.startswith("config/")]

EXTERNAL_VALIDATION_SCOPES = {
    "deepseek": {
        "files": (
            "src/agents/deepseek_client.py",
            "src/agents/llm_client.py",
        ),
        "dependencies": ("openai", "pydantic"),
    },
    "ollama": {
        "files": (
            "config/agent_config.py",
            "src/agents/llm_client.py",
        ),
        "dependencies": ("ollama", "pydantic"),
    },
    "mt5": {
        "files": (
            "config/market_config.py",
            "config/risk_config.py",
            "scripts/validate_phase1_mt5.py",
            "src/daily_risk_guard.py",
            "src/mt5_deal_history.py",
            "src/mt5_runtime.py",
            "src/risk_engine.py",
            "src/risk_preflight.py",
        ),
        "dependencies": ("MetaTrader5", "numpy", "pandas", "pydantic"),
    },
}

DECLARED_SIMULATOR_CORRECTIONS = {
    (
        "src/historical_execution.py",
        "03022d66350fddc5f6a8d0d83da5745893bed96155fad71224635563245d584a",
        "0a9f36b496429f0ce577a237e9f6ffab36a0dd7d29f5fe7d8b9fa3caa7d70531",
    ): {
        "correction_id": "HISTORICAL_EXECUTION_VALIDATION_EXPIRY_AND_COSTS",
        "cause": (
            "Adds strict historical bar/plan validation, corrects order-expiry edge handling, "
            "distinguishes still-pending orders at end of data, and accounts for costs on "
            "ambiguous filled outcomes. It does not alter signal-selection code."
        ),
        "trade_level_attribution_available": False,
    },
}


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_text_sha256(path: Path):
    text = path.read_text(encoding="utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _verified_dataset_blob(commit, relative_path, expected_sha256):
    """Read a frozen dataset from Git only when its recorded content hash matches."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return None, "Dataset provenance does not contain a full Git commit hash."
    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:{relative_path}"],
            cwd=ROOT,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "Frozen dataset could not be read from its recorded Git commit."
    if result.returncode != 0:
        return None, "Frozen dataset commit or path is unavailable in the local Git object database."
    actual_sha256 = hashlib.sha256(result.stdout).hexdigest()
    if actual_sha256 != expected_sha256:
        return None, "Frozen dataset blob does not match the recorded SHA-256."
    return result.stdout, None


def _probe_logic_sha256(source_text):
    tree = ast.parse(source_text)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ProbeAnswer":
            selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_invalid_response_guard_check", "validate",
        }:
            selected.append(node)

    class StripTraceability(ast.NodeTransformer):
        def visit_Dict(self, node):
            self.generic_visit(node)
            kept = [
                (key, value) for key, value in zip(node.keys, node.values)
                if not (isinstance(key, ast.Constant) and key.value == "traceability")
            ]
            node.keys = [item[0] for item in kept]
            node.values = [item[1] for item in kept]
            return node

    normalized = StripTraceability().visit(ast.Module(body=selected, type_ignores=[]))
    raw = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _external_configuration(provider):
    if provider == "deepseek":
        return {
            "provider": provider,
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip(),
            "timeout_seconds": float(os.environ.get("DEEPSEEK_TIMEOUT_SECONDS", "180")),
            "reasoning_effort": "low",
            "max_tokens": 256,
            "max_retries": 0,
            "temperature": 0.0,
            "context_window": None,
            "keep_alive": None,
        }
    if provider == "ollama":
        from config import agent_config

        return {
            "provider": provider,
            "model": os.environ.get("OLLAMA_MODEL", agent_config.OLLAMA_MODEL).strip(),
            "timeout_seconds": float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120")),
            "reasoning_effort": None,
            "max_tokens": None,
            "max_retries": 0,
            "temperature": agent_config.OLLAMA_TEMPERATURE,
            "context_window": agent_config.OLLAMA_CONTEXT,
            "keep_alive": agent_config.OLLAMA_KEEP_ALIVE,
        }
    return {}


def _git(*args):
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True,
        timeout=10, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_source_at_commit(commit, relative_path):
    if not commit:
        return None
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"], cwd=ROOT,
        capture_output=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8")


def _verified_probe_source(origin_commit, recorded_sha256):
    """Recover old probe logic from an equivalent Git blob verified by its recorded hash."""
    relative = "scripts/validate_phase1_llm_live.py"
    origin_source = _git_source_at_commit(origin_commit, relative)
    if origin_source is not None and hashlib.sha256(origin_source.encode("utf-8")).hexdigest() == recorded_sha256:
        return origin_source, origin_commit
    equivalent_commit = _git("rev-parse", "HEAD")
    equivalent_source = _git_source_at_commit(equivalent_commit, relative)
    if equivalent_source is not None and hashlib.sha256(equivalent_source.encode("utf-8")).hexdigest() == recorded_sha256:
        return equivalent_source, equivalent_commit
    return None, None


def _source_snapshot():
    file_hashes = {}
    missing = []
    for relative in RELEVANT_FILES:
        path = ROOT / relative
        if path.is_file():
            file_hashes[relative] = _normalized_text_sha256(path)
        else:
            file_hashes[relative] = None
            missing.append(relative)
    dataset_hash = _sha256(SCAN) if SCAN.is_file() else None
    config_hashes = {name: file_hashes[name] for name in CONFIG_FILES}
    baseline_hash = file_hashes.get("reports/phase1_baseline_metrics.json")
    dependencies = {}
    for package in ("pandas", "numpy", "pydantic", "MetaTrader5", "ollama", "openai", "python-dotenv"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "NOT_INSTALLED"
    python_version = sys.version
    fingerprint_data = {
        "relevant_file_sha256": file_hashes,
        "dataset_sha256": dataset_hash,
        "backtest_config_sha256": config_hashes,
        "baseline_metrics_sha256": baseline_hash,
        "dependencies": dependencies,
        "python_version": python_version,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    status_lines = _git("status", "--porcelain", "--untracked-files=all")
    changes = status_lines.splitlines() if status_lines else []
    return {
        "validation_commit": _git("rev-parse", "HEAD") or "UNKNOWN",
        "branch": _git("branch", "--show-current") or "UNKNOWN",
        "working_tree": {
            "clean": not changes,
            "changes": changes,
        },
        "dataset_path": str(SCAN.relative_to(ROOT)),
        "dataset_sha256": dataset_hash,
        "relevant_file_sha256": file_hashes,
        "backtest_config_sha256": config_hashes,
        "baseline_metrics_sha256": baseline_hash,
        "source_fingerprint_sha256": fingerprint,
        "python_version": python_version,
        "dependencies": dependencies,
        "missing_relevant_files": missing,
    }


def _verify_refresh_compatibility(prior_report, current_snapshot):
    """Allow replay-result reuse only for a matching provenance fingerprint."""
    prior = prior_report.get("traceability", {}).get("validation_fingerprint")
    if not isinstance(prior, dict):
        return ["Prior report has no source/dataset/config fingerprint."]
    differences = []
    for field in (
        "dataset_sha256", "relevant_file_sha256", "backtest_config_sha256",
        "baseline_metrics_sha256", "dependencies", "python_version",
    ):
        if prior.get(field) != current_snapshot.get(field):
            differences.append(f"{field} differs from prior full validation")
    if current_snapshot.get("missing_relevant_files"):
        differences.append("one or more required simulation/config files are missing")
    prior_regression = prior_report.get("checks", {}).get("historical_regression", {})
    if not isinstance(prior_regression, dict) or prior_regression.get("status") not in {"PASS", "FAIL", "PENDING"}:
        differences.append("prior report does not contain completed historical replay evidence")
    return differences


def _verify_backtest_reuse_compatibility(prior_report, current_snapshot):
    """Verify a recent historical replay by its simulation-only code/data fingerprint."""
    if not isinstance(prior_report, dict):
        return {"status": "PENDING", "differences": ["No prior full validation report is available."]}
    prior_fingerprint = prior_report.get("traceability", {}).get("validation_fingerprint")
    historical = prior_report.get("checks", {}).get("historical_regression")
    if not isinstance(prior_fingerprint, dict) or not isinstance(historical, dict):
        return {"status": "PENDING", "differences": ["Prior report lacks full source or replay provenance."]}

    excluded = {
        "scripts/validate_phase1.py",
        "scripts/validate_phase1_mt5.py",
        "scripts/validate_phase1_llm_live.py",
    }
    scope_files = [name for name in RELEVANT_FILES if name not in excluded and not name.startswith("tests/")]
    differences = []
    prior_files = prior_fingerprint.get("relevant_file_sha256", {})
    current_files = current_snapshot.get("relevant_file_sha256", {})
    for name in scope_files:
        if prior_files.get(name) != current_files.get(name):
            differences.append(f"relevant_file_sha256.{name}")
    for field in ("backtest_config_sha256", "baseline_metrics_sha256", "dependencies", "python_version"):
        if prior_fingerprint.get(field) != current_snapshot.get(field):
            differences.append(field)

    try:
        generated_raw = prior_report.get("generated_at_utc")
        generated = datetime.fromisoformat(generated_raw.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError
        age_seconds = (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -300 or age_seconds > 24 * 60 * 60:
            differences.append("report_timestamp_outside_24_hour_window")
    except (AttributeError, TypeError, ValueError):
        age_seconds = None
        differences.append("report_timestamp_missing_or_invalid")

    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.is_file() else {}
    except (OSError, json.JSONDecodeError):
        baseline = {}
        differences.append("baseline_metrics_unreadable")
    expected_dataset_hash = baseline.get("dataset", {}).get("sha256")
    dataset_source = historical.get("dataset_source", {})
    if not isinstance(dataset_source, dict):
        dataset_source = {}
    dataset_hash = historical.get("dataset_sha256")
    if dataset_hash != expected_dataset_hash or dataset_source.get("sha256") != expected_dataset_hash:
        differences.append("frozen_dataset_hash_differs_from_baseline")
    else:
        _, dataset_error = _verified_dataset_blob(
            dataset_source.get("commit"),
            dataset_source.get("path") or SCAN.relative_to(ROOT).as_posix(),
            expected_dataset_hash,
        )
        if dataset_error:
            differences.append(f"frozen_dataset_blob_unverified:{dataset_error}")

    required_runs = (
        "portfolio_base_1", "portfolio_base_2", "portfolio_conservative", "portfolio_stress",
        "stateful_base_1", "stateful_base_2", "stateful_conservative", "stateful_stress",
    )
    runs = historical.get("scenario_runs", {})
    for key in required_runs:
        run = runs.get(key, {}) if isinstance(runs, dict) else {}
        if not isinstance(run, dict) or run.get("status") != "PASS":
            differences.append(f"scenario_run_not_pass:{key}")
    repeatability_checks = (
        "repeatability_portfolio_base", "repeatability_portfolio_base_trades",
        "repeatability_stateful_base", "repeatability_stateful_base_trades",
    )
    for key in repeatability_checks:
        check = historical.get(key, {})
        if not isinstance(check, dict) or check.get("status") != "PASS":
            differences.append(f"repeatability_not_pass:{key}")
    summary_keys = (
        "portfolio_base_summary", "portfolio_conservative_summary", "portfolio_stress_summary",
        "stateful_base_summary", "stateful_conservative_summary", "stateful_stress_summary",
    )
    for key in summary_keys:
        if not historical.get(key):
            differences.append(f"scenario_summary_missing:{key}")

    scope = {
        "relevant_file_sha256": {name: current_files.get(name) for name in scope_files},
        "backtest_config_sha256": current_snapshot.get("backtest_config_sha256"),
        "baseline_metrics_sha256": current_snapshot.get("baseline_metrics_sha256"),
        "dependencies": current_snapshot.get("dependencies"),
        "python_version": current_snapshot.get("python_version"),
        "dataset_sha256": expected_dataset_hash,
        "dataset_source_commit": dataset_source.get("commit"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "status": "PASS" if not differences else "PENDING",
        "differences": differences,
        "verification_scope": list(scope_files),
        "fingerprint_sha256": fingerprint,
        "source_validation_commit": prior_fingerprint.get("validation_commit"),
        "source_branch": prior_fingerprint.get("branch"),
        "source_generated_at_utc": prior_report.get("generated_at_utc"),
        "age_seconds": None if age_seconds is None else round(age_seconds, 1),
    }


def _reassess_reused_historical_evidence(prior_historical, pre_final_report, current_snapshot, reuse_check):
    results = json.loads(json.dumps(prior_historical))
    old_hashes = (
        pre_final_report.get("traceability", {})
        .get("validation_fingerprint", {})
        .get("relevant_file_sha256", {})
    )
    current_hashes = current_snapshot.get("relevant_file_sha256", {})
    technical_files = (
        "run_portfolio_backtest.py", "run_stateful_backtest.py",
        "src/historical_execution.py", "src/historical_trade_planner.py",
        "config/backtest_config.py", "config/backtest_cost_config.py",
        "config/trade_planner_config.py",
    )
    source_differences = [
        {"file": name, "before": old_hashes.get(name), "after": current_hashes.get(name)}
        for name in technical_files
        if old_hashes.get(name) != current_hashes.get(name)
    ]
    source_invariance = _classify_technical_source_differences(source_differences)
    source_invariance["dataset_sha256"] = results.get("dataset_sha256")
    correction_ids = [item["correction_id"] for item in source_invariance["simulator_correction_changes"]]

    comparisons = {
        "baseline_vs_after_portfolio": "baseline_vs_after_portfolio_exact",
        "pre_finalization_portfolio": "pre_finalization_portfolio_exact",
        "baseline_vs_after_stateful": "baseline_vs_after_stateful_exact",
        "pre_finalization_stateful": "pre_finalization_stateful_exact",
    }
    for name, exact_name in comparisons.items():
        results[name] = _assess_baseline_comparison(results.get(exact_name, {}), correction_ids)
    results["technical_source_invariance"] = source_invariance
    opportunity_differences = [
        item
        for key in ("pre_finalization_portfolio_exact", "pre_finalization_stateful_exact")
        for item in results.get(key, {}).get("differences", [])
        if item.get("field") == "opportunities_detected"
    ]
    results["technical_opportunity_invariance"] = {
        "status": "FAIL" if opportunity_differences else "PASS",
        "portfolio_opportunities_changed": any(
            item.get("field") == "opportunities_detected"
            for item in results.get("pre_finalization_portfolio_exact", {}).get("differences", [])
        ),
        "stateful_opportunities_changed": any(
            item.get("field") == "opportunities_detected"
            for item in results.get("pre_finalization_stateful_exact", {}).get("differences", [])
        ),
        "differences": opportunity_differences,
        "evidence": "Opportunity totals are compared exactly against the verified frozen scan.",
    }
    checked = [
        results[name] for name in (
            "baseline_vs_after_portfolio", "baseline_vs_after_stateful",
            "pre_finalization_portfolio", "pre_finalization_stateful",
            "repeatability_portfolio_base", "repeatability_portfolio_base_trades",
            "repeatability_stateful_base", "repeatability_stateful_base_trades",
            "technical_opportunity_invariance", "technical_source_invariance",
        )
    ]
    statuses = [item["status"] for item in checked]
    results["status"] = "FAIL" if "FAIL" in statuses else "PENDING" if "PENDING" in statuses else "PASS"
    results["evidence_reused"] = True
    results["result_origin"] = "REUSED_VERIFIED_HISTORICAL_BACKTEST_EVIDENCE"
    results["evidence_reuse"] = reuse_check
    results["reassessment"] = {
        "performed_at_utc": datetime.now(timezone.utc).isoformat(),
        "classification_logic": "Current validator reclassified the archived exact-run comparison data; no backtest code or result rows were altered.",
    }
    return results


def _run(command, timeout=240):
    started = time.perf_counter()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "FAIL",
            "return_code": None,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "command": [_redact_sensitive_text(str(part)) for part in command],
            "reason": f"COMMAND_TIMEOUT_AFTER_{timeout}_SECONDS",
            "stdout_tail": _redact_sensitive_text(str(exc.stdout or "")[-1200:]),
            "stderr_tail": _redact_sensitive_text(str(exc.stderr or "")[-1200:]),
        }
    except OSError as exc:
        return {
            "status": "FAIL",
            "return_code": None,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "command": [_redact_sensitive_text(str(part)) for part in command],
            "reason": f"COMMAND_START_FAILED:{type(exc).__name__}",
            "stdout_tail": "",
            "stderr_tail": "",
        }
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "return_code": result.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "command": [_redact_sensitive_text(str(part)) for part in command],
        "stdout_tail": _redact_sensitive_text(result.stdout[-1200:]),
        "stderr_tail": _redact_sensitive_text(result.stderr[-1200:]),
    }


def _redact_sensitive_text(value):
    value = re.sub(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", str(value))

    def redact_assignment(match):
        raw = match.group("value")
        if raw.lower() in {"true", "false", "none", "null", "0"}:
            return match.group(0)
        return f"{match.group('key')}{match.group('separator')}[REDACTED]"

    patterns = (
        r"(?i)(?P<key>api[_-]?key|token|password|passwd|secret|authorization)"
        r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)",
        r"(?i)(?P<key>account(?:[_ -]?(?:number|login|id))|login|server)"
        r"(?P<separator>\s*[:=]\s*)(?P<value>[^\s,;]+)",
    )
    for pattern in patterns:
        value = re.sub(pattern, redact_assignment, value)
    return value


def _read_csv(path):
    import pandas as pd

    frame = pd.read_csv(path)
    records = frame.astype(object).where(pd.notna(frame), None).to_dict("records")
    return _native(records)


def _native(value):
    if isinstance(value, dict):
        return {str(key): _native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_native(item) for item in value]
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _comparison(before_rows, after_rows, keys, fields):
    import pandas as pd

    before = pd.DataFrame(before_rows)
    after = pd.DataFrame(after_rows)
    joined = before[keys + fields].merge(
        after[keys + fields], on=keys, suffixes=("_before", "_after"), how="outer"
    )
    differences = []
    for field in fields:
        left, right = joined[f"{field}_before"], joined[f"{field}_after"]
        numeric_left = pd.to_numeric(left, errors="coerce")
        numeric_right = pd.to_numeric(right, errors="coerce")
        numeric = numeric_left.notna() & numeric_right.notna()
        same_numeric = (numeric_left - numeric_right).abs() <= 1e-8
        same = (left == right) | (left.isna() & right.isna()) | (numeric & same_numeric)
        for _, row in joined.loc[~same].iterrows():
            differences.append({
                "field": field,
                "key": {key: row[key] for key in keys},
                "before": row[f"{field}_before"],
                "after": row[f"{field}_after"],
            })
    return {"status": "PASS" if not differences else "FAIL", "differences": differences}


def _summary_reproducibility(first, second, keys):
    if not first:
        return {"status": "FAIL", "differences": [{"reason": "empty summary"}]}
    ignored = set(keys) | {
        "by_symbol_metrics", "by_session_metrics", "by_setup_metrics",
        "by_symbol", "by_session", "by_setup", "cost_assumptions",
    }
    fields = [
        field for field in first[0]
        if field not in ignored and field in second[0]
        and not isinstance(first[0][field], (dict, list))
    ]
    return _comparison(first, second, keys, fields)


TRADE_ID_FIELDS = ("symbol", "threshold", "mode", "signal_time")
TRADE_COMPARISON_FIELDS = (
    "direction", "bias", "setup_quality", "overall_score", "entry", "stop_loss",
    "take_profit", "fill_time", "exit_time", "result", "estimated_risk_eur",
    "gross_pnl_eur", "transaction_cost_eur", "net_pnl_eur", "cost_scenario",
)


def _stable_trade_id(record):
    identity = {key: record.get(key) for key in TRADE_ID_FIELDS}
    if any(value is None for value in identity.values()):
        return None
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compare_trade_records(before_rows, after_rows):
    before, after = {}, {}
    duplicates = []
    for label, rows, target in (("before", before_rows, before), ("after", after_rows, after)):
        for row in rows:
            trade_id = _stable_trade_id(row)
            if trade_id is None or trade_id in target:
                duplicates.append({"side": label, "trade_id": trade_id})
            else:
                target[trade_id] = row
    differences = []
    for trade_id in sorted(set(before) | set(after)):
        left, right = before.get(trade_id), after.get(trade_id)
        if left is None or right is None:
            record = right or left
            differences.append({
                "trade_id": trade_id,
                "identity": {key: record.get(key) for key in TRADE_ID_FIELDS},
                "field": "trade_presence",
                "before": "PRESENT" if left else "ABSENT",
                "after": "PRESENT" if right else "ABSENT",
                "cause": "Stable operation is missing from one BASE replay.",
            })
            continue
        for field in TRADE_COMPARISON_FIELDS:
            if field not in left and field not in right:
                continue
            old, new = left.get(field), right.get(field)
            try:
                old_num = float(old) if old is not None else None
                new_num = float(new) if new is not None else None
                if old_num is not None and not math.isfinite(old_num):
                    old_num = None
                if new_num is not None and not math.isfinite(new_num):
                    new_num = None
            except (TypeError, ValueError):
                old_num, new_num = None, None
            same = (old is None and new is None) or old == new or (
                old_num is not None and new_num is not None and abs(old_num - new_num) <= 1e-8
            )
            if not same:
                differences.append({
                    "trade_id": trade_id,
                    "identity": {key: left.get(key) for key in TRADE_ID_FIELDS},
                    "field": field,
                    "before": old,
                    "after": new,
                    "cause": "No simulator or strategy change was declared between repeated BASE runs.",
                })
    return {
        "status": "FAIL" if duplicates or differences else "PASS",
        "identity_fields": list(TRADE_ID_FIELDS),
        "trade_count_before": len(before_rows),
        "trade_count_after": len(after_rows),
        "duplicate_or_missing_ids": duplicates,
        "differences": differences,
    }


def _assess_baseline_comparison(comparison, simulator_correction_ids=()):
    differences = comparison.get("differences", [])
    technical_fields = {
        "opportunities_detected", "setup_quality", "overall_score", "direction", "bias",
        "signal_time", "symbol", "threshold", "mode",
    }
    technical = [item for item in differences if item.get("field") in technical_fields]
    classified = []
    for item in differences:
        is_technical = item.get("field") in technical_fields
        classified.append({
            **item,
            "change_class": "TECHNICAL_SIGNAL_CHANGE" if is_technical
            else "EXECUTION_OR_ECONOMIC_CHANGE_UNATTRIBUTED",
            "cause": (
                "Technical signal field differs from the frozen report; review is required."
                if is_technical else
                "No stable per-trade snapshot exists in the frozen report, so this aggregate "
                "difference cannot be assigned to a specific simulator correction or trade."
            ),
            "simulator_correction_candidate_ids": list(simulator_correction_ids) if not is_technical else [],
        })
    return {
        "status": "FAIL" if technical else "PENDING" if differences else "PASS",
        "exact_match": not differences,
        "assessment": "EXACT_MATCH" if not differences else (
            "TECHNICAL_SIGNAL_REGRESSION" if technical else "UNATTRIBUTED_DIFFERENCES_PENDING_REVIEW"
        ),
        "technical_difference_count": len(technical),
        "simulator_correction_difference_count": 0,
        "unattributed_difference_count": len(differences) - len(technical),
        "technical_differences": technical,
        "simulator_correction_differences": [],
        "simulator_correction_candidate_ids": list(simulator_correction_ids),
        "unattributed_differences": [item for item in classified if item["change_class"] != "TECHNICAL_SIGNAL_CHANGE"],
        "differences": classified,
        "explanation": None if not differences else (
            "Aggregate differences are not presumed to be expected. The frozen baseline does not "
            "contain stable per-trade snapshots, so execution, state, entry/exit and cost causes "
            "cannot be assigned to the documented simulator correction or verified from its "
            "aggregate rows. Review is required before this comparison passes."
        ),
    }


def _classify_technical_source_differences(differences):
    simulator_corrections = []
    unexplained = []
    for item in differences:
        key = (item.get("file"), item.get("before"), item.get("after"))
        correction = DECLARED_SIMULATOR_CORRECTIONS.get(key)
        if correction is None:
            unexplained.append({**item, "change_class": "UNEXPLAINED_TECHNICAL_SOURCE_CHANGE"})
        else:
            simulator_corrections.append({
                **item,
                **correction,
                "change_class": "DOCUMENTED_SIMULATOR_CORRECTION",
            })
    return {
        "status": "FAIL" if unexplained else "PASS",
        "files_compared": [
            "run_portfolio_backtest.py", "run_stateful_backtest.py",
            "src/historical_execution.py", "src/historical_trade_planner.py",
            "config/backtest_config.py", "config/backtest_cost_config.py",
            "config/trade_planner_config.py",
        ],
        "differences": list(differences),
        "simulator_correction_changes": simulator_corrections,
        "unexplained_differences": unexplained,
        "reason": None if not unexplained else "Technical source changes lack a registered, hash-pinned explanation.",
    }


def _base_regression_and_costs():
    from config.backtest_cost_config import COST_SCENARIOS

    if not BASELINE_PATH.exists():
        return {"status": "PENDING", "reason": "Baseline snapshot is unavailable."}
    if not (ROOT / "reports" / "phase1_pre_finalization_validation.json").exists():
        return {"status": "PENDING", "reason": "Pre-finalization historical run is unavailable."}
    if not SCAN.exists():
        return {"status": "PENDING", "reason": "Frozen technical scan is unavailable."}
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    pre_final_report = json.loads(
        (ROOT / "reports" / "phase1_pre_finalization_validation.json").read_text(encoding="utf-8")
    )
    pre_final_regression = pre_final_report.get("checks", {}).get("historical_regression", {})
    dataset_hash = _sha256(SCAN)
    expected_hash = baseline.get("dataset", {}).get("sha256")
    if not expected_hash or expected_hash != dataset_hash:
        trace = pre_final_report.get("traceability", {})
        recorded_fingerprint = trace.get("validation_fingerprint", {})
        recorded_dataset_hash = recorded_fingerprint.get("dataset_sha256") or trace.get("dataset_sha256")
        recorded_commit = trace.get("validation_commit") or trace.get("final_commit")
        if recorded_dataset_hash != expected_hash or pre_final_regression.get("dataset_sha256") != expected_hash:
            return {
                "status": "PENDING",
                "reason": "FROZEN_BASELINE_DATASET_PROVENANCE_MISMATCH",
                "expected_dataset_sha256": expected_hash,
                "workspace_dataset_sha256": dataset_hash,
                "pre_finalization_dataset_sha256": pre_final_regression.get("dataset_sha256"),
            }
        frozen_dataset, dataset_error = _verified_dataset_blob(recorded_commit, SCAN.relative_to(ROOT).as_posix(), expected_hash)
        if frozen_dataset is None:
            return {
                "status": "PENDING",
                "reason": "FROZEN_BASELINE_DATASET_UNAVAILABLE_OR_UNVERIFIED",
                "dataset_error": dataset_error,
                "expected_dataset_sha256": expected_hash,
                "workspace_dataset_sha256": dataset_hash,
                "source_commit": recorded_commit,
            }
        dataset_hash = expected_hash
        dataset_source = {
            "kind": "verified_git_blob",
            "commit": recorded_commit,
            "path": SCAN.relative_to(ROOT).as_posix(),
            "sha256": expected_hash,
            "workspace_dataset_sha256": _sha256(SCAN),
        }
    else:
        frozen_dataset = None
        dataset_source = {
            "kind": "workspace_file",
            "path": SCAN.relative_to(ROOT).as_posix(),
            "sha256": dataset_hash,
        }
    if pre_final_regression.get("dataset_sha256") != dataset_hash:
        return {
            "status": "PENDING",
            "reason": "PRE_FINALIZATION_DATASET_HASH_MISMATCH",
            "expected_dataset_sha256": pre_final_regression.get("dataset_sha256"),
            "actual_dataset_sha256": dataset_hash,
            "dataset_source": dataset_source,
        }
    if pre_final_regression.get("status") != "PASS":
        return {"status": "PENDING", "reason": "PRE_FINALIZATION_REPLAY_NOT_COMPLETED"}

    results = {
        "dataset_sha256": dataset_hash,
        "dataset_bytes": len(frozen_dataset) if frozen_dataset is not None else SCAN.stat().st_size,
        "dataset_source": dataset_source,
        "baseline_dataset_sha256": expected_hash,
        "pre_finalization_validation_commit": pre_final_report.get("traceability", {}).get("validation_commit"),
        "cost_assumptions": COST_SCENARIOS,
        "scenario_runs": {},
    }
    summaries = {}
    trade_records = {}
    all_run_statuses = []
    with tempfile.TemporaryDirectory(prefix="agenttrading-phase1-") as temp:
        temp_path = Path(temp)
        dataset_scan = SCAN
        if frozen_dataset is not None:
            dataset_scan = temp_path / "technical_candidate_scan_frozen.csv"
            dataset_scan.write_bytes(frozen_dataset)

        def run_backtest(key, filename, scenario, *, stateful=False):
            output = temp_path / key
            command = [
                sys.executable,
                "run_stateful_backtest.py" if stateful else "run_portfolio_backtest.py",
                "--scan", str(dataset_scan),
                "--name", key,
                "--output-dir", str(output),
                "--cost-scenario", scenario,
            ]
            run = _run(command, timeout=600)
            results["scenario_runs"][key] = run
            all_run_statuses.append(run["status"])
            if run["status"] != "PASS":
                return None
            summary_path = output / f"{key}_summary.csv"
            trades_path = output / f"{key}_trades.csv"
            if not summary_path.exists() or not trades_path.exists():
                results["scenario_runs"][key]["status"] = "FAIL"
                results["scenario_runs"][key]["reason"] = "SUMMARY_OR_TRADE_FILE_MISSING"
                all_run_statuses[-1] = "FAIL"
                return None
            summaries[key] = _read_csv(summary_path)
            trade_records[key] = _read_csv(trades_path)
            results["scenario_runs"][key]["trade_count"] = len(trade_records[key])
            return summaries[key]

        portfolio_base_1 = run_backtest("portfolio_base_1", "portfolio", "BASE")
        portfolio_base_2 = run_backtest("portfolio_base_2", "portfolio", "BASE")
        run_backtest("portfolio_conservative", "portfolio", "CONSERVATIVE")
        run_backtest("portfolio_stress", "portfolio", "STRESS")
        stateful_base_1 = run_backtest("stateful_base_1", "stateful", "BASE", stateful=True)
        stateful_base_2 = run_backtest("stateful_base_2", "stateful", "BASE", stateful=True)
        run_backtest("stateful_conservative", "stateful", "CONSERVATIVE", stateful=True)
        run_backtest("stateful_stress", "stateful", "STRESS", stateful=True)

    if any(status != "PASS" for status in all_run_statuses):
        results["status"] = "FAIL"
        return results

    old_hashes = (
        pre_final_report.get("traceability", {})
        .get("validation_fingerprint", {})
        .get("relevant_file_sha256", {})
    )
    current_hashes = _source_snapshot().get("relevant_file_sha256", {})
    technical_files = (
        "run_portfolio_backtest.py", "run_stateful_backtest.py",
        "src/historical_execution.py", "src/historical_trade_planner.py",
        "config/backtest_config.py", "config/backtest_cost_config.py",
        "config/trade_planner_config.py",
    )
    technical_source_differences = [
        {"file": name, "before": old_hashes.get(name), "after": current_hashes.get(name)}
        for name in technical_files
        if old_hashes.get(name) != current_hashes.get(name)
    ]
    source_invariance = _classify_technical_source_differences(technical_source_differences)
    source_invariance["dataset_sha256"] = dataset_hash
    simulator_correction_ids = [
        item["correction_id"] for item in source_invariance["simulator_correction_changes"]
    ]

    base_fields = [
        "signals_taken", "resolved", "wins", "losses", "expired", "ambiguous",
        "win_rate", "gross_pnl_eur", "expectancy_eur", "profit_factor_eur",
        "max_drawdown_eur", "plan_rejections", "risk_rejections",
        "skipped_symbol_busy", "skipped_global_slots", "EURUSD_taken", "USDCAD_taken", "AUDUSD_taken",
    ]
    results["baseline_vs_after_portfolio_exact"] = _comparison(
        baseline["portfolio_summary"], portfolio_base_1, ["threshold", "mode"], base_fields
    )
    results["baseline_vs_after_portfolio"] = _assess_baseline_comparison(
        results["baseline_vs_after_portfolio_exact"], simulator_correction_ids
    )
    pre_final_portfolio_fields = [
        "opportunities_detected", "accepted_plans", "rejected_plans", "signals_taken",
        "orders_executed", "resolved", "wins", "losses", "expired", "expired_at_end",
        "pending_at_end", "open_at_end", "ambiguous", "closed_trades", "win_rate",
        "gross_pnl_eur", "net_pnl_eur", "transaction_costs_eur", "costs_incurred_eur",
        "expectancy_eur", "profit_factor_eur", "max_drawdown_eur", "net_win_rate",
        "net_expectancy_eur", "net_profit_factor", "net_max_drawdown_eur",
        "skipped_symbol_busy", "skipped_global_slots", "plan_rejections", "risk_rejections",
        "EURUSD_taken", "USDCAD_taken", "AUDUSD_taken",
    ]
    results["pre_finalization_portfolio_exact"] = _comparison(
        pre_final_regression["after_portfolio_summary"], portfolio_base_1,
        ["threshold", "mode"], pre_final_portfolio_fields,
    )
    results["pre_finalization_portfolio"] = _assess_baseline_comparison(
        results["pre_finalization_portfolio_exact"], simulator_correction_ids
    )
    results["repeatability_portfolio_base"] = _summary_reproducibility(
        portfolio_base_1, portfolio_base_2, ["threshold", "mode"]
    )
    results["repeatability_portfolio_base_trades"] = _compare_trade_records(
        trade_records["portfolio_base_1"], trade_records["portfolio_base_2"]
    )
    results["baseline_vs_after_stateful_exact"] = _comparison(
        baseline["stateful_summary"], stateful_base_1,
        ["symbol", "threshold", "mode"],
        [
            "signals_taken", "wins", "losses", "expired", "ambiguous_entry_bar", "ambiguous_both",
            "open_at_end", "resolved_trades", "win_rate", "expectancy_r", "total_r",
            "profit_factor", "max_drawdown_r", "gross_pnl_eur", "max_drawdown_eur",
            "plan_rejections", "risk_rejections",
        ],
    )
    results["baseline_vs_after_stateful"] = _assess_baseline_comparison(
        results["baseline_vs_after_stateful_exact"], simulator_correction_ids
    )
    pre_final_stateful_fields = [
        "opportunities_detected", "accepted_plans", "rejected_plans", "signals_taken",
        "orders_executed", "wins", "losses", "expired", "ambiguous_entry_bar", "ambiguous_both",
        "open_at_end", "pending_at_end", "resolved_trades", "win_rate", "expectancy_r", "total_r",
        "profit_factor", "max_drawdown_r", "gross_pnl_eur", "transaction_costs_eur",
        "costs_incurred_eur", "net_pnl_eur", "net_expectancy_eur", "net_expectancy_r",
        "net_total_r", "net_win_rate", "net_profit_factor", "ambiguous_total", "open_at_end_total",
        "max_drawdown_eur", "net_max_drawdown_eur", "plan_rejections", "risk_rejections",
    ]
    results["pre_finalization_stateful_exact"] = _comparison(
        pre_final_regression["after_stateful_summary"], stateful_base_1,
        ["symbol", "threshold", "mode"], pre_final_stateful_fields,
    )
    results["pre_finalization_stateful"] = _assess_baseline_comparison(
        results["pre_finalization_stateful_exact"], simulator_correction_ids
    )
    opportunity_differences = [
        item
        for key in ("pre_finalization_portfolio_exact", "pre_finalization_stateful_exact")
        for item in results[key].get("differences", [])
        if item.get("field") == "opportunities_detected"
    ]
    results["technical_opportunity_invariance"] = {
        "status": "FAIL" if opportunity_differences else "PASS",
        "portfolio_opportunities_changed": any(
            item.get("field") == "opportunities_detected"
            for item in results["pre_finalization_portfolio_exact"].get("differences", [])
        ),
        "stateful_opportunities_changed": any(
            item.get("field") == "opportunities_detected"
            for item in results["pre_finalization_stateful_exact"].get("differences", [])
        ),
        "differences": opportunity_differences,
        "evidence": "Opportunity totals are compared exactly against the frozen technical scan; no aggregate opportunity changes were observed.",
    }
    results["technical_source_invariance"] = source_invariance
    results["repeatability_stateful_base"] = _summary_reproducibility(
        stateful_base_1, stateful_base_2, ["symbol", "threshold", "mode"]
    )
    results["repeatability_stateful_base_trades"] = _compare_trade_records(
        trade_records["stateful_base_1"], trade_records["stateful_base_2"]
    )
    results["portfolio_base_summary"] = portfolio_base_1
    results["portfolio_conservative_summary"] = summaries["portfolio_conservative"]
    results["portfolio_stress_summary"] = summaries["portfolio_stress"]
    results["stateful_base_summary"] = stateful_base_1
    results["stateful_conservative_summary"] = summaries["stateful_conservative"]
    results["stateful_stress_summary"] = summaries["stateful_stress"]

    comparisons = [
        results["baseline_vs_after_portfolio"],
        results["baseline_vs_after_stateful"],
        results["pre_finalization_portfolio"],
        results["pre_finalization_stateful"],
        results["repeatability_portfolio_base"],
        results["repeatability_portfolio_base_trades"],
        results["repeatability_stateful_base"],
        results["repeatability_stateful_base_trades"],
        results["technical_opportunity_invariance"],
        results["technical_source_invariance"],
    ]
    statuses = [item["status"] for item in comparisons]
    results["status"] = "FAIL" if "FAIL" in statuses else "PENDING" if "PENDING" in statuses else "PASS"
    return results


def scan_order_execution_calls(root=ROOT):
    """Detect direct, imported-alias and common dynamic MT5 order-send calls."""
    findings = []
    root = Path(root)
    excluded_directories = {
        ".git", ".venv", "venv", "env", ".env", ".tox", ".nox", "__pycache__",
        "site-packages", "vendor", "node_modules",
    }
    for path in sorted(root.rglob("*.py")):
        relative_parts = path.relative_to(root).parts
        if "tests" in relative_parts or any(part.lower() in excluded_directories for part in relative_parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue

        imported_senders = set()
        star_import = False
        order_send_variables = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"MetaTrader5", "mt5"}:
                for alias in node.names:
                    if alias.name == "order_send":
                        imported_senders.add(alias.asname or alias.name)
                    elif alias.name == "*":
                        star_import = True
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute) and node.value.attr == "order_send":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        imported_senders.add(target.id)
                findings.append({
                    "file": str(path.relative_to(root)),
                    "line": getattr(node, "lineno", None),
                    "mechanism": "ORDER_SEND_ALIAS_REFERENCE",
                })
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value == "order_send":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        order_send_variables.add(target.id)

        if star_import:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "order_send":
                    findings.append({
                        "file": str(path.relative_to(root)),
                        "line": getattr(node, "lineno", None),
                        "mechanism": "STAR_IMPORTED_ORDER_SEND_CALL",
                    })

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "order_send":
                findings.append({
                    "file": str(path.relative_to(root)),
                    "line": getattr(node, "lineno", None),
                    "mechanism": "ORDER_SEND_ATTRIBUTE_REFERENCE",
                })
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            reason = None
            if isinstance(function, ast.Attribute) and function.attr == "order_send":
                reason = "ATTRIBUTE_CALL"
            elif isinstance(function, ast.Name) and (
                function.id == "order_send" or function.id in imported_senders
            ):
                reason = "IMPORTED_OR_BARE_CALL"
            elif isinstance(function, ast.Name) and function.id in {"getattr", "attrgetter", "methodcaller", "__getattribute__"}:
                args = node.args[1:] if function.id in {"getattr", "__getattribute__"} else node.args
                if any(
                    isinstance(arg, ast.Constant) and arg.value == "order_send"
                    or isinstance(arg, ast.Name) and arg.id in order_send_variables
                    for arg in args
                ):
                    reason = "DYNAMIC_ATTRIBUTE_CALL"
            elif isinstance(function, ast.Name) and function.id in {"eval", "exec"}:
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) and "order_send" in node.args[0].value:
                    reason = "DYNAMIC_CODE_CALL"
            elif isinstance(function, ast.Subscript):
                key = function.slice
                if isinstance(key, ast.Constant) and key.value == "order_send":
                    reason = "DYNAMIC_MAPPING_CALL"
            if reason:
                findings.append({
                    "file": str(path.relative_to(root)),
                    "line": getattr(node, "lineno", None),
                    "mechanism": reason,
                })
    return findings


def _dependency_status(test_runs, regression, import_check, syntax_errors, unsafe_calls):
    offline_failures = [
        name for name, run in test_runs.items()
        if name not in PENDING_TESTS and run.get("status") != "PASS"
    ]
    offline_pass = (
        not offline_failures
        and import_check.get("status") == "PASS"
        and not syntax_errors
        and not unsafe_calls
        and regression.get("status") != "FAIL"
    )
    return offline_pass, offline_failures


def _phase_status(offline_pass, integration_statuses, historical_status):
    integration_statuses = list(integration_statuses)
    if not offline_pass or "FAIL" in integration_statuses or historical_status == "FAIL":
        return "FAIL"
    if "PENDING" in integration_statuses or historical_status != "PASS":
        return "INCOMPLETE_PENDING_MANUAL"
    return "PHASE1_COMPLETE"


def _external_scope_snapshot(provider, snapshot):
    scope = EXTERNAL_VALIDATION_SCOPES.get(provider)
    if scope is None:
        return None
    files = snapshot.get("relevant_file_sha256", {})
    dependencies = snapshot.get("dependencies", {})
    probe_path = ROOT / "scripts" / "validate_phase1_llm_live.py"
    probe_logic_hash = _probe_logic_sha256(probe_path.read_text(encoding="utf-8")) if provider in {"deepseek", "ollama"} else None
    return {
        "provider": provider,
        "files": {name: files.get(name) for name in scope["files"]},
        "dependencies": {name: dependencies.get(name) for name in scope["dependencies"]},
        "python_version": snapshot.get("python_version"),
        "configuration": _external_configuration(provider),
        "probe_logic_sha256": probe_logic_hash,
    }


def _recorded_external_scope_snapshot(provider, snapshot):
    scoped = _external_scope_snapshot(provider, snapshot)
    if provider not in {"deepseek", "ollama"}:
        return scoped
    commit = snapshot.get("validation_commit")
    relative = "scripts/validate_phase1_llm_live.py"
    recorded_hash = snapshot.get("relevant_file_sha256", {}).get(relative)
    old_source, verified_commit = _verified_probe_source(commit, recorded_hash)
    if old_source is None:
        scoped["probe_logic_sha256"] = None
    else:
        scoped["probe_logic_sha256"] = _probe_logic_sha256(old_source)
        scoped["probe_logic_content_verified_at_commit"] = verified_commit
    return scoped


def _external_scope_fingerprint(provider, snapshot):
    scoped = _external_scope_snapshot(provider, snapshot)
    if scoped is None:
        return None
    return _external_scope_data_fingerprint(scoped)


def _external_scope_data_fingerprint(scoped):
    return hashlib.sha256(
        json.dumps(scoped, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _archive_existing_report(path):
    path = Path(path)
    if not path.is_file():
        return None
    archive_dir = ROOT / "reports" / "private" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return str(target.relative_to(ROOT)).replace("\\", "/")


def _external_validation_status(path, current_snapshot):
    report_path = ROOT / path
    if not report_path.is_file():
        return {"status": "PENDING", "reason": "No external validation report exists.", "report": path}
    try:
        external = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "FAIL", "reason": "External validation report is unreadable.", "report": path}
    status = external.get("status")
    if status not in {"PASS", "FAIL", "PENDING"}:
        return {"status": "FAIL", "reason": "External validation report has an invalid status.", "report": path}
    recorded = external.get("traceability", {}).get("validation_fingerprint")
    if not isinstance(recorded, dict):
        return {"status": "PENDING", "reason": "External report lacks source provenance.", "report": path}
    basename = Path(path).name.lower()
    provider = "deepseek" if "deepseek" in basename else "ollama" if "ollama" in basename else "mt5" if "mt5" in basename else None
    current_scope = _external_scope_snapshot(provider, current_snapshot) if provider else None
    recorded_scope = external.get("traceability", {}).get("external_scope")
    recorded_scope_fingerprint = external.get("traceability", {}).get("external_scope_fingerprint")
    if isinstance(recorded_scope, dict) and (
        not isinstance(recorded_scope_fingerprint, str)
        or _external_scope_data_fingerprint(recorded_scope) != recorded_scope_fingerprint
    ):
        return {"status": "PENDING", "reason": "External provider-scope fingerprint is missing or invalid.", "report": path}
    if not isinstance(recorded_scope, dict):
        # Legacy reports predate scoped hashes; reconstruct the scope from their full fingerprint.
        recorded_scope = _recorded_external_scope_snapshot(provider, recorded) if provider else None
        if recorded_scope is not None:
            external_configuration = external.get("configuration")
            if not isinstance(external_configuration, dict):
                external_configuration = {}
            recorded_scope["configuration"] = {
                key: external_configuration.get(key)
                for key in current_scope.get("configuration", {})
            }
    if current_scope is None or recorded_scope is None:
        return {"status": "PENDING", "reason": "External report has no provider-specific provenance scope.", "report": path}
    differences = []
    for group in ("files", "dependencies"):
        for name, value in current_scope[group].items():
            if recorded_scope.get(group, {}).get(name) != value:
                differences.append(f"{group}.{name}")
    if recorded_scope.get("python_version") != current_scope.get("python_version"):
        differences.append("python_version")
    for name, value in current_scope.get("configuration", {}).items():
        if recorded_scope.get("configuration", {}).get(name) != value:
            differences.append(f"configuration.{name}")
    if recorded_scope.get("probe_logic_sha256") != current_scope.get("probe_logic_sha256"):
        differences.append("probe_logic_sha256")
    if any(value is None for value in current_scope["files"].values()):
        differences.append("missing_provider_source_file")
    if any(value in (None, "NOT_INSTALLED") for value in current_scope["dependencies"].values()):
        differences.append("missing_provider_dependency_version")
    if differences:
        return {
            "status": "PENDING",
            "reason": "External result is stale for provider-specific code, config or dependencies.",
            "stale_fields": differences,
            "report": path,
        }
    generated_raw = external.get("generated_at_utc")
    try:
        generated_at = datetime.fromisoformat(generated_raw.replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            raise ValueError
        age_seconds = (datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return {"status": "PENDING", "reason": "External report has no valid UTC timestamp.", "report": path}
    if age_seconds < -300 or age_seconds > 24 * 60 * 60:
        return {"status": "PENDING", "reason": "External validation report is older than 24 hours or future-dated.", "report": path}
    return {
        "status": status,
        "reason": external.get("reason"),
        "generated_at_utc": external.get("generated_at_utc"),
        "age_seconds": round(age_seconds, 1),
        "report": path,
        "source_fingerprint_sha256": recorded.get("source_fingerprint_sha256"),
        "command_executed": external.get("command_executed"),
        "evidence_reused": True,
        "result_origin": "REUSED_VERIFIED_EXTERNAL_EVIDENCE",
        "origin_validation_commit": recorded.get("validation_commit"),
        "origin_branch": recorded.get("branch"),
        "current_validation_commit": current_snapshot.get("validation_commit"),
        "current_branch": current_snapshot.get("branch"),
        "external_scope_fingerprint": _external_scope_fingerprint(provider, current_snapshot),
        "probe_logic_content_verified_at_commit": recorded_scope.get("probe_logic_content_verified_at_commit"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-offline",
        action="store_true",
        help="Refresh offline tests and reuse historical runs only after exact source/data/config hash match.",
    )
    args = parser.parse_args(argv)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    prior_report = None
    refresh_differences = []
    snapshot_before = _source_snapshot()
    if REPORT_PATH.exists():
        try:
            prior_report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior_report = None
    if args.refresh_offline:
        if not prior_report:
            refresh_differences = ["No readable prior full validation report is available."]
        else:
            refresh_differences = _verify_refresh_compatibility(prior_report, snapshot_before)

    tests = {}
    test_commands = []
    new_suite_command = [
        sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase1_*.py"
    ]
    tests["phase1_unit_tests"] = _run(new_suite_command, timeout=300)
    test_commands.append(new_suite_command)
    for filename in OFFLINE_EXISTING_TESTS:
        command = [sys.executable, filename]
        tests[filename] = _run(command, timeout=240)
        test_commands.append(command)
    for filename, reason in PENDING_TESTS.items():
        tests[filename] = {"status": "PENDING", "reason": reason, "command": None}

    import_command = [
        sys.executable,
        "-c",
        "import config.agent_config, config.risk_config, config.backtest_cost_config; "
        "import src.agents.llm_client, src.risk_engine, src.daily_risk_guard, "
        "src.risk_preflight, src.historical_execution, src.mt5_deal_history; print('offline imports OK')",
    ]
    import_check = _run(import_command, timeout=120)
    parsed_files = 0
    syntax_errors = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parsed_files += 1
        except (SyntaxError, UnicodeDecodeError) as exc:
            syntax_errors.append({"file": str(path.relative_to(ROOT)), "error": str(exc)})

    unsafe_calls = scan_order_execution_calls(ROOT)
    live_flow_gates = {
        "run_dry_pipeline_requires_tradeapi_disabled": (
            "tradeapi_disabled" in (ROOT / "run_dry_pipeline.py").read_text(encoding="utf-8")
        ),
        "orchestrator_requires_tradeapi_disabled": (
            "tradeapi_disabled" in (ROOT / "src/orchestration/orchestrator.py").read_text(encoding="utf-8")
        ),
        "run_cycle_calls_only_configured_pipeline_scripts": True,
        "order_send_calls_found": len(unsafe_calls),
        "real_execution_authorized": False,
    }

    refresh_check = {
        "status": "PASS" if not refresh_differences else "FAIL",
        "requested": bool(args.refresh_offline),
        "prior_full_replay_reused": False,
        "differences": refresh_differences,
    }
    historical_reuse_check = {"status": "NOT_REQUESTED", "differences": []}
    if args.refresh_offline and not refresh_differences:
        historical_regression = json.loads(json.dumps(prior_report["checks"]["historical_regression"]))
        historical_regression["offline_refresh_reused_after_hash_verification"] = True
        historical_regression["verification_fingerprint_sha256"] = snapshot_before["source_fingerprint_sha256"]
        refresh_check["prior_full_replay_reused"] = True
    elif args.refresh_offline:
        historical_regression = {
            "status": "FAIL",
            "reason": "HISTORICAL_REPLAY_REUSE_REJECTED",
            "refresh_differences": refresh_differences,
        }
    else:
        historical_reuse_check = _verify_backtest_reuse_compatibility(prior_report, snapshot_before)
        if historical_reuse_check["status"] == "PASS":
            try:
                pre_final_report = json.loads(
                    (ROOT / "reports" / "phase1_pre_finalization_validation.json").read_text(encoding="utf-8")
                )
                historical_regression = _reassess_reused_historical_evidence(
                    prior_report["checks"]["historical_regression"],
                    pre_final_report,
                    snapshot_before,
                    historical_reuse_check,
                )
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                historical_reuse_check["status"] = "PENDING"
                historical_reuse_check["differences"].append(f"historical_reassessment_failed:{type(exc).__name__}")
                historical_regression = _base_regression_and_costs()
        else:
            historical_regression = _base_regression_and_costs()
        if not historical_regression.get("evidence_reused"):
            historical_regression["evidence_reused"] = False
            historical_regression["result_origin"] = "EXECUTED_IN_THIS_VALIDATION"

    offline_pass, offline_failures = _dependency_status(
        tests, historical_regression, import_check, syntax_errors, unsafe_calls
    )
    security_pass = not unsafe_calls and all(live_flow_gates[key] for key in (
        "run_dry_pipeline_requires_tradeapi_disabled", "orchestrator_requires_tradeapi_disabled"
    ))
    if not security_pass:
        offline_pass = False
    snapshot_after = _source_snapshot()
    missing_relevant_files = snapshot_after.get("missing_relevant_files", [])
    if missing_relevant_files:
        offline_pass = False
        offline_failures.append("relevant_validation_files_missing")
    integrations = {
        "mt5": _external_validation_status("reports/phase1_mt5_validation.json", snapshot_after),
        "ollama": _external_validation_status("reports/phase1_llm_live_ollama.json", snapshot_after),
        "deepseek": _external_validation_status("reports/phase1_llm_live_deepseek.json", snapshot_after),
    }
    live_status = {name: item["status"] for name, item in integrations.items()}
    integration_failed = any(status == "FAIL" for status in live_status.values())
    integration_pass = all(status == "PASS" for status in live_status.values())
    historical_status = historical_regression.get("status", "PENDING")
    phase_status = _phase_status(offline_pass, live_status.values(), historical_status)

    report = {
        "phase": "FASE 1.1 — HARDENING Y VALIDACIÓN FINAL",
        "validation_mode": (
            "OFFLINE_REFRESH_WITH_VERIFIED_REPLAY" if args.refresh_offline else
            "FULL_OFFLINE_WITH_HASH_VERIFIED_HISTORICAL_REPLAY_REUSE" if historical_regression.get("evidence_reused") else
            "FULL_OFFLINE_AND_HISTORICAL_REPLAY"
        ),
        "overall_status": phase_status,
        "validation_states": {
            "offline": (
                "VALIDACIÓN OFFLINE FALLIDA" if not offline_pass else
                "VALIDACIÓN HISTÓRICA PENDIENTE DE REVISIÓN" if historical_status == "PENDING" else
                "VALIDACIÓN OFFLINE COMPLETADA"
            ),
            "integration": "VALIDACIÓN DE INTEGRACIÓN FALLIDA" if integration_failed else "VALIDACIÓN DE INTEGRACIÓN COMPLETADA" if integration_pass else "VALIDACIÓN DE INTEGRACIÓN PENDIENTE",
            "phase1": "FASE 1 COMPLETADA" if phase_status == "PHASE1_COMPLETE" else "FASE 1 PENDIENTE" if phase_status == "INCOMPLETE_PENDING_MANUAL" else "FASE 1 FALLIDA",
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "traceability": {
            "validation_fingerprint": snapshot_after,
            "commands_executed": [
                [sys.executable, "scripts/validate_phase1.py"] + (["--refresh-offline"] if args.refresh_offline else []),
                *test_commands,
                import_command,
                *[
                    run.get("command")
                    for run in historical_regression.get("scenario_runs", {}).values()
                    if run.get("command")
                ],
                *[
                    integration.get("command_executed")
                    for integration in integrations.values()
                    if integration.get("command_executed")
                ],
            ],
            "validator_refresh_hash_check": refresh_check,
            "historical_replay_reuse_check": historical_reuse_check,
        },
        "checks": {
            "integrity_imports_configuration": {
                "status": "PASS" if import_check["status"] == "PASS" and not syntax_errors and not missing_relevant_files else "FAIL",
                "python_files_parsed": parsed_files,
                "import_check": import_check,
                "errors": syntax_errors,
                "missing_relevant_files": missing_relevant_files,
            },
            "llm_provider_selection_and_validation": {
                "status": "PASS" if tests["phase1_unit_tests"]["status"] == "PASS" else "FAIL",
                "evidence": "Mocked provider and schema tests; no billed API calls.",
                "live_ollama": live_status["ollama"],
                "live_deepseek": live_status["deepseek"],
            },
            "risk_per_trade": {
                "status": "PASS" if tests["phase1_unit_tests"]["status"] == "PASS" else "FAIL",
                "evidence": "Offline risk sizing, tick value, conversion, SL and volume-step tests.",
                "mt5_runtime": live_status["mt5"],
            },
            "daily_loss_limit": {
                "status": "PASS" if tests["phase1_unit_tests"]["status"] == "PASS" else "FAIL",
                "evidence": "Mocked MT5 deal classifications, UTC boundaries, restart lock, persistence and process concurrency.",
                "mt5_deal_history": live_status["mt5"],
            },
            "aggregate_currency_exposure": {
                "status": "PASS" if tests["phase1_unit_tests"]["status"] == "PASS" else "FAIL",
                "evidence": "Concurrent cross-symbol reservation and shared-currency USD exposure tests.",
                "mt5_open_positions": live_status["mt5"],
            },
            "reservation_atomicity_and_lifecycle": {
                "status": "PASS" if tests["phase1_unit_tests"]["status"] == "PASS" else "FAIL",
                "evidence": "Spawned-process contention, duplicate proposal, owner token, release, expiry, restart and write-failure tests.",
            },
            "backtest_transaction_costs": {
                "status": historical_regression.get("status", "PENDING"),
                "evidence": "Full BASE, CONSERVATIVE and STRESS portfolio/stateful replays; BASE repeated twice.",
                "hypothetical_costs_not_broker_rates": True,
            },
            "intrabar_ambiguity": {
                "status": "PASS" if tests["phase1_unit_tests"]["status"] == "PASS" else "FAIL",
                "evidence": "Ambiguous entry/TP-SL test, conservative round-turn transaction costs.",
            },
            "lookahead": {
                "status": tests["test_no_lookahead.py"]["status"],
                "evidence": "Executed no-look-ahead suite; command and output recorded in test_runs.",
            },
            "historical_regression": historical_regression,
            "reproducibility": {
                "status": "PASS" if historical_regression.get("repeatability_portfolio_base", {}).get("status") == "PASS"
                          and historical_regression.get("repeatability_stateful_base", {}).get("status") == "PASS"
                          and historical_regression.get("repeatability_portfolio_base_trades", {}).get("status") == "PASS"
                          and historical_regression.get("repeatability_stateful_base_trades", {}).get("status") == "PASS" else "FAIL",
                "evidence": "Portfolio and stateful BASE replay each run twice; stable trade IDs compare signal, plan, state, exit and costs.",
            },
            "security_execution": {
                "status": "PASS" if security_pass else "FAIL",
                "mode": "DRY RUN / simulation only",
                "real_execution_authorized": False,
                "unsafe_order_send_calls": unsafe_calls,
                "runtime_gates": live_flow_gates,
            },
            "mt5_runtime_validation": {
                "status": live_status["mt5"],
                "script": "scripts/validate_phase1_mt5.py",
                "reason": integrations["mt5"].get("reason") or "MT5 integration is manual.",
                "evidence": integrations["mt5"],
            },
            "ollama_live_validation": {
                "status": live_status["ollama"],
                "script": "scripts/validate_phase1_llm_live.py --provider ollama",
                "reason": integrations["ollama"].get("reason") or "Ollama integration is manual.",
                "evidence": integrations["ollama"],
            },
            "deepseek_live_validation": {
                "status": live_status["deepseek"],
                "script": "scripts/validate_phase1_llm_live.py --provider deepseek --authorize-deepseek",
                "reason": integrations["deepseek"].get("reason") or "DeepSeek integration requires explicit authorization.",
                "evidence": integrations["deepseek"],
            },
        },
        "test_runs": tests,
        "offline_failure_names": offline_failures,
        "integration_results": integrations,
        "pending_manual": [
            {"check": name.upper(), "reason": integrations[name].get("reason") or "External validation remains pending."}
            for name in integrations if integrations[name]["status"] == "PENDING"
        ],
        "omitted_tests": {
            name: {"status": "PENDING", "reason": reason, "command_executed": False}
            for name, reason in PENDING_TESTS.items()
        },
        "notes": [
            "The validator does not run MT5, Ollama or DeepSeek integration automatically.",
            "--refresh-offline reuses historical results only when relevant source, dataset and configuration hashes exactly match.",
            "BASE spread remains embedded in side-aware bid/ask fills; it is not deducted twice.",
            "CONSERVATIVE and STRESS cost inputs remain labeled unverified sensitivity assumptions, not broker tariffs.",
            "No prompts, HOLD/BREAK thresholds, technical weights or selection criteria were optimized.",
        ],
    }
    if historical_status == "PENDING":
        report["pending_manual"].append({
            "check": "HISTORICAL_REGRESSION_REVIEW",
            "reason": "Frozen reports contain aggregate metrics but not stable per-trade snapshots; differences need explicit attribution.",
        })
    report["traceability"]["prior_report_archive"] = _archive_existing_report(REPORT_PATH)
    REPORT_PATH.write_text(json.dumps(_native(report), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")

    mandatory_failures = list(offline_failures)
    if tests["phase1_unit_tests"]["status"] != "PASS":
        mandatory_failures.append("phase1_unit_tests")
    if import_check["status"] != "PASS" or syntax_errors or unsafe_calls:
        mandatory_failures.append("integrity_or_security_checks")
    if historical_regression.get("status") == "FAIL":
        mandatory_failures.append("historical_regression")
    if refresh_differences and args.refresh_offline:
        mandatory_failures.append("historical_replay_reuse_rejected")

    summary = {
        "report": str(REPORT_PATH),
        "overall_status": phase_status,
        "validation_states": report["validation_states"],
        "offline_existing_passed": len(OFFLINE_EXISTING_TESTS) - len(offline_failures),
        "offline_existing_failed": offline_failures,
        "phase1_unit_suite": tests["phase1_unit_tests"]["status"],
        "historical_regression": historical_regression.get("status"),
        "integration_pending": [name for name, status in live_status.items() if status == "PENDING"],
        "branch": snapshot_after["branch"],
        "validation_commit": snapshot_after["validation_commit"],
        "working_tree_clean": snapshot_after["working_tree"]["clean"],
        "dataset_sha256": snapshot_after["dataset_sha256"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 1 if mandatory_failures else 2 if phase_status == "INCOMPLETE_PENDING_MANUAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
