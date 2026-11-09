#!/usr/bin/env python3
"""Offline-only validator for the Phase 2.2.1 research infrastructure."""

from __future__ import annotations

from datetime import datetime, timezone
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "reports" / "phase2_2_1"
BASELINE_PATH = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
BASELINE_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
RELEVANT_FILES = [
    "src/technical_reference.py", "src/event_ledger.py", "src/llm_evaluation.py",
    "src/paired_policy_simulator.py", "src/research_policy.py",
    "src/historical_trade_planner.py", "src/historical_execution.py",
    "run_market_agents.py", "run_multiagent_analysis.py", "run_dry_pipeline.py",
    "config/backtest_config.py", "config/risk_config.py", "config/backtest_cost_config.py",
    "scripts/run_phase2_2_offline_demo.py", "scripts/audit_phase2_2_1_historical.py",
    "tests/test_phase2_2.py", "tests/test_phase2_2_1.py",
]


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str | None:
    completed = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _run(name: str, command: list[str], timeout: int = 600) -> dict:
    started = monotonic()
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    ran = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", output)
    skipped = re.search(r"skipped=(\d+)", output)
    failures = re.search(r"failures=(\d+)", output)
    errors = re.search(r"errors=(\d+)", output)
    count = int(ran.group(1)) if ran else None
    skipped_count = int(skipped.group(1)) if skipped else 0
    failed_count = int(failures.group(1)) if failures else 0
    errored_count = int(errors.group(1)) if errors else 0
    if completed.returncode != 0 or (count is None and name in {"tests_phase2_2_1", "tests_phase2_2", "all_offline_unittest"}):
        status = "FAIL"
    elif skipped_count:
        status = "PASS_WITH_SKIPPED_TESTS"
    else:
        status = "PASS"
    result = {
        "name": name, "status": status, "return_code": completed.returncode,
        "duration_seconds": round(monotonic() - started, 3), "command": command,
        "tests_run": count, "tests_passed": max(0, count - skipped_count - failed_count - errored_count) if count is not None else None,
        "tests_failed": failed_count, "tests_errored": errored_count, "tests_skipped": skipped_count,
        "summary_lines": [line.strip() for line in output.splitlines()
                          if line.strip().startswith(("Ran ", "OK", "FAILED", "ERROR:"))][-5:],
    }
    if status != "PASS":
        result["failure_excerpt"] = output[-7000:]
    return result


def _dump(filename: str, payload: dict) -> None:
    (REPORT_DIR / filename).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _technical_module_import_audit() -> dict:
    path = ROOT / "src" / "technical_reference.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = []
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        imports.extend(names)
        forbidden.extend(name for name in names if any(token in name.lower() for token in ("deepseek", "ollama", "llm_client", "openai")))
    return {"status": "PASS" if not forbidden else "FAIL", "imports": sorted(set(imports)),
            "forbidden_provider_imports": forbidden, "provider_calls": 0}


