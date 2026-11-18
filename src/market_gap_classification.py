"""Conservative, deterministic classification of observed public-market gaps.

This module describes gaps between adjacent candle timestamps. It deliberately
does not infer a broker's legal entity, hours, holidays, or quote availability.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from config.market_config import SYMBOLS, TIMEFRAMES
from src.market_history_audit import REQUIRED_COLUMNS, parse_utc_timestamp


SCHEMA_VERSION = 1
CLASSIFIER_VERSION = "market-gap-classifier-v1"
AS_OF_UTC = "2026-11-17T20:13:49.261352+00:00"
PUBLIC_SOURCE_REVIEWED_UTC = "2026-11-18T10:11:19+00:00"
EXPECTED_GAP_COUNT = 3167
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
ALLOWED_CLASSIFICATIONS = frozenset({
    "VERIFIED_BROKER_CLOSED", "VERIFIED_BROKER_SCHEDULE_EXCEPTION",
    "SUPPORTED_WEEKEND_PATTERN_BROKER_UNVERIFIED", "SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED",
    "VERIFIED_TIMEFRAME_BOUNDARY_EFFECT", "MIXED_PARTIAL_EVIDENCE",
    "UNRESOLVED_OPEN_SESSION_GAP", "UNRESOLVED", "BROKER_ENTITY_UNVERIFIED", "EVIDENCE_INSUFFICIENT",
})
# Curated date-only context transcribed from the dated official calendars in the
# source registry. These are public holidays, never broker-session evidence.
PUBLIC_HOLIDAY_CONTEXT: dict[str, tuple[str, ...]] = {}
_US_OPM_DATES = {
    2023: ("2023-01-02", "2023-01-16", "2023-02-20", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-10-09", "2023-11-10", "2023-11-23", "2023-12-25"),
    2024: ("2024-01-01", "2024-01-15", "2024-02-19", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-10-14", "2024-11-11", "2024-11-28", "2024-12-25"),
    2025: ("2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-10-13", "2025-11-11", "2025-11-27", "2025-12-25"),
    2026: ("2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26", "2026-12-25"),
}
_UK_ENGLAND_WALES_DATES = {
    2023: ("2023-01-02", "2023-04-07", "2023-04-10", "2023-05-01", "2023-05-08", "2023-05-29", "2023-08-28", "2023-12-25", "2023-12-26"),
    2024: ("2024-01-01", "2024-03-29", "2024-04-01", "2024-05-06", "2024-05-27", "2024-08-26", "2024-12-25", "2024-12-26"),
    2025: ("2025-01-01", "2025-04-18", "2025-04-21", "2025-05-05", "2025-05-26", "2025-08-25", "2025-12-25", "2025-12-26"),
    2026: ("2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04", "2026-05-25", "2026-08-31", "2026-12-25", "2026-12-28"),
}
_CANADA_CRA_2026_DATES = ("2026-01-01", "2026-04-03", "2026-04-06", "2026-05-18", "2026-06-24", "2026-07-01", "2026-08-03", "2026-09-07", "2026-09-30", "2026-10-12", "2026-11-11", "2026-12-25", "2026-12-26")
_AUSTRALIA_FAIRWORK_2026_DATES = ("2026-01-01", "2026-01-26", "2026-03-09", "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-25", "2026-06-08", "2026-10-05", "2026-12-25", "2026-12-26")
for _year, _dates in _US_OPM_DATES.items():
    for _date in _dates:
        PUBLIC_HOLIDAY_CONTEXT[_date] = tuple(sorted(set(PUBLIC_HOLIDAY_CONTEXT.get(_date, ())) | {"US_OPM_HOLIDAYS"}))
for _year, _dates in _UK_ENGLAND_WALES_DATES.items():
    for _date in _dates:
        PUBLIC_HOLIDAY_CONTEXT[_date] = tuple(sorted(set(PUBLIC_HOLIDAY_CONTEXT.get(_date, ())) | {"UK_GOV_HOLIDAYS"}))
for _date in _CANADA_CRA_2026_DATES:
    PUBLIC_HOLIDAY_CONTEXT[_date] = tuple(sorted(set(PUBLIC_HOLIDAY_CONTEXT.get(_date, ())) | {"CANADA_CRA_HOLIDAYS"}))
for _date in _AUSTRALIA_FAIRWORK_2026_DATES:
    PUBLIC_HOLIDAY_CONTEXT[_date] = tuple(sorted(set(PUBLIC_HOLIDAY_CONTEXT.get(_date, ())) | {"AU_FAIRWORK_HOLIDAYS"}))
UTC = timezone.utc
EPISODE_EDGE_REVIEW_TOLERANCE_SECONDS = TIMEFRAME_SECONDS["M15"]


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_by_path(source_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = source_manifest.get("source_manifest", {}).get("sources")
    if not isinstance(sources, list):
        raise ValueError("PUBLIC_SOURCE_LIST_MISSING")
    result = {item.get("path"): item for item in sources if isinstance(item, dict)}
    if len(result) != len(sources) or len(result) != len(SYMBOLS) * len(TIMEFRAMES):
        raise ValueError("PUBLIC_SOURCE_LIST_INVALID")
    return result


def extract_gap_inventory(root: Path, source_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract every observed open-to-open interval longer than nominal cadence."""
    by_path = _source_by_path(source_manifest)
    expected_paths = {
        f"data/market/{symbol}/{timeframe}.csv"
        for symbol in SYMBOLS for timeframe in TIMEFRAMES
    }
    if set(by_path) != expected_paths:
        raise ValueError("PUBLIC_SOURCE_COVERAGE_MISMATCH")

    inventory: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            relpath = f"data/market/{symbol}/{timeframe}.csv"
            source = by_path[relpath]
            path = root / relpath
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"PUBLIC_SOURCE_UNAVAILABLE:{relpath}")
            if _sha256(path) != source.get("raw_sha256"):
                raise ValueError(f"PUBLIC_SOURCE_HASH_MISMATCH:{relpath}")
            nominal = TIMEFRAME_SECONDS[timeframe]
            previous: datetime | None = None
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.reader(handle)
                if next(reader, None) != list(REQUIRED_COLUMNS):
                    raise ValueError(f"CSV_HEADER_INVALID:{relpath}")
                for row_number, row in enumerate(reader, start=2):
                    if len(row) != len(REQUIRED_COLUMNS):
                        raise ValueError(f"CSV_ROW_INVALID:{relpath}:{row_number}")
                    stamp = parse_utc_timestamp(dict(zip(REQUIRED_COLUMNS, row))["time"])
                    if previous is not None:
                        open_delta = int((stamp - previous).total_seconds())
                        if open_delta <= 0:
                            raise ValueError(f"CSV_TIMESTAMP_ORDER_INVALID:{relpath}:{row_number}")
                        if open_delta > nominal:
                            gap_start = previous + timedelta(seconds=nominal)
                            gap_seconds = open_delta - nominal
                            identity = {
                                "algorithm": CLASSIFIER_VERSION,
                                "source_canonical_sha256": source.get("canonical_sha256"),
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "previous_open_utc": previous.isoformat(),
                                "next_open_utc": stamp.isoformat(),
                                "nominal_cadence_seconds": nominal,
                                "observed_open_delta_seconds": open_delta,
                            }
                            gap_id = "gap_" + _canonical_hash(identity)[:24]
                            inventory.append({
                                "gap_id": gap_id,
                                "episode_id": "",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "source_path": relpath,
                                "source_raw_sha256": source["raw_sha256"],
                                "source_canonical_sha256": source["canonical_sha256"],
                                "source_row_previous": row_number - 1,
                                "source_row_next": row_number,
                                "previous_open_utc": previous.isoformat(),
                                "previous_bar_open_utc": previous.isoformat(),
                                "next_open_utc": stamp.isoformat(),
                                "next_bar_open_utc": stamp.isoformat(),
                                "previous_open_weekday_utc": previous.strftime("%A"),
                                "next_open_weekday_utc": stamp.strftime("%A"),
                                "previous_open_hour_utc": previous.hour,
                                "previous_open_minute_utc": previous.minute,
                                "next_open_hour_utc": stamp.hour,
                                "next_open_minute_utc": stamp.minute,
                                "gap_start_date_utc": gap_start.date().isoformat(),
                                "gap_end_date_utc": stamp.date().isoformat(),
                                "year_utc": gap_start.year,
                                "month_utc": gap_start.month,
                                "dates_implicated_utc": [
                                    (gap_start.date() + timedelta(days=offset)).isoformat()
                                    for offset in range((stamp.date() - gap_start.date()).days + 1)
                                ],
                                "crosses_weekend_calendar": any(
                                    (gap_start.date() + timedelta(days=offset)).weekday() >= 5
                                    for offset in range((stamp.date() - gap_start.date()).days + 1)
                                ),
                                "gap_start_utc": gap_start.isoformat(),
                                "gap_end_utc": stamp.isoformat(),
                                "gap_seconds": gap_seconds,
                                "elapsed_seconds": open_delta,
                                "observed_open_delta_seconds": open_delta,
                                "nominal_cadence_seconds": nominal,
                                "nominal_timeframe_seconds": nominal,
                                "as_of_utc": AS_OF_UTC,
                            })
                    previous = stamp

    inventory.sort(key=lambda row: (
        row["gap_start_utc"], row["gap_end_utc"], row["symbol"], row["timeframe"]
    ))
    if len(inventory) != EXPECTED_GAP_COUNT:
        raise ValueError(f"OBSERVED_GAP_COUNT_MISMATCH:{len(inventory)}")
    if len({row["gap_id"] for row in inventory}) != len(inventory):
        raise ValueError("GAP_ID_COLLISION")
    return inventory


