from __future__ import annotations

from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_LIVE_REQUESTS = 4
TIMEOUT_SECONDS = 10
CACHE_TTL_SECONDS = 6 * 60 * 60
USER_AGENT = "AgentTradingResearch/2.2.2e (+https://github.com/cmirlop/AgentTrading; offline-first)"
ALLOWED_FEEDS = {
    "ECB_PRESS_RSS": ("https://www.ecb.europa.eu/rss/press.html", "www.ecb.europa.eu", "RSS_OR_ATOM"),
    "FED_PRESS_RSS": ("https://www.federalreserve.gov/feeds/press_all.xml", "www.federalreserve.gov", "RSS_OR_ATOM"),
    "BANK_OF_CANADA_PRESS_RSS": ("https://www.bankofcanada.ca/content_type/press-releases/feed/", "www.bankofcanada.ca", "RSS_OR_ATOM"),
    "RBA_MEDIA_RSS": ("https://www.rba.gov.au/rss/rss-cb-media-releases.xml", "www.rba.gov.au", "RSS_OR_ATOM"),
    "BLS_RELEASE_ICS": ("https://www.bls.gov/schedule/news_release/bls.ics", "www.bls.gov", "ICS"),
}
HIGH_TITLE_TERMS = (
    re.compile(r"\b(policy rate|monetary policy decision|interest rate decision|fomc statement)\b", re.I),
    re.compile(r"\b(employment|jobs report|consumer price index|inflation|cpi)\b", re.I),
)
_TAG = re.compile(r"<[^>]*>")
_UNSAFE_CONTENT = re.compile(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_DOCTYPE = re.compile(br"<!\s*(DOCTYPE|ENTITY)\b", re.I)


class MacroSourceError(ValueError):
    pass


def _utc_iso(value: datetime | None) -> str | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _clean_text(value: str | None, limit: int = 240) -> str:
    if not value:
        return ""
    text = html.unescape(_TAG.sub(" ", _UNSAFE_CONTENT.sub(" ", value)))
    text = " ".join(text.split())
    return text[:limit]


def _priority(title: str) -> tuple[str, str]:
    for pattern in HIGH_TITLE_TERMS:
        if pattern.search(title):
            return "HIGH_RESEARCH", "TITLE_KEYWORD_RULE"
    return "UNKNOWN", "NO_VERIFIED_CLASSIFICATION"


def _local_datetime(value: datetime, zone_name: str) -> datetime:
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise MacroSourceError("ICS_TIMEZONE_UNKNOWN") from None
    candidates: set[datetime] = set()
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(aware.astimezone(timezone.utc))
    if not candidates:
        raise MacroSourceError("ICS_DST_NONEXISTENT_LOCAL_TIME")
    if len(candidates) > 1:
        raise MacroSourceError("ICS_DST_AMBIGUOUS_LOCAL_TIME")
    return next(iter(candidates))


def _parse_ics_datetime(raw: str, params: dict[str, str]) -> tuple[datetime | None, str, str | None]:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", raw):
        try:
            date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
        except ValueError:
            return None, "INVALID", None
        return None, "DATE_ONLY", params.get("TZID")
    if re.fullmatch(r"\d{8}T\d{6}Z", raw):
        try:
            value = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None, "INVALID", None
        return value, "SECOND", "UTC"
    if re.fullmatch(r"\d{8}T\d{6}", raw):
        try:
            value = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        except ValueError:
            return None, "INVALID", None
        zone_name = params.get("TZID")
        if not zone_name:
            return None, "FLOATING_TIMEZONE_UNKNOWN", None
        try:
            return _local_datetime(value, zone_name), "SECOND", zone_name
        except MacroSourceError:
            return None, "DST_OR_TIMEZONE_INVALID", zone_name
    return None, "INVALID", None


def parse_ics(payload: bytes, source: dict[str, Any], retrieved_at: datetime) -> list[dict[str, Any]]:
    if len(payload) > MAX_FEED_BYTES:
        raise MacroSourceError("FEED_TOO_LARGE")
    text = payload.decode("utf-8-sig", errors="strict")
    if "BEGIN:VCALENDAR" not in text.upper() or "END:VCALENDAR" not in text.upper():
        raise MacroSourceError("ICS_MALFORMED")
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    events: list[dict[str, Any]] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise MacroSourceError("ICS_NESTED_VEVENT")
            current = {}
        elif line == "END:VEVENT":
            if current is None:
                raise MacroSourceError("ICS_UNBALANCED_VEVENT")
            events.append(_normalize_ics_event(current, source, retrieved_at))
            current = None
        elif current is not None and ":" in line:
            left, value = line.split(":", 1)
            parts = left.split(";")
            key = parts[0].upper()
            params = {}
            for part in parts[1:]:
                if "=" in part:
                    name, item = part.split("=", 1)
                    params[name.upper()] = item.strip('"')
            current[key] = (params, value)
    if current is not None:
        raise MacroSourceError("ICS_UNBALANCED_VEVENT")
    return events


def _normalize_ics_event(fields: dict[str, tuple[dict[str, str], str]], source: dict[str, Any],
                          retrieved_at: datetime) -> dict[str, Any]:
    def val(name: str) -> tuple[dict[str, str], str]:
        return fields.get(name, ({}, ""))
    uid = _clean_text(val("UID")[1], 180)
    summary = _clean_text(val("SUMMARY")[1], 240)
    raw_dtstart_params, raw_dtstart = val("DTSTART")
    scheduled, precision, original_zone = _parse_ics_datetime(raw_dtstart, raw_dtstart_params)
    raw_updated = val("LAST-MODIFIED")[1] or val("DTSTAMP")[1]
    updated, _, _ = _parse_ics_datetime(raw_updated, val("LAST-MODIFIED")[0] or val("DTSTAMP")[0])
    sequence_text = val("SEQUENCE")[1]
    try:
        revision = max(0, int(sequence_text or 0))
    except ValueError:
        revision = None
    priority, basis = _priority(summary)
    event_id = uid or hashlib.sha256(f"{source['source_id']}|{summary}|{raw_dtstart}".encode()).hexdigest()
    return {
        "source_id": source["source_id"], "source_type": "SCHEDULED_EVENT",
        "event_id": event_id, "title": summary, "url": "",
        "currencies": list(source["currencies"]), "published_at_utc": None,
        "scheduled_at_utc": _utc_iso(scheduled), "observed_at_utc": _utc_iso(retrieved_at),
        "retrieved_at_utc": _utc_iso(retrieved_at), "updated_at_utc": _utc_iso(updated),
        "timezone_original": original_zone, "timestamp_precision": precision,
        "source_sha256": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
        "status": "SCHEDULED_VERIFIED" if scheduled else "SCHEDULED_TIME_UNKNOWN",
        "priority_basis": basis, "importance": priority, "revision": revision,
        "recurrence_status": "RECURRENCE_NOT_EXPANDED" if val("RRULE")[1] else "SINGLE_EVENT",
    }


def parse_rss_atom(payload: bytes, source: dict[str, Any], retrieved_at: datetime) -> list[dict[str, Any]]:
    if len(payload) > MAX_FEED_BYTES:
        raise MacroSourceError("FEED_TOO_LARGE")
    if _DOCTYPE.search(payload):
        raise MacroSourceError("XML_DTD_OR_ENTITY_REJECTED")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        raise MacroSourceError("XML_MALFORMED") from None
    root_name = root.tag.rsplit("}", 1)[-1].lower()
    root_namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if root_name not in {"rss", "feed"} and not (
        root_name == "rdf" and root_namespace == "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    ):
        raise MacroSourceError("XML_ROOT_INVALID")
    nodes = [
        element for element in root.iter()
        if str(element.tag).rsplit("}", 1)[-1].lower() == "item"
    ]
    atom_ns = "{http://www.w3.org/2005/Atom}"
    if not nodes and root_name == "feed":
        nodes = [
            element for element in root.iter()
            if str(element.tag).rsplit("}", 1)[-1].lower() == "entry"
        ]

    def safe_xml_text(node: Any) -> str:
        pieces: list[str] = []

        def visit(element: Any) -> None:
            local_name = str(element.tag).rsplit("}", 1)[-1].lower()
            if local_name in {"script", "style"}:
                return
            if element.text:
                pieces.append(element.text)
            for child in list(element):
                visit(child)
                if child.tail:
                    pieces.append(child.tail)

        visit(node)
        return "".join(pieces)

    events = []
    for item in nodes:
        def child_text(names: tuple[str, ...]) -> str:
            for name in names:
                found = item.find(name)
                if found is not None:
                    value = safe_xml_text(found)
                    if value:
                        return value
            return ""
        title = _clean_text(child_text(("title", f"{atom_ns}title")))
        link = item.find("link")
        atom_link = item.find(f"{atom_ns}link")
        url = ""
        if link is not None:
            url = (link.text or "").strip()
            if not url:
                url = (link.attrib.get("href") or "").strip()
        elif atom_link is not None:
            url = (atom_link.attrib.get("href") or "").strip()
        if url and urllib.parse.urlsplit(url).scheme not in {"https", "http"}:
            url = ""
        published_raw = child_text(("pubDate", "published", f"{atom_ns}published", "updated", f"{atom_ns}updated"))
        published = _parse_datetime(published_raw)
        identifier = child_text(("guid", "id", f"{atom_ns}id")).strip() or url
        if not identifier:
            identifier = hashlib.sha256(f"{source['source_id']}|{title}|{published_raw}".encode()).hexdigest()
        priority, basis = _priority(title)
        events.append({
            "source_id": source["source_id"], "source_type": "NEWS_RELEASE",
            "event_id": _clean_text(identifier, 240), "title": title,
            "url": url, "currencies": list(source["currencies"]),
            "published_at_utc": _utc_iso(published), "scheduled_at_utc": None,
            "observed_at_utc": _utc_iso(retrieved_at), "retrieved_at_utc": _utc_iso(retrieved_at),
            "updated_at_utc": _utc_iso(_parse_datetime(child_text(("updated", f"{atom_ns}updated")))),
            "timezone_original": "UTC" if published else None,
            "timestamp_precision": "SECOND" if published else "UNKNOWN",
            "source_sha256": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
            "status": "NEWS_PUBLISHED" if published else "NEWS_PUBLICATION_TIME_UNKNOWN",
            "priority_basis": basis, "importance": priority, "revision": None,
        })
    return events


def normalize_feed(payload: bytes, source: dict[str, Any], retrieved_at: datetime) -> dict[str, Any]:
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise MacroSourceError("RETRIEVAL_TIME_MUST_BE_AWARE")
    expected = ALLOWED_FEEDS.get(source.get("source_id"))
    if not expected or source.get("feed_url") != expected[0] or source.get("host") != expected[1]:
        raise MacroSourceError("SOURCE_NOT_ALLOWLISTED")
    if len(payload) > MAX_FEED_BYTES:
        raise MacroSourceError("FEED_TOO_LARGE")
    raw_hash = hashlib.sha256(payload).hexdigest()
    if expected[2] == "ICS":
        events = parse_ics(payload, source, retrieved_at)
    else:
        events = parse_rss_atom(payload, source, retrieved_at)
    events = _dedupe_event_revisions(events)
    status = "FETCH_OK" if events else "PARTIAL"
    return {
        "source_id": source["source_id"], "source_type": source["source_type"],
        "origin": source["host"], "status": status, "event_count": len(events),
        "source_sha256": raw_hash, "retrieved_at_utc": _utc_iso(retrieved_at),
        "events": events,
    }


def _dedupe_event_revisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one current event while retaining first publication and revisions."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (str(event.get("source_id")), str(event.get("event_id")))
        current = grouped.get(key)
        revision = {
            "revision": event.get("revision"),
            "published_at_utc": event.get("published_at_utc"),
            "updated_at_utc": event.get("updated_at_utc"),
            "observed_at_utc": event.get("observed_at_utc"),
            "source_sha256": event.get("source_sha256"),
        }
        if current is None:
            selected = dict(event)
            selected["initial_published_at_utc"] = event.get("published_at_utc")
            selected["revision_history"] = [revision]
            selected["revision_count"] = 1
            grouped[key] = selected
            continue
        current["revision_history"].append(revision)
        current["revision_count"] += 1
        initial = [
            value for value in (current.get("initial_published_at_utc"), event.get("published_at_utc"))
            if value
        ]
        current["initial_published_at_utc"] = min(initial) if initial else None
        prior_revision = current.get("revision")
        candidate_revision = event.get("revision")
        replace_current = (
            isinstance(candidate_revision, int)
            and (not isinstance(prior_revision, int) or candidate_revision >= prior_revision)
        ) or (
            candidate_revision is None
            and event.get("updated_at_utc")
            and event.get("updated_at_utc") >= (current.get("updated_at_utc") or "")
        )
        if replace_current:
            history = current["revision_history"]
            count = current["revision_count"]
            initial_published = current["initial_published_at_utc"]
            current.update(event)
            current["revision_history"] = history
            current["revision_count"] = count
            current["initial_published_at_utc"] = initial_published
    return list(grouped.values())


def event_is_available_as_of(event: dict[str, Any], decision_time: datetime) -> bool:
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise MacroSourceError("DECISION_TIME_MUST_BE_AWARE")
    if event.get("source_type") == "NEWS_RELEASE":
        published = _parse_datetime(event.get("initial_published_at_utc") or event.get("published_at_utc"))
        observed = _parse_datetime(event.get("observed_at_utc"))
        if published is None or observed is None:
            return False
        available = max(published, observed)
    else:
        available = _parse_datetime(event.get("observed_at_utc"))
    return available is not None and available <= decision_time.astimezone(timezone.utc)


def safe_calendar_preview(events: list[dict[str, Any]], as_of: datetime) -> dict[str, Any]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise MacroSourceError("PREVIEW_TIME_MUST_BE_AWARE")
    selected = []
    for event in events:
        if (event.get("source_type") != "SCHEDULED_EVENT"
                or event.get("status") != "SCHEDULED_VERIFIED"
                or event.get("timestamp_precision") != "SECOND"
                or event.get("importance") != "HIGH_RESEARCH"
                or not event_is_available_as_of(event, as_of)):
            continue
        selected.append({
            "source_id": event["source_id"], "event_id": event["event_id"],
            "scheduled_at_utc": event["scheduled_at_utc"], "currencies": event["currencies"],
            "importance": "HIGH_RESEARCH", "preview_only": True,
            "active_risk_gate": False,
        })
    return {
        "schema_version": 1, "status": "PREVIEW_ONLY_NOT_ACTIVE",
        "as_of_utc": _utc_iso(as_of), "events": selected,
        "legacy_calendar_state_modified": False,
        "note": "News is excluded; broker sessions are a separate unknown evidence domain.",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise MacroSourceError("HTTP_REDIRECT_REJECTED")


def _assert_public_dns(host: str) -> None:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise MacroSourceError("DNS_LOOKUP_FAILED") from None
    if not answers:
        raise MacroSourceError("DNS_LOOKUP_FAILED")
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise MacroSourceError("NON_PUBLIC_ADDRESS_REJECTED")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{hashlib.sha256(data).hexdigest()[:12]}.tmp")
    temp.write_bytes(data)
    temp.replace(path)


def fetch_live_sources(root: Path, registry: dict[str, Any], *, request_limit: int = MAX_LIVE_REQUESTS,
                       now: datetime | None = None,
                       progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if isinstance(request_limit, bool) or not isinstance(request_limit, int) or not 0 <= request_limit <= MAX_LIVE_REQUESTS:
        raise MacroSourceError("LIVE_REQUEST_LIMIT_EXCEEDED")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise MacroSourceError("RETRIEVAL_TIME_MUST_BE_AWARE")
    sources = registry.get("sources")
    if not isinstance(sources, list):
        raise MacroSourceError("SOURCE_REGISTRY_INVALID")
    cache_dir = root / "reports" / "private" / "phase2_2_2e" / "macro_cache"
    results = []
    requests_made = 0
    origins_seen: set[str] = set()
    for index, source in enumerate(sources, 1):
        response_status = None
        response_sha256 = None
        if source.get("live_enabled") is not True:
            if progress:
                progress(f"source {index}/{len(sources)} {source.get('source_id')}: skipped by policy")
            results.append({"source_id": source.get("source_id"), "status": "NOT_FETCHED_BY_POLICY", "events": []})
            continue
        allow = ALLOWED_FEEDS.get(source.get("source_id"))
        if not allow or source.get("feed_url") != allow[0] or source.get("host") != allow[1]:
            results.append({"source_id": source.get("source_id"), "status": "BLOCKED_NOT_ALLOWLISTED", "events": []})
            continue
        host = allow[1]
        if host in origins_seen:
            results.append({"source_id": source["source_id"], "status": "BLOCKED_DUPLICATE_ORIGIN", "events": []})
            continue
        origins_seen.add(host)
        raw_path = cache_dir / f"{source['source_id'].lower()}.feed"
        meta_path = cache_dir / f"{source['source_id'].lower()}.json"
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        age = None
        cached_at = _parse_datetime(meta.get("retrieved_at_utc"))
        if cached_at:
            age = (now.astimezone(timezone.utc) - cached_at).total_seconds()
        if raw_path.exists() and age is not None and 0 <= age < CACHE_TTL_SECONDS:
            try:
                payload = raw_path.read_bytes()
                normalized = normalize_feed(payload, source, cached_at)
                normalized.update({"fetch_status": "CACHE_HIT", "http_status": None, "cache_age_seconds": int(age)})
                results.append(normalized)
                if progress:
                    progress(f"source {index}/{len(sources)} {source['source_id']}: private cache hit")
                continue
            except (OSError, MacroSourceError):
                pass
        if requests_made >= request_limit:
            if progress:
                progress(f"source {index}/{len(sources)} {source['source_id']}: request budget exhausted")
            results.append({"source_id": source["source_id"], "status": "NOT_FETCHED_REQUEST_BUDGET", "events": []})
            continue
        if progress:
            progress(f"source {index}/{len(sources)} {source['source_id']}: at most one HTTPS request")
        try:
            _assert_public_dns(host)
            headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/xml, text/calendar;q=0.9"}
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
            request = urllib.request.Request(source["feed_url"], headers=headers, method="GET")
            opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            requests_made += 1
            try:
                with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                    status_code = int(response.status)
                    response_status = status_code
                    if status_code in {403, 429}:
                        results.append({"source_id": source["source_id"], "status": "UNAVAILABLE", "http_status": status_code, "events": []})
                        continue
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except (TypeError, ValueError):
                            raise MacroSourceError("CONTENT_LENGTH_INVALID") from None
                        if declared_length < 0:
                            raise MacroSourceError("CONTENT_LENGTH_INVALID")
                        if declared_length > MAX_FEED_BYTES:
                            raise MacroSourceError("FEED_TOO_LARGE")
                    payload = response.read(MAX_FEED_BYTES + 1)
                    if len(payload) > MAX_FEED_BYTES:
                        raise MacroSourceError("FEED_TOO_LARGE")
                    response_sha256 = hashlib.sha256(payload).hexdigest()
                    normalized = normalize_feed(payload, source, now)
                    new_meta = {
                        "source_id": source["source_id"], "retrieved_at_utc": _utc_iso(now),
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "source_sha256": hashlib.sha256(payload).hexdigest(),
                        "http_status": status_code,
                    }
                    _atomic_write(raw_path, payload)
                    _atomic_write(meta_path, json.dumps(new_meta, sort_keys=True, indent=2).encode("utf-8"))
                    normalized.update({"fetch_status": "FETCHED", "http_status": status_code, "cache_age_seconds": 0})
                    results.append(normalized)
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and raw_path.exists():
                    payload = raw_path.read_bytes()
                    normalized = normalize_feed(payload, source, now)
                    meta.update({"retrieved_at_utc": _utc_iso(now), "http_status": 304})
                    _atomic_write(meta_path, json.dumps(meta, sort_keys=True, indent=2).encode("utf-8"))
                    normalized.update({"fetch_status": "NOT_MODIFIED_CACHE_REUSED", "http_status": 304})
                    results.append(normalized)
                elif exc.code in {403, 429}:
                    results.append({"source_id": source["source_id"], "status": "UNAVAILABLE", "http_status": exc.code, "events": []})
                else:
                    results.append({"source_id": source["source_id"], "status": "UNAVAILABLE", "http_status": exc.code, "events": []})
            except TimeoutError:
                results.append({"source_id": source["source_id"], "status": "UNAVAILABLE", "error": "TIMEOUT_NO_RETRY", "events": []})
        except MacroSourceError as exc:
            results.append({
                "source_id": source["source_id"], "status": "BLOCKED",
                "http_status": response_status, "source_sha256": response_sha256,
                "error": str(exc), "events": [],
            })
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            results.append({"source_id": source["source_id"], "status": "UNAVAILABLE", "error": type(exc).__name__, "events": []})
    return {
        "status": "PARTIAL" if any(item.get("status") not in {"FETCH_OK", "CACHE_HIT"} for item in results) else "FETCH_OK",
        "requests_made": requests_made, "request_limit": request_limit,
        "origin_requests": len(origins_seen), "automatic_retry_count": 0,
        "sources": results,
    }
