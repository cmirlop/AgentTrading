#!/usr/bin/env python3
"""Run Phase 2.2 offline validations only; never contacts MT5 or an LLM."""

from __future__ import annotations

from datetime import datetime, timezone
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
REPORTS = ROOT / "reports" / "phase2_2"
BASELINE_PATH = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
EXPECTED_BASELINE_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
RELEVANT_FILES = [
    "src/event_ledger.py", "src/llm_evaluation.py", "src/paired_policy_simulator.py",
    "src/research_policy.py", "src/agents/llm_client.py", "src/agents/market_analyst.py",
    "run_market_agents.py", "run_multiagent_analysis.py", "run_dry_pipeline.py",
    "config/risk_config.py", "config/backtest_cost_config.py",
]


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str | None:
    completed = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


def _run(name: str, command: list[str], *, timeout: int = 300) -> dict:
    started = monotonic()
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                               timeout=timeout, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    output = (completed.stdout + "\n" + completed.stderr).strip()
    ran = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", output)
    skipped = re.search(r"skipped=(\d+)", output)
    failures = re.search(r"failures=(\d+)", output)
    errors = re.search(r"errors=(\d+)", output)
    summary = {
        "name": name, "status": "PASS" if completed.returncode == 0 else "FAIL",
        "return_code": completed.returncode, "duration_seconds": round(monotonic() - started, 3),
        "command": command, "tests_run": int(ran.group(1)) if ran else None,
        "unittest_duration_seconds": float(ran.group(2)) if ran else None,
        "tests_skipped": int(skipped.group(1)) if skipped else 0,
        "tests_failed": int(failures.group(1)) if failures else 0,
        "tests_errored": int(errors.group(1)) if errors else 0,
        "tests_passed": max(0, int(ran.group(1)) - (int(skipped.group(1)) if skipped else 0)
                             - (int(failures.group(1)) if failures else 0)
                             - (int(errors.group(1)) if errors else 0)) if ran else None,
        "summary_lines": [line.strip() for line in output.splitlines()
                          if line.strip().startswith(("Ran ", "OK", "FAILED", "ERROR:"))][-5:],
        "validation_markers": [line.strip() for line in output.splitlines()
                               if line.strip().startswith(("[OK]", "[FAIL]"))][-10:],
    }
    if completed.returncode:
        summary["failure_excerpt"] = output[-5000:]
    return summary