def _episode_id(gap: dict[str, Any], classification: str, holiday_dates: list[str]) -> str:
    previous = datetime.fromisoformat(gap["previous_open_utc"])
    is_weekend_episode = (
        previous.weekday() == 4
        and datetime.fromisoformat(gap["next_open_utc"]).weekday() in {6, 0}
        and 36 * 3600 <= gap["observed_open_delta_seconds"] <= 96 * 3600
    )
    if is_weekend_episode:
        key = {"episode_kind": "weekend_pattern", "anchor_friday_utc_date": previous.date().isoformat()}
    elif classification == "SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED" and holiday_dates:
        key = {"episode_kind": "holiday_context", "anchor_holiday_utc_date": holiday_dates[0]}
    else:
        # For unresolved gaps, exact missing-window coincidence is descriptive
        # only and does not assert a shared cause.
        key = {"episode_kind": "exact_observed_window", "gap_start_utc": gap["gap_start_utc"], "gap_end_utc": gap["gap_end_utc"]}
    return "episode_" + _canonical_hash(key)[:20]


def _holiday_context_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    dates: list[dict[str, Any]] = []
    day = start.date()
    end_date_inclusive = (end - timedelta(microseconds=1)).date()
    while day <= end_date_inclusive:
        if day.isoformat() in PUBLIC_HOLIDAY_CONTEXT:
            dates.append({"date_utc": day.isoformat(), "evidence_ids": list(PUBLIC_HOLIDAY_CONTEXT[day.isoformat()])})
        day += timedelta(days=1)
    return dates


