"""Fail-closed broker-session continuity helpers for Phase 2.2.2d.

The old fixed-clock cadence check remains the baseline.  Session-aware
continuity is available only when a dated, hashed, symbol-scoped and
timeframe-scoped broker manifest covers the requested bar and its exception
calendar.  No weekday/weekend or DST rule is inferred from missing bars.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
INDICATOR_MINIMUM_BARS = 200
SESSION_CLASSIFICATIONS = frozenset({
    "VERIFIED_BROKER_CLOSED",
    "VERIFIED_BROKER_OPEN_NO_BAR",
    "BROKER_SESSION_OPEN_FEED_UNVERIFIED",
    "OBSERVED_NO_QUOTE_UNCONFIRMED",
    "UNKNOWN",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
SESSION_DOCUMENT_ROOT = Path(__file__).resolve().parents[1]


def _trusted_broker_source_hosts() -> frozenset[str]:
    """Read only the reviewed host allowlist; never trust manifest declarations."""
    path = SESSION_DOCUMENT_ROOT / "config" / "broker_calendar_trusted_sources.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        hosts = payload.get("trusted_source_hosts", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return frozenset()
    if not isinstance(hosts, list):
        return frozenset()
    normalized = {
        item.lower() for item in hosts
        if isinstance(item, str) and item and "*" not in item and "/" not in item and ":" not in item
    }
    return frozenset(normalized)


# Empty until the broker's exact legal entity and source domain are confirmed.
# A manifest cannot authorize its own source host.
TRUSTED_BROKER_SOURCE_HOSTS: frozenset[str] = _trusted_broker_source_hosts()
_RULE_REFERENCE_FIELDS = frozenset({
    "symbols", "timeframes", "timezone_rule", "weekly_sessions",
    "exceptions", "timeframe_boundary_policy", "effective_range",
    "exception_coverage",
})


def _utc(value: datetime | str | int | float) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("SESSION_TIMESTAMP_INVALID")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("SESSION_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return parsed.astimezone(UTC)


def _parse_local_date(value: Any) -> date:
    if not isinstance(value, str):
        raise ValueError("SESSION_DATE_INVALID")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("SESSION_DATE_INVALID") from None
    if parsed.isoformat() != value:
        raise ValueError("SESSION_DATE_INVALID")
    return parsed


def _parse_hhmm(value: Any, *, allow_24: bool = False) -> time:
    if allow_24 and value == "24:00":
        return time(0, 0)
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        raise ValueError("SESSION_LOCAL_TIME_INVALID")
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _timezone_rule(manifest: dict[str, Any]) -> dict[str, Any]:
    rule = manifest.get("timezone_rule")
    if not isinstance(rule, dict):
        raise ValueError("SESSION_TIMEZONE_RULE_MISSING")
    mode = rule.get("mode")
    if mode == "IANA":
        name = rule.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("SESSION_TIMEZONE_NAME_MISSING")
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError:
            raise ValueError("SESSION_TIMEZONE_UNKNOWN") from None
        return rule
    if mode == "NEW_YORK_SERVER_OFFSET":
        if not isinstance(rule.get("reference_timezone"), str):
            raise ValueError("SESSION_DST_REFERENCE_TIMEZONE_MISSING")
        try:
            ZoneInfo(rule["reference_timezone"])
        except ZoneInfoNotFoundError:
            raise ValueError("SESSION_DST_REFERENCE_TIMEZONE_UNKNOWN") from None
        standard = rule.get("standard_offset_minutes")
        daylight = rule.get("daylight_offset_minutes")
        if not isinstance(standard, int) or not isinstance(daylight, int) or standard == daylight:
            raise ValueError("SESSION_SERVER_OFFSETS_INVALID")
        return rule
    raise ValueError("SESSION_TIMEZONE_MODE_UNSUPPORTED")


def _server_local(value: datetime | str | int | float, rule: dict[str, Any]) -> datetime:
    instant = _utc(value)
    if rule["mode"] == "IANA":
        return instant.astimezone(ZoneInfo(rule["name"])).replace(tzinfo=None)
    reference = instant.astimezone(ZoneInfo(rule["reference_timezone"]))
    is_dst = bool(reference.dst() and reference.dst() != timedelta(0))
    offset = rule["daylight_offset_minutes"] if is_dst else rule["standard_offset_minutes"]
    return (instant + timedelta(minutes=offset)).replace(tzinfo=None)


def _local_to_utc(value: datetime, rule: dict[str, Any]) -> datetime:
    if value.tzinfo is not None:
        raise ValueError("SESSION_LOCAL_DATETIME_MUST_BE_NAIVE")
    if rule["mode"] == "IANA":
        zone = ZoneInfo(rule["name"])
        candidates: set[datetime] = set()
        for fold in (0, 1):
            candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == value:
                candidates.add(candidate)
        if not candidates:
            raise ValueError("SESSION_DST_NONEXISTENT_LOCAL_TIME")
        if len(candidates) > 1:
            raise ValueError("SESSION_DST_AMBIGUOUS_LOCAL_TIME")
        return next(iter(candidates))

    reference_zone = ZoneInfo(rule["reference_timezone"])
    candidates: set[datetime] = set()
    for offset in {rule["standard_offset_minutes"], rule["daylight_offset_minutes"]}:
        candidate = (value - timedelta(minutes=offset)).replace(tzinfo=UTC)
        reference = candidate.astimezone(reference_zone)
        is_dst = bool(reference.dst() and reference.dst() != timedelta(0))
        expected_offset = rule["daylight_offset_minutes"] if is_dst else rule["standard_offset_minutes"]
        if expected_offset == offset and _server_local(candidate, rule) == value:
            candidates.add(candidate)
    if not candidates:
        raise ValueError("SESSION_DST_NONEXISTENT_LOCAL_TIME")
    if len(candidates) > 1:
        raise ValueError("SESSION_DST_AMBIGUOUS_LOCAL_TIME")
    return next(iter(candidates))


def _effective_range(manifest: dict[str, Any]) -> tuple[datetime, datetime | None]:
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("SESSION_EVIDENCE_MISSING")
    start = evidence.get("effective_from_utc")
    end = evidence.get("effective_to_utc")
    if not start:
        raise ValueError("SESSION_EFFECTIVE_START_MISSING")
    try:
        start_utc = _utc(start)
        end_utc = _utc(end) if end else None
    except (ValueError, TypeError, OverflowError):
        raise ValueError("SESSION_EFFECTIVE_RANGE_INVALID") from None
    if end_utc is not None and end_utc <= start_utc:
        raise ValueError("SESSION_EFFECTIVE_RANGE_INVALID")
    return start_utc, end_utc


def _verify_document_evidence(evidence: dict[str, Any]) -> str | None:
    """Bind session rules to preserved bytes and a separately trusted host."""
    source_url = evidence.get("source_url")
    if not isinstance(source_url, str):
        return "SESSION_SOURCE_URL_INVALID"
    try:
        parsed_url = urlsplit(source_url)
        host = (parsed_url.hostname or "").lower()
        if (parsed_url.scheme != "https" or not host or parsed_url.username
                or parsed_url.password or parsed_url.port not in (None, 443)
                or parsed_url.query or parsed_url.fragment):
            return "SESSION_SOURCE_URL_INVALID"
    except ValueError:
        return "SESSION_SOURCE_URL_INVALID"
    if host not in TRUSTED_BROKER_SOURCE_HOSTS:
        return "SESSION_SOURCE_HOST_UNTRUSTED"

    raw_path = evidence.get("document_path")
    if not isinstance(raw_path, str) or "\\" in raw_path:
        return "SESSION_DOCUMENT_PATH_INVALID"
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return "SESSION_DOCUMENT_PATH_INVALID"
    candidate = SESSION_DOCUMENT_ROOT.joinpath(*relative.parts)
    try:
        root = SESSION_DOCUMENT_ROOT.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return "SESSION_DOCUMENT_PATH_UNAVAILABLE_OR_OUTSIDE_ROOT"
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            return "SESSION_DOCUMENT_SYMLINK_REJECTED"
    try:
        payload = resolved.read_bytes()
    except OSError:
        return "SESSION_DOCUMENT_READ_FAILED"
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    declared_sha256 = evidence.get("document_sha256")
    if not isinstance(declared_sha256, str) or not _SHA256.fullmatch(declared_sha256):
        return "SESSION_DOCUMENT_HASH_INVALID"
    if actual_sha256 != declared_sha256 or evidence.get("sha256") != actual_sha256:
        return "SESSION_DOCUMENT_HASH_MISMATCH"

    references = evidence.get("rule_references")
    if not isinstance(references, dict) or set(references) != _RULE_REFERENCE_FIELDS:
        return "SESSION_RULE_EVIDENCE_INCOMPLETE"
    for field in _RULE_REFERENCE_FIELDS:
        refs = references.get(field)
        if not isinstance(refs, list) or not refs:
            return "SESSION_RULE_EVIDENCE_INCOMPLETE"
        for ref in refs:
            if not isinstance(ref, dict):
                return "SESSION_RULE_REFERENCE_INVALID"
            start, end = ref.get("byte_start"), ref.get("byte_end")
            excerpt_sha256 = ref.get("excerpt_sha256")
            if (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start < 0 or end <= start or end > len(payload)
                    or not isinstance(excerpt_sha256, str)
                    or not _SHA256.fullmatch(excerpt_sha256)):
                return "SESSION_RULE_REFERENCE_INVALID"
            if hashlib.sha256(payload[start:end]).hexdigest() != excerpt_sha256:
                return "SESSION_RULE_REFERENCE_HASH_MISMATCH"
    return None


def validate_session_manifest(
    manifest: Any,
    symbol: str,
    timeframe: str,
    at_utc: datetime | str | int | float,
) -> str | None:
    """Return ``None`` only when all provenance and scope gates pass."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        return "SESSION_MANIFEST_SCHEMA_INVALID"
    if timeframe not in TIMEFRAME_SECONDS:
        return "SESSION_TIMEFRAME_UNSUPPORTED"
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("status") != "OFFICIAL_BROKER_DOCUMENTED":
        return "SESSION_OFFICIAL_SOURCE_UNVERIFIED"
    if evidence.get("official_source") is not True:
        return "SESSION_OFFICIAL_SOURCE_UNVERIFIED"
    document_error = _verify_document_evidence(evidence)
    if document_error:
        return document_error
    if not evidence.get("accessed_utc"):
        return "SESSION_SOURCE_ACCESS_TIME_MISSING"
    try:
        _utc(evidence["accessed_utc"])
    except (ValueError, TypeError, OverflowError):
        return "SESSION_SOURCE_ACCESS_TIME_INVALID"
    symbols = manifest.get("symbols")
    timeframes = manifest.get("timeframes")
    if not isinstance(symbols, (list, tuple)) or not all(isinstance(item, str) for item in symbols):
        return "SESSION_SYMBOL_SCOPE_INVALID"
    if not isinstance(timeframes, (list, tuple)) or not all(isinstance(item, str) for item in timeframes):
        return "SESSION_TIMEFRAME_SCOPE_INVALID"
    if symbol not in symbols or timeframe not in timeframes:
        return "SESSION_SCOPE_MISMATCH"
    version_id = manifest.get("rule_version_id")
    if not isinstance(version_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", version_id):
        return "SESSION_RULE_VERSION_INVALID"
    try:
        start, end = _effective_range(manifest)
        instant = _utc(at_utc)
        _timezone_rule(manifest)
    except (ValueError, TypeError, OverflowError) as exc:
        return str(exc) if str(exc).startswith("SESSION_") else "SESSION_MANIFEST_INVALID"
    if instant < start or (end is not None and instant >= end):
        return "SESSION_EVIDENCE_OUTSIDE_EFFECTIVE_DATES"
    if manifest.get("sessions_complete") is not True:
        return "SESSION_WEEKLY_RULE_INCOMPLETE"
    if manifest.get("exceptions_complete") is not True:
        return "SESSION_EXCEPTION_CALENDAR_INCOMPLETE"
    if not isinstance(manifest.get("exceptions"), dict):
        return "SESSION_EXCEPTION_MAP_INVALID"
    if not isinstance(manifest.get("weekly_sessions"), dict):
        return "SESSION_WEEKLY_RULE_MISSING"
    weekly_sessions = manifest["weekly_sessions"]
    if any(str(day) not in weekly_sessions for day in range(7)):
        return "SESSION_WEEKDAY_RULES_INCOMPLETE"
    policies = manifest.get("timeframe_boundary_policy")
    if not isinstance(policies, dict) or policies.get(timeframe) not in {"FIXED_UTC", "SERVER_WALL_CLOCK", "SERVER_LOCAL_DAILY"}:
        return "SESSION_BAR_BOUNDARY_UNVERIFIED"
    try:
        coverage = manifest.get("exception_coverage")
        if not isinstance(coverage, dict):
            return "SESSION_EXCEPTION_COVERAGE_INVALID"
        coverage_start = _parse_local_date(coverage.get("from_local_date"))
        coverage_end = _parse_local_date(coverage.get("through_local_date"))
        local_day = _server_local(instant, _timezone_rule(manifest)).date()
    except (ValueError, TypeError, OverflowError):
        return "SESSION_EXCEPTION_COVERAGE_INVALID"
    if coverage_end < coverage_start or not coverage_start <= local_day <= coverage_end:
        return "SESSION_EXCEPTION_COVERAGE_INCOMPLETE"
    try:
        for day, windows in manifest["weekly_sessions"].items():
            if day not in {str(index) for index in range(7)} or not isinstance(windows, list):
                return "SESSION_WEEKLY_RULE_INVALID"
            _validate_window_list(windows)
        for day, windows in manifest["exceptions"].items():
            _parse_local_date(day)
            _validate_window_list(windows, exception=True)
    except ValueError as exc:
        return str(exc) if str(exc).startswith("SESSION_") else "SESSION_WEEKLY_RULE_INVALID"
    return None


def select_session_manifest(
    manifests: list[Any],
    symbol: str,
    timeframe: str,
    at_utc: datetime | str | int | float,
) -> tuple[dict[str, Any] | None, str]:
    """Select exactly one dated rule version; gaps/overlaps remain UNKNOWN."""
    if not isinstance(manifests, list):
        return None, "UNKNOWN_MANIFEST_REGISTRY_INVALID"
    matches = [
        item for item in manifests
        if isinstance(item, dict)
        and validate_session_manifest(item, symbol, timeframe, at_utc) is None
    ]
    if len(matches) == 1:
        return matches[0], "VERIFIED_VERSION"
    if not matches:
        return None, "UNKNOWN_NO_RULE_VERSION_COVERS_INSTANT"
    return None, "CONFLICT_MULTIPLE_RULE_VERSIONS_COVER_INSTANT"


def _validate_window(window: Any) -> None:
    if not isinstance(window, dict):
        raise ValueError("SESSION_WINDOW_INVALID")
    start = window.get("start_local")
    end = window.get("end_local")
    start_time = _parse_hhmm(start)
    end_time = _parse_hhmm(end, allow_24=True)
    if end != "24:00" and start_time == end_time:
        raise ValueError("SESSION_WINDOW_ZERO_OR_AMBIGUOUS_LENGTH")


def _validate_window_list(windows: Any, *, exception: bool = False) -> None:
    if not isinstance(windows, list):
        raise ValueError("SESSION_EXCEPTION_RULE_INVALID" if exception else "SESSION_WEEKLY_RULE_INVALID")
    occupied: list[tuple[int, int]] = []
    for window in windows:
        _validate_window(window)
        start_time = _parse_hhmm(window["start_local"])
        end_text = window["end_local"]
        end_time = _parse_hhmm(end_text, allow_24=True)
        start_minute = start_time.hour * 60 + start_time.minute
        end_minute = 24 * 60 if end_text == "24:00" else end_time.hour * 60 + end_time.minute
        if end_minute > start_minute:
            occupied.append((start_minute, end_minute))
        else:
            occupied.extend(((start_minute, 24 * 60), (0, end_minute)))
    occupied.sort()
    for prior, current in zip(occupied, occupied[1:]):
        if current[0] < prior[1]:
            raise ValueError("SESSION_WINDOWS_OVERLAP")


def _local_window_to_utc(day: date, window: dict[str, Any], rule: dict[str, Any]) -> tuple[datetime, datetime]:
    start_time = _parse_hhmm(window["start_local"])
    end_text = window["end_local"]
    end_time = _parse_hhmm(end_text, allow_24=True)
    start_local = datetime.combine(day, start_time)
    end_day = day + timedelta(days=1) if end_text == "24:00" or end_time <= start_time else day
    end_local = datetime.combine(end_day, end_time)
    return _local_to_utc(start_local, rule), _local_to_utc(end_local, rule)


def expected_bar_close_utc(
    bar_open_utc: datetime | str | int | float,
    timeframe: str,
    manifest: dict[str, Any],
    *,
    symbol: str | None = None,
) -> datetime:
    """Compute the close only from an explicitly declared boundary policy."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("SESSION_TIMEFRAME_UNSUPPORTED")
    opened = _utc(bar_open_utc)
    symbols = manifest.get("symbols")
    target_symbol = symbol or (symbols[0] if isinstance(symbols, (list, tuple)) and symbols else None)
    error = validate_session_manifest(manifest, target_symbol, timeframe, opened)
    if error:
        raise ValueError(error)
    policy = manifest["timeframe_boundary_policy"][timeframe]
    seconds = TIMEFRAME_SECONDS[timeframe]
    if policy == "FIXED_UTC":
        if int(opened.timestamp()) % seconds:
            raise ValueError("SESSION_FIXED_UTC_BAR_ALIGNMENT_MISMATCH")
        return opened + timedelta(seconds=seconds)
    rule = _timezone_rule(manifest)
    local_open = _server_local(opened, rule)
    if policy == "SERVER_LOCAL_DAILY":
        if timeframe != "D1" or local_open.time() != time(0, 0):
            raise ValueError("SESSION_D1_BOUNDARY_MISMATCH")
        local_close = datetime.combine(local_open.date() + timedelta(days=1), time(0, 0))
    elif policy == "SERVER_WALL_CLOCK":
        local_close = local_open + timedelta(seconds=seconds)
    else:
        raise ValueError("SESSION_BAR_BOUNDARY_UNVERIFIED")
    return _local_to_utc(local_close, rule)


def classify_missing_bar_slot(
    manifest: dict[str, Any] | None,
    symbol: str,
    timeframe: str,
    bar_open_utc: datetime | str | int | float,
    *,
    feed_completeness_verified: bool = False,
    observed_no_quote: bool = False,
) -> str:
    """Classify one absent slot; missing data alone never proves closure."""
    try:
        opened = _utc(bar_open_utc)
    except (ValueError, TypeError, OverflowError):
        return "UNKNOWN"
    if manifest is None:
        return "OBSERVED_NO_QUOTE_UNCONFIRMED" if observed_no_quote else "UNKNOWN"
    error = validate_session_manifest(manifest, symbol, timeframe, opened)
    if error:
        return "OBSERVED_NO_QUOTE_UNCONFIRMED" if observed_no_quote else "UNKNOWN"
    try:
        closed = expected_bar_close_utc(opened, timeframe, manifest, symbol=symbol)
        if validate_session_manifest(manifest, symbol, timeframe, closed - timedelta(microseconds=1)):
            return "UNKNOWN"
        rule = _timezone_rule(manifest)
        local_start = _server_local(opened, rule)
        local_end = _server_local(closed - timedelta(microseconds=1), rule)
        coverage = manifest["exception_coverage"]
        coverage_start = _parse_local_date(coverage["from_local_date"])
        coverage_end = _parse_local_date(coverage["through_local_date"])
        if not coverage_start <= local_start.date() <= coverage_end or not coverage_start <= local_end.date() <= coverage_end:
            return "UNKNOWN"
        relevant_dates = set()
        day = local_start.date() - timedelta(days=1)
        final_day = local_end.date()
        while day <= final_day:
            relevant_dates.add(day)
            day += timedelta(days=1)
        windows: list[tuple[datetime, datetime]] = []
        for session_day in sorted(relevant_dates):
            key = session_day.isoformat()
            sessions = manifest["exceptions"].get(key, manifest["weekly_sessions"][str(session_day.weekday())])
            for window in sessions:
                windows.append(_local_window_to_utc(session_day, window, rule))
        overlaps = [
            (left, right)
            for left, right in windows
            if max(opened, left) < min(closed, right)
        ]
        fully_open = any(left <= opened and closed <= right for left, right in windows)
        if fully_open:
            return "VERIFIED_BROKER_OPEN_NO_BAR" if feed_completeness_verified else "BROKER_SESSION_OPEN_FEED_UNVERIFIED"
        if overlaps:
            return "UNKNOWN"
        return "VERIFIED_BROKER_CLOSED"
    except (KeyError, ValueError, TypeError, OverflowError):
        return "OBSERVED_NO_QUOTE_UNCONFIRMED" if observed_no_quote else "UNKNOWN"


def _expected_next_open(
    opened: datetime,
    timeframe: str,
    manifest: dict[str, Any],
) -> datetime:
    policy = manifest["timeframe_boundary_policy"][timeframe]
    if policy == "FIXED_UTC":
        if int(opened.timestamp()) % TIMEFRAME_SECONDS[timeframe]:
            raise ValueError("SESSION_FIXED_UTC_BAR_ALIGNMENT_MISMATCH")
        return opened + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    rule = _timezone_rule(manifest)
    local = _server_local(opened, rule)
    if policy == "SERVER_LOCAL_DAILY":
        return _local_to_utc(datetime.combine(local.date() + timedelta(days=1), time(0, 0)), rule)
    if policy == "SERVER_WALL_CLOCK":
        return _local_to_utc(local + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]), rule)
    raise ValueError("SESSION_BAR_BOUNDARY_UNVERIFIED")


def _gap_is_verified_closed(
    left_open: datetime,
    right_open: datetime,
    symbol: str,
    timeframe: str,
    manifest: dict[str, Any],
    *,
    max_slots: int = 20000,
) -> bool:
    if validate_session_manifest(manifest, symbol, timeframe, left_open):
        return False
    if validate_session_manifest(manifest, symbol, timeframe, right_open):
        return False
    try:
        expected_bar_close_utc(right_open, timeframe, manifest, symbol=symbol)
    except (ValueError, TypeError, OverflowError):
        return False
    cursor = left_open
    visited = 0
    while True:
        next_open = _expected_next_open(cursor, timeframe, manifest)
        if next_open == right_open:
            return True
        if next_open > right_open:
            return False
        if classify_missing_bar_slot(manifest, symbol, timeframe, next_open) != "VERIFIED_BROKER_CLOSED":
            return False
        cursor = next_open
        visited += 1
        if visited > max_slots:
            return False


def build_session_aware_segments(
    timestamps: list[int],
    timeframe: str,
    symbol: str,
    manifest: dict[str, Any] | None,
) -> list[tuple[int, int]]:
    """Return half-open ranges; bridge only gaps fully verified as closed."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("SESSION_TIMEFRAME_UNSUPPORTED")
    ordered = sorted(set(int(value) for value in timestamps))
    if not ordered:
        return []
    result: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(ordered)):
        left = datetime.fromtimestamp(ordered[index - 1], UTC)
        right = datetime.fromtimestamp(ordered[index], UTC)
        if manifest is None:
            join = ordered[index] - ordered[index - 1] == TIMEFRAME_SECONDS[timeframe]
        else:
            try:
                error = validate_session_manifest(manifest, symbol, timeframe, left)
                right_error = validate_session_manifest(manifest, symbol, timeframe, right)
                join = not error and not right_error and _gap_is_verified_closed(left, right, symbol, timeframe, manifest)
                if ordered[index] - ordered[index - 1] == TIMEFRAME_SECONDS[timeframe]:
                    join = not error
            except (ValueError, TypeError, OverflowError):
                join = False
        if not join:
            result.append((start, index))
            start = index
    result.append((start, len(ordered)))
    return result


