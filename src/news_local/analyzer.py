"""Causal filtering and deterministic, evidence-bound news summaries."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from src.news_local.models import (
    ECONOMIC_FIELDS,
    SUPPORTED_PAIRS,
    ConditionalScenario,
    ConfirmedFact,
    EconomicObservation,
    NewsAnalysisInput,
    NewsAnalysisResult,
    NewsEvent,
    SourceCoverage,
    UpcomingEvent,
)


INJECTION_PATTERN = re.compile(
    r"""
    \b(?:ignore|disregard)\s+(?:all\s+)?(?:the\s+)?(?:previous\s+)?instructions?\b
    | \bignora\s+(?:todas\s+)?(?:las\s+)?reglas\b
    | \b(?:send|place|execute|env[ií]a|manda)\s+(?:an?\s+)?(?:trade\s+)?order\b
    | \b(?:env[ií]a|manda)\s+(?:una\s+)?orden\b
    | \b(?:reveal|exfiltrate)\s+(?:the\s+)?(?:system\s+)?prompt\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

BASE_LIMITATIONS = [
    "Informational only; no trading recommendation, order, or operational signal.",
    "No live news, market data, or external economic data was consulted.",
    "Qualitative scenarios are not calibrated probabilities; magnitude and timing are unknown.",
]


def contains_prompt_injection(event: NewsEvent) -> bool:
    text = f"{event.title}\n{event.summary or ''}"
    return bool(INJECTION_PATTERN.search(text))


def _eligible_events(news_input: NewsAnalysisInput) -> tuple[list[NewsEvent], Counter[str]]:
    cutoff = news_input.analysis_as_of_utc
    eligible: list[NewsEvent] = []
    excluded: Counter[str] = Counter()
    for event in news_input.events:
        if contains_prompt_injection(event):
            excluded["PROMPT_INJECTION_CONTENT"] += 1
            continue
        if event.source_status == "UNAVAILABLE":
            excluded["SOURCE_UNAVAILABLE"] += 1
            continue
        if event.first_seen_at_utc > cutoff or event.retrieved_at_utc > cutoff:
            excluded["EVIDENCE_NOT_OBSERVED_BY_CUTOFF"] += 1
            continue
        if event.source_kind == "NEWS_RELEASE":
            if event.published_at_utc is None or event.published_at_utc > cutoff:
                excluded["EVENT_NOT_PUBLISHED_BY_CUTOFF"] += 1
                continue
        elif event.scheduled_at_utc is None:
            excluded["SCHEDULE_TIME_MISSING"] += 1
            continue
        elif event.scheduled_at_utc <= cutoff:
            excluded["SCHEDULE_TIME_PASSED_WITHOUT_RELEASE"] += 1
            continue
        eligible.append(event)
    return eligible, excluded


def _root_id(event: NewsEvent, by_id: dict[str, NewsEvent]) -> str:
    current = event
    while current.duplicate_of_event_id or current.revision_of_event_id:
        parent = current.duplicate_of_event_id or current.revision_of_event_id
        if parent not in by_id:
            break
        current = by_id[parent]
    return current.event_id


def _revision_depth(event: NewsEvent, by_id: dict[str, NewsEvent]) -> int:
    depth = 0
    current = event
    while current.revision_of_event_id:
        if current.revision_of_event_id not in by_id:
            break
        depth += 1
        current = by_id[current.revision_of_event_id]
    return depth


def _group_events(events: list[NewsEvent]) -> list[list[NewsEvent]]:
    by_id = {event.event_id: event for event in events}
    groups: dict[str, list[NewsEvent]] = {}
    for event in events:
        groups.setdefault(_root_id(event, by_id), []).append(event)
    return [groups[key] for key in sorted(groups)]


def _chosen_event(group: list[NewsEvent]) -> NewsEvent:
    by_id = {event.event_id: event for event in group}
    return max(
        group,
        key=lambda event: (
            _revision_depth(event, by_id),
            event.source_status != "DUPLICATE",
            event.retrieved_at_utc,
            event.event_id,
        ),
    )


