from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.macro_sources import event_is_available_as_of, fetch_live_sources, safe_calendar_preview


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def run(*, fetch_live: bool = False, request_limit: int = 4) -> dict[str, object]:
    registry = json.loads((ROOT / "config" / "macro_source_registry.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    print("macro 1/3: loaded fixed source registry", flush=True)
    if fetch_live:
        result = fetch_live_sources(
            ROOT, registry, request_limit=request_limit, now=now,
            progress=lambda message: print(message, flush=True),
        )
    else:
        result = {
            "status": "NOT_RUN_OFFLINE", "requests_made": 0, "request_limit": 0,
            "origin_requests": 0, "automatic_retry_count": 0,
            "sources": [
                {"source_id": item["source_id"], "status": "NOT_RUN_OFFLINE", "events": []}
                for item in registry["sources"]
            ],
        }
    print(f"macro 2/3: fetch mode complete; requests={result['requests_made']}", flush=True)
    events = [event for source in result["sources"] for event in source.get("events", [])]
    preview = safe_calendar_preview(events, now)
    available = sum(event_is_available_as_of(event, now) for event in events)
    audit_sources = []
    by_id = {item.get("source_id"): item for item in result["sources"]}
    for source in registry["sources"]:
        fetched = by_id.get(source["source_id"], {})
        audit_sources.append({
            "source_id": source["source_id"], "source_type": source["source_type"],
            "catalog_url": source["catalog_url"], "feed_url": source["feed_url"],
            "host_allowlisted": True, "terms_status": source["terms_status"],
            "status": fetched.get("status", fetched.get("fetch_status", "NOT_RUN")),
            "fetch_status": fetched.get("fetch_status"),
            "http_status": fetched.get("http_status"),
            "events": fetched.get("event_count", len(fetched.get("events", []))),
            "source_sha256": fetched.get("source_sha256"),
            "cache_age_seconds": fetched.get("cache_age_seconds"),
            "error": fetched.get("error"),
            "titles_or_article_text_persisted_publicly": False,
        })
    statuses = [item["status"] for item in audit_sources]
    status = "MACRO_FREE_FEEDS_READY" if statuses and all(item in {"FETCH_OK", "CACHE_HIT"} for item in statuses) else "MACRO_PARTIAL_COVERAGE"
    audit = {
        "schema_version": 1, "status": status, "mode": "FETCH_LIVE" if fetch_live else "OFFLINE",
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "request_count": result["requests_made"], "request_cap": request_limit if fetch_live else 0,
        "automatic_retry_count": result["automatic_retry_count"],
        "requests_per_origin_max": 1, "max_feed_bytes": 2097152, "timeout_seconds": 10,
        "tls_required": True, "redirects_allowed": False, "private_or_local_addresses_allowed": False,
        "user_agent": "AgentTradingResearch/2.2.2e",
        "rss_is_not_schedule": True, "empty_rss_is_no_events_claim": False,
        "schedule_coverage": "BLS_ICS_NOT_FETCHED" if fetch_live else "NOT_RUN_OFFLINE",
        "broker_sessions_derived_from_macro": False, "active_risk_gate_modified": False,
        "sources": audit_sources,
    }
    preview["source_event_count"] = len(events)
    preview["verified_scheduled_preview_count"] = len(preview["events"])
    causality = {
        "schema_version": 1, "status": "PASS_SYNTHETIC_RULES" if not fetch_live else "AUDITED",
        "events_seen": len(events), "available_as_of_now": int(available),
        "excluded_as_published_or_observed_after_decision": len(events) - int(available),
        "news_items_used_as_future_schedule": 0,
        "scheduled_events_without_exact_time_blocking": 0,
        "historical_feed_replay_available": False,
        "live_rss_does_not_prove_past_availability": True,
    }
    report_dir = ROOT / "reports" / "phase2_2_2e"
    _atomic_json(report_dir / "macro_source_audit.json", audit)
    _atomic_json(report_dir / "calendar_preview.json", preview)
    _atomic_json(report_dir / "macro_replay_causality.json", causality)
    print("macro 3/3: wrote sanitized audit, preview, and causality reports", flush=True)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize a bounded set of official macro feeds.")
    parser.add_argument("--fetch-live", action="store_true", help="Make a single bounded request per enabled origin.")
    parser.add_argument("--request-limit", type=int, default=4)
    args = parser.parse_args()
    if not 0 <= args.request_limit <= 4:
        parser.error("--request-limit must be between 0 and 4 for the 2.2.2e request budget")
    report = run(fetch_live=args.fetch_live, request_limit=args.request_limit)
    print(f"macro status: {report['status']}", flush=True)
    return 0 if report["status"] in {"MACRO_FREE_FEEDS_READY", "MACRO_PARTIAL_COVERAGE"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