def _dst_context(start: datetime, end: datetime, transition_dates: Iterable[str]) -> list[str]:
    result = []
    for value in transition_dates:
        transition = datetime.fromisoformat(value).replace(tzinfo=UTC)
        if start - timedelta(days=2) <= transition <= end + timedelta(days=2):
            result.append(value)
    return result


def classify_gap(gap: dict[str, Any], *, source_ids: dict[str, str], dst_transition_dates: Iterable[str]) -> dict[str, Any]:
    previous = datetime.fromisoformat(gap["previous_open_utc"])
    following = datetime.fromisoformat(gap["next_open_utc"])
    gap_start = datetime.fromisoformat(gap["gap_start_utc"])
    holiday_context = _holiday_context_between(gap_start, following)
    holiday_dates = [item["date_utc"] for item in holiday_context]
    weekend_shape = (
        previous.weekday() == 4 and following.weekday() in {6, 0}
        and 36 * 3600 <= gap["observed_open_delta_seconds"] <= 96 * 3600
    )
    dst_dates = _dst_context(gap_start, following, dst_transition_dates)

    if holiday_dates:
        classification = "SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED"
        observation_class = "OBSERVED_HOLIDAY_ADJACENT"
        confidence = "MODERATE_CONTEXTUAL_CAUSE_UNVERIFIED"
        reason = "The observed missing interval overlaps a dated US/UK public-holiday context date; no evidence ties this gap to this account's broker schedule."
        contextual_evidence = sorted({evidence_id for item in holiday_context for evidence_id in item["evidence_ids"]})
        evidence_ids = [source_ids["public_data"], *contextual_evidence]
        notes = ["UTC date(s) overlap official public-holiday context from: " + "; ".join(f"{item['date_utc']} ({', '.join(item['evidence_ids'])})" for item in holiday_context) + "."]
        evidence_scope = "public cadence observation plus US/UK holiday dates as broad context; no broker-specific proof"
        if weekend_shape:
            notes.append("The same interval also has a Friday-to-Sunday/Monday weekend shape.")
    elif weekend_shape:
        classification = "SUPPORTED_WEEKEND_PATTERN_BROKER_UNVERIFIED"
        observation_class = "OBSERVED_WEEKEND_SHAPED"
        confidence = "HIGH_REPEATED_PATTERN_CAUSE_UNVERIFIED"
        reason = "The observed timestamps show a repeated Friday-to-Sunday/Monday gap shape across the captured series; broker-specific close/reopen hours are not established."
        evidence_ids = [source_ids["public_data"], source_ids["bis_fx_context"]]
        notes = ["Pattern support is observational and cross-series; it is not proof that the broker was closed for every second of this interval."]
        evidence_scope = "public repeated cadence pattern plus general FX context; no broker-specific proof"
    elif gap["observed_open_delta_seconds"] < 36 * 3600 and previous.weekday() < 5 and following.weekday() < 5:
        classification = "UNRESOLVED_OPEN_SESSION_GAP"
        observation_class = "OBSERVED_SHORT_CADENCE_GAP"
        confidence = "LOW_CAUSE_UNVERIFIED"
        reason = "A weekday/intraday timestamp interval exceeds nominal cadence; without the broker's dated session and feed-completeness records, closure and missing-feed causes cannot be distinguished."
        evidence_ids = [source_ids["public_data"]]
        notes = ["Treat as a data-quality exception; do not omit it from any eligible-event denominator."]
        evidence_scope = "public timestamp observation only; no broker-specific session or feed-completeness proof"
    else:
        classification = "UNRESOLVED"
        observation_class = "OBSERVED_MULTI_DAY_OR_NONSTANDARD_GAP"
        confidence = "LOW_EVIDENCE_INSUFFICIENT"
        reason = "The timestamp pattern does not meet the conservative weekend or dated-holiday context rules; cause remains undetermined."
        evidence_ids = [source_ids["public_data"]]
        notes = ["No broker-specific session calendar, outage evidence, or feed-completeness proof is available."]
        evidence_scope = "public timestamp observation only; no broker-specific session or feed-completeness proof"

    if dst_dates:
        notes.append("Near official US/EU daylight-saving transition date(s) " + ", ".join(dst_dates) + "; this is context only because the broker/server timezone is unknown.")
        evidence_ids.extend([source_ids["us_dst"], source_ids["eu_dst"]])
    notes.append("BROKER_ENTITY_UNVERIFIED; the trusted broker-calendar allowlist is empty.")
    result = dict(gap)
    result.update({
        "episode_id": _episode_id(gap, classification, holiday_dates),
        "observation_class": observation_class,
        "classification": classification,
        "confidence": confidence,
        "reason": reason,
        "evidence_ids": sorted(set(evidence_ids)),
        "evidence_scope": evidence_scope,
        "source_kind": "OBSERVED_PUBLIC_CSV_WITH_CONTEXTUAL_REFERENCES" if classification.startswith("SUPPORTED_") else "OBSERVED_PUBLIC_CSV_ONLY",
        "reason_code": {
            "SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED": "DATED_PUBLIC_HOLIDAY_OVERLAP_CAUSE_UNVERIFIED",
            "SUPPORTED_WEEKEND_PATTERN_BROKER_UNVERIFIED": "REPEATED_FRI_TO_SUN_MON_SHAPE_BROKER_HOURS_UNVERIFIED",
            "UNRESOLVED_OPEN_SESSION_GAP": "NON_NOMINAL_WEEKDAY_CADENCE_NO_SESSION_OR_FEED_PROOF",
            "UNRESOLVED": "NONSTANDARD_GAP_EVIDENCE_INSUFFICIENT",
        }[classification],
        "confidence_level": confidence,
        "broker_entity_status": "BROKER_ENTITY_UNVERIFIED",
        "broker_calendar_status": "NO_BROKER_ENTITY_HOST_CONFIRMED",
        "provider_evidence_status": "NOT_ESTABLISHED",
        "holiday_context_utc_dates": holiday_dates,
        "holiday_context_sources": holiday_context,
        "dst_context_utc_dates": dst_dates,
        "remaining_unknown_portion": {
            "start_utc": gap["gap_start_utc"],
            "end_utc": gap["gap_end_utc"],
            "seconds": gap["gap_seconds"],
            "reason": "Exact broker close/reopen schedule and quote/feed completeness remain unverified.",
        },
        "notes": notes,
        "validation_notes": notes,
        "verified_broker_closure": False,
        "verified_cause": False,
    })
    validate_gap_record(result)
    return result


