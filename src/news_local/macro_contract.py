"""Read-only, provenance-checked NEWS-02 brief for future macro consumers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import hmac
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.news_local.analyzer import validate_result_references
from src.news_local.macro_adapter import adapt_macro_source_results
from src.news_local.models import NewsAnalysisResult, SUPPORTED_PAIRS
from src.news_local.research import (
    NewsResearchError,
    _file_sha256,
    _canonical_sha256,
    _load_registry,
    _pipeline_status,
    _read_json,
    _snapshot_integrity,
    _source_summary,
    _verify_cached_source_payloads,
    resolve_private_path,
)


DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60


class MacroNewsContractError(ValueError):
    """Stable-code rejection for invalid or unverifiable research reports."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _parse_aware(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise MacroNewsContractError("TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise MacroNewsContractError("TIMESTAMP_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MacroNewsContractError("TIMESTAMP_INVALID")
    return parsed.astimezone(timezone.utc)


def _pair_scenarios(result: NewsAnalysisResult) -> list[dict[str, Any]]:
    return [
        {
            "driver_currency": item.driver_currency,
            "direction_condition": item.driver_direction_condition,
            "pair": item.pair,
            "pair_direction": item.pair_direction,
            "condition": item.condition,
            "possible_effect": item.possible_effect,
            "event_ids": list(item.event_ids),
            "evidence_ids": list(item.evidence_ids),
        }
        for item in result.conditional_scenarios
    ]


def read_macro_news_brief(
    *,
    root: Path,
    result_path: str | Path,
    expected_symbol: str | None = None,
    now_utc: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Read and validate a private result without network, inference, or side effects.

    The returned object is informational only. It deliberately has no BUY/SELL,
    risk-gate, order, or approval field that could be wired into execution.
    """
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0:
        raise MacroNewsContractError("MAX_AGE_INVALID")
    root = root.resolve()
    try:
        report_path = resolve_private_path(root, result_path)
        report = _read_json(report_path)
    except NewsResearchError as exc:
        raise MacroNewsContractError(exc.code) from None
    if report.get("schema_version") != "1.0":
        raise MacroNewsContractError("RESULT_SCHEMA_UNSUPPORTED")
    result_integrity = report.get("result_integrity")
    if not isinstance(result_integrity, dict) or result_integrity.get("algorithm") != "SHA-256":
        raise MacroNewsContractError("RESULT_INTEGRITY_MISSING")
    result_digest = result_integrity.get("canonical_payload_sha256")
    unsigned_report = {key: value for key, value in report.items() if key != "result_integrity"}
    if (
        not isinstance(result_digest, str)
        or len(result_digest) != 64
        or not hmac.compare_digest(_canonical_sha256(unsigned_report), result_digest.lower())
    ):
        raise MacroNewsContractError("RESULT_INTEGRITY_INVALID")
    symbol = report.get("symbol")
    if symbol not in SUPPORTED_PAIRS or (expected_symbol and symbol != expected_symbol):
        raise MacroNewsContractError("SYMBOL_MISMATCH")
    snapshot_ref = report.get("snapshot_path")
    snapshot_sha = report.get("snapshot_sha256")
    if not isinstance(snapshot_ref, str) or not isinstance(snapshot_sha, str) or len(snapshot_sha) != 64:
        raise MacroNewsContractError("SNAPSHOT_REFERENCE_MISSING")
    try:
        snapshot_path = resolve_private_path(root, snapshot_ref)
        snapshot = _read_json(snapshot_path)
        integrity = _snapshot_integrity(snapshot)
        actual_file_sha = _file_sha256(snapshot_path)
    except NewsResearchError as exc:
        raise MacroNewsContractError(exc.code) from None
    if integrity.get("status") != "HASH_VERIFIED":
        raise MacroNewsContractError("SNAPSHOT_INTEGRITY_REQUIRED")
    if actual_file_sha != snapshot_sha.lower():
        raise MacroNewsContractError("RESULT_SNAPSHOT_MISMATCH")
    if report.get("snapshot_integrity") != integrity:
        raise MacroNewsContractError("RESULT_INTEGRITY_MISMATCH")

    source_results = snapshot.get("sources")
    fetch = snapshot.get("fetch")
    if not isinstance(source_results, list) or not isinstance(fetch, dict):
        raise MacroNewsContractError("SNAPSHOT_SCHEMA_INVALID")
    try:
        collected_at = _parse_aware(snapshot.get("collected_at_utc"))
        analysis_as_of = _parse_aware(report.get("analysis_as_of_utc"))
    except NewsResearchError as exc:
        raise MacroNewsContractError(exc.code) from None
    if analysis_as_of > collected_at:
        raise MacroNewsContractError("ANALYSIS_AFTER_SNAPSHOT_COLLECTION")

    try:
        registry = _load_registry(root)
    except NewsResearchError as exc:
        raise MacroNewsContractError(exc.code) from None
    try:
        source_payloads = _verify_cached_source_payloads(
            root, source_results, registry
        )
    except NewsResearchError as exc:
        raise MacroNewsContractError(exc.code) from None
    if source_payloads["status"] != "VERIFIED":
        raise MacroNewsContractError("SOURCE_PAYLOAD_CACHE_MISSING")
    try:
        news_input, adapter_report = adapt_macro_source_results(
            source_results,
            source_registry=registry,
            symbol=symbol,
            analysis_as_of_utc=analysis_as_of,
        )
        result = NewsAnalysisResult.model_validate(report.get("analysis"))
        validate_result_references(news_input, result)
    except (ValidationError, ValueError, TypeError):
        raise MacroNewsContractError("ANALYSIS_PROVENANCE_INVALID") from None

    if result.schema_version != "2.0" or result.symbol != symbol:
        raise MacroNewsContractError("ANALYSIS_SCHEMA_OR_SYMBOL_INVALID")
    if report.get("source_adapter") != adapter_report:
        raise MacroNewsContractError("ADAPTER_PROVENANCE_MISMATCH")
    source_fetch = report.get("source_fetch")
    if not isinstance(source_fetch, dict):
        raise MacroNewsContractError("SOURCE_SUMMARY_MISSING")
    if source_fetch.get("sources") != _source_summary(source_results):
        raise MacroNewsContractError("SOURCE_SUMMARY_MISMATCH")
    if source_fetch.get("status") != fetch.get("status"):
        raise MacroNewsContractError("FETCH_STATUS_MISMATCH")
    if report.get("event_ids_used") != result.event_ids_used:
        raise MacroNewsContractError("EVENT_REFERENCE_MISMATCH")
    if report.get("driver_currencies") != [item.driver_currency for item in result.driver_assessments]:
        raise MacroNewsContractError("DRIVER_REFERENCE_MISMATCH")
    if report.get("conditional_pair_directions") != [
        {
            "driver_currency": item.driver_currency,
            "direction": item.driver_direction_condition,
            "pair": item.pair,
            "pair_direction": item.pair_direction,
        }
        for item in result.conditional_scenarios
    ]:
        raise MacroNewsContractError("SCENARIO_REFERENCE_MISMATCH")
    expected_status = _pipeline_status(
        result,
        fetch_status=source_fetch["status"],
        adapter_status=adapter_report.get("status"),
        snapshot_integrity_status=integrity["status"],
        source_payload_status=source_payloads["status"],
    )
    report_status = report.get("status")
    if report_status != expected_status:
        raise MacroNewsContractError("RESULT_STATUS_MISMATCH")

    source_rows = {item.get("source_id"): item for item in source_results if isinstance(item, dict)}
    source_event_rows = {
        (source.get("source_id"), event.get("event_id")): event
        for source in source_results
        if isinstance(source, dict) and isinstance(source.get("events"), list)
        for event in source["events"]
        if isinstance(event, dict)
    }
    accepted_events = {item.event_id: item for item in news_input.events}
    event_rows = []
    for event_id in result.event_ids_used:
        event = accepted_events.get(event_id)
        if event is None:
            raise MacroNewsContractError("EVENT_REFERENCE_MISMATCH")
        source = source_rows.get(event.source_id, {})
        source_event = source_event_rows.get((event.source_id, event.source_event_id), {})
        event_rows.append(
            {
                "event_id": event.event_id,
                "evidence_id": event.evidence_id,
                "source_id": event.source_id,
                "source_sha256": source.get("source_sha256"),
                "source_url": event.source_url,
                "source_kind": event.source_kind,
                "title": event.title,
                "currencies": list(event.currencies),
                "published_at_utc": event.published_at_utc.isoformat().replace("+00:00", "Z")
                if event.published_at_utc
                else None,
                "scheduled_at_utc": event.scheduled_at_utc.isoformat().replace("+00:00", "Z")
                if event.scheduled_at_utc
                else None,
                "first_seen_at_utc": event.first_seen_at_utc.isoformat().replace("+00:00", "Z"),
                "retrieved_at_utc": event.retrieved_at_utc.isoformat().replace("+00:00", "Z"),
                "economic_observations": [
                    observation.model_dump(mode="json")
                    for fact in result.confirmed_facts
                    for observation in fact.observations
                    if observation.event_id == event.event_id
                ],
                "research_priority": source_event.get("importance", "UNKNOWN"),
                "priority_basis": source_event.get("priority_basis"),
            }
        )

    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise MacroNewsContractError("CURRENT_TIME_MUST_BE_AWARE")
    age_seconds = (now.astimezone(timezone.utc) - collected_at).total_seconds()
    if age_seconds < 0:
        raise MacroNewsContractError("SNAPSHOT_FROM_FUTURE")
    freshness = "FRESH" if age_seconds <= max_age_seconds else "STALE"
    report_status = report.get("status")
    if report_status not in {"PASS", "PARTIAL", "PENDING", "FALLBACK", "BLOCKED"}:
        raise MacroNewsContractError("RESULT_STATUS_INVALID")
    return {
        "contract_schema_version": "1.0",
        "status": report_status,
        "freshness": freshness,
        "snapshot_age_seconds": int(age_seconds),
        "symbol": symbol,
        "analysis_as_of_utc": result.analysis_as_of_utc.isoformat().replace("+00:00", "Z"),
        "source_coverage": {
            "status": report["source_fetch"]["status"],
            "sources": _source_summary(source_results),
            "eligible_event_count": result.source_coverage.eligible_event_count,
            "excluded_event_count": result.source_coverage.excluded_event_count,
            "notes": list(result.source_coverage.notes),
            "raw_source_payloads_verified": source_payloads["verified_source_count"],
            "raw_source_payloads_unavailable": source_payloads["unverified_source_count"],
        },
        "events_used": event_rows,
        "confirmed_publications": [
            {
                "description": fact.description,
                "event_ids": list(fact.event_ids),
                "evidence_ids": list(fact.evidence_ids),
                "economic_observations": [item.model_dump(mode="json") for item in fact.observations],
                "independent_confirmation_count": fact.independent_confirmation_count,
            }
            for fact in result.confirmed_facts
        ],
        "upcoming_agenda": [item.model_dump(mode="json") for item in result.upcoming_events],
        "directional_hypotheses": [
            {
                "origin": result.provider,
                "calibrated": False,
                **item.model_dump(mode="json"),
            }
            for item in result.driver_assessments
        ],
        "conditional_pair_scenarios": _pair_scenarios(result),
        "uncertainties": list(result.uncertainties),
        "limitations": list(result.limitations),
        "provenance": {
            "snapshot_path": snapshot_ref,
            "snapshot_sha256": actual_file_sha,
            "canonical_payload_sha256": integrity["canonical_payload_sha256"],
            "result_canonical_payload_sha256": result_digest.lower(),
            "analysis_provider": result.provider,
            "analysis_model": result.model,
        },
        "informational_only": True,
        "operational_signal": False,
        "trading_decisions_enabled": False,
        "external_fetches_during_read": 0,
        "llm_calls_during_read": 0,
    }
