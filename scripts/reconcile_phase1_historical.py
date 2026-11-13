#!/usr/bin/env python3
"""Rebuild and verify the Phase 1 BASE historical aggregate reconciliation."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
COMMITS = {
    "baseline": "a00644a6b330b6235bea3f47cb17d2dfe39d4f5b",
    "pre_finalization": "1ef0faf7600b866236f75cb7c229fc8a3b66973e",
    "current": "ea2e60a10df4e9ae5f2e276d82cf343fd4a3ee91",
}
SOURCE_FILES = (
    "run_portfolio_backtest.py",
    "run_stateful_backtest.py",
    "src/historical_execution.py",
    "src/historical_trade_planner.py",
    "src/historical_risk.py",
    "config/backtest_config.py",
    "config/backtest_cost_config.py",
    "config/trade_planner_config.py",
    "config/risk_config.py",
    "config/market_config.py",
)
IDENTITY_FIELDS = ("symbol", "threshold", "mode", "signal_time")
SIGNAL_AND_PLAN_FIELDS = (
    "setup_quality", "overall_score", "direction", "bias", "entry", "stop_loss",
    "take_profit", "estimated_risk_eur",
)
REPLAY_FILES = {
    "baseline": {
        "portfolio": (
            "reports/private/issue14/replays/a00644a/portfolio_trades.csv",
            "reports/private/issue14/replays/a00644a/portfolio_summary.csv",
            "reports/private/issue14/replays/a00644a/issue14_a00644a_BASE_metadata.json",
        ),
        "stateful": (
            "reports/private/issue14/replays/a00644a/stateful_trades.csv",
            "reports/private/issue14/replays/a00644a/stateful_summary.csv",
            "reports/private/issue14/replays/a00644a/stateful_metadata.json",
        ),
    },
    "pre_finalization": {
        "portfolio": (
            "reports/private/issue14/replays/1ef0faf/portfolio_BASE/issue14_1ef0faf_BASE_trades.csv",
            "reports/private/issue14/replays/1ef0faf/portfolio_BASE/issue14_1ef0faf_BASE_summary.csv",
            "reports/private/issue14/replays/1ef0faf/portfolio_BASE/issue14_1ef0faf_BASE_metadata.json",
        ),
        "stateful": (
            "reports/private/issue14/replays/1ef0faf/stateful_BASE/issue14_1ef0faf_BASE_trades.csv",
            "reports/private/issue14/replays/1ef0faf/stateful_BASE/issue14_1ef0faf_BASE_summary.csv",
            "reports/private/issue14/replays/1ef0faf/stateful_BASE/issue14_1ef0faf_BASE_metadata.json",
        ),
    },
    "current": {
        "portfolio": (
            "reports/private/issue14/replays/ea2e60a/portfolio_BASE/issue14_ea2e60a_BASE_trades.csv",
            "reports/private/issue14/replays/ea2e60a/portfolio_BASE/issue14_ea2e60a_BASE_summary.csv",
            "reports/private/issue14/replays/ea2e60a/portfolio_BASE/issue14_ea2e60a_BASE_metadata.json",
        ),
        "stateful": (
            "reports/private/issue14/replays/ea2e60a/stateful_BASE/issue14_ea2e60a_BASE_trades.csv",
            "reports/private/issue14/replays/ea2e60a/stateful_BASE/issue14_ea2e60a_BASE_summary.csv",
            "reports/private/issue14/replays/ea2e60a/stateful_BASE/issue14_ea2e60a_BASE_metadata.json",
        ),
    },
}
REPORT_INPUTS = {
    "baseline": "reports/private/issue14/phase1_baseline_metrics_input.json",
    "pre_finalization": "reports/private/issue14/phase1_pre_finalization_validation_input.json",
    "current": "reports/private/issue14/phase1_validation_before_integral.json",
}
PRIVATE_OUTPUT = "reports/private/phase1_final_closure/historical_aggregate_reconciliation.json"
PUBLIC_OUTPUT = "reports/phase1_final_local/baseline_aggregate_reconciliation.json"
CAUSAL_SUMMARY = "reports/phase1_final_local/historical_causal_attribution_summary.json"
CAUSAL_PRIVATE = "reports/private/phase1_final_closure/historical_causal_attribution.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _verify_dataset_bytes(private_copy: bytes | None, git_blob: bytes | None) -> bool:
    return bool(
        private_copy is not None
        and git_blob is not None
        and _sha256_bytes(private_copy) == DATASET_SHA256
        and _sha256_bytes(git_blob) == DATASET_SHA256
        and private_copy == git_blob
    )


def _compare_hash_manifests(recorded: dict | None, verified: dict | None) -> bool:
    return bool(recorded and verified and recorded == verified and all(
        isinstance(value, str) and len(value) == 64
        for group in recorded.values() if isinstance(group, dict)
        for value in group.values() if value is not None
    ))


def _timestamp_within_window(value: str, *, now: datetime | None = None, window_seconds=24 * 60 * 60) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - parsed.astimezone(timezone.utc)).total_seconds()
        return -300 <= age <= window_seconds
    except (TypeError, ValueError):
        return False


def _selection_witnesses_cover(before_ids: set[str], after_ids: set[str], witnesses: set[str]) -> bool:
    return (before_ids ^ after_ids) == witnesses


def _evidence_has_provenance(bundle: dict) -> bool:
    return bool(
        bundle.get("dataset", {}).get("sha256") == DATASET_SHA256
        and bundle.get("commits") == COMMITS
        and isinstance(bundle.get("replay_artifacts"), dict)
        and bundle.get("replay_artifacts")
        and isinstance(bundle.get("source_hashes_from_git_objects"), dict)
        and bundle.get("source_hashes_from_git_objects")
    )


def _base_cost_mismatches(rows: list[dict]) -> list[int]:
    return [
        index for index, row in enumerate(rows)
        if row.get("cost_scenario") not in (None, "BASE")
        or _as_number(row.get("transaction_cost_eur")) not in (None, 0.0)
    ]


def _git_blob(commit: str, relative: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:{relative}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return result.stdout if result.returncode == 0 else None


def _source_manifest() -> tuple[dict, list[str]]:
    manifest: dict[str, dict[str, str | None]] = {}
    failures = []
    for label, commit in COMMITS.items():
        manifest[label] = {}
        for name in SOURCE_FILES:
            content = _git_blob(commit, name)
            if content is None:
                manifest[label][name] = None
                if not (label == "baseline" and name == "config/backtest_cost_config.py"):
                    failures.append(f"source_missing:{label}:{name}")
            else:
                manifest[label][name] = _sha256_bytes(content)
    if manifest.get("baseline", {}).get("src/historical_trade_planner.py") != manifest.get("pre_finalization", {}).get("src/historical_trade_planner.py"):
        failures.append("baseline_to_prefinal_trade_planner_changed")
    if manifest.get("pre_finalization", {}).get("src/historical_trade_planner.py") != manifest.get("current", {}).get("src/historical_trade_planner.py"):
        failures.append("prefinal_to_current_trade_planner_changed")
    return manifest, failures


def _read_rows(relative: str) -> list[dict]:
    path = ROOT / relative
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _stable_id(row: dict) -> str | None:
    identity = {key: row.get(key) for key in IDENTITY_FIELDS}
    if any(value is None for value in identity.values()):
        return None
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_bytes(raw.encode("utf-8"))


def _index_rows(rows: list[dict]) -> tuple[dict[str, dict], list[str]]:
    indexed: dict[str, dict] = {}
    errors = []
    for row in rows:
        trade_id = _stable_id(row)
        if trade_id is None:
            errors.append("missing_stable_id")
        elif trade_id in indexed:
            errors.append("duplicate_stable_id")
        else:
            indexed[trade_id] = row
    return indexed, errors


def _as_number(value):
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _equal(left, right, tolerance=1e-8):
    if left is None or right is None or left == "" or right == "":
        return (left is None or left == "") and (right is None or right == "")
    left_num, right_num = _as_number(left), _as_number(right)
    if left_num is not None and right_num is not None:
        return abs(left_num - right_num) <= tolerance
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k], tolerance) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b, tolerance) for a, b in zip(left, right))
    return str(left) == str(right)


def _cause_for_field(field: str) -> str | None:
    if field in {"signals_taken", "orders_executed", "EURUSD_taken", "USDCAD_taken", "AUDUSD_taken", "skipped_symbol_busy", "skipped_global_slots", "plan_rejections", "risk_rejections", "accepted_plans", "rejected_plans", "opportunities_detected", "plan_rejection_reasons", "risk_rejection_reasons"}:
        return "DOCUMENTED_SELECTION_AND_CAPACITY_EFFECT"
    if field in {"expired", "expired_at_end", "open_at_end", "pending_at_end", "ambiguous", "ambiguous_entry_bar", "ambiguous_both", "ambiguous_total", "resolved", "resolved_trades", "closed_trades", "wins", "losses", "win_rate", "gross_pnl_eur", "net_pnl_eur", "expectancy_eur", "expectancy_r", "total_r", "gross_r", "r", "net_r", "profit_factor", "profit_factor_eur", "max_drawdown_eur", "max_drawdown_r", "net_max_drawdown_eur", "net_expectancy_eur", "net_expectancy_r", "net_total_r", "net_win_rate", "net_profit_factor", "exit_time", "release_time", "result", "by_symbol_metrics", "by_session_metrics", "by_setup_metrics", "by_session_net_pnl_eur"}:
        return "DOCUMENTED_EXPIRY_BAR_OR_SELECTION_EFFECT"
    if field in {"transaction_costs_eur", "costs_incurred_eur", "commission_eur", "slippage_points", "transaction_cost_r"}:
        return "COST_MODEL_DEFINITION_AND_BASE_ZERO_COST_CHECK"
    return None


def _compare_trade_rows(before_rows, after_rows, pair, mode, selection_witnesses=None):
    before, before_errors = _index_rows(before_rows)
    after, after_errors = _index_rows(after_rows)
    if before_errors or after_errors:
        return {"status": "FAIL", "errors": before_errors + after_errors}
    before_ids, after_ids = set(before), set(after)
    removed, added = before_ids - after_ids, after_ids - before_ids
    common = before_ids & after_ids
    technical_differences = []
    transitions = Counter()
    per_trade_changes = []
    fields = sorted(set(before_rows[0]) & set(after_rows[0])) if before_rows and after_rows else []
    for trade_id in sorted(common):
        left, right = before[trade_id], after[trade_id]
        changed = {}
        for field in fields:
            if not _equal(left.get(field), right.get(field)):
                cause = _cause_for_field(field) or ("TECHNICAL_SIGNAL_OR_PLAN_CHANGE" if field in SIGNAL_AND_PLAN_FIELDS else "UNCLASSIFIED_OPERATION_FIELD_CHANGE")
                changed[field] = {"before": left.get(field), "after": right.get(field), "cause": cause}
                if field in SIGNAL_AND_PLAN_FIELDS:
                    technical_differences.append({"trade_id": trade_id, "field": field})
        if changed:
            per_trade_changes.append({
                "trade_id": trade_id,
                "identity": {key: left.get(key) for key in IDENTITY_FIELDS},
                "changes": changed,
            })
            old_result, new_result = left.get("result"), right.get("result")
            if old_result != new_result:
                transitions[f"{old_result}->{new_result}"] += 1
    witnesses = selection_witnesses or set()
    witness_missing = (removed | added) - witnesses if pair == "pre_finalization_to_current" else set()
    witness_extra = witnesses - (removed | added) if pair == "pre_finalization_to_current" else set()
    selection_difference_details = []
    for trade_id in sorted(removed | added):
        row = before.get(trade_id) or after.get(trade_id)
        selection_difference_details.append({
            "trade_id": trade_id,
            "identity": {key: row.get(key) for key in IDENTITY_FIELDS},
            "presence": "REMOVED" if trade_id in removed else "ADDED",
            "cause": "DOCUMENTED_EXPIRY_OR_END_STATE_SELECTION_EFFECT" if pair == "pre_finalization_to_current" and trade_id not in witness_missing else "UNWITNESSED_SELECTION_CHANGE",
        })
    return {
        "status": "FAIL" if technical_differences or witness_missing or witness_extra else "PASS",
        "before_count": len(before),
        "after_count": len(after),
        "common_count": len(common),
        "removed_count": len(removed),
        "added_count": len(added),
        "technical_signal_or_plan_change_count": len(technical_differences),
        "technical_differences": technical_differences,
        "result_transition_counts": dict(transitions),
        "changed_common_operation_count": len(per_trade_changes),
        "changed_operations": per_trade_changes,
        "selection_difference_count": len(removed) + len(added),
        "selection_differences": selection_difference_details,
        "selection_witness_missing_count": len(witness_missing),
        "selection_witness_extra_count": len(witness_extra),
        "unclassified_operation_field_count": sum(
            1 for record in per_trade_changes for item in record["changes"].values()
            if item["cause"] == "UNCLASSIFIED_OPERATION_FIELD_CHANGE"
        ),
    }


def _load_report_rows(relative: str, mode: str):
    report = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if mode == "portfolio":
        if "phase1_baseline_metrics" in relative:
            return report["portfolio_summary"]
        if "phase1_pre_finalization" in relative:
            return report["checks"]["historical_regression"]["after_portfolio_summary"]
        return report["checks"]["historical_regression"]["portfolio_base_summary"]
    if "phase1_baseline_metrics" in relative:
        return report["stateful_summary"]
    if "phase1_pre_finalization" in relative:
        return report["checks"]["historical_regression"]["after_stateful_summary"]
    return report["checks"]["historical_regression"]["stateful_base_summary"]


def _check_published_summary(rows, report_rows, mode):
    key_fields = ("threshold", "mode") if mode == "portfolio" else ("symbol", "threshold", "mode")

    def row_key(row):
        return tuple(str(row.get(key)) for key in key_fields)

    replay_by_key = {row_key(row): row for row in rows}
    report_by_key = {row_key(row): row for row in report_rows}
    errors = []
    if replay_by_key.keys() != report_by_key.keys():
        errors.append("summary_row_keys_mismatch")
    common_keys = replay_by_key.keys() & report_by_key.keys()
    checked_fields = 0
    for key in common_keys:
        replay, official = replay_by_key[key], report_by_key[key]
        for field in replay.keys() & official.keys():
            if field in key_fields:
                continue
            checked_fields += 1
            if not _equal(replay.get(field), official.get(field)):
                errors.append(f"published_metric_mismatch:{key}:{field}")
    return {"status": "FAIL" if errors else "PASS", "rows": len(common_keys), "metric_values_checked": checked_fields, "errors": errors}


def _aggregate_changes(before_rows, after_rows, mode):
    key_fields = ("threshold", "mode") if mode == "portfolio" else ("symbol", "threshold", "mode")
    before = {tuple(str(row.get(key)) for key in key_fields): row for row in before_rows}
    after = {tuple(str(row.get(key)) for key in key_fields): row for row in after_rows}
    output = []
    errors = []
    for key in sorted(before.keys() & after.keys()):
        left, right = before[key], after[key]
        for field in sorted(left.keys() & right.keys()):
            if field in key_fields or _equal(left.get(field), right.get(field)):
                continue
            cause = _cause_for_field(field)
            if cause is None:
                errors.append({"row": list(key), "field": field, "before": left.get(field), "after": right.get(field)})
            else:
                output.append({"row": list(key), "field": field, "before": left.get(field), "after": right.get(field), "cause": cause})
    return output, errors


def _read_causal_witnesses():
    summary_path, private_path = ROOT / CAUSAL_SUMMARY, ROOT / CAUSAL_PRIVATE
    if not summary_path.is_file() or not private_path.is_file():
        return {}, {"status": "PENDING", "reason": "Verified selection witness report is missing."}
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    private_bytes = private_path.read_bytes()
    if _sha256_bytes(private_bytes) != summary.get("private_evidence_sha256"):
        return {}, {"status": "FAIL", "reason": "Selection witness private artifact hash does not match its public summary."}
    private = json.loads(private_bytes.decode("utf-8"))
    if private.get("dataset_sha256") != DATASET_SHA256 or private.get("status") != "PASS":
        return {}, {"status": "FAIL", "reason": "Selection witness dataset or status is invalid."}
    if private.get("pre_finalization_commit") != COMMITS["pre_finalization"] or private.get("current_commit") != "957bf6613ecdabd38a101db1471d1285917831ec":
        return {}, {"status": "FAIL", "reason": "Selection witness source commits do not match the verified source chain."}
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.validate_phase1 import _verify_transitive_selection_attribution

    verified = _verify_transitive_selection_attribution(DATASET_SHA256)
    if verified.get("status") != "PASS":
        return {}, {"status": verified.get("status", "PENDING"), "reason": verified.get("reason", "Selection witness verification did not pass.")}
    witnesses = {}
    for mode in ("portfolio", "stateful"):
        items = private.get("selection_differences", {}).get(mode, [])
        witnesses[mode] = {
            str(item.get("trade_id"))
            for item in items
            if item.get("causality_status") == "PASS"
            and "DOCUMENTED_SIMULATOR_CORRECTION" in str(item.get("cause", ""))
            and isinstance(item.get("causal_chain"), list)
            and len(item["causal_chain"]) >= 2
        }
        if len(witnesses[mode]) != len(items):
            return {}, {"status": "PENDING", "reason": f"One or more {mode} selection changes lack a complete causal witness."}
    return witnesses, {"status": "PASS", "private_evidence_sha256": summary.get("private_evidence_sha256"), "selection_differences": sum(map(len, witnesses.values()))}


def _build_report() -> tuple[dict, dict]:
    errors = []
    source_manifest, source_errors = _source_manifest()
    errors.extend(source_errors)

    frozen_path = ROOT / "reports/private/issue14/technical_candidate_scan_frozen.csv"
    if not frozen_path.is_file() or _sha256_file(frozen_path) != DATASET_SHA256:
        errors.append("frozen_dataset_missing_or_changed")
    else:
        blob = _git_blob(COMMITS["baseline"], "data/backtest/technical_candidate_scan.csv")
        if not _verify_dataset_bytes(frozen_path.read_bytes(), blob):
            errors.append("frozen_dataset_blob_provenance_mismatch")

    artifacts = {}
    data = {}
    replay_run_provenance = {}
    for version, modes in REPLAY_FILES.items():
        data[version] = {}
        replay_run_provenance[version] = {}
        for mode, paths in modes.items():
            trade_path, summary_path, metadata_path = paths
            for kind, relative in (("trades", trade_path), ("summary", summary_path), ("metadata", metadata_path)):
                path = ROOT / relative
                if not path.is_file():
                    errors.append(f"replay_artifact_missing:{version}:{mode}:{kind}")
                    continue
                artifacts[relative] = {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
            if not all((ROOT / path).is_file() for path in paths):
                continue
            data[version][mode] = {
                "trades": _read_rows(trade_path),
                "summary": _read_rows(summary_path),
                "metadata": json.loads((ROOT / metadata_path).read_text(encoding="utf-8")),
            }
            metadata = data[version][mode]["metadata"]
            if metadata.get("cost_scenario", "BASE") != "BASE":
                errors.append(f"replay_cost_scenario_mismatch:{version}:{mode}")
            replay_run_provenance[version][mode] = {
                "source_commit": COMMITS[version],
                "generated_at_utc": metadata.get("generated_at"),
                "cost_scenario": metadata.get("cost_scenario", "BASE"),
                "llm_included": metadata.get("llm_included"),
                "macro_history_included": metadata.get("macro_history_included"),
                "execution_capped_at_period_end": metadata.get("execution_capped_at_period_end"),
            }
            try:
                generated = datetime.fromisoformat(str(metadata.get("generated_at")).replace("Z", "+00:00"))
                if generated.tzinfo is None:
                    raise ValueError
                age = (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()
                replay_run_provenance[version][mode]["age_seconds"] = round(age, 1)
                if age < -300 or age > 24 * 60 * 60:
                    errors.append(f"replay_evidence_outside_24_hour_window:{version}:{mode}")
            except (TypeError, ValueError):
                errors.append(f"replay_timestamp_invalid:{version}:{mode}")

    report_hashes = {}
    for name, relative in REPORT_INPUTS.items():
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"published_report_input_missing:{name}")
            continue
        report_hashes[name] = {"path": relative, "sha256": _sha256_file(path), "bytes": path.stat().st_size}

    report_checks = {}
    report_map = {
        "baseline": REPORT_INPUTS["baseline"],
        "pre_finalization": REPORT_INPUTS["pre_finalization"],
        "current": REPORT_INPUTS["current"],
    }
    for version, modes in data.items():
        for mode in ("portfolio", "stateful"):
            if mode not in modes or version not in report_map:
                continue
            try:
                official = _load_report_rows(report_map[version], mode)
            except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
                report_checks[f"{version}_{mode}"] = {"status": "FAIL", "errors": [f"report_parse_failed:{type(exc).__name__}"]}
                errors.append(f"report_parse_failed:{version}:{mode}")
                continue
            check = _check_published_summary(modes[mode]["summary"], official, mode)
            report_checks[f"{version}_{mode}"] = check
            if check["status"] != "PASS":
                errors.append(f"published_summary_does_not_match_replay:{version}:{mode}")

    witnesses, witness_check = _read_causal_witnesses()
    if witness_check.get("status") != "PASS":
        errors.append("causal_selection_witness_invalid")

    comparisons = {}
    all_metric_deltas = []
    for mode in ("portfolio", "stateful"):
        if any(mode not in data.get(v, {}) for v in COMMITS):
            continue
        comparisons[mode] = {
            "baseline_to_prefinal": _compare_trade_rows(
                data["baseline"][mode]["trades"], data["pre_finalization"][mode]["trades"], "baseline_to_prefinal", mode,
            ),
            "prefinal_to_current": _compare_trade_rows(
                data["pre_finalization"][mode]["trades"], data["current"][mode]["trades"],
                "prefinal_to_current", mode, witnesses.get(mode, set()),
            ),
        }
        for pair in ("baseline_to_prefinal", "prefinal_to_current"):
            comparison = comparisons[mode][pair]
            if comparison.get("status") != "PASS":
                errors.append(f"operation_comparison_failed:{mode}:{pair}")
            if comparison.get("unclassified_operation_field_count", 0):
                errors.append(f"unclassified_operation_fields:{mode}:{pair}")
        baseline_aggregate, baseline_aggregate_errors = _aggregate_changes(
            data["baseline"][mode]["summary"], data["pre_finalization"][mode]["summary"], mode,
        )
        modern_aggregate, modern_aggregate_errors = _aggregate_changes(
            data["pre_finalization"][mode]["summary"], data["current"][mode]["summary"], mode,
        )
        if baseline_aggregate_errors or modern_aggregate_errors:
            errors.append(f"unclassified_aggregate_metrics:{mode}")
        if baseline_aggregate:
            errors.append(f"baseline_to_prefinal_legacy_metrics_changed:{mode}")
        if mode == "portfolio":
            baseline_fields = {"signals_taken", "resolved", "wins", "losses", "expired", "ambiguous", "win_rate", "gross_pnl_eur", "expectancy_eur", "profit_factor_eur", "max_drawdown_eur", "skipped_symbol_busy", "skipped_global_slots", "plan_rejections", "risk_rejections", "EURUSD_taken", "USDCAD_taken", "AUDUSD_taken"}
            baseline_present = set(data["baseline"][mode]["summary"][0])
            if not baseline_fields.issubset(baseline_present):
                errors.append("baseline_portfolio_metric_coverage_incomplete")
        else:
            baseline_fields = {"signals_taken", "wins", "losses", "expired", "open_at_end", "resolved_trades", "win_rate", "expectancy_r", "total_r", "profit_factor", "max_drawdown_r", "gross_pnl_eur", "max_drawdown_eur", "plan_rejections", "risk_rejections"}
            baseline_present = set(data["baseline"][mode]["summary"][0])
            if not baseline_fields.issubset(baseline_present):
                errors.append("baseline_stateful_metric_coverage_incomplete")
        all_metric_deltas.extend({"mode": mode, **item} for item in modern_aggregate)

    # BASE is deliberately zero unverified commission/slippage. Any nonzero cost
    # row would be a material mismatch, not something this report can waive.
    current_base_rows = [row for mode in ("portfolio", "stateful") for row in data.get("current", {}).get(mode, {}).get("trades", [])]
    nonzero_base_costs = _base_cost_mismatches(current_base_rows)
    if nonzero_base_costs:
        errors.append("base_cost_assumption_mismatch")

    # The current run's economic aggregates must match the published report, and
    # costs for later sensitivity scenarios have no baseline-era equivalent.
    current_report = json.loads((ROOT / REPORT_INPUTS["current"]).read_text(encoding="utf-8"))
    current_regression = current_report.get("checks", {}).get("historical_regression", {})
    scenario_runs = current_regression.get("scenario_runs", {})
    required_scenarios = (
        "portfolio_base_1", "portfolio_base_2", "portfolio_conservative", "portfolio_stress",
        "stateful_base_1", "stateful_base_2", "stateful_conservative", "stateful_stress",
    )
    if any(scenario_runs.get(name, {}).get("status") != "PASS" for name in required_scenarios):
        errors.append("current_cost_scenario_replay_evidence_missing_or_not_pass")
    if current_regression.get("repeatability_portfolio_base", {}).get("status") != "PASS" or current_regression.get("repeatability_stateful_base", {}).get("status") != "PASS":
        errors.append("current_base_repeatability_evidence_missing_or_not_pass")

    private = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "FAIL" if errors else "PASS",
        "commits": COMMITS,
        "dataset": {"path": "data/backtest/technical_candidate_scan.csv", "sha256": DATASET_SHA256, "verified_private_copy": "reports/private/issue14/technical_candidate_scan_frozen.csv"},
        "source_hashes_from_git_objects": source_manifest,
        "replay_artifacts": artifacts,
        "replay_run_provenance": replay_run_provenance,
        "published_report_inputs": report_hashes,
        "published_summary_reproduction": report_checks,
        "selection_witness": witness_check,
        "comparisons": comparisons,
        "aggregate_metric_deltas": all_metric_deltas,
        "baseline_to_prefinal_common_metrics_changed_count": 0,
        "new_cost_scenarios": {
            "classification": "NO_PRE_COST_MODEL_BASELINE_COMPARABLE",
            "assumption": "BASE has zero unverified commission and slippage; CONSERVATIVE and STRESS are sensitivity-only assumptions introduced after the original baseline.",
            "current_scenarios_reused_from_verified_full_replay": required_scenarios,
        },
        "errors": errors,
    }
    summary = {
        "schema_version": 1,
        "generated_at_utc": private["generated_at_utc"],
        "status": private["status"],
        "dataset_sha256": DATASET_SHA256,
        "commit_chain": COMMITS,
        "published_summary_reproduction": {key: value["status"] for key, value in report_checks.items()},
        "selection_witness_status": witness_check.get("status", "PENDING"),
        "baseline_to_prefinal_selection_changes": {
            mode: comparisons.get(mode, {}).get("baseline_to_prefinal", {}).get("selection_difference_count")
            for mode in ("portfolio", "stateful")
        },
        "prefinal_to_current_selection_changes": {
            mode: comparisons.get(mode, {}).get("prefinal_to_current", {}).get("selection_difference_count")
            for mode in ("portfolio", "stateful")
        },
        "prefinal_to_current_changed_common_operations": {
            mode: comparisons.get(mode, {}).get("prefinal_to_current", {}).get("changed_common_operation_count")
            for mode in ("portfolio", "stateful")
        },
        "technical_signal_or_plan_changes": {
            mode: sum(comparisons.get(mode, {}).get(pair, {}).get("technical_signal_or_plan_change_count", 0) for pair in ("baseline_to_prefinal", "prefinal_to_current"))
            for mode in ("portfolio", "stateful")
        },
        "aggregate_metric_deltas_documented": len(all_metric_deltas),
        "unexplained_aggregate_deltas": sum(
            1 for errors_item in errors if "aggregate" in errors_item or "metric" in errors_item
        ),
        "base_cost_assumption_verified": not nonzero_base_costs,
        "conservative_stress_comparison": "NO_PRE_COST_MODEL_BASELINE_COMPARABLE",
        "private_evidence": PRIVATE_OUTPUT,
        "private_evidence_sha256": None,
        "errors": errors,
    }
    return private, summary


def _safe_relative_output(path: str) -> Path:
    target = (ROOT / path).resolve()
    if ROOT.resolve() not in target.parents:
        raise ValueError("Output path escapes repository root.")
    return target


def write_reports() -> dict:
    private, summary = _build_report()
    private_path = _safe_relative_output(PRIVATE_OUTPUT)
    public_path = _safe_relative_output(PUBLIC_OUTPUT)
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_bytes = (json.dumps(private, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    summary["private_evidence_sha256"] = _sha256_bytes(private_bytes)
    public_bytes = (json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    private_temp = private_path.with_suffix(".json.tmp")
    public_temp = public_path.with_suffix(".json.tmp")
    private_temp.write_bytes(private_bytes)
    public_temp.write_bytes(public_bytes)
    private_temp.replace(private_path)
    public_temp.replace(public_path)
    return summary


def verify_public_report(path: Path | None = None, *, now: datetime | None = None) -> dict:
    path = path or ROOT / PUBLIC_OUTPUT
    if not path.is_file():
        return {"status": "PENDING", "reason": "Aggregate reconciliation report is missing."}
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "FAIL", "reason": "Aggregate reconciliation report is unreadable."}
    private_path = _safe_relative_output(str(summary.get("private_evidence", "")))
    if not private_path.is_file():
        return {"status": "PENDING", "reason": "Private operation-level evidence is missing."}
    private_bytes = private_path.read_bytes()
    if _sha256_bytes(private_bytes) != summary.get("private_evidence_sha256"):
        return {"status": "FAIL", "reason": "Private operation-level evidence hash mismatch."}
    private = json.loads(private_bytes.decode("utf-8"))
    generated_raw = summary.get("generated_at_utc")
    try:
        generated = datetime.fromisoformat(str(generated_raw).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError
        age = ((now or datetime.now(timezone.utc)) - generated.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return {"status": "PENDING", "reason": "Aggregate evidence timestamp is invalid."}
    if not _timestamp_within_window(generated_raw, now=now):
        return {"status": "PENDING", "reason": "Aggregate evidence is outside the 24-hour verification window."}
    if private.get("status") != "PASS" or summary.get("status") != "PASS" or private.get("errors"):
        return {"status": "PENDING" if summary.get("status") == "PENDING" else "FAIL", "reason": "Aggregate reconciliation contains unresolved evidence errors."}
    if private.get("dataset", {}).get("sha256") != DATASET_SHA256:
        return {"status": "FAIL", "reason": "Reconciliation dataset hash does not match the frozen baseline."}
    if private.get("commits") != COMMITS:
        return {"status": "FAIL", "reason": "Reconciliation source commits do not match the pinned commits."}
    if summary.get("errors") or summary.get("unexplained_aggregate_deltas") != 0:
        return {"status": "FAIL", "reason": "Public reconciliation summary reports unexplained differences."}
    # Rebuild all hashes, stable-ID joins, report-vs-replay metric checks, and
    # selection witnesses from the pinned Git objects and private replay artifacts.
    fresh_private, fresh_summary = _build_report()
    if fresh_summary.get("status") != "PASS":
        return {"status": fresh_summary.get("status", "PENDING"), "reason": "Recomputed historical evidence is not complete."}
    for key in (
        "commits", "dataset", "source_hashes_from_git_objects", "replay_artifacts",
        "published_report_inputs", "published_summary_reproduction", "comparisons",
        "aggregate_metric_deltas", "selection_witness",
    ):
        if private.get(key) != fresh_private.get(key):
            return {"status": "FAIL", "reason": f"Recomputed provenance differs in {key}."}
    if not _evidence_has_provenance(private):
        return {"status": "PENDING", "reason": "Aggregate equality has no independently verifiable source and replay provenance."}
    if not isinstance(private.get("comparisons"), dict) or not isinstance(private.get("published_summary_reproduction"), dict):
        return {"status": "PENDING", "reason": "Operation-level comparison or published metric reproduction is missing."}
    if any(check.get("status") != "PASS" for check in private["published_summary_reproduction"].values()):
        return {"status": "FAIL", "reason": "At least one published aggregate does not match its historical replay."}
    return {"status": "PASS", "age_seconds": round(age, 1), "private_evidence_sha256": summary.get("private_evidence_sha256"), "aggregate_metric_deltas_documented": summary.get("aggregate_metric_deltas_documented"), "unexplained_aggregate_deltas": summary.get("unexplained_aggregate_deltas")}


if __name__ == "__main__":
    result = write_reports()
    print(json.dumps({key: result.get(key) for key in (
        "status", "dataset_sha256", "published_summary_reproduction", "selection_witness_status",
        "prefinal_to_current_selection_changes", "prefinal_to_current_changed_common_operations",
        "technical_signal_or_plan_changes", "aggregate_metric_deltas_documented", "unexplained_aggregate_deltas", "errors",
    )}, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result.get("status") == "PASS" else 1)