def build_segment_start_index(
    series: dict[str, list[int]],
    symbol: str,
    manifest: dict[str, Any] | None,
) -> dict[str, list[int]]:
    """Precompute the start index of each unresolved-gap segment once."""
    result: dict[str, list[int]] = {}
    for timeframe in TIMEFRAME_SECONDS:
        timestamps = sorted(set(int(item) for item in series.get(timeframe, [])))
        starts = [0] * len(timestamps)
        for first, end in build_session_aware_segments(timestamps, timeframe, symbol, manifest):
            for index in range(first, end):
                starts[index] = first
        result[timeframe] = starts
    return result


def _bar_close_or_none(opened_epoch: int, timeframe: str, manifest: dict[str, Any], symbol: str) -> int | None:
    try:
        return int(expected_bar_close_utc(opened_epoch, timeframe, manifest, symbol=symbol).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _fresh_after_verified_closures(
    last_open: datetime,
    timeframe: str,
    decision: datetime,
    symbol: str,
    manifest: dict[str, Any],
    *,
    max_slots: int = 20000,
) -> bool:
    cursor = last_open
    visited = 0
    while True:
        try:
            next_open = _expected_next_open(cursor, timeframe, manifest)
            next_close = expected_bar_close_utc(next_open, timeframe, manifest, symbol=symbol)
        except (ValueError, TypeError, OverflowError):
            return False
        if next_close > decision:
            return True
        if classify_missing_bar_slot(manifest, symbol, timeframe, next_open) != "VERIFIED_BROKER_CLOSED":
            return False
        cursor = next_open
        visited += 1
        if visited > max_slots:
            return False


def evaluate_broker_session_mtf_decision(
    symbol: str,
    decision_time_utc: datetime | str | int | float,
    series: dict[str, list[int]],
    manifest: dict[str, Any] | None,
    *,
    segment_starts_by_timeframe: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    """Evaluate causal MTF readiness using only verified session continuity."""
    decision = _utc(decision_time_utc)
    per_timeframe: dict[str, Any] = {}
    reasons: list[str] = []
    verified = manifest is not None
    if not verified:
        reasons.append("BROKER_CALENDAR_UNVERIFIED")
    for timeframe, seconds in TIMEFRAME_SECONDS.items():
        timestamps = sorted(set(int(item) for item in series.get(timeframe, [])))
        closes: list[int | None] = [
            _bar_close_or_none(item, timeframe, manifest, symbol) if manifest is not None else item + seconds
            for item in timestamps
        ]
        eligible_indices = [index for index, closed in enumerate(closes) if closed is not None and closed <= int(decision.timestamp())]
        if not eligible_indices:
            reasons.append(f"{timeframe}_NO_CLOSED_BAR")
            per_timeframe[timeframe] = {"closed_bar_available": False, "bars_since_unresolved_gap": 0, "fresh": False}
            continue
        index = eligible_indices[-1]
        opened_epoch = timestamps[index]
        closed_epoch = closes[index]
        assert closed_epoch is not None
        if closed_epoch > int(decision.timestamp()):
            reasons.append(f"{timeframe}_LOOKAHEAD_REJECTED")
        if segment_starts_by_timeframe is not None and len(segment_starts_by_timeframe.get(timeframe, [])) == len(timestamps):
            segment_start = segment_starts_by_timeframe[timeframe][index]
        else:
            segments = build_session_aware_segments(timestamps[: index + 1], timeframe, symbol, manifest)
            segment_start = segments[-1][0] if segments else index
        bars_since_gap = index - segment_start + 1
        age = int(decision.timestamp()) - closed_epoch
        if manifest is None:
            fresh = 0 <= age <= seconds
        else:
            error = validate_session_manifest(manifest, symbol, timeframe, decision)
            fresh = False if error else (
                0 <= age <= seconds
                or _fresh_after_verified_closures(
                    datetime.fromtimestamp(opened_epoch, UTC), timeframe, decision, symbol, manifest
                )
            )
        if not fresh:
            reasons.append(f"{timeframe}_CLOSED_BAR_STALE_OR_CALENDAR_UNKNOWN")
        if bars_since_gap < INDICATOR_MINIMUM_BARS:
            reasons.append(f"{timeframe}_WARMUP_INSUFFICIENT_200")
        per_timeframe[timeframe] = {
            "closed_bar_available": closed_epoch <= int(decision.timestamp()),
            "latest_closed_bar_open_utc": datetime.fromtimestamp(opened_epoch, UTC).isoformat().replace("+00:00", "Z"),
            "latest_closed_bar_close_utc": datetime.fromtimestamp(closed_epoch, UTC).isoformat().replace("+00:00", "Z"),
            "bars_since_unresolved_gap": bars_since_gap,
            "fresh_by_verified_session": fresh,
            "warmup_200": bars_since_gap >= INDICATOR_MINIMUM_BARS,
            "ema200_stabilized_692": bars_since_gap >= 692,
        }
    ready = verified and len(per_timeframe) == len(TIMEFRAME_SECONDS) and all(
        item["closed_bar_available"] and item["fresh_by_verified_session"] and item["warmup_200"]
        for item in per_timeframe.values()
    )
    stable = ready and all(item["ema200_stabilized_692"] for item in per_timeframe.values())
    return {
        "symbol": symbol,
        "decision_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "mode": "BROKER_VERIFIED_SESSIONS" if verified else "STRICT_FIXED_CADENCE",
        "calendar_verified": verified,
        "feature_ready_all_timeframes": ready,
        "ema200_stabilized_all_timeframes": stable,
        "lookahead_detected": any(reason.endswith("LOOKAHEAD_REJECTED") for reason in reasons),
        "ineligibility_reasons": sorted(set(reasons)),
        "timeframes": per_timeframe,
    }