def _dump(name: str, payload: dict) -> None:
    (REPORTS / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    base_commit = _git("rev-parse", "HEAD")
    branch = _git("branch", "--show-current")
    status_before = _git("status", "--porcelain", "--untracked-files=all") or ""
    package_versions = {}
    for package in ("pandas", "numpy", "pydantic", "MetaTrader5", "ollama", "openai"):
        try:
            package_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_versions[package] = "NOT_INSTALLED"

    actual_baseline_hash = _sha256(BASELINE_PATH)
    baseline_report = json.loads((ROOT / "reports" / "phase1_baseline_metrics.json").read_text(encoding="utf-8"))
    baseline_status = "PASS" if actual_baseline_hash == EXPECTED_BASELINE_SHA256 == baseline_report.get("dataset", {}).get("sha256") else "FAIL"
    baseline = {
        "status": baseline_status, "path": str(BASELINE_PATH.relative_to(ROOT)),
        "expected_sha256": EXPECTED_BASELINE_SHA256, "actual_sha256": actual_baseline_hash,
        "baseline_report_sha256": _sha256(ROOT / "reports" / "phase1_baseline_metrics.json"),
        "baseline_report_dataset_sha256": baseline_report.get("dataset", {}).get("sha256"),
        "source_dataset_modified": actual_baseline_hash != EXPECTED_BASELINE_SHA256,
    }
    _dump("baseline_integrity.json", baseline)

    commands = [
        _run("phase2_2_tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_2.py", "-v"]),
        _run("all_offline_unittest", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]),
        _run("lookahead_replay", [sys.executable, "test_no_lookahead.py"], timeout=600),
    ]
    demo_path = REPORTS / "synthetic_experiment.json"
    commands.append(_run("offline_end_to_end_demo", [sys.executable, "scripts/run_phase2_2_offline_demo.py", "--output", str(demo_path)]))
    demo = json.loads(demo_path.read_text(encoding="utf-8")) if demo_path.is_file() else {}
    suite = commands[1]
    targeted = commands[0]
    lookahead = commands[2]
    demo_run = commands[3]

    from scripts.validate_phase1 import scan_order_execution_calls
    order_findings = scan_order_execution_calls(ROOT)
    security = {
        "status": "PASS" if not order_findings and demo.get("orders_sent") == 0
                  and demo.get("real_deepseek_calls") == 0 and demo.get("real_ollama_calls") == 0 else "FAIL",
        "order_execution_findings": order_findings,
        "orders_sent_by_phase2_2_validation": int(demo.get("orders_sent", -1)),
        "mt5_connections_attempted": 0,
        "provider_calls_attempted": int(demo.get("real_deepseek_calls", -1)) + int(demo.get("real_ollama_calls", -1)),
        "live_validations": "NOT_RUN_BY_DESIGN",
    }

    risk_parity = {
        "status": "PASS" if targeted["status"] == "PASS" and demo.get("risk_policy_simulation", {}).get("status") == "OFFLINE_RISK_PARITY_SIMULATION" else "FAIL",
        "risk_config": demo.get("risk_policy_simulation", {}).get("risk_config", {}),
        "synthetic_policy_metrics": demo.get("risk_policy_simulation", {}).get("policies", {}),
        "independent_portfolio_states": True,
        "technical_only_plan_available_from_current_product_flow": False,
        "current_product_plan_result": "NO_EVALUABLE_UNTIL_TECHNICAL_ONLY_PLAN_IS_FROZEN",
        "unavailable_live_margin_check": "PENDING_MT5_DEMO",
        "primary_cost_scenario": "CONSERVATIVE",
        "cost_assumption_status": "SYNTHETIC_ONLY_NOT_A_BROKER_TARIFF",
        "BASE_and_STRESS_historical_policy_comparison": "PENDING_COMMON_TECHNICAL_PLAN_AND_PROSPECTIVE_OUTCOMES",
    }
    ledger = {
        "status": "PASS" if targeted["status"] == "PASS" and demo.get("coverage_reconciliation", {}).get("status") == "PASS" else "FAIL",
        "sqlite_wal_append_only": True,
        "strict_research_logging": True,
        "concurrent_multiprocess_test": "PASS" if targeted["status"] == "PASS" else "FAIL",
        "reconciliation": demo.get("coverage_reconciliation", {}),
        "trade_contexts_exclude_account_balance_positions_and_volume": True,
    }
    coverage = demo.get("coverage_reconciliation", {})
    coverage["validator_status"] = "PASS" if coverage.get("status") == "PASS" else "FAIL"
    _dump("portfolio_parity.json", risk_parity)
    _dump("ledger_reliability.json", ledger)
    _dump("coverage_reconciliation.json", coverage)

    prospective = {
        "status": "PROSPECTIVE_EXPERIMENT_BLOCKED",
        "strategy_conclusion": "INCONCLUSIVE",
        "deepseek_value_add": "INCONCLUSIVE_INSUFFICIENT_DATA",
        "blockers": [
            "Fase 1 sigue INCOMPLETE_PENDING_MANUAL para verificaciones locales MT5/provider.",
            "El flujo actual usa la familia de estrategia DeepSeek para crear el plan; falta un plan TECHNICAL ONLY independiente para la misma población.",
            "No hay outcomes prospectivos emparejados ni periodo confirmatorio UTC congelado.",
            "Los precios API actuales y el presupuesto real deben autorizarse antes de cualquier llamada live.",
            "La calidad de spread histórica no permite tratar todos los costes como tarifas observadas del bróker.",
        ],
        "manual_checks_not_run": ["MT5 DEMO", "DeepSeek live", "Ollama live"],
        "no_real_calls_or_orders": True,
    }
    _dump("prospective_readiness.json", prospective)

    required_statuses = [baseline_status, targeted["status"], suite["status"], lookahead["status"], demo_run["status"], security["status"], risk_parity["status"], ledger["status"]]
    phase_status = "PHASE2_2_COMPLETE" if all(state == "PASS" for state in required_statuses) else "PHASE2_2_INCOMPLETE" if "FAIL" not in required_statuses else "PHASE2_2_FAILED"
    relevant_hashes = {relative: _sha256(ROOT / relative) for relative in RELEVANT_FILES}
    summary = {
        "schema_version": "2.2",
        "phase_status": phase_status,
        "prospective_experiment_status": prospective["status"],
        "strategy_conclusion": prospective["strategy_conclusion"],
        "deepseek_value_add": prospective["deepseek_value_add"],
        "generated_at_utc": started_at,
        "traceability": {
            "base_commit": base_commit, "branch": branch,
            "working_tree_clean_before_validation": not bool(status_before),
            "working_tree_status_before_validation": status_before.splitlines(),
            "python_version": sys.version, "dependencies": package_versions,
            "baseline_dataset_sha256": actual_baseline_hash,
            "relevant_file_sha256": relevant_hashes,
            "commands_executed": [row["command"] for row in commands],
        },
        "checks": {
            "baseline_integrity": baseline_status,
            "phase2_2_tests": targeted["status"],
            "all_offline_unittest": suite["status"],
            "lookahead": lookahead["status"],
            "transaction_costs": "PENDING_HISTORICAL_OR_PROSPECTIVE_COMMON_PLAN_DATA",
            "synthetic_end_to_end": demo_run["status"],
            "portfolio_risk_parity_fixtures": risk_parity["status"],
            "ledger_and_coverage": ledger["status"],
            "execution_safety": security["status"],
            "mt5_live": "PENDING",
            "deepseek_live": "PENDING",
            "ollama_live": "PENDING",
            "confirmed_economic_comparison": "NOT_APPLICABLE_NO_PROSPECTIVE_DATA",
        },
        "test_runs": commands,
        "security": security,
        "limitations": prospective["blockers"],
        "all_provider_calls": 0,
        "all_mt5_connections": 0,
        "all_orders_sent": 0,
    }
    _dump("validation_summary.json", summary)
    print(json.dumps({"phase_status": phase_status, "prospective": prospective["status"],
                      "checks": summary["checks"], "report": str((REPORTS / "validation_summary.json").relative_to(ROOT))},
                     indent=2, ensure_ascii=False))
    return 0 if phase_status == "PHASE2_2_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