def validate_gap_record(record: dict[str, Any]) -> None:
    """Minimal fail-closed record schema and evidence-level validation."""
    required = {
        "gap_id", "episode_id", "symbol", "timeframe", "previous_bar_open_utc", "next_bar_open_utc",
        "elapsed_seconds", "nominal_timeframe_seconds", "classification", "confidence_level", "reason_code",
        "evidence_ids", "evidence_scope", "source_kind", "validation_notes", "remaining_unknown_portion",
        "observation_class", "verified_cause", "provider_evidence_status",
    }
    missing = required - set(record)
    if missing:
        raise ValueError("GAP_RECORD_FIELDS_MISSING:" + ",".join(sorted(missing)))
    if record["classification"] not in ALLOWED_CLASSIFICATIONS:
        raise ValueError("GAP_CLASSIFICATION_INVALID")
    if not isinstance(record["evidence_ids"], list) or not all(isinstance(item, str) and item for item in record["evidence_ids"]):
        raise ValueError("GAP_EVIDENCE_IDS_INVALID")
    if not isinstance(record["validation_notes"], list) or not all(isinstance(item, str) for item in record["validation_notes"]):
        raise ValueError("GAP_VALIDATION_NOTES_INVALID")
    unknown = record["remaining_unknown_portion"]
    if not isinstance(unknown, dict) or not isinstance(unknown.get("seconds"), int) or unknown["seconds"] < 0:
        raise ValueError("GAP_UNKNOWN_PORTION_INVALID")
    if record["classification"].startswith("VERIFIED_"):
        if record.get("provider_evidence_status") != "DATED_OFFICIAL_PROVIDER_EVIDENCE_VERIFIED" or record.get("verified_cause") is not True:
            raise ValueError("VERIFIED_GAP_WITHOUT_PROVIDER_EVIDENCE")
    elif record.get("verified_cause") is True:
        raise ValueError("UNVERIFIED_CLASSIFICATION_MARKED_CAUSAL")
    if record["classification"] == "MIXED_PARTIAL_EVIDENCE" and not record.get("partial_evidence_portion"):
        raise ValueError("MIXED_GAP_WITHOUT_EXPLICIT_PARTIAL_COVERAGE")


