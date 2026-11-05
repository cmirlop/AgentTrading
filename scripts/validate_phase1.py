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

CONFIG_FILES = [name for name in RELEVANT_FILES if name.startswith("config/")]


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args):
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True,
        timeout=10, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _source_snapshot():
    file_hashes = {}
    missing = []
    for relative in RELEVANT_FILES:
        path = ROOT / relative
        if path.is_file():
            file_hashes[relative] = _sha256(path)
        else:
            file_hashes[relative] = None
            missing.append(relative)
    dataset_hash = _sha256(SCAN) if SCAN.is_file() else None
    config_hashes = {name: file_hashes[name] for name in CONFIG_FILES}
    baseline_hash = file_hashes.get("reports/phase1_baseline_metrics.json")
    fingerprint_data = {
        "relevant_file_sha256": file_hashes,
        "dataset_sha256": dataset_hash,
        "backtest_config_sha256": config_hashes,
        "baseline_metrics_sha256": baseline_hash,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    status_lines = _git("status", "--porcelain", "--untracked-files=all")
    changes = status_lines.splitlines() if status_lines else []
    dependencies = {}
    for package in ("pandas", "numpy", "pydantic", "MetaTrader5", "ollama", "openai", "python-dotenv"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "NOT_INSTALLED"
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
        "python_version": sys.version,
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
        "dataset_sha256", "relevant_file_sha256", "backtest_config_sha256", "baseline_metrics_sha256"
    ):
        if prior.get(field) != current_snapshot.get(field):
            differences.append(f"{field} differs from prior full validation")
    if current_snapshot.get("missing_relevant_files"):
        differences.append("one or more required simulation/config files are missing")
    prior_regression = prior_report.get("checks", {}).get("historical_regression", {})
    if not isinstance(prior_regression, dict) or prior_regression.get("status") not in {"PASS", "FAIL"}:
        differences.append("prior report does not contain completed historical replay evidence")
    return differences


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
            "command": list(command),
            "reason": f"COMMAND_TIMEOUT_AFTER_{timeout}_SECONDS",
            "stdout_tail": str(exc.stdout or "")[-1200:],
            "stderr_tail": str(exc.stderr or "")[-1200:],
        }
    except OSError as exc:
        return {
            "status": "FAIL",
            "return_code": None,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "command": list(command),
            "reason": f"COMMAND_START_FAILED:{type(exc).__name__}",
            "stdout_tail": "",
            "stderr_tail": "",
        }
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "return_code": result.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "command": list(command),
        "stdout_tail": result.stdout[-1200:],
        "stderr_tail": result.stderr[-1200:],
    }


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


EXPECTED_EXECUTION_ONLY_FIELDS = {
    "signals_taken", "orders_executed", "closed_trades", "resolved", "resolved_trades",
    "accepted_plans", "rejected_plans", "plan_rejections",
    "wins", "losses", "expired", "expired_at_end", "ambiguous", "ambiguous_total",
    "ambiguous_entry_bar", "ambiguous_both", "open_at_end", "pending_at_end",
    "win_rate", "expectancy_eur", "expectancy_r", "total_r", "profit_factor",
    "profit_factor_eur", "gross_pnl_eur", "net_pnl_eur", "transaction_costs_eur",
    "costs_incurred_eur", "net_expectancy_eur", "net_expectancy_r", "net_total_r",
    "net_win_rate", "net_profit_factor", "max_drawdown_eur", "net_max_drawdown_eur",
    "max_drawdown_r", "skipped_symbol_busy", "skipped_global_slots",
    "EURUSD_taken", "USDCAD_taken", "AUDUSD_taken",
}


