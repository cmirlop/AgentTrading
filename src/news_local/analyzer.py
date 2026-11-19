"""Causal filtering and deterministic, evidence-bound news summaries."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta

from src.news_local.models import (
    ECONOMIC_FIELDS,
    IMPACT_ORIGIN,
    SUPPORTED_PAIRS,
    ConditionalScenario,
    ConfirmedFact,
    DriverAssessment,
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
    "Analysis is limited to supplied records; the summarizer does not fetch live feeds or market data, and missing sources do not prove that no events occurred.",
    "Qualitative scenarios are not calibrated probabilities; magnitude and timing are unknown.",
]
MAX_EVENT_AGE = timedelta(days=7)


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
        if any(revision.observed_at_utc is None for revision in event.source_revision_history):
            excluded["SOURCE_REVISION_TIME_UNKNOWN"] += 1
            continue
        future_revision = next(
            (
                reason
                for revision in event.source_revision_history
                for value, reason in (
                    (revision.published_at_utc, "EVENT_NOT_PUBLISHED_BY_CUTOFF"),
                    (revision.updated_at_utc, "REVISION_AFTER_CUTOFF"),
                    (revision.observed_at_utc, "EVIDENCE_NOT_OBSERVED_BY_CUTOFF"),
                )
                if value is not None and value > cutoff
            ),
            None,
        )
        if future_revision:
            excluded[future_revision] += 1
            continue
        if event.source_kind == "NEWS_RELEASE":
            if event.published_at_utc is None or event.published_at_utc > cutoff:
                excluded["EVENT_NOT_PUBLISHED_BY_CUTOFF"] += 1
                continue
            if cutoff - event.published_at_utc > MAX_EVENT_AGE:
                excluded["SOURCE_STALE_AT_CUTOFF"] += 1
                continue
        elif event.scheduled_at_utc is None:
            excluded["SCHEDULE_TIME_MISSING"] += 1
            continue
        elif event.scheduled_at_utc <= cutoff:
            excluded["SCHEDULE_TIME_PASSED_WITHOUT_RELEASE"] += 1
            continue
        elif cutoff - event.retrieved_at_utc > MAX_EVENT_AGE:
            excluded["SOURCE_STALE_AT_CUTOFF"] += 1
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


def _group_events(
    events: list[NewsEvent], *, lineage_events: list[NewsEvent] | None = None
) -> list[list[NewsEvent]]:
    # Keep known but cutoff-ineligible parents in the link map so a duplicate whose
    # canonical record arrived late cannot become an independent confirmation.
    by_id = {event.event_id: event for event in (lineage_events or events)}
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
    description = (
        "A published event and its typed economic observations were observed by the analysis cut-off."
        if observations
        else "A published source event was observed by the analysis cut-off; no numeric economic result was supplied."
    )
    return ConfirmedFact(
        description=description,
        event_ids=sorted(event.event_id for event in group),
        evidence_ids=sorted(event.evidence_id for event in group),
        observations=observations,
        independent_confirmation_count=1,
    )


def _scenario_for_driver(
    symbol: str,
    driver_currency: str,
    direction: str,
    supporting_events: list[NewsEvent],
) -> ConditionalScenario:
    base, quote = SUPPORTED_PAIRS[symbol]
    counterpart = quote if driver_currency == base else base
    strengthens = direction == "STRENGTHENS"
    if strengthens:
        condition = f"If {driver_currency} relatively strengthens against {counterpart}, all else equal."
    else:
        condition = f"If {driver_currency} relatively weakens against {counterpart}, all else equal."
    pair_rises = (driver_currency == base) == strengthens
    pair_direction = "UP" if pair_rises else "DOWN"
    movement = "rise" if pair_rises else "fall"
    effect = (
        f"{symbol} could {movement} under this conditional relative scenario; other information "
        "or repricing may alter or reverse the reaction."
    )
    return ConditionalScenario(
        scenario_key=f"{symbol}:{driver_currency}:{direction}",
        driver_currency=driver_currency,
        pair=symbol,
        base_currency=base,
        quote_currency=quote,
        counterpart_currency=counterpart,
        driver_direction_condition=direction,
        pair_direction=pair_direction,
        impact_origin=IMPACT_ORIGIN,
        condition=condition,
        possible_effect=effect,
        uncertainty_range="QUALITATIVE_WIDE_UNCALIBRATED",
        event_ids=sorted(event.event_id for event in supporting_events),
        evidence_ids=sorted({event.evidence_id for event in supporting_events}),
    )


def _supporting_events(
    events: list[NewsEvent], driver_currency: str
) -> list[NewsEvent]:
    return sorted(
        (event for event in events if driver_currency in event.currencies),
        key=lambda event: event.event_id,
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
    driver_hypotheses: dict[str, str] | None = None,
) -> NewsAnalysisResult:
    eligible, excluded = _eligible_events(news_input)
    groups = _group_events(eligible, lineage_events=news_input.events)
    facts: list[ConfirmedFact] = []
    upcoming: list[UpcomingEvent] = []
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

    base, quote = SUPPORTED_PAIRS[news_input.symbol]
    hypotheses = driver_hypotheses or {}
    driver_assessments: list[DriverAssessment] = []
    scenarios: list[ConditionalScenario] = []
    for driver in (base, quote):
        supporting = _supporting_events(eligible, driver)
        if not supporting:
            continue
        driver_assessments.append(
            DriverAssessment(
                driver_currency=driver,
                direction_hypothesis=hypotheses.get(driver, "UNKNOWN"),
                event_ids=[event.event_id for event in supporting],
                evidence_ids=sorted({event.evidence_id for event in supporting}),
            )
        )
        # Always retain both counterfactual branches. A model direction is stored
        # separately as an uncalibrated hypothesis; it never erases the alternative.
        for direction in ("STRENGTHENS", "WEAKENS"):
            scenarios.append(
                _scenario_for_driver(news_input.symbol, driver, direction, supporting)
            )

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
        driver_assessments=driver_assessments,
        conditional_scenarios=scenarios,
        uncertainties=[
            "Driver direction assessments are uncalibrated hypotheses, not observed facts.",
            "Relative pair scenarios assume all else equal; reactions can differ or reverse.",
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
    """Re-check event provenance and every derived pair mapping outside an LLM."""
    if result.symbol != news_input.symbol or result.analysis_as_of_utc != news_input.analysis_as_of_utc:
        raise ValueError("ANALYSIS_CONTEXT_MISMATCH")
    eligible, excluded = _eligible_events(news_input)
    eligible_by_id = {event.event_id: event for event in eligible}
    eligible_event_evidence = {event.event_id: event.evidence_id for event in eligible}
    expected_event_ids = set(eligible_by_id)
    expected_evidence = set(eligible_event_evidence.values())
    if set(result.event_ids_used) != expected_event_ids:
        raise ValueError("EVENT_EVIDENCE_REFERENCE_MISMATCH")
    if set(result.evidence_ids_used) != expected_evidence:
        raise ValueError("EVENT_EVIDENCE_REFERENCE_MISMATCH")
    if len(result.event_ids_used) != len(set(result.event_ids_used)):
        raise ValueError("DUPLICATE_EVENT_REFERENCE")

    lineage_groups = _group_events(eligible, lineage_events=news_input.events)
    if (
        result.source_coverage.eligible_event_count != len(eligible)
        or result.source_coverage.excluded_event_count != len(news_input.events) - len(eligible)
        or result.source_coverage.independent_event_groups != len(lineage_groups)
    ):
        raise ValueError("SOURCE_COVERAGE_MISMATCH")
    expected_notes = [
        f"{reason}: {count} event(s) excluded." for reason, count in sorted(excluded.items())
    ]
    if result.source_coverage.notes != expected_notes:
        raise ValueError("SOURCE_COVERAGE_MISMATCH")

    # Facts and scheduled events are deterministic summaries of the eligible
    # source groups. Validate the complete objects, not only their references:
    # otherwise a caller could attach invented prose or relabel a scheduled
    # release as an already published fact while retaining valid IDs.
    expected_facts: list[ConfirmedFact] = []
    expected_upcoming: list[UpcomingEvent] = []
    for group in lineage_groups:
        chosen = _chosen_event(group)
        if chosen.source_kind == "SCHEDULED_EVENT":
            if chosen.scheduled_at_utc and chosen.scheduled_at_utc > news_input.analysis_as_of_utc:
                expected_upcoming.append(
                    UpcomingEvent(
                        event_id=chosen.event_id,
                        title=chosen.title,
                        scheduled_at_utc=chosen.scheduled_at_utc,
                        evidence_id=chosen.evidence_id,
                    )
                )
        else:
            expected_facts.append(_fact_for_group(group))
    for fact in result.confirmed_facts:
        if not fact.event_ids or not set(fact.event_ids) <= expected_event_ids:
            raise ValueError("INVALID_FACT_EVENT_REFERENCE")
        fact_evidence = {eligible_event_evidence[event_id] for event_id in fact.event_ids}
        if set(fact.evidence_ids) != fact_evidence:
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

    if result.confirmed_facts != expected_facts:
        raise ValueError("INVALID_CONFIRMED_FACT")
    if result.upcoming_events != expected_upcoming:
        raise ValueError("INVALID_UPCOMING_EVENT")

    base, quote = SUPPORTED_PAIRS[news_input.symbol]
    expected_drivers = [
        driver for driver in (base, quote) if _supporting_events(eligible, driver)
    ]
    actual_drivers = [item.driver_currency for item in result.driver_assessments]
    if actual_drivers != expected_drivers or len(actual_drivers) != len(set(actual_drivers)):
        raise ValueError("INVALID_DRIVER_EVIDENCE")
    expected_scenarios: list[ConditionalScenario] = []
    for assessment in result.driver_assessments:
        supporting = _supporting_events(eligible, assessment.driver_currency)
        expected_ids = [event.event_id for event in supporting]
        expected_driver_evidence = sorted({event.evidence_id for event in supporting})
        if (
            assessment.event_ids != expected_ids
            or assessment.evidence_ids != expected_driver_evidence
        ):
            raise ValueError("INVALID_DRIVER_EVIDENCE")
        for direction in ("STRENGTHENS", "WEAKENS"):
            expected_scenarios.append(
                _scenario_for_driver(
                    news_input.symbol, assessment.driver_currency, direction, supporting
                )
            )
    if result.conditional_scenarios != expected_scenarios:
        raise ValueError("INVALID_RELATIVE_MAPPING")