def build_episode_groups(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    episodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gap in inventory:
        episodes[gap["episode_id"]].append(gap)
    result = []
    for episode_id, gaps in sorted(episodes.items(), key=lambda item: (item[1][0]["gap_start_utc"], item[1][0]["gap_end_utc"])):
        windows = sorted({(row["gap_start_utc"], row["gap_end_utc"]) for row in gaps})
        window_counts = Counter((row["gap_start_utc"], row["gap_end_utc"]) for row in gaps)
        dominant_window = sorted(window_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        edge_review_ids = sorted(row["gap_id"] for row in gaps if row.get("episode_alignment_status") == "EDGE_WINDOW_DIFFERENCE_REQUIRES_REVIEW")
        result.append({
            "episode_id": episode_id,
            "gap_start_utc": min(start for start, _ in windows),
            "gap_end_utc": max(end for _, end in windows),
            "observed_windows": [{"gap_start_utc": start, "gap_end_utc": end} for start, end in windows],
            "dominant_observed_window": {"gap_start_utc": dominant_window[0], "gap_end_utc": dominant_window[1]},
            "exact_window_count": len(windows),
            "edge_alignment_exception_count": len(edge_review_ids),
            "edge_review_gap_ids": edge_review_ids,
            "observed_gap_count": len(gaps),
            "symbols": sorted({row["symbol"] for row in gaps}),
            "timeframes": sorted({row["timeframe"] for row in gaps}, key=lambda item: list(TIMEFRAME_SECONDS).index(item)),
            "classifications": sorted({row["classification"] for row in gaps}),
            "gap_ids": sorted(row["gap_id"] for row in gaps),
            "grouping_semantics": (
                "candidate weekly or holiday context episode; aligned by UTC event date, not proof of shared cause"
                if any(row["classification"].startswith("SUPPORTED_") for row in gaps)
                else "exact same expected-missing-window timestamps; grouping is descriptive and does not assert common cause"
            ),
        })
    return result


def annotate_episode_alignment(inventory: list[dict[str, Any]]) -> None:
    """Flag residual edge differences inside otherwise shared date episodes."""
    episodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inventory:
        episodes[row["episode_id"]].append(row)
    for rows in episodes.values():
        if not any(row["classification"].startswith("SUPPORTED_") for row in rows):
            for row in rows:
                row["episode_alignment_status"] = "EXACT_WINDOW_OR_UNRESOLVED_EPISODE"
            continue
        counts = Counter((row["gap_start_utc"], row["gap_end_utc"]) for row in rows)
        dominant_start, dominant_end = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
        for row in rows:
            same = (row["gap_start_utc"], row["gap_end_utc"]) == (dominant_start, dominant_end)
            start_delta = int((datetime.fromisoformat(row["gap_start_utc"]) - datetime.fromisoformat(dominant_start)).total_seconds())
            end_delta = int((datetime.fromisoformat(row["gap_end_utc"]) - datetime.fromisoformat(dominant_end)).total_seconds())
            near_edge = (
                (start_delta == 0 and 0 < abs(end_delta) <= EPISODE_EDGE_REVIEW_TOLERANCE_SECONDS)
                or (end_delta == 0 and 0 < abs(start_delta) <= EPISODE_EDGE_REVIEW_TOLERANCE_SECONDS)
            )
            row["episode_alignment_status"] = (
                "MATCHES_DOMINANT_EPISODE_WINDOW" if same else
                "EDGE_WINDOW_DIFFERENCE_REQUIRES_REVIEW" if near_edge else
                "DISTINCT_WINDOW_SAME_CONTEXT_EPISODE"
            )
            row["episode_reference_window"] = {"gap_start_utc": dominant_start, "gap_end_utc": dominant_end}
            row["episode_window_delta_seconds"] = {"gap_start": start_delta, "gap_end": end_delta}
            if near_edge:
                note = "This bar interval differs from the dominant episode window at one edge; the full interval remains unverified and needs review."
                row["notes"].append(note)
                row["validation_notes"].append(note)


def build_classification_summary(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    def aggregate(key_fn):
        rows: dict[str, Counter[str]] = defaultdict(Counter)
        for gap in inventory:
            rows[key_fn(gap)][gap["classification"]] += 1
        return {key: dict(sorted(counts.items())) for key, counts in sorted(rows.items())}

    by_class = Counter(row["classification"] for row in inventory)
    by_observation = Counter(row["observation_class"] for row in inventory)
    by_provenance = Counter(row["evidence_scope"] for row in inventory)
    confidence = Counter(row["confidence"] for row in inventory)
    return {
        "schema_version": SCHEMA_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "as_of_utc": AS_OF_UTC,
        "observed_gap_count": len(inventory),
        "unique_episode_count": len({row["episode_id"] for row in inventory}),
        "classification_counts": dict(sorted(by_class.items())),
        "outcome_rollup": {
            "verified": sum(count for name, count in by_class.items() if name.startswith("VERIFIED_")),
            "supported": sum(count for name, count in by_class.items() if name.startswith("SUPPORTED_")),
            "unresolved": sum(count for name, count in by_class.items() if name.startswith("UNRESOLVED") or name in {"BROKER_ENTITY_UNVERIFIED", "EVIDENCE_INSUFFICIENT"}),
            "mixed": sum(count for name, count in by_class.items() if name == "MIXED_PARTIAL_EVIDENCE"),
        },
        "inventory_status": "INVENTORY_AND_CLASSIFICATION_COMPLETE",
        "official_broker_session_coverage_status": "UNKNOWN",
        "observation_class_counts": dict(sorted(by_observation.items())),
        "confidence_counts": dict(sorted(confidence.items())),
        "provenance_scope_counts": dict(sorted(by_provenance.items())),
        "by_confidence": aggregate(lambda row: row["confidence"]),
        "by_provenance": aggregate(lambda row: row["evidence_scope"]),
        "by_type": aggregate(lambda row: row["observation_class"]),
        "by_source_kind": aggregate(lambda row: row["source_kind"]),
        "by_symbol": aggregate(lambda row: row["symbol"]),
        "by_timeframe": aggregate(lambda row: row["timeframe"]),
        "by_symbol_timeframe": aggregate(lambda row: f"{row['symbol']}:{row['timeframe']}"),
        "by_year": aggregate(lambda row: row["gap_start_utc"][:4]),
        "by_month": aggregate(lambda row: row["gap_start_utc"][:7]),
        "by_reason": aggregate(lambda row: row["reason_code"]),
        "by_class_and_observation": aggregate(lambda row: row["classification"] + ":" + row["observation_class"]),
        "verified_broker_closure_count": sum(bool(row["verified_broker_closure"]) for row in inventory),
        "episode_edge_alignment_exception_count": sum(row.get("episode_alignment_status") == "EDGE_WINDOW_DIFFERENCE_REQUIRES_REVIEW" for row in inventory),
        "broker_entity_status": "BROKER_ENTITY_UNVERIFIED",
        "known_unknowns": [
            "Exact legal entity and account jurisdiction",
            "Historical broker hours and dated schedule exceptions",
            "Broker server timezone and timeframe boundary rules",
            "Quote/feed completeness and outages for each interval",
            "Whether the spread field and bars represent broker-observed execution conditions",
        ],
    }


def source_registry(as_of_utc: str, source_manifest_sha256: str) -> dict[str, Any]:
    holiday_calendar = {
        "US_OPM_2023_2026": {str(year): list(days) for year, days in sorted(_US_OPM_DATES.items())},
        "UK_ENGLAND_WALES_2023_2026": {str(year): list(days) for year, days in sorted(_UK_ENGLAND_WALES_DATES.items())},
        "CANADA_CRA_2026": list(_CANADA_CRA_2026_DATES),
        "AUSTRALIA_FAIRWORK_2026_CONTEXT": list(_AUSTRALIA_FAIRWORK_2026_DATES),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of_utc": as_of_utc,
        "public_source_reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC,
        "scope": "public context only; no source is trusted as this account's broker calendar",
        "classification_taxonomy": sorted(ALLOWED_CLASSIFICATIONS),
        "verification_gate": "A VERIFIED_* label requires dated official provider evidence covering the exact instrument and complete relevant interval; this report has none.",
        "trusted_broker_calendar_hosts": [],
        "holiday_calendar_snapshot": holiday_calendar,
        "holiday_calendar_snapshot_sha256": _canonical_hash(holiday_calendar),
        "sources": [
            {"evidence_id": "PUBLIC_DATA_2023_PLUS", "kind": "OBSERVED_DATA", "title": "Frozen public 2023+ OHLC snapshot", "path": "data/market/{symbol}/{timeframe}.csv", "source_manifest": "reports/phase2_2_2h/public_2023_source_manifest.json", "source_manifest_sha256": source_manifest_sha256, "domain": "repository-local", "issuer": "AgentTrading public repository snapshot", "publication_or_issue_date": None, "validity_scope": "Frozen snapshot through the declared per-file cutoff", "symbols_timeframes_covered": "EURUSD/USDCAD/AUDUSD × M5/M15/H1/H4/D1", "scope": "15 tracked CSVs; timestamp cadence observation only", "captured_as_of_utc": as_of_utc, "artifact_sha256": source_manifest_sha256, "license_limitations": "Repository data retained as-is; no external artifact copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "BIS_FX_GLOBAL_CONTEXT", "kind": "GENERAL_MARKET_CONTEXT", "title": "Implementation of the FX Global Code", "url": "https://www.bis.org/speeches/20171004-implementation-fx-global-code", "domain": "bis.org", "issuer": "Bank for International Settlements", "publication_or_issue_date": "2017-10-04", "validity_scope": "General market-structure explanation; no dated schedule", "symbols_timeframes_covered": "General FX only; no account symbols/session rules", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "Describes FX activity across centres over the day; it does not document a weekend closure or broker schedule.", "scope": "General FX activity across global financial centres; not this broker's schedule or a dated closure calendar", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase retained; no source document copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "US_OPM_HOLIDAYS", "kind": "DATED_PUBLIC_HOLIDAY_CONTEXT", "title": "Federal Holidays", "url": "https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/", "domain": "opm.gov", "issuer": "U.S. Office of Personnel Management", "publication_or_issue_date": None, "validity_scope": "Dated federal schedules for 2023–2026; not FX quote availability", "symbols_timeframes_covered": "General U.S. calendar context only", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "The page lists annual schedules including 2023–2026 and explains observed days; federal employee holidays do not define FX broker quote availability.", "scope": "U.S. federal employee holiday schedule; not a broker market-hours rule", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase and a date-only local calendar snapshot retained; no source document copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "UK_GOV_HOLIDAYS", "kind": "DATED_PUBLIC_HOLIDAY_CONTEXT", "title": "UK bank holidays", "url": "https://www.gov.uk/bank-holidays", "domain": "gov.uk", "issuer": "UK Government", "publication_or_issue_date": None, "validity_scope": "England and Wales date tables for 2023–2026; not FX quote availability", "symbols_timeframes_covered": "General UK calendar context only", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "Lists historical and scheduled bank holidays, including Good Friday/Easter Monday and Christmas/Boxing Day; dates do not define FX broker quote availability.", "scope": "England and Wales bank holiday dates; not a broker market-hours rule", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase and a date-only local calendar snapshot retained; no source document copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "CANADA_CRA_HOLIDAYS", "kind": "DATED_PUBLIC_HOLIDAY_CONTEXT", "title": "Public holidays", "url": "https://www.canada.ca/en/revenue-agency/services/tax/public-holidays.html", "domain": "canada.ca", "issuer": "Canada Revenue Agency", "publication_or_issue_date": "2026-01-06", "validity_scope": "Canada CRA's 2026 federal calendar; provinces and territories differ", "symbols_timeframes_covered": "General CAD calendar context only", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "Lists 2026 dates including Good Friday, Canada Day and Labour Day; its page explicitly notes provincial/territorial holidays vary.", "scope": "Canadian federal/public dates for 2026 only; not a broker market-hours rule", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase retained; no source document copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "AU_FAIRWORK_HOLIDAYS", "kind": "DATED_PUBLIC_HOLIDAY_CONTEXT", "title": "2026 public holidays", "url": "https://www.fairwork.gov.au/employment-conditions/public-holidays/2026-public-holidays", "domain": "fairwork.gov.au", "issuer": "Australian Fair Work Ombudsman", "publication_or_issue_date": None, "validity_scope": "Australia 2026 state and territory public dates; locations differ", "symbols_timeframes_covered": "General AUD calendar context only", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "Lists 2026 holidays by state/territory and warns dates differ by location; includes Good Friday, Easter Monday and Christmas.", "scope": "Australian state/territory calendar context for 2026 only; not a broker market-hours rule", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase retained; no source document copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "US_NIST_DST", "kind": "OFFICIAL_TIME_RULE_CONTEXT", "title": "Daylight Saving Time Rules", "url": "https://www.nist.gov/pml/time-and-frequency-division/popular-links/daylight-saving-time-dst", "domain": "nist.gov", "issuer": "U.S. National Institute of Standards and Technology", "publication_or_issue_date": None, "validity_scope": "U.S. civil-time DST rule reviewed as published on 2026-11-18", "symbols_timeframes_covered": "General U.S. civil-time context only", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "Second Sunday in March and first Sunday in November, with local time change at 2 a.m.; not this broker's server-time rule.", "scope": "U.S. civil time transitions; broker/server timezone not known", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase retained; no source document copied", "trusted_for_broker_sessions": False},
            {"evidence_id": "EU_SUMMERTIME_DATES", "kind": "OFFICIAL_TIME_RULE_CONTEXT", "title": "Summer-time arrangements for 2022–2026", "url": "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX%3A52021XC0427%2801%29", "domain": "eur-lex.europa.eu", "issuer": "Publications Office of the European Union", "publication_or_issue_date": "2021-04-27", "validity_scope": "Announced transition dates for 2022–2026 at 01:00 UTC", "symbols_timeframes_covered": "General EU civil-time context only", "reviewed_utc": PUBLIC_SOURCE_REVIEWED_UTC, "supporting_detail": "Lists EU summer-time transition dates at 01:00 UTC through 2026; broker/server timezone not known.", "scope": "EU civil time transitions; broker/server timezone not known", "artifact_sha256": None, "license_limitations": "Only a short factual paraphrase retained; no source document copied", "trusted_for_broker_sessions": False},
        ],
        "limitations": ["No account/broker identity is inferred from a platform brand.", "General FX or public-holiday references cannot establish broker closure.", "Dated public-holiday overlap is context only; it does not prove that quotes were unavailable."],
    }


def official_dst_transition_dates() -> list[str]:
    """Return dates from the fixed 2023–2026 US/EU transition calendar."""
    dates = []
    for year in range(2023, 2027):
        # US: second Sunday in March, first Sunday in November.
        march_first = date(year, 3, 1)
        first_sunday_march = 1 + ((6 - march_first.weekday()) % 7)
        dates.append(date(year, 3, first_sunday_march + 7).isoformat())
        november_first = date(year, 11, 1)
        dates.append(date(year, 11, 1 + ((6 - november_first.weekday()) % 7)).isoformat())
        # EU: last Sunday of March and October. Source registry identifies the
        # legal schedule source and the timezone remains contextual only.
        for month in (3, 10):
            last_day = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).day
            last_date = date(year, month, last_day)
            dates.append(date(year, month, last_day - ((last_date.weekday() - 6) % 7)).isoformat())
    return sorted(set(dates))