def _fact_for_group(group: list[NewsEvent]) -> ConfirmedFact:
    chosen = _chosen_event(group)
    observations = [
        EconomicObservation(
            event_id=chosen.event_id,
            field=field,
            value=getattr(chosen, field),
            units=chosen.units,
        )
        for field in ("actual", "forecast", "previous")
        if field in ECONOMIC_FIELDS and getattr(chosen, field) is not None
    ]
    return ConfirmedFact(
        description="A published event and its typed economic fields were observed by the analysis cut-off.",
        event_ids=sorted(event.event_id for event in group),
        evidence_ids=sorted(event.evidence_id for event in group),
        observations=observations,
        independent_confirmation_count=1,
    )


def _scenario_for_currency(
    symbol: str,
    currency: str,
    group: list[NewsEvent],
    chosen: NewsEvent,
) -> ConditionalScenario:
    base, quote = SUPPORTED_PAIRS[symbol]
    other = quote if currency == base else base
    if chosen.source_kind == "SCHEDULED_EVENT":
        condition = (
            f"If the future release, once available, shifts relative expectations "
            f"in favor of {currency} over {other}."
        )
    else:
        condition = (
            f"If the reported information shifts relative expectations "
            f"in favor of {currency} over {other}."
        )
    if currency == base:
        effect = (
            f"That relative shift could support {symbol}; the effect could reverse "
            f"if expectations favor {other}."
        )
    else:
        effect = (
            f"That relative shift could weigh on {symbol}; the effect could reverse "
            f"if expectations favor {other}."
        )
    return ConditionalScenario(
        currency=currency,
        pair=symbol,
        condition=condition,
        possible_effect=effect,
        uncertainty_range="QUALITATIVE_WIDE_UNCALIBRATED",
        event_ids=sorted(event.event_id for event in group),
        evidence_ids=sorted(event.evidence_id for event in group),
    )


def build_deterministic_result(
    news_input: NewsAnalysisInput,
    *,
    mode: str = "DETERMINISTIC",
    provider: str = "deterministic",
    model: str = "none",
    status_override: str | None = None,
    error_code: str | None = None,
    request_count: int = 0,
    duration_ms: int | None = None,
    generated_at_utc: datetime | None = None,
) -> NewsAnalysisResult:
    eligible, excluded = _eligible_events(news_input)
    groups = _group_events(eligible)
    facts: list[ConfirmedFact] = []
    upcoming: list[UpcomingEvent] = []
    scenarios: list[ConditionalScenario] = []
    event_ids_used: set[str] = set()
    evidence_ids_used: set[str] = set()

    for group in groups:
        chosen = _chosen_event(group)
        group_event_ids = {event.event_id for event in group}
        group_evidence_ids = {event.evidence_id for event in group}
        event_ids_used.update(group_event_ids)
        evidence_ids_used.update(group_evidence_ids)
        if chosen.source_kind == "SCHEDULED_EVENT":
            if chosen.scheduled_at_utc and chosen.scheduled_at_utc > news_input.analysis_as_of_utc:
                upcoming.append(
                    UpcomingEvent(
                        event_id=chosen.event_id,
                        title=chosen.title,
                        scheduled_at_utc=chosen.scheduled_at_utc,
                        evidence_id=chosen.evidence_id,
                    )
                )
        else:
            facts.append(_fact_for_group(group))

        group_currencies = sorted({currency for event in group for currency in event.currencies})
        base, quote = SUPPORTED_PAIRS[news_input.symbol]
        for currency in group_currencies:
            if currency in {base, quote}:
                scenarios.append(_scenario_for_currency(news_input.symbol, currency, group, chosen))

    notes = [f"{reason}: {count} event(s) excluded." for reason, count in sorted(excluded.items())]
    if len(eligible) == 0:
        default_status = "PENDING"
    elif excluded.get("PROMPT_INJECTION_CONTENT"):
        default_status = "FALLBACK"
    else:
        default_status = "PASS"
    status = status_override or default_status
    limitations = list(BASE_LIMITATIONS)
    if news_input.fixture_label:
        limitations.insert(0, news_input.fixture_label)
    result = NewsAnalysisResult(
        status=status,
        mode=mode,
        provider=provider,
        model=model,
        analysis_as_of_utc=news_input.analysis_as_of_utc,
        symbol=news_input.symbol,
        event_ids_used=sorted(event_ids_used),
        evidence_ids_used=sorted(evidence_ids_used),
        confirmed_facts=facts,
        upcoming_events=upcoming,
        conditional_scenarios=scenarios,
        uncertainties=[
            "No empirical price response or calibrated uncertainty range is available."
        ],
        source_coverage=SourceCoverage(
            eligible_event_count=len(eligible),
            excluded_event_count=len(news_input.events) - len(eligible),
            independent_event_groups=len(groups),
            notes=notes,
        ),
        limitations=limitations,
        generated_at_utc=generated_at_utc or news_input.analysis_as_of_utc,
        execution={
            "request_count": request_count,
            "duration_ms": duration_ms,
            "json_valid": True,
            "error_code": error_code,
        },
        fixture_label=news_input.fixture_label,
    )
    validate_result_references(news_input, result)
    return result


