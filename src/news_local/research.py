"""Bounded research pipeline from approved macro feeds to NEWS-02 analysis."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from src.macro_sources.core import ALLOWED_FEEDS, fetch_live_sources, normalize_feed
from src.news_local.analyzer import build_deterministic_result
from src.news_local.fixtures import build_fixture
from src.news_local.macro_adapter import adapt_macro_source_results
from src.news_local.models import NewsAnalysisInput, NewsAnalysisResult
from src.news_local.providers import NewsSummarizer


PIPELINE_SCHEMA_VERSION = "1.0"
SNAPSHOT_SCHEMA_VERSION = "2.0"
MAX_SOURCE_REQUESTS = 4
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
PRIVATE_RELATIVE_DIR = Path("reports") / "private" / "news_local"


class NewsResearchError(ValueError):
    """Safe pipeline error containing only a stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _DuplicateJSONKey(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise NewsResearchError("TIMESTAMP_MUST_BE_AWARE")
    return value.astimezone(timezone.utc)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_private_path(root: Path, value: str | Path) -> Path:
    private_root = (root / PRIVATE_RELATIVE_DIR).resolve()
    requested = Path(value)
    resolved = (root / requested).resolve() if not requested.is_absolute() else requested.resolve()
    if not _within(resolved, private_root):
        raise NewsResearchError("PRIVATE_OUTPUT_PATH_REQUIRED")
    return resolved


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise NewsResearchError("OUTPUT_ALREADY_EXISTS")
    payload = json.dumps(
        value, indent=2, sort_keys=True, ensure_ascii=False, default=str
    ) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    try:
        # A same-directory hard link publishes a complete file atomically and
        # fails if another writer already created the destination.
        os.link(temporary, path)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise NewsResearchError("OUTPUT_ALREADY_EXISTS") from None
    except OSError:
        temporary.unlink(missing_ok=True)
        raise NewsResearchError("OUTPUT_ATOMIC_COMMIT_FAILED") from None
    temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise NewsResearchError("SNAPSHOT_TOO_LARGE")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except NewsResearchError:
        raise
    except _DuplicateJSONKey:
        raise NewsResearchError("SNAPSHOT_DUPLICATE_KEY") from None
    except (OSError, json.JSONDecodeError):
        raise NewsResearchError("SNAPSHOT_READ_FAILED") from None
    if not isinstance(value, dict):
        raise NewsResearchError("SNAPSHOT_SCHEMA_INVALID")
    return value


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise NewsResearchError("SNAPSHOT_CANONICALIZATION_FAILED") from None
    return hashlib.sha256(payload).hexdigest()


def _snapshot_integrity(snapshot_data: dict[str, Any]) -> dict[str, Any]:
    """Verify the v2 canonical digest; keep v1 readable but explicitly unverified."""
    version = snapshot_data.get("schema_version")
    if version == "1.0":
        return {
            "status": "LEGACY_UNVERIFIED",
            "algorithm": None,
            "canonical_payload_sha256": None,
        }
    if version != SNAPSHOT_SCHEMA_VERSION:
        raise NewsResearchError("SNAPSHOT_SCHEMA_INVALID")
    integrity = snapshot_data.get("integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("algorithm") != "SHA-256"
        or not isinstance(integrity.get("canonical_payload_sha256"), str)
        or len(integrity["canonical_payload_sha256"]) != 64
    ):
        raise NewsResearchError("SNAPSHOT_INTEGRITY_MISSING")
    unsigned = {key: value for key, value in snapshot_data.items() if key != "integrity"}
    actual = _canonical_sha256(unsigned)
    if not hmac.compare_digest(actual, integrity["canonical_payload_sha256"].lower()):
        raise NewsResearchError("SNAPSHOT_INTEGRITY_INVALID")
    return {
        "status": "HASH_VERIFIED",
        "algorithm": "SHA-256",
        "canonical_payload_sha256": actual,
    }


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise NewsResearchError("SNAPSHOT_READ_FAILED") from None


def _verify_cached_source_payloads(
    root: Path,
    source_results: list[dict[str, Any]],
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Cross-check normalized snapshot events against retained private feed bytes."""
    registry_by_id = {
        item.get("source_id"): item
        for item in registry.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("source_id"), str)
    }
    cache_dir = (root / "reports" / "private" / "phase2_2_2e" / "macro_cache").resolve()
    checked = 0
    unavailable = 0
    for source_result in source_results:
        if not isinstance(source_result, dict):
            raise NewsResearchError("SOURCE_RESULT_INVALID")
        source_id = source_result.get("source_id")
        if source_id not in ALLOWED_FEEDS or source_id not in registry_by_id:
            raise NewsResearchError("SOURCE_NOT_ALLOWLISTED")
        events = source_result.get("events")
        if not isinstance(events, list):
            raise NewsResearchError("SOURCE_EVENT_LIST_INVALID")
        if source_result.get("status") not in {"FETCH_OK", "PARTIAL", "CACHE_HIT"}:
            if events:
                raise NewsResearchError("UNAVAILABLE_SOURCE_HAS_EVENTS")
            continue
        digest = source_result.get("source_sha256")
        raw_path = (cache_dir / f"{source_id.lower()}.feed").resolve()
        try:
            raw_path.relative_to(cache_dir)
        except ValueError:
            raise NewsResearchError("SOURCE_CACHE_PATH_INVALID") from None
        if not raw_path.is_file():
            unavailable += 1
            continue
        try:
            payload = raw_path.read_bytes()
        except OSError:
            raise NewsResearchError("SOURCE_CACHE_READ_FAILED") from None
        if not isinstance(digest, str) or hashlib.sha256(payload).hexdigest() != digest.lower():
            raise NewsResearchError("SOURCE_PAYLOAD_HASH_MISMATCH")
        retrieved = source_result.get("retrieved_at_utc")
        if not isinstance(retrieved, str):
            raise NewsResearchError("SOURCE_RETRIEVAL_TIME_MISSING")
        try:
            retrieved_at = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
            retrieved_at = _utc(retrieved_at)
            normalized = normalize_feed(payload, registry_by_id[source_id], retrieved_at)
        except (ValueError, TypeError):
            raise NewsResearchError("SOURCE_RENORMALIZATION_FAILED") from None
        for key in (
            "source_id",
            "source_type",
            "origin",
            "status",
            "source_sha256",
            "retrieved_at_utc",
            "event_count",
            "events",
        ):
            if normalized.get(key) != source_result.get(key):
                raise NewsResearchError("SOURCE_NORMALIZATION_MISMATCH")
        checked += 1
    return {
        "status": "VERIFIED" if unavailable == 0 else "PARTIAL",
        "verified_source_count": checked,
        "unverified_source_count": unavailable,
    }


def _load_registry(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            (root / "config" / "macro_source_registry.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        raise NewsResearchError("MACRO_REGISTRY_READ_FAILED") from None
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise NewsResearchError("MACRO_REGISTRY_INVALID")
    return value


def _source_summary(source_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for source in source_results:
        if not isinstance(source, dict):
            summary.append({"source_id": "unknown", "status": "SOURCE_RESULT_INVALID", "event_count": 0})
            continue
        summary.append(
            {
                "source_id": source.get("source_id", "unknown"),
                "status": source.get("status", "UNKNOWN"),
                "fetch_status": source.get("fetch_status"),
                "http_status": source.get("http_status"),
                "event_count": len(source.get("events", [])) if isinstance(source.get("events"), list) else 0,
                "source_sha256": source.get("source_sha256"),
                "cache_age_seconds": source.get("cache_age_seconds"),
                "error_code": source.get("error"),
            }
        )
    return summary


def _pipeline_status(
    analysis: NewsAnalysisResult,
    *,
    fetch_status: str,
    adapter_status: str | None,
    snapshot_integrity_status: str,
    source_payload_status: str,
) -> str:
    if analysis.status == "BLOCKED":
        return "BLOCKED"
    if analysis.status == "FALLBACK":
        return "FALLBACK"
    if analysis.status == "PENDING":
        return "PARTIAL" if fetch_status == "PARTIAL" or adapter_status == "PARTIAL" else "PENDING"
    if snapshot_integrity_status == "LEGACY_UNVERIFIED":
        return "PARTIAL"
    if source_payload_status == "PARTIAL":
        return "PARTIAL"
    if fetch_status == "PARTIAL" or adapter_status == "PARTIAL":
        return "PARTIAL"
    return "PASS"


def _source_analysis(
    *,
    root: Path,
    source_results: list[dict[str, Any]],
    registry: dict[str, Any],
    symbol: str,
    as_of: datetime,
    summarizer: NewsSummarizer,
    live_analysis: bool,
) -> tuple[NewsAnalysisInput, NewsAnalysisResult, dict[str, Any]]:
    adapted, adapter_report = adapt_macro_source_results(
        source_results,
        source_registry=registry,
        symbol=symbol,
        analysis_as_of_utc=as_of,
    )
    result = summarizer.summarize(
        adapted,
        mode="live-ollama" if live_analysis else "offline",
    )
    return adapted, result, adapter_report


def run_research(
    *,
    root: Path,
    mode: Literal["offline", "collect", "local"] = "offline",
    symbol: str = "EURUSD",
    fixture: str | None = None,
    snapshot: str | Path | None = None,
    output: str | Path | None = None,
    request_limit: int = 4,
    as_of_utc: datetime | None = None,
    now: datetime | None = None,
    fetcher: Callable[..., dict[str, Any]] | None = None,
    summarizer: NewsSummarizer | None = None,
    source_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one explicit offline, collection, or stored-snapshot local analysis pass."""
    if mode not in {"offline", "collect", "local"}:
        raise NewsResearchError("MODE_UNSUPPORTED")
    if symbol not in {"EURUSD", "USDCAD", "AUDUSD"}:
        raise NewsResearchError("SYMBOL_UNSUPPORTED")
    if isinstance(request_limit, bool) or not isinstance(request_limit, int) or not 0 <= request_limit <= MAX_SOURCE_REQUESTS:
        raise NewsResearchError("REQUEST_LIMIT_INVALID")
    if mode != "offline" and fixture is not None:
        raise NewsResearchError("FIXTURE_ONLY_ALLOWED_OFFLINE")
    if mode == "collect" and snapshot is not None:
        raise NewsResearchError("SNAPSHOT_IS_OUTPUT_IN_COLLECT_MODE")
    if mode == "local" and snapshot is None:
        raise NewsResearchError("LOCAL_MODE_REQUIRES_SNAPSHOT")
    if mode == "offline" and fixture is not None and snapshot is not None:
        raise NewsResearchError("FIXTURE_AND_SNAPSHOT_ARE_MUTUALLY_EXCLUSIVE")

    root = root.resolve()
    run_time = _utc(now or datetime.now(timezone.utc))
    requested_as_of = _utc(as_of_utc) if as_of_utc is not None else None
    if mode == "collect" and requested_as_of is not None and requested_as_of > run_time:
        raise NewsResearchError("ANALYSIS_CUTOFF_AFTER_COLLECTION")
    # Synthetic fixture runs have no need to read the live-source registry.
    # This keeps the zero-network mode usable from an isolated checkout as well.
    registry = source_registry
    if registry is None:
        registry = (
            {"schema_version": 1, "sources": []}
            if mode == "offline" and snapshot is None
            else _load_registry(root)
        )
    summarizer = summarizer or NewsSummarizer()
    snapshot_path: Path | None = None
    snapshot_digest: str | None = None
    snapshot_integrity: dict[str, Any] = {"status": "NOT_APPLICABLE"}
    snapshot_source_payloads: dict[str, Any] = {"status": "NOT_APPLICABLE"}
    source_results: list[dict[str, Any]] = []
    adapter_report: dict[str, Any] | None = None
    fetch_report: dict[str, Any] = {
        "status": "NOT_RUN_OFFLINE",
        "requests_made": 0,
        "request_limit": 0,
        "origin_requests": 0,
        "automatic_retry_count": 0,
    }

    if mode == "offline" and snapshot is None:
        news_input = build_fixture(fixture or "usd_only", symbol)
        as_of = requested_as_of if requested_as_of else news_input.analysis_as_of_utc
        if as_of != news_input.analysis_as_of_utc:
            news_input = NewsAnalysisInput.model_validate(
                {**news_input.model_dump(mode="python"), "analysis_as_of_utc": as_of}
            )
        analysis = build_deterministic_result(news_input)
    elif mode == "collect":
        fetch_fn = fetcher or fetch_live_sources
        try:
            fetch_report = fetch_fn(
                root,
                registry,
                request_limit=request_limit,
                now=run_time,
            )
        except Exception as exc:
            # Do not copy provider bodies, private paths, or URLs to the report.
            safe_code = getattr(exc, "code", "MACRO_FETCH_FAILED")
            if not isinstance(safe_code, str) or not safe_code.isupper():
                safe_code = "MACRO_FETCH_FAILED"
            fetch_report = {
                "status": "PARTIAL",
                "requests_made": 0,
                "request_limit": request_limit,
                "origin_requests": 0,
                "automatic_retry_count": 0,
                "sources": [],
                "error_code": safe_code[:80],
            }
        source_results = fetch_report.get("sources", [])
        if not isinstance(source_results, list):
            raise NewsResearchError("MACRO_FETCH_RESULT_INVALID")
        snapshot_path = resolve_private_path(
            root,
            snapshot
            or PRIVATE_RELATIVE_DIR
            / f"macro_source_snapshot_{run_time.strftime('%Y%m%dT%H%M%S%fZ')}.json",
        )
        snapshot_data = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "collected_at_utc": run_time.isoformat().replace("+00:00", "Z"),
            "fetch": fetch_report,
            "sources": source_results,
        }
        snapshot_data["integrity"] = {
            "algorithm": "SHA-256",
            "canonical_payload_sha256": _canonical_sha256(snapshot_data),
        }
        snapshot_integrity = _snapshot_integrity(snapshot_data)
        _atomic_json(snapshot_path, snapshot_data)
        snapshot_digest = _file_sha256(snapshot_path)
        as_of = requested_as_of if requested_as_of else run_time
        news_input, analysis, adapter_report = _source_analysis(
            root=root,
            source_results=source_results,
            registry=registry,
            symbol=symbol,
            as_of=as_of,
            summarizer=summarizer,
            live_analysis=False,
        )
    else:
        snapshot_path = resolve_private_path(root, snapshot)
        snapshot_data = _read_json(snapshot_path)
        snapshot_integrity = _snapshot_integrity(snapshot_data)
        snapshot_digest = _file_sha256(snapshot_path)
        raw_sources = snapshot_data.get("sources")
        if not isinstance(raw_sources, list):
            raise NewsResearchError("SNAPSHOT_SCHEMA_INVALID")
        source_results = raw_sources
        snapshot_source_payloads = _verify_cached_source_payloads(
            root, source_results, registry
        )
        if mode == "local" and (
            snapshot_integrity["status"] != "HASH_VERIFIED"
            or snapshot_source_payloads["status"] != "VERIFIED"
        ):
            raise NewsResearchError("SNAPSHOT_PROVENANCE_UNVERIFIED")
        original_fetch = snapshot_data.get("fetch")
        if isinstance(original_fetch, dict):
            fetch_report = {
                "status": original_fetch.get("status", "UNKNOWN"),
                "requests_made": 0,
                "original_requests_made": original_fetch.get("requests_made", 0),
                "request_limit": 0,
                "origin_requests": 0,
                "automatic_retry_count": 0,
                "mode": "LOCAL_SNAPSHOT",
            }
        collected_at = snapshot_data.get("collected_at_utc")
        if not isinstance(collected_at, str):
            raise NewsResearchError("SNAPSHOT_TIME_MISSING")
        try:
            snapshot_time = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            snapshot_time = _utc(snapshot_time)
        except (ValueError, TypeError):
            raise NewsResearchError("SNAPSHOT_TIME_INVALID") from None
        as_of = requested_as_of if requested_as_of else snapshot_time
        if as_of > snapshot_time:
            raise NewsResearchError("ANALYSIS_CUTOFF_AFTER_COLLECTION")
        news_input, analysis, adapter_report = _source_analysis(
            root=root,
            source_results=source_results,
            registry=registry,
            symbol=symbol,
            as_of=as_of,
            summarizer=summarizer,
            live_analysis=(mode == "local"),
        )

    if adapter_report is None:
        source_status = "NOT_APPLICABLE" if mode == "offline" and snapshot is None else fetch_report["status"]
    else:
        source_status = fetch_report.get("status", "UNKNOWN")
    pipeline_status = _pipeline_status(
        analysis,
        fetch_status=source_status,
        adapter_status=adapter_report.get("status") if adapter_report else None,
        snapshot_integrity_status=snapshot_integrity["status"],
        source_payload_status=snapshot_source_payloads["status"],
    )
    source_rows = _source_summary(source_results)
    event_ids = analysis.event_ids_used
    report: dict[str, Any] = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "status": pipeline_status,
        "mode": mode.upper(),
        "symbol": symbol,
        "analysis_as_of_utc": analysis.analysis_as_of_utc.isoformat().replace("+00:00", "Z"),
        "source_fetch": {
            "status": source_status,
            "requests_made": fetch_report.get("requests_made", 0),
            "request_limit": fetch_report.get("request_limit", 0),
            "origin_requests": fetch_report.get("origin_requests", 0),
            "automatic_retry_count": fetch_report.get("automatic_retry_count", 0),
            "error_code": fetch_report.get("error_code"),
            "sources": source_rows,
        },
        "source_adapter": adapter_report,
        "snapshot_integrity": snapshot_integrity,
        "snapshot_source_payloads": snapshot_source_payloads,
        "snapshot_sha256": snapshot_digest,
        "analysis": analysis.model_dump(mode="json"),
        "event_count_received": sum(
            len(source.get("events", []))
            for source in source_results
            if isinstance(source, dict) and isinstance(source.get("events"), list)
        )
        if adapter_report is not None
        else len(news_input.events),
        "eligible_event_count": analysis.source_coverage.eligible_event_count,
        "excluded_event_count": analysis.source_coverage.excluded_event_count,
        "event_ids_used": event_ids,
        "driver_currencies": [item.driver_currency for item in analysis.driver_assessments],
        "conditional_pair_directions": [
            {
                "driver_currency": item.driver_currency,
                "direction": item.driver_direction_condition,
                "pair": item.pair,
                "pair_direction": item.pair_direction,
            }
            for item in analysis.conditional_scenarios
        ],
        "snapshot_path": (
            snapshot_path.relative_to(root).as_posix() if snapshot_path is not None else None
        ),
    }

    default_output = PRIVATE_RELATIVE_DIR / f"news_research_{run_time.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    output_path = resolve_private_path(root, output or default_output)
    if snapshot_path is not None and output_path == snapshot_path:
        raise NewsResearchError("SNAPSHOT_AND_RESULT_PATH_COLLISION")
    report["output_path"] = output_path.relative_to(root).as_posix()
    report["result_integrity"] = {
        "algorithm": "SHA-256",
        "canonical_payload_sha256": _canonical_sha256(report),
    }
    _atomic_json(output_path, report)
    return report