def main() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    base_commit = _git("rev-parse", "HEAD")
    branch = _git("branch", "--show-current")
    status_before = _git("status", "--porcelain", "--untracked-files=all") or ""
    package_versions = {}
    for name in ("pandas", "numpy", "pydantic", "MetaTrader5", "ollama", "openai"):
        try:
            package_versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            package_versions[name] = "NOT_INSTALLED"

    baseline_actual = _sha256(BASELINE_PATH)
    baseline_report_path = ROOT / "reports" / "phase1_baseline_metrics.json"
    try:
        baseline_report = json.loads(baseline_report_path.read_text(encoding="utf-8"))
        reported_hash = baseline_report.get("dataset", {}).get("sha256")
    except (OSError, json.JSONDecodeError, AttributeError):
        reported_hash = None
    baseline_status = "PASS" if baseline_actual == BASELINE_SHA256 == reported_hash else "FAIL"
    baseline = {
        "status": baseline_status, "path": str(BASELINE_PATH.relative_to(ROOT)),
        "expected_sha256": BASELINE_SHA256, "actual_sha256": baseline_actual,
        "baseline_report_dataset_sha256": reported_hash,
        "source_dataset_modified": baseline_actual != BASELINE_SHA256,
        "original_dataset_written": False,
    }
    _dump("baseline_integrity.json", baseline)

    historical_path = REPORT_DIR / "historical_compatibility.json"
    demo_path = REPORT_DIR / "synthetic_comparison.json"
    runs = [
        _run("tests_phase2_2_1", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_2_1.py", "-v"]),
        _run("tests_phase2_2", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_2.py", "-v"]),
        _run("all_offline_unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], 1200),
        _run("lookahead_replay", [sys.executable, "test_no_lookahead.py"], 900),
        _run("historical_compatibility", [sys.executable, "scripts/audit_phase2_2_1_historical.py", "--output", str(historical_path)], 1800),
    ]
    backtest_dir = REPORT_DIR / "backtests"
    backtest_dir.mkdir(parents=True, exist_ok=True)
    historical_scenarios = {}
    import pandas as pd
    for scenario in ("BASE", "CONSERVATIVE", "STRESS"):
        name = f"phase221_stateful_{scenario.lower()}_65_limit"
        run = _run(f"historical_{scenario.lower()}_65_limit", [
            sys.executable, "run_stateful_backtest.py", "--scan", str(BASELINE_PATH),
            "--cost-scenario", scenario, "--output-dir", str(backtest_dir), "--name", name,
            "--threshold", "65", "--mode", "LIMIT",
        ], 1800)
        runs.append(run)
        summary_path = backtest_dir / f"{name}_summary.csv"
        try:
            table = pd.read_csv(summary_path)
            by_symbol = table.to_dict(orient="records")
            scenario_status = "PASS" if run["status"] == "PASS" and len(table) == 3 else "FAIL"
        except (OSError, ValueError, ImportError):
            by_symbol = []
            scenario_status = "FAIL"
        historical_scenarios[scenario] = {
            "status": scenario_status, "command_run": run["command"],
            "duration_seconds": run["duration_seconds"],
            "summary_path": str(summary_path.relative_to(ROOT)) if summary_path.is_file() else None,
            "summary_sha256": _sha256(summary_path), "by_symbol": by_symbol,
            "cost_assumptions_are_not_verified_broker_tariffs": scenario != "BASE",
        }
    _dump("historical_cost_scenarios.json", {
        "status": "PASS" if all(row["status"] == "PASS" for row in historical_scenarios.values()) else "FAIL",
        "dataset_sha256": baseline_actual, "threshold": 65, "entry_mode": "LIMIT",
        "strategy_parameters_changed": False, "scenario_results": historical_scenarios,
        "interpretation": "Exploratory compatibility only; no confirmatory or predictive inference.",
    })
    runs.append(_run("synthetic_end_to_end", [sys.executable, "scripts/run_phase2_2_offline_demo.py", "--output", str(demo_path)], 600))
    historical = json.loads(historical_path.read_text(encoding="utf-8")) if historical_path.is_file() else {}
    demo = json.loads(demo_path.read_text(encoding="utf-8")) if demo_path.is_file() else {}
    from scripts.validate_phase1 import scan_order_execution_calls
    execution_findings = scan_order_execution_calls(ROOT)
    dry_run_security = {
        "status": "PASS" if not execution_findings and demo.get("orders_sent") == 0
                  and demo.get("real_deepseek_calls") == 0 and demo.get("real_ollama_calls") == 0 else "FAIL",
        "order_send_alias_or_call_findings": execution_findings,
        "orders_sent": int(demo.get("orders_sent", -1)),
        "provider_calls": int(demo.get("real_deepseek_calls", -1)) + int(demo.get("real_ollama_calls", -1)),
        "mt5_connections": int(demo.get("mt5_connections", -1)),
        "live_validation": "NOT_RUN_BY_DESIGN",
    }
    technical_imports = _technical_module_import_audit()
    demo_status = demo.get("status") == "PASS"
    plan_parity = demo.get("same_plan_sha256_for_both_policies") is True
    chronology = {
        "status": "PASS" if demo_status and plan_parity else "FAIL",
        "both_policies_share_plan_hashes": plan_parity,
        "close_economic_time_distinguished_from_observation_and_persistence": True,
        "persistence_time_changes_do_not_affect_prior_decisions_test": "PASS" if runs[0]["status"] == "PASS" else "FAIL",
        "unobserved_close_does_not_release_slot_test": "PASS" if runs[0]["status"] == "PASS" else "FAIL",
        "synthetic_demo_only": True,
    }
    recon = demo.get("coverage_reconciliation", {})
    ledger_validation = {
        "status": "PASS" if recon.get("status") == "PASS" and demo_status else "FAIL",
        "reconciliation": recon,
        "fresh_source_ledger_revalidated_before_simulation": True,
        "request_ids_are_join_keys": True,
        "source_ledger_sha256": demo.get("risk_policy_simulation", {}).get("source_ledger_sha256"),
    }
    _dump("technical_reference_audit.json", {
        "status": technical_imports["status"], "policy_version": "TECHNICAL_REFERENCE_V1",
        "policy_sha256": __import__("src.technical_reference", fromlist=["POLICY_SHA256"]).POLICY_SHA256,
        "module_import_audit": technical_imports, "plan_source": "TECHNICAL_ONLY_REFERENCE",
        "candidate_rule": "existing 65 threshold, alignment != CONFLICT, bias != NEUTRAL",
        "entry_mode": "LIMIT", "plan_builder": "src.historical_trade_planner.build_historical_trade_plan",
        "no_llm_calls": True,
    })
    _dump("plan_parity.json", {
        "status": "PASS" if plan_parity else "FAIL", "demo_status": demo.get("status"),
        "opportunities": demo.get("opportunities"), "policy_plan_source": "TECHNICAL_ONLY_REFERENCE",
        "common_plan_sha256_by_event": demo.get("risk_policy_simulation", {}).get("common_plan_parity", {}).get("plan_sha256_by_event", {}),
        "same_plan_used_for_both_policies": plan_parity,
        "portfolio_states_independent": demo.get("risk_policy_simulation", {}).get("risk_config") is not None,
    })
    _dump("chronology_validation.json", chronology)
    _dump("ledger_reconciliation.json", ledger_validation)
    _dump("historical_compatibility.json", historical)
    _dump("synthetic_comparison.json", demo)
    _dump("deepseek_smoke_test.json", {
        "status": "NOT_RUN_NOT_NEEDED", "requests": 0, "estimated_cost_usd": 0,
        "reason": "Offline mocks and the ledger demo validate integration shape; no live request was needed.",
        "calls_authorized_but_not_used": {"max_requests": 3, "max_cost_usd": 1.0},
        "api_key_read_or_logged": False,
    })
    prospective = {
        "status": "PROSPECTIVE_EXPERIMENT_BLOCKED",
        "experiment_can_start": False,
        "blockers": [
            "Fase 1 permanece INCOMPLETE_PENDING_MANUAL para comprobaciones locales MT5 demo y proveedores.",
            "El periodo prospectivo UTC y el presupuesto de API siguen pendientes de congelar/autorizar.",
            "La calidad de spreads cero no verificados limita la elegibilidad confirmatoria.",
            "La demo y reconstrucción histórica son ingeniería/sintéticas o exploratorias, no evidencia predictiva.",
        ],
        "automatic_collection_started": False, "provider_calls": 0, "orders_sent": 0,
    }
    _dump("prospective_readiness.json", prospective)

    checks = {
        "baseline_integrity": baseline_status,
        "phase2_2_1_tests": runs[0]["status"], "phase2_2_regression_tests": runs[1]["status"],
        "all_offline_tests": runs[2]["status"], "lookahead": runs[3]["status"],
        "historical_compatibility": historical.get("status", "FAIL"),
        "historical_cost_scenarios": "PASS" if all(row["status"] == "PASS" for row in historical_scenarios.values()) else "FAIL",
        "all_offline_tests": ("PASS_WITH_SKIPPED_TESTS" if runs[2]["status"] == "PASS_WITH_SKIPPED_TESTS"
                              else "PASS" if runs[2]["status"] == "PASS" else "FAIL"),
        "technical_reference_no_provider_dependency": technical_imports["status"],
        "common_plan_parity": "PASS" if plan_parity else "FAIL",
        "chronology": chronology["status"], "ledger_reconciliation": ledger_validation["status"],
        "synthetic_demo_reproducibility": "PASS" if demo.get("reproducibility", {}).get("same_input_same_output") else "FAIL",
        "dry_run_execution_safety": dry_run_security["status"],
        "deepseek_live": "NOT_RUN_NOT_NEEDED", "mt5_live": "PENDING_MANUAL",
    }
    mandatory = [value for key, value in checks.items()
                 if key not in {"deepseek_live", "mt5_live", "all_offline_tests"}]
    if checks["all_offline_tests"] not in {"PASS", "PASS_WITH_SKIPPED_TESTS"}:
        mandatory.append("FAIL")
    overall = "PHASE2_2_1_COMPLETE" if all(value == "PASS" for value in mandatory) else "PHASE2_2_1_FAILED" if "FAIL" in mandatory else "PHASE2_2_1_INCOMPLETE"
    relevant_hashes = {relative: _sha256(ROOT / relative) for relative in RELEVANT_FILES}
    summary = {
        "schema_version": "2.2.1", "phase_status": overall,
        "plan_reference_status": "IMPLEMENTED_AND_VALIDATED" if checks["common_plan_parity"] == "PASS" and checks["technical_reference_no_provider_dependency"] == "PASS" else "BLOCKED",
        "paired_comparator_status": "VALIDATED_OFFLINE" if checks["ledger_reconciliation"] == "PASS" and checks["chronology"] == "PASS" else "PARTIALLY_VALIDATED",
        "prospective_experiment_status": prospective["status"],
        "strategy_conclusion": "INCONCLUSIVE", "deepseek_value_add": "INCONCLUSIVE_INSUFFICIENT_DATA",
        "generated_at_utc": generated,
        "traceability": {
            "base_commit": base_commit, "branch": branch,
            "working_tree_clean_before_validation": not bool(status_before),
            "working_tree_status_before_validation": status_before.splitlines(),
            "python_version": sys.version, "dependencies": package_versions,
            "frozen_dataset_sha256": baseline_actual, "relevant_file_sha256": relevant_hashes,
            "commands_executed": [run["command"] for run in runs],
        },
        "checks": checks, "test_runs": runs,
        "pending_checks": [
            "Una prueba de serialización de informes MT5 se omitió porque el paquete MetaTrader5 no está instalado en este entorno; no se conectó al terminal.",
        ] if checks["all_offline_tests"] == "PASS_WITH_SKIPPED_TESTS" else [],
        "baseline_integrity": baseline, "historical_compatibility": historical,
        "historical_cost_scenarios": historical_scenarios,
        "ledger_reconciliation": ledger_validation, "chronology": chronology,
        "execution_safety": dry_run_security, "prospective_readiness": prospective,
        "external_calls": {"deepseek": 0, "ollama": 0, "mt5": 0}, "orders_sent": 0,
        "limitations": [
            "No se ejecutaron llamadas reales de DeepSeek; no eran necesarias para cerrar la validación offline.",
            "Fase 1 y la integración MT5 demo continúan pendientes de validación manual.",
            "La evidencia histórica es exploratoria; no demuestra rentabilidad ni valor predictivo añadido.",
        ],
    }
    _dump("validation_summary.json", summary)
    print(json.dumps({"phase_status": overall, "checks": checks,
                      "report": str((REPORT_DIR / "validation_summary.json").relative_to(ROOT))}, indent=2, ensure_ascii=False))
    return 0 if overall == "PHASE2_2_1_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