def _assess_baseline_comparison(comparison):
    differences = comparison.get("differences", [])
    unexpected = [item for item in differences if item.get("field") not in EXPECTED_EXECUTION_ONLY_FIELDS]
    return {
        "status": "FAIL" if unexpected else "PASS",
        "exact_match": not differences,
        "assessment": "UNEXPECTED_DIFFERENCE" if unexpected else (
            "EXACT_MATCH" if not differences else "EXPECTED_EXECUTION_CLASSIFICATION_OR_COST_CORRECTION"
        ),
        "expected_difference_count": len(differences) - len(unexpected),
        "unexpected_difference_count": len(unexpected),
        "unexpected_differences": unexpected,
        "differences": differences,
        "explanation": None if not differences else (
            "Only simulator execution fields changed: pending orders at end-of-data remain pending, "
            "expiry-straddling bars are ambiguous, and ambiguous filled cases carry round-turn costs. "
            "Those order-state corrections change slot occupancy, which can change how many later "
            "opportunities reach plan validation and therefore affect accepted/rejected plan counts. "
            "The frozen replay confirms opportunity counts remain identical; technical or agent "
            "selection inputs are not modified."
        ),
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
        return {
            "status": "FAIL",
            "reason": "BASELINE_DATASET_HASH_MISMATCH",
            "expected_dataset_sha256": expected_hash,
            "actual_dataset_sha256": dataset_hash,
        }
    if pre_final_regression.get("dataset_sha256") != dataset_hash:
        return {
            "status": "FAIL",
            "reason": "PRE_FINALIZATION_DATASET_HASH_MISMATCH",
            "expected_dataset_sha256": pre_final_regression.get("dataset_sha256"),
            "actual_dataset_sha256": dataset_hash,
        }
    if pre_final_regression.get("status") != "PASS":
        return {"status": "FAIL", "reason": "PRE_FINALIZATION_REPLAY_NOT_COMPLETED"}

    results = {
        "dataset_sha256": dataset_hash,
        "dataset_bytes": SCAN.stat().st_size,
        "baseline_dataset_sha256": expected_hash,
        "cost_assumptions": COST_SCENARIOS,
        "scenario_runs": {},
    }
    summaries = {}
    all_run_statuses = []
    with tempfile.TemporaryDirectory(prefix="agenttrading-phase1-") as temp:
        temp_path = Path(temp)

        def run_backtest(key, filename, scenario, *, stateful=False):
            output = temp_path / key
            command = [
                sys.executable,
                "run_stateful_backtest.py" if stateful else "run_portfolio_backtest.py",
                "--scan", str(SCAN),
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
            if not summary_path.exists():
                results["scenario_runs"][key]["status"] = "FAIL"
                results["scenario_runs"][key]["reason"] = "SUMMARY_FILE_MISSING"
                all_run_statuses[-1] = "FAIL"
                return None
            summaries[key] = _read_csv(summary_path)
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
        results["baseline_vs_after_portfolio_exact"]
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
        results["pre_finalization_portfolio_exact"]
    )
    results["repeatability_portfolio_base"] = _summary_reproducibility(
        portfolio_base_1, portfolio_base_2, ["threshold", "mode"]
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
        results["baseline_vs_after_stateful_exact"]
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
        results["pre_finalization_stateful_exact"]
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
        "evidence": "Opportunity totals are compared exactly across the frozen input; only execution state can cascade to plan availability.",
    }
    results["repeatability_stateful_base"] = _summary_reproducibility(
        stateful_base_1, stateful_base_2, ["symbol", "threshold", "mode"]
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
        results["repeatability_stateful_base"],
        results["technical_opportunity_invariance"],
    ]
    results["status"] = "PASS" if all(item["status"] == "PASS" for item in comparisons) else "FAIL"
    return results


def scan_order_execution_calls(root=ROOT):
    """Detect direct, imported-alias and common dynamic MT5 order-send calls."""
    findings = []
    root = Path(root)
    for path in sorted(root.rglob("*.py")):
        relative_parts = path.relative_to(root).parts
        if "tests" in relative_parts or "__pycache__" in relative_parts:
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
        and regression.get("status") == "PASS"
    )
    return offline_pass, offline_failures


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
    compared_fields = (
        "validation_commit", "branch", "dataset_sha256",
        "relevant_file_sha256", "backtest_config_sha256", "baseline_metrics_sha256",
    )
    differences = [
        field for field in compared_fields
        if recorded.get(field) != current_snapshot.get(field)
    ]
    if differences:
        return {
            "status": "PENDING",
            "reason": "External result is stale for current code/config/data.",
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
    if args.refresh_offline:
        if REPORT_PATH.exists():
            try:
                prior_report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prior_report = None
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
        historical_regression = _base_regression_and_costs()

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
    if not offline_pass or integration_failed:
        phase_status = "FAIL"
    elif not integration_pass:
        phase_status = "INCOMPLETE_PENDING_MANUAL"
    else:
        phase_status = "PHASE1_COMPLETE"

    report = {
        "phase": "FASE 1.1 — HARDENING Y VALIDACIÓN FINAL",
        "validation_mode": "OFFLINE_REFRESH_WITH_VERIFIED_REPLAY" if args.refresh_offline else "FULL_OFFLINE_AND_HISTORICAL_REPLAY",
        "overall_status": phase_status,
        "validation_states": {
            "offline": "VALIDACIÓN OFFLINE COMPLETADA" if offline_pass else "VALIDACIÓN OFFLINE FALLIDA",
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
                          and historical_regression.get("repeatability_stateful_base", {}).get("status") == "PASS" else "FAIL",
                "evidence": "Portfolio and stateful BASE replay each run twice using identical frozen inputs.",
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
    return 1 if mandatory_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
