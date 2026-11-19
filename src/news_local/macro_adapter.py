"""Pure, fail-closed adapter for normalized ``src.macro_sources`` records.

This module never fetches feeds, writes files, or invents economic observations.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from src.macro_sources.core import ALLOWED_FEEDS
from src.news_local.models import NewsAnalysisInput, NewsEvent, SourceRevision


ALLOWED_SOURCE_STATUSES = {"FETCH_OK", "PARTIAL"}


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _stable_ids(source_id: str, source_event_id: str, record: dict[str, Any]) -> tuple[str, str]:
    source_identity = f"{source_id}|{source_event_id}"
    composite = f"{source_id}:{source_event_id}"
    event_id = (
        composite
        if len(composite) <= 120
        else "MACRO-" + hashlib.sha256(source_identity.encode("utf-8")).hexdigest()[:48]
    )
    evidence_material = {
        "source_id": source_id,
        "source_event_id": source_event_id,
        "source_sha256": record.get("source_sha256"),
        "title": record.get("title"),
        "published_at_utc": record.get("initial_published_at_utc") or record.get("published_at_utc"),
        "scheduled_at_utc": record.get("scheduled_at_utc"),
        "updated_at_utc": record.get("updated_at_utc"),
        "revision_count": record.get("revision_count"),
        "revision_history": record.get("revision_history", []),
    }
    canonical = json.dumps(evidence_material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    evidence_id = "MACRO-EVIDENCE-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:48]
    return event_id, evidence_id


def adapt_macro_source_results(
    source_results: list[dict[str, Any]],
    *,
    source_registry: dict[str, Any],
    symbol: str,
    analysis_as_of_utc: datetime,
) -> tuple[NewsAnalysisInput, dict[str, Any]]:
    """Convert normalized RSS/ICS results without network, filesystem, or inference.

    Malformed, unverified, un-timestamped, and unsupported records are omitted and
    enumerated by safe reason code in the second returned value.
    """
    if analysis_as_of_utc.tzinfo is None or analysis_as_of_utc.utcoffset() is None:
        raise ValueError("ANALYSIS_TIME_MUST_BE_AWARE")
    registry_items = source_registry.get("sources") if isinstance(source_registry, dict) else None
    if not isinstance(registry_items, list):
        raise ValueError("MACRO_REGISTRY_INVALID")
    registry_by_id = {
        item.get("source_id"): item
        for item in registry_items
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
    }
    if len(registry_by_id) != len(registry_items):
        raise ValueError("MACRO_REGISTRY_INVALID")

    accepted: list[NewsEvent] = []
    excluded: list[dict[str, str]] = []
    partial_sources: list[str] = []
    used_event_ids: set[str] = set()

    def reject(reason: str, source_id: str = "unknown", source_event_id: str = "unknown") -> None:
        excluded.append(
            {
                "source_id": source_id[:120],
                "source_event_id": source_event_id[:240],
                "reason": reason,
            }
        )

    for source_result in sorted(
        source_results,
        key=lambda item: str(item.get("source_id", "")) if isinstance(item, dict) else "",
    ):
        if not isinstance(source_result, dict):
            reject("SOURCE_RESULT_INVALID")
            continue
        source_id = source_result.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            reject("SOURCE_ID_MISSING")
            continue
        if source_result.get("status") not in ALLOWED_SOURCE_STATUSES:
            reject(f"SOURCE_STATUS_{str(source_result.get('status', 'MISSING'))[:40]}", source_id)
            continue
        if source_result.get("status") == "PARTIAL":
            partial_sources.append(source_id)
        registry_source = registry_by_id.get(source_id)
        allowlisted = ALLOWED_FEEDS.get(source_id)
        expected_type = (
            "SCHEDULED_EVENT" if allowlisted and allowlisted[2] == "ICS" else "NEWS_RELEASE"
        )
        if (
            registry_source is None
            or allowlisted is None
            or source_result.get("origin") != allowlisted[1]
            or source_result.get("source_type") != expected_type
            or registry_source.get("source_type") != expected_type
            or not isinstance(registry_source.get("currencies"), list)
            or not registry_source.get("currencies")
        ):
            reject("SOURCE_NOT_ALLOWLISTED", source_id)
            continue
        top_hash = source_result.get("source_sha256")
        if not _is_sha256(top_hash):
            reject("SOURCE_HASH_MISSING", source_id)
            continue
        if not isinstance(source_result.get("events"), list):
            reject("EVENT_LIST_INVALID", source_id)
            continue
        for record in source_result["events"]:
            if not isinstance(record, dict):
                reject("EVENT_RECORD_INVALID", source_id)
                continue
            source_event_id = record.get("event_id")
            if not isinstance(source_event_id, str) or not source_event_id.strip():
                reject("EVENT_ID_MISSING", source_id)
                continue
            if record.get("source_id") != source_id:
                reject("SOURCE_ID_MISMATCH", source_id, source_event_id)
                continue
            if record.get("source_type") != expected_type:
                reject("SOURCE_KIND_MISMATCH", source_id, source_event_id)
                continue
            kind = record.get("source_type")
            if kind == "NEWS_RELEASE":
                source_kind = "NEWS_RELEASE"
                status = record.get("status")
                published = record.get("initial_published_at_utc") or record.get("published_at_utc")
                if status != "NEWS_PUBLISHED" or _parse_aware(published) is None:
                    reject("NEWS_PUBLICATION_TIME_UNVERIFIED", source_id, source_event_id)
                    continue
                scheduled = None
            elif kind == "SCHEDULED_EVENT":
                source_kind = "SCHEDULED_EVENT"
                status = record.get("status")
                if status != "SCHEDULED_VERIFIED" or record.get("timestamp_precision") != "SECOND":
                    reject("SCHEDULE_TIME_UNVERIFIED", source_id, source_event_id)
                    continue
                scheduled = record.get("scheduled_at_utc")
                if _parse_aware(scheduled) is None:
                    reject("SCHEDULE_TIME_UNVERIFIED", source_id, source_event_id)
                    continue
                published = None
            else:
                reject("SOURCE_KIND_UNSUPPORTED", source_id, source_event_id)
                continue

            first_seen = record.get("observed_at_utc")
            retrieved = record.get("retrieved_at_utc") or source_result.get("retrieved_at_utc")
            if _parse_aware(first_seen) is None or _parse_aware(retrieved) is None:
                reject("OBSERVATION_TIME_UNVERIFIED", source_id, source_event_id)
                continue
            currencies = record.get("currencies")
            title = record.get("title")
            if (
                not isinstance(currencies, list)
                or not currencies
                or any(not isinstance(currency, str) for currency in currencies)
                or not isinstance(title, str)
                or not title.strip()
            ):
                reject("EVENT_FIELDS_INVALID", source_id, source_event_id)
                continue
            if set(currencies) != set(registry_source.get("currencies", [])):
                reject("SOURCE_CURRENCY_MISMATCH", source_id, source_event_id)
                continue
            if not _is_sha256(record.get("source_sha256")):
                reject("SOURCE_HASH_MISSING", source_id, source_event_id)
                continue
            history_raw = record.get("revision_history", [])
            if not isinstance(history_raw, list) or len(history_raw) > 100:
                reject("REVISION_HISTORY_INVALID", source_id, source_event_id)
                continue
            try:
                revision_history = [SourceRevision.model_validate(item) for item in history_raw]
            except (ValidationError, ValueError, TypeError):
                reject("REVISION_HISTORY_INVALID", source_id, source_event_id)
                continue

            event_id, evidence_id = _stable_ids(source_id, source_event_id, record)
            composite = f"{source_id}:{source_event_id}"
            event_id = composite if len(composite) <= 120 else event_id
            if event_id in used_event_ids:
                reject("EVENT_ID_COLLISION", source_id, source_event_id)
                continue
            url = record.get("url")
            if not isinstance(url, str) or not url.strip():
                url = None
            try:
                adapted_event = NewsEvent(
                    event_id=event_id,
                    source_event_id=source_event_id,
                    source_id=source_id,
                    evidence_id=evidence_id,
                    source_kind=source_kind,
                    title=title[:500],
                    summary=None,
                    currencies=currencies,
                    source_url=url,
                    scheduled_at_utc=_parse_aware(scheduled) if scheduled else None,
                    published_at_utc=_parse_aware(published) if published else None,
                    first_seen_at_utc=_parse_aware(first_seen),
                    retrieved_at_utc=_parse_aware(retrieved),
                    source_status="AVAILABLE",
                    origin_kind="MACRO_SOURCE_NORMALIZED_V1",
                    # Normalized RSS/ICS records contain no typed result or consensus fields.
                    actual=None,
                    forecast=None,
                    previous=None,
                    units=None,
                    source_revision_count=record.get("revision_count"),
                    source_revision_history=revision_history,
                )
                accepted.append(adapted_event)
                used_event_ids.add(event_id)
            except (ValidationError, ValueError, TypeError):
                reject("EVENT_SCHEMA_INVALID", source_id, source_event_id)

    adapted = NewsAnalysisInput(
        analysis_as_of_utc=analysis_as_of_utc,
        symbol=symbol,
        events=accepted,
    )
    report = {
        "schema_version": "1.0",
        "status": "PASS" if not excluded and not partial_sources else "PARTIAL",
        "accepted_event_count": len(accepted),
        "excluded_event_count": len(excluded),
        "partial_sources": sorted(set(partial_sources)),
        "excluded": excluded,
        "external_fetches": 0,
        "filesystem_writes": 0,
        "economic_observations_invented": 0,
    }
    return adapted, report
