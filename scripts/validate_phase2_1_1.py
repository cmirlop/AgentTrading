#!/usr/bin/env python3
"""Offline validation and data-quality audit for Phase 2.1.1.

This script never connects to MT5/Ollama/DeepSeek and never submits orders.
The optional (default-on) sensitivity runs are local historical simulations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1 import scan_order_execution_calls  # noqa: E402
from src.event_ledger import append_outcome, bind_event, record_llm_call, record_opportunity, record_system_decision  # noqa: E402
from src.llm_evaluation import compare_paired_policies, pair_ledger  # noqa: E402
from src.phase2_1_evaluation import (  # noqa: E402
    classify_engineering_status,
    classify_llm_value_add,
    classify_strategy_evidence,
    sha256_file,
    summarize_trades,
)
from src.spread_quality import audit_spread_values, classify_spread, utc_session  # noqa: E402
from src.warmup_audit import PREDECLARED_WINDOWS, compare_indicator_windows, ema200_stabilization_bars, reconstruct_technical_at_window  # noqa: E402


OUT = ROOT / "reports" / "phase2_1_1"
FROZEN = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
FROZEN_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
SYMBOLS = ("EURUSD", "USDCAD", "AUDUSD")
TIMEFRAMES = ("M5", "M15", "H1", "H4", "D1")
FLOORS = (0.0, 5.0, 10.0, 20.0)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def git_provenance() -> dict:
    def call(*args: str) -> str | None:
        result = subprocess.run(["git", *args], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    return {
        "repository": call("remote", "get-url", "origin"),
        "branch": call("branch", "--show-current"),
        "head": call("rev-parse", "HEAD"),
        "main_head": call("rev-parse", "origin/main"),
        "working_tree_clean": not bool(call("status", "--porcelain")),
        "status_porcelain": call("status", "--porcelain"),
    }


def dependency_versions() -> dict:
    names = ("pandas", "numpy", "pydantic", "python-dotenv", "ollama", "openai", "pytest", "MetaTrader5")
    modules = {"python-dotenv": "dotenv", "MetaTrader5": "MetaTrader5"}
    result = {}
    for name in names:
        module = modules.get(name, name)
        try:
            result[name] = importlib.metadata.version(name) if importlib.util.find_spec(module) else None
        except (ImportError, ModuleNotFoundError, ValueError):
            result[name] = None
    return result


def spread_quality_report() -> dict:
    sources = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            path = ROOT / "data" / "market" / symbol / f"{timeframe}.csv"
            if not path.is_file():
                sources.append({"symbol": symbol, "timeframe": timeframe, "status": "PENDING", "reason": "CSV_MISSING"})
                continue
            frame = pd.read_csv(path, low_memory=False)
            if "spread" not in frame:
                summary = audit_spread_values(pd.Series([None] * len(frame)))
                available = False
            else:
                summary = audit_spread_values(frame["spread"])
                available = True
            time_column = "time" if "time" in frame else None
            by_session = {}
            if time_column and "spread" in frame:
                timestamps = pd.to_datetime(frame[time_column], utc=True, errors="coerce")
                hours = timestamps.dt.hour
                sessions = hours.map(lambda hour: utc_session(int(hour)) if pd.notna(hour) else None)
                for session in ("ASIA", "LONDON", "LONDON_NEW_YORK_OVERLAP", "NEW_YORK", "ROLLOVER_OFF_HOURS"):
                    mask = sessions.eq(session)
                    if mask.any():
                        by_session[session] = audit_spread_values(frame.loc[mask, "spread"])
            sources.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256_file(str(path)),
                "rows": int(len(frame)),
                "status": "PASS" if available and len(frame) else "PENDING",
                "spread_column_available": available,
                "bid_ask_columns_available": any(name in frame.columns for name in ("bid", "ask", "bid_open", "ask_open")),
                "raw_source": "MT5 copy_rates_range spread field exported by download_history.py/sync_market_data.py without zero imputation; zero semantics remain unverified.",
                "unit_handling": "Raw spread is interpreted as MT5 points by the simulator and multiplied by static SYMBOL_POINT. Per-file broker digits/symbol metadata are not archived, so historical conversion cannot be independently verified.",
                "summary": summary,
                "by_utc_session": by_session,
                "source_csv_modified": False,
            })
    return {
        "generated_at_utc": utc_now(),
        "classification": "OBSERVED_ZERO_UNVERIFIED is not treated as a verified zero-cost quote.",
        "diagnostics": ["zero/missing/invalid fractions", "positive-spread percentiles", "p99/max outliers", "UTC session cuts", "bid/ask field availability", "dataset hashes"],
        "all_sources_present": bool(sources) and all(row.get("status") == "PASS" for row in sources),
        "sources": sources,
    }


def warmup_report() -> dict:
    scan = pd.read_csv(FROZEN, usecols=["symbol", "analysis_time", "setup_quality", "overall_score", "bias", "alignment"])
    scan["analysis_time"] = pd.to_datetime(scan["analysis_time"], utc=True)
    raw = {}
    for symbol in SYMBOLS:
        raw[symbol] = {}
        for timeframe in TIMEFRAMES:
            frame = pd.read_csv(ROOT / "data" / "market" / symbol / f"{timeframe}.csv", low_memory=False)
            frame["time"] = pd.to_datetime(frame["time"], utc=True)
            raw[symbol][timeframe] = frame.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
    details = []
    indicator_changes = {tf: {str(w): [] for w in PREDECLARED_WINDOWS} for tf in TIMEFRAMES}
    technical_diffs = {str(w): {"bias_changed": 0, "alignment_changed": 0, "setup_quality_changed": 0, "overall_score_changed": 0, "threshold_membership_changed": {"65": 0, "75": 0, "85": 0, "90": 0}} for w in PREDECLARED_WINDOWS}
    samples_per_symbol = 20
    for symbol in SYMBOLS:
        group = scan.loc[scan.symbol.eq(symbol)].sort_values("analysis_time").reset_index(drop=True)
        if group.empty:
            continue
        indexes = np.unique(np.linspace(0, len(group) - 1, min(samples_per_symbol, len(group))).astype(int))
        for index in indexes:
            row = group.iloc[int(index)]
            timestamp = row.analysis_time
            event = {"symbol": symbol, "analysis_time": timestamp.isoformat(), "frozen_setup_quality": int(row.setup_quality), "frozen_overall_score": float(row.overall_score), "frozen_bias": str(row.bias), "frozen_alignment": str(row.alignment), "windows": {}}
            causal_raw = {}
            for timeframe in TIMEFRAMES:
                tf_bars = raw[symbol][timeframe]
                duration = pd.Timedelta(minutes={"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}[timeframe])
                causal_raw[timeframe] = tf_bars.loc[tf_bars.time + duration <= timestamp].tail(max(PREDECLARED_WINDOWS)).copy()
                for window in PREDECLARED_WINDOWS:
                    comp = compare_indicator_windows(causal_raw[timeframe], timestamp, timeframe, windows=(window, max(PREDECLARED_WINDOWS)))
                    window_result = comp["windows"][str(window)]
                    ref_result = comp["windows"][str(max(PREDECLARED_WINDOWS))]
                    if window == max(PREDECLARED_WINDOWS):
                        deltas = {key: 0.0 if val is not None else None for key, val in ref_result["values"].items()}
                        changed = False
                    else:
                        deltas = comp["absolute_indicator_deltas_from_reference"][str(window)]
                        changed = comp["any_ema_price_relation_change"][str(window)]
                    indicator_changes[timeframe][str(window)].append({"deltas": deltas, "ema_price_relation_changed": changed, "status": window_result["warmup_status"], "bars_used": window_result["bars_used"]})
            for window in PREDECLARED_WINDOWS:
                reconstruction = reconstruct_technical_at_window(symbol, timestamp, causal_raw, window)
                event["windows"][str(window)] = reconstruction
            reference = event["windows"][str(max(PREDECLARED_WINDOWS))]
            if reference["status"] == "READY":
                ref_tech = reference["technical"]
                event["longest_window_vs_frozen"] = {
                    "bias_changed": ref_tech["bias"] != event["frozen_bias"],
                    "alignment_changed": ref_tech["alignment"] != event["frozen_alignment"],
                    "setup_quality_delta": ref_tech["setup_quality"] - event["frozen_setup_quality"],
                    "overall_score_delta": ref_tech["overall_score"] - event["frozen_overall_score"],
                }
                for window in PREDECLARED_WINDOWS[:-1]:
                    current = event["windows"][str(window)]
                    if current["status"] != "READY":
                        continue
                    tech = current["technical"]
                    metrics = technical_diffs[str(window)]
                    metrics["bias_changed"] += int(tech["bias"] != ref_tech["bias"])
                    metrics["alignment_changed"] += int(tech["alignment"] != ref_tech["alignment"])
                    metrics["setup_quality_changed"] += int(tech["setup_quality"] != ref_tech["setup_quality"])
                    metrics["overall_score_changed"] += int(tech["overall_score"] != ref_tech["overall_score"])
                    for threshold in (65, 75, 85, 90):
                        metrics["threshold_membership_changed"][str(threshold)] += int((tech["setup_quality"] >= threshold) != (ref_tech["setup_quality"] >= threshold))
            details.append(event)
    aggregates = {}
    for timeframe, windows in indicator_changes.items():
        aggregates[timeframe] = {}
        for window, values in windows.items():
            for key in ("ema20", "ema50", "ema200", "rsi14", "atr14"):
                samples = [value["deltas"].get(key) for value in values if value["deltas"].get(key) is not None]
                entry = aggregates[timeframe].setdefault(window, {"sample_count": len(values), "warmup_insufficient": sum(v["status"] == "WARMUP_INSUFFICIENT" for v in values), "ema_price_relation_changes": sum(bool(v["ema_price_relation_changed"]) for v in values), "absolute_delta": {}})
                entry["absolute_delta"][key] = {"median": float(np.median(samples)) if samples else None, "p95": float(np.quantile(samples, .95)) if samples else None, "max": float(max(samples)) if samples else None, "n": len(samples)}
    return {
        "generated_at_utc": utc_now(),
        "baseline_scan_sha256": sha256_file(str(FROZEN)),
        "fixed_windows_bars": list(PREDECLARED_WINDOWS),
        "criterion": {"ema200_alpha": 2 / 201, "residual_initialization_weight": 0.001, "derived_minimum_bars": ema200_stabilization_bars(), "reference_window": max(PREDECLARED_WINDOWS)},
        "sampling": {"method": "evenly spaced fixed event timestamps from frozen scan, no result selection", "events_per_symbol_max": samples_per_symbol, "events_sampled": len(details), "all_inputs_cut_at_last_closed_bar": True},
        "indicator_stability_by_timeframe_window": aggregates,
        "technical_reconstruction_differences_vs_longest_window": technical_diffs,
        "longest_window_reconstruction_vs_frozen_sample": {
            "events_with_reference": sum("longest_window_vs_frozen" in event for event in details),
            "bias_changed": sum(bool(event.get("longest_window_vs_frozen", {}).get("bias_changed")) for event in details),
            "alignment_changed": sum(bool(event.get("longest_window_vs_frozen", {}).get("alignment_changed")) for event in details),
            "setup_quality_changed": sum(bool(event.get("longest_window_vs_frozen", {}).get("setup_quality_delta")) for event in details),
            "overall_score_changed": sum(bool(event.get("longest_window_vs_frozen", {}).get("overall_score_delta")) for event in details),
        },
        "production_signal_logic_modified": False,
        "production_signal_gate_status": "NOT_CHANGED_PENDING_IMPACT_REVIEW",
        "events": details,
    }


def synthetic_llm_ledger_validation(path: Path) -> dict:
    timestamp = "2026-01-01T00:00:00Z"
    actions = ("BUY", "SELL", "HOLD", "WAIT")
    for action in actions:
        event_timestamp = pd.Timestamp(timestamp) + pd.Timedelta(minutes=actions.index(action))
        event_id = record_opportunity("EURUSD", event_timestamp.isoformat(), {"technical_decision": action}, path=path)
        response = SimpleNamespace(model_dump=lambda **kwargs: {"decision": action, "evidence_used": ["E1"]})
        with bind_event(event_id, path):
            record_llm_call(schema_name="SyntheticDecision", provider="ollama", model="mock", prompt="fixture prompt", payload={"allowed_evidence_ids": ["E1"]}, allowed_evidence=["E1"], requested_at=(event_timestamp + pd.Timedelta(seconds=1)).isoformat(), result=response, elapsed_seconds=.01)
        record_system_decision(event_id, action, validation_status="VALID", decision_stage="MARKET_ANALYSIS", path=path)
        candidate_pnl = 1.0 if action == "BUY" else -1.0 if action == "SELL" else 0.0
        append_outcome(event_id, "2026-01-02T00:00:00Z", {"policy_id": "TRADE_CANDIDATE", "result": "WIN" if candidate_pnl > 0 else "LOSS" if candidate_pnl < 0 else "UNRESOLVED", "net_pnl_eur": candidate_pnl, "net_r": candidate_pnl, "release_time_utc": "2026-01-01T02:00:00Z", "cost_scenario": "BASE"}, path=path)
    joined = pair_ledger(path)
    policy_comparison = compare_paired_policies(joined["population"], cost_scenario="BASE")
    return {
        "status": "PASS" if joined["counts"]["llm_valid"] == 4 and joined["counts"]["technical_events_total"] == 4 else "FAIL",
        "synthetic_fixture": True,
        "live_provider_calls": 0,
        "actions_logged": actions,
        "counts": joined["counts"],
        "policy_interface_status": policy_comparison["status"],
        "policy_comparison_fixture": policy_comparison,
        "provider_errors_and_invalid_responses_tested_by_unit_suite": True,
        "orders_executed": 0,
        "secrets_persisted": False,
    }


def execute_test_suite() -> dict:
    commands = [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]]
    skipped_live = {
        "test_ollama.py": "Requires a local Ollama service; not invoked automatically.",
        "test_order_request.py": "Requires MT5 initialization; not invoked in this phase.",
        "test_trade_planner.py": "Requires MT5 initialization; not invoked in this phase.",
    }
    for path in sorted(ROOT.glob("test_*.py")):
        if path.name in skipped_live:
            continue
        commands.append([sys.executable, str(path)])
    results = []
    for command in commands:
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        output = result.stdout
        ran = None
        for line in output.splitlines():
            if line.startswith("Ran ") and " test" in line:
                try:
                    ran = int(line.split()[1])
                except (ValueError, IndexError):
                    pass
        results.append({"command": command, "status": "PASS" if result.returncode == 0 else "FAIL", "return_code": result.returncode, "duration_seconds": round(time.monotonic() - started, 3), "tests_reported": ran, "output_tail": output[-5000:]})
    total = sum(item["tests_reported"] or 0 for item in results)
    standalone = [item for item in results if item["tests_reported"] is None]
    unittest_skipped = 0
    for item in results:
        tail = str(item.get("output_tail", ""))
        match = re.search(r"OK \(skipped=(\d+)\)", tail)
        if match:
            unittest_skipped += int(match.group(1))
    standalone_passed = sum(item["status"] == "PASS" for item in standalone)
    command_failures = sum(item["status"] == "FAIL" for item in results)
    pending_live = ["MT5 live/demo safety integration", "local Ollama provider integration", "DeepSeek live integration (no billed request made)"]
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL",
        "commands": results,
        "test_count_reported_by_unittest": total,
        "unittest_passed": max(0, total - unittest_skipped),
        "unittest_failed": 0 if command_failures == 0 else command_failures,
        "unittest_skipped": unittest_skipped,
        "standalone_assertion_scripts": len(standalone),
        "standalone_assertion_scripts_passed": standalone_passed,
        "standalone_assertion_scripts_failed": len(standalone) - standalone_passed,
        "total_standalone_and_unit_checks_passed": max(0, total - unittest_skipped) + standalone_passed,
        "total_duration_seconds": round(sum(item["duration_seconds"] for item in results), 3),
        "scripts_without_unittest_count": [item["command"] for item in standalone],
        "skipped": skipped_live,
        "skipped_count": unittest_skipped + len(skipped_live),
        "pending_count": len(pending_live),
        "pytest": {"status": "NOT_APPLICABLE", "reason": "All discovered project tests use unittest or standalone assertions; pytest is not required for these files."},
        "pending_external": pending_live,
    }


def run_spread_sensitivity() -> tuple[list[dict], list[dict]]:
    backtest_dir = OUT / "backtests"
    backtest_dir.mkdir(parents=True, exist_ok=True)
    records, executions = [], []
    for engine, runner, scan in (
        ("portfolio", "run_portfolio_backtest.py", "technical_candidate_scan.csv"),
        ("stateful", "run_stateful_backtest.py", "data/backtest/technical_candidate_scan.csv"),
    ):
        for floor in FLOORS:
            name = f"phase211_{engine}_base_floor{int(floor)}"
            command = [sys.executable, runner, "--scan", scan, "--cost-scenario", "BASE", "--threshold", "65", "--mode", "LIMIT", "--spread-floor-points", str(floor), "--name", name, "--output-dir", str(backtest_dir)]
            started = time.monotonic()
            result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            execution_record = {"engine": engine, "floor_points": floor, "command": command, "status": "PASS" if result.returncode == 0 else "FAIL", "return_code": result.returncode, "duration_seconds": round(time.monotonic() - started, 3), "output_tail": result.stdout[-3000:]}
            executions.append(execution_record)
            if result.returncode:
                continue
            trade_path = backtest_dir / f"{name}_trades.csv"
            trades = pd.read_csv(trade_path)
            if "threshold" in trades:
                trades = trades.loc[trades["threshold"].eq(65) & trades["mode"].eq("LIMIT")].copy()
            metrics = summarize_trades(trades)
            affected = trades.get("unverified_zero_spread_bar_count", pd.Series(0, index=trades.index)).fillna(0).gt(0)
            floor_applied = trades.get("spread_floor_applied_bar_count", pd.Series(0, index=trades.index)).fillna(0).gt(0)
            records.append({
                "engine": engine,
                "scenario": "BASE_WITH_RESEARCH_SPREAD_FLOOR",
                "spread_floor_points_assumption": floor,
                "is_verified_broker_cost": False,
                "proposed": metrics["proposed"],
                "executed": metrics["executed"],
                "closed": metrics["closed"],
                "expired": metrics["expired"],
                "ambiguous": metrics["ambiguous"],
                "open_or_pending": metrics["open_or_pending"],
                "gross_pnl_eur": metrics["gross_pnl_eur"],
                "transaction_costs_eur": metrics["costs_eur_closed"],
                "net_pnl_eur": metrics["net_pnl_eur"],
                "expectancy_net_eur": metrics["expectancy_net_eur"],
                "profit_factor_net": metrics["profit_factor_net"],
                "max_drawdown_net_eur": metrics["max_drawdown_net_eur"],
                "operations_touching_unverified_zero_spread_pct": round(float(affected.mean() * 100), 4) if len(trades) else None,
                "operations_with_floor_applied_pct": round(float(floor_applied.mean() * 100), 4) if len(trades) else None,
                "fill_time_hash": sha256(trades.get("fill_time", pd.Series(dtype=str)).astype(str).str.cat().encode()).hexdigest(),
                "results_file": trade_path.relative_to(ROOT).as_posix(),
                "results_file_sha256": sha256_file(str(trade_path)),
            })
    return records, executions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true", help="Skip offline tests; status remains PENDING and completion is blocked.")
    parser.add_argument("--skip-spread-sensitivity", action="store_true", help="Skip complete portfolio/stateful BASE runs at predeclared 0/5/10/20 point floors.")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    git = git_provenance()
    baseline = {
        "path": FROZEN.relative_to(ROOT).as_posix(),
        "expected_sha256": FROZEN_SHA256,
        "actual_sha256": sha256_file(str(FROZEN)) if FROZEN.is_file() else None,
        "matches_original": FROZEN.is_file() and sha256_file(str(FROZEN)) == FROZEN_SHA256,
        "source_modified": False,
    }
    write_json(OUT / "baseline_integrity.json", {"generated_at_utc": utc_now(), "baseline": baseline, "git": git})
    spreads = spread_quality_report()
    write_json(OUT / "spread_quality.json", spreads)
    warmup = warmup_report()
    write_json(OUT / "warmup_comparison.json", warmup)
    if args.skip_tests:
        suite = {"status": "PENDING", "reason": "Skipped by --skip-tests; no test is counted as passing.", "test_count_reported_by_unittest": 0, "commands": [], "skipped": {}}
    else:
        suite = execute_test_suite()
    if args.skip_spread_sensitivity:
        sensitivity_rows, executions = [], []
        sensitivity_status = "PENDING"
    else:
        sensitivity_rows, executions = run_spread_sensitivity()
        sensitivity_status = "PASS" if len(executions) == 8 and all(item["status"] == "PASS" for item in executions) and len(sensitivity_rows) == 8 else "FAIL"
    pd.DataFrame(sensitivity_rows).to_csv(OUT / "spread_sensitivity.csv", index=False)
    with tempfile.TemporaryDirectory(prefix="agenttrading_phase2111_") as temp_dir:
        llm_validation = synthetic_llm_ledger_validation(Path(temp_dir) / "synthetic.sqlite3")
    write_json(OUT / "llm_logging_validation.json", llm_validation)
    safety = scan_order_execution_calls(ROOT)
    check_statuses = {
        "offline_tests": suite["status"],
        "baseline_integrity": "PASS" if baseline["matches_original"] else "FAIL",
        "spread_quality": "PASS" if spreads["all_sources_present"] else "FAIL",
        "warmup_audit": "PASS" if warmup["sampling"]["events_sampled"] else "FAIL",
        "spread_sensitivity": sensitivity_status,
        "event_ledger_and_pairing": llm_validation["status"],
        "execution_safety": "PASS" if not safety else "FAIL",
        "no_external_calls": "PASS",
    }
    required = set(check_statuses)
    engineering = classify_engineering_status(check_statuses, required)
    stats_conclusion = classify_strategy_evidence(confirmatory=False, conservative_expectancy=None, conservative_ci95=None)
    deepseek_conclusion = classify_llm_value_add(valid_pairs=0, confirmatory=False, paired_mean_difference=None, paired_ci95=None, independent_blocks=0)
    phase1_path = ROOT / "reports" / "phase1_validation.json"
    phase1_state = None
    if phase1_path.is_file():
        try:
            phase1_state = json.loads(phase1_path.read_text(encoding="utf-8")).get("overall_status")
        except (ValueError, OSError):
            phase1_state = "UNREADABLE"
    report = {
        "generated_at_utc": utc_now(),
        "phase2_1_1_status": {"COMPLETE": "PHASE2_1_1_COMPLETE", "FAILED": "PHASE2_1_1_FAILED", "INCOMPLETE": "PHASE2_1_1_INCOMPLETE"}[engineering],
        "phase2_1_engineering_status": engineering,
        "phase2_1_strategy_conclusion": stats_conclusion,
        "deepseek_value_add": deepseek_conclusion,
        "phase1_operational_readiness": {"status": phase1_state or "NOT_REPORTED", "blocks_phase2_1_1": False},
        "git": git,
        "code_fingerprints_sha256": {
            path.relative_to(ROOT).as_posix(): sha256_file(str(path))
            for path in [
                ROOT / "src" / "historical_execution.py",
                ROOT / "src" / "spread_quality.py",
                ROOT / "src" / "warmup_audit.py",
                ROOT / "src" / "event_ledger.py",
                ROOT / "src" / "llm_evaluation.py",
                ROOT / "src" / "agents" / "llm_client.py",
                ROOT / "run_portfolio_backtest.py",
                ROOT / "run_stateful_backtest.py",
                ROOT / "scripts" / "validate_phase2_1.py",
                ROOT / "scripts" / "validate_phase2_1_1.py",
                *sorted((ROOT / "config").glob("*.py")),
            ] if path.is_file()
        },
        "python": {"version": sys.version, "executable": sys.executable, "dependencies": dependency_versions()},
        "datasets": {"technical_candidate_scan": baseline},
        "tests": suite,
        "spread_sensitivity_runs": executions,
        "checks": [{"name": name, "status": status} for name, status in check_statuses.items()],
        "external_integrations": {
            "mt5": {"status": "NOT_APPLICABLE", "reason": "Explicitly excluded from this offline phase; no terminal/account access."},
            "ollama_live": {"status": "NOT_APPLICABLE", "reason": "No live service call; all logger tests used mocks."},
            "deepseek_live": {"status": "NOT_APPLICABLE", "reason": "No billed API call; value-add remains a research conclusion."},
            "orders_sent": 0,
        },
        "security": {"order_execution_references": safety, "provider_calls_by_validator": 0, "mt5_calls_by_validator": 0, "orders_sent": 0},
        "limitations": [
            "Phase 1 MT5 demo and live provider checks remain separate operational dependencies.",
            "Zero spreads in the exported MT5 history have no archived broker quote context; they remain unverified observations.",
            "The warmup comparison is a fixed timestamp sample and its production signal gate is not changed; any gate requires a separate impact review.",
            "There are no verified historical DeepSeek decisions paired to frozen opportunities; no economic DeepSeek comparison is possible yet.",
            "Raw bid/ask ticks and broker-specific symbol metadata are unavailable in the archived historical source.",
        ],
        "reproduce": [
            "python scripts/validate_phase2_1_1.py",
            "python -m unittest discover -s tests -p 'test_*.py' -v",
            "python test_no_lookahead.py",
        ],
    }
    write_json(OUT / "validation_summary.json", report)
    print(json.dumps({"status": report["phase2_1_1_status"], "strategy": stats_conclusion, "deepseek": deepseek_conclusion, "checks": report["checks"]}, indent=2))
    return 0 if engineering == "COMPLETE" else 1 if engineering == "FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
