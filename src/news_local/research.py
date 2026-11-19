"""Bounded research pipeline from approved macro feeds to NEWS-02 analysis."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from src.macro_sources.core import fetch_live_sources
from src.news_local.analyzer import build_deterministic_result
from src.news_local.fixtures import build_fixture
from src.news_local.macro_adapter import adapt_macro_source_results
from src.news_local.models import NewsAnalysisInput, NewsAnalysisResult
from src.news_local.providers import NewsSummarizer


PIPELINE_SCHEMA_VERSION = "1.0"
MAX_SOURCE_REQUESTS = 4
PRIVATE_RELATIVE_DIR = Path("reports") / "private" / "news_local"


class NewsResearchError(ValueError):
    """Safe pipeline error containing only a stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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
    if path.exists():
        temporary.unlink(missing_ok=True)
        raise NewsResearchError("OUTPUT_ALREADY_EXISTS")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise NewsResearchError("SNAPSHOT_READ_FAILED") from None
    if not isinstance(value, dict):
        raise NewsResearchError("SNAPSHOT_SCHEMA_INVALID")
    return value


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
) -> str:
    if analysis.status == "BLOCKED":
        return "BLOCKED"
    if analysis.status == "FALLBACK":
        return "FALLBACK"
    if analysis.status == "PENDING":
        return "PARTIAL" if fetch_status == "PARTIAL" or adapter_status == "PARTIAL" else "PENDING"
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
        as_of = _utc(as_of_utc) if as_of_utc else news_input.analysis_as_of_utc
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
        _atomic_json(
            snapshot_path,
            {
                "schema_version": "1.0",
                "collected_at_utc": run_time.isoformat().replace("+00:00", "Z"),
                "fetch": fetch_report,
                "sources": source_results,
            },
        )
        as_of = _utc(as_of_utc) if as_of_utc else run_time
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
        if snapshot_data.get("schema_version") != "1.0":
            raise NewsResearchError("SNAPSHOT_SCHEMA_INVALID")
        raw_sources = snapshot_data.get("sources")
        if not isinstance(raw_sources, list):
            raise NewsResearchError("SNAPSHOT_SCHEMA_INVALID")
        source_results = raw_sources
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
        as_of = _utc(as_of_utc) if as_of_utc else snapshot_time
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
    _atomic_json(output_path, report)
    return report