def validate_result_references(
    news_input: NewsAnalysisInput,
    result: NewsAnalysisResult,
) -> None:
    """Re-check every structured citation and numeric observation outside an LLM."""
    eligible, _ = _eligible_events(news_input)
    eligible_by_id = {event.event_id: event for event in eligible}
    eligible_event_evidence = {
        event.event_id: event.evidence_id for event in eligible
    }
    for event_id in result.event_ids_used:
        if event_id not in eligible_by_id:
            raise ValueError("INVALID_EVENT_REFERENCE")
    eligible_evidence = set(eligible_event_evidence.values())
    if any(evidence_id not in eligible_evidence for evidence_id in result.evidence_ids_used):
        raise ValueError("INVALID_EVIDENCE_REFERENCE")
    if len(result.event_ids_used) != len(set(result.event_ids_used)):
        raise ValueError("DUPLICATE_EVENT_REFERENCE")
    expected_global_evidence = {
        eligible_event_evidence[event_id] for event_id in result.event_ids_used
    }
    if set(result.evidence_ids_used) != expected_global_evidence:
        raise ValueError("EVENT_EVIDENCE_REFERENCE_MISMATCH")

    for fact in result.confirmed_facts:
        if not fact.event_ids or not set(fact.event_ids) <= set(eligible_by_id):
            raise ValueError("INVALID_FACT_EVENT_REFERENCE")
        if not fact.evidence_ids or not set(fact.evidence_ids) <= eligible_evidence:
            raise ValueError("INVALID_FACT_EVIDENCE_REFERENCE")
        fact_evidence = {eligible_event_evidence[event_id] for event_id in fact.event_ids}
        if not set(fact.evidence_ids) <= fact_evidence:
            raise ValueError("INVALID_FACT_EVIDENCE_REFERENCE")
        for observation in fact.observations:
            event = eligible_by_id.get(observation.event_id)
            if (
                event is None
                or observation.event_id not in fact.event_ids
                or getattr(event, observation.field) != observation.value
            ):
                raise ValueError("INVALID_NUMERIC_OBSERVATION")

    for upcoming_event in result.upcoming_events:
        event = eligible_by_id.get(upcoming_event.event_id)
        if (
            event is None
            or event.source_kind != "SCHEDULED_EVENT"
            or event.title != upcoming_event.title
            or event.scheduled_at_utc != upcoming_event.scheduled_at_utc
            or event.evidence_id != upcoming_event.evidence_id
            or upcoming_event.scheduled_at_utc <= news_input.analysis_as_of_utc
        ):
            raise ValueError("INVALID_UPCOMING_EVENT")

    for scenario in result.conditional_scenarios:
        if scenario.pair != news_input.symbol:
            raise ValueError("INVALID_SCENARIO_PAIR")
        if scenario.currency not in SUPPORTED_PAIRS[scenario.pair]:
            raise ValueError("INVALID_SCENARIO_CURRENCY")
        if not scenario.event_ids or not set(scenario.event_ids) <= set(eligible_by_id):
            raise ValueError("INVALID_SCENARIO_EVENT_REFERENCE")
        cited_evidence = {
            eligible_by_id[event_id].evidence_id for event_id in scenario.event_ids
        }
        cited_currencies = {
            currency
            for event_id in scenario.event_ids
            for currency in eligible_by_id[event_id].currencies
        }
        if scenario.currency not in cited_currencies:
            raise ValueError("INVALID_SCENARIO_CURRENCY")
        if not scenario.evidence_ids or not set(scenario.evidence_ids) <= cited_evidence:
            raise ValueError("INVALID_SCENARIO_EVIDENCE_REFERENCE")
