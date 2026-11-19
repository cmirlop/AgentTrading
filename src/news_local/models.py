"""Versioned, provider-independent contracts for local news analysis."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


INPUT_SCHEMA_VERSION = "1.0"
RESULT_SCHEMA_VERSION = "2.0"
SCHEMA_VERSION = INPUT_SCHEMA_VERSION
IMPACT_ORIGIN = "DIRECT_EVENT+DERIVED_RELATIVE_PAIR_EFFECT"
SUPPORTED_PAIRS = {
    "EURUSD": ("EUR", "USD"),
    "USDCAD": ("USD", "CAD"),
    "AUDUSD": ("AUD", "USD"),
}
SUPPORTED_CURRENCIES = {"EUR", "USD", "CAD", "AUD"}
ECONOMIC_FIELDS = {"actual", "forecast", "previous"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp_must_include_timezone")
    return value.astimezone(timezone.utc)


class SourceRevision(StrictModel):
    revision: int | None = Field(default=None, ge=0)
    published_at_utc: datetime | None = None
    updated_at_utc: datetime | None = None
    observed_at_utc: datetime | None = None
    source_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("published_at_utc", "updated_at_utc", "observed_at_utc")
    @classmethod
    def revision_times_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("source_sha256")
    @classmethod
    def revision_hash_is_sha256(cls, value: str | None) -> str | None:
        if value is not None and any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("revision_hash_invalid")
        return value


class NewsEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=120)
    source_event_id: str | None = Field(default=None, max_length=240)
    source_id: str = Field(min_length=1, max_length=120)
    evidence_id: str = Field(min_length=1, max_length=120)
    source_kind: Literal["SCHEDULED_EVENT", "NEWS_RELEASE"]
    title: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=1200)
    currencies: list[str] = Field(min_length=1, max_length=4)
    source_url: str | None = Field(default=None, max_length=500)
    scheduled_at_utc: datetime | None = None
    published_at_utc: datetime | None = None
    first_seen_at_utc: datetime
    retrieved_at_utc: datetime
    source_status: Literal["AVAILABLE", "UNAVAILABLE", "REVISED", "DUPLICATE"]
    origin_kind: str = Field(min_length=1, max_length=40)
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    units: str | None = Field(default=None, max_length=40)
    duplicate_of_event_id: str | None = Field(default=None, max_length=120)
    revision_of_event_id: str | None = Field(default=None, max_length=120)
    source_revision_count: int | None = Field(default=None, ge=1)
    source_revision_history: list[SourceRevision] = Field(default_factory=list, max_length=100)

    @field_validator(
        "scheduled_at_utc",
        "published_at_utc",
        "first_seen_at_utc",
        "retrieved_at_utc",
    )
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("currencies")
    @classmethod
    def currencies_are_supported(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(item.strip().upper() for item in value))
        if not normalized or any(item not in SUPPORTED_CURRENCIES for item in normalized):
            raise ValueError("unsupported_currency")
        return normalized

    @field_validator("actual", "forecast", "previous")
    @classmethod
    def economic_values_are_finite(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("economic_value_must_be_finite")
        return value

    @field_validator("source_url")
    @classmethod
    def source_url_is_non_executable_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(char) < 32 for char in value):
            raise ValueError("source_url_invalid")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            raise ValueError("source_url_invalid") from None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("source_url_invalid")
        return value

    @model_validator(mode="after")
    def source_kind_has_required_time_and_fields(self) -> "NewsEvent":
        if self.retrieved_at_utc < self.first_seen_at_utc:
            raise ValueError("retrieved_before_first_seen")
        if self.source_kind == "SCHEDULED_EVENT":
            if self.scheduled_at_utc is None:
                raise ValueError("scheduled_event_requires_scheduled_at")
            if self.actual is not None:
                raise ValueError("scheduled_event_cannot_claim_actual")
        if self.source_kind == "NEWS_RELEASE" and self.published_at_utc is None:
            raise ValueError("news_release_requires_published_at")
        if self.source_kind == "NEWS_RELEASE" and self.published_at_utc is not None:
            if (
                self.first_seen_at_utc < self.published_at_utc
                or self.retrieved_at_utc < self.published_at_utc
            ):
                raise ValueError("news_observed_before_publication")
        if self.duplicate_of_event_id and self.revision_of_event_id:
            raise ValueError("event_cannot_be_duplicate_and_revision")
        if (
            self.source_revision_count is not None
            and self.source_revision_count != len(self.source_revision_history)
        ):
            raise ValueError("source_revision_count_mismatch")
        return self


class NewsAnalysisInput(StrictModel):
    schema_version: Literal["1.0"] = INPUT_SCHEMA_VERSION
    analysis_as_of_utc: datetime
    symbol: Literal["EURUSD", "USDCAD", "AUDUSD"]
    events: list[NewsEvent] = Field(default_factory=list, max_length=100)
    fixture_label: str | None = Field(default=None, max_length=80)

    @field_validator("analysis_as_of_utc")
    @classmethod
    def cutoff_is_aware_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def event_ids_are_unique_and_links_exist(self) -> "NewsAnalysisInput":
        by_id = {event.event_id: event for event in self.events}
        if len(by_id) != len(self.events):
            raise ValueError("duplicate_event_id")
        for event in self.events:
            parent = event.duplicate_of_event_id or event.revision_of_event_id
            if parent is not None and (parent not in by_id or parent == event.event_id):
                raise ValueError("event_link_invalid")
        for event in self.events:
            seen: set[str] = set()
            current = event
            while current.duplicate_of_event_id or current.revision_of_event_id:
                parent = current.duplicate_of_event_id or current.revision_of_event_id
                if parent in seen:
                    raise ValueError("event_link_cycle")
                seen.add(parent)
                current = by_id[parent]
        return self


class EconomicObservation(StrictModel):
    event_id: str
    field: Literal["actual", "forecast", "previous"]
    value: float
    units: str | None = None

    @field_validator("value")
    @classmethod
    def observation_is_finite(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("economic_value_must_be_finite")
        return value


class ConfirmedFact(StrictModel):
    description: str = Field(min_length=1, max_length=240)
    event_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    observations: list[EconomicObservation] = Field(default_factory=list)
    independent_confirmation_count: Literal[1] = 1


class UpcomingEvent(StrictModel):
    event_id: str
    title: str = Field(min_length=1, max_length=500)
    scheduled_at_utc: datetime
    evidence_id: str

    @field_validator("scheduled_at_utc")
    @classmethod
    def upcoming_time_is_aware_utc(cls, value: datetime) -> datetime:
        return _utc(value)


class ConditionalScenario(StrictModel):
    scenario_key: str = Field(min_length=8, max_length=80)
    driver_currency: Literal["EUR", "USD", "CAD", "AUD"]
    pair: Literal["EURUSD", "USDCAD", "AUDUSD"]
    base_currency: Literal["EUR", "USD", "AUD"]
    quote_currency: Literal["USD", "CAD"]
    counterpart_currency: Literal["EUR", "USD", "CAD", "AUD"]
    driver_direction_condition: Literal["STRENGTHENS", "WEAKENS"]
    pair_direction: Literal["UP", "DOWN"]
    impact_origin: Literal["DIRECT_EVENT+DERIVED_RELATIVE_PAIR_EFFECT"] = IMPACT_ORIGIN
    condition: str = Field(min_length=8, max_length=320)
    possible_effect: str = Field(min_length=8, max_length=320)
    uncertainty_range: Literal["QUALITATIVE_WIDE_UNCALIBRATED"] = "QUALITATIVE_WIDE_UNCALIBRATED"
    event_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class DriverAssessment(StrictModel):
    """An explicitly uncalibrated driver hypothesis; pair effects are derived in Python."""

    driver_currency: Literal["EUR", "USD", "CAD", "AUD"]
    direction_hypothesis: Literal["STRENGTHENS", "WEAKENS", "UNKNOWN"]
    event_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SourceCoverage(StrictModel):
    eligible_event_count: int = Field(ge=0)
    excluded_event_count: int = Field(ge=0)
    independent_event_groups: int = Field(ge=0)
    notes: list[str] = Field(default_factory=list)


class ExecutionMetadata(StrictModel):
    request_count: int = Field(ge=0, le=1)
    duration_ms: int | None = Field(default=None, ge=0)
    json_valid: bool = True
    error_code: str | None = Field(default=None, max_length=80)


class NewsAnalysisResult(StrictModel):
    schema_version: Literal["2.0"] = RESULT_SCHEMA_VERSION
    status: Literal["PASS", "FALLBACK", "BLOCKED", "PENDING"]
    mode: Literal["MOCK", "LOCAL_OLLAMA", "DETERMINISTIC"]
    provider: Literal["mock", "ollama", "deterministic"]
    model: str
    analysis_as_of_utc: datetime
    symbol: Literal["EURUSD", "USDCAD", "AUDUSD"]
    event_ids_used: list[str] = Field(default_factory=list)
    evidence_ids_used: list[str] = Field(default_factory=list)
    confirmed_facts: list[ConfirmedFact] = Field(default_factory=list)
    upcoming_events: list[UpcomingEvent] = Field(default_factory=list)
    driver_assessments: list[DriverAssessment] = Field(default_factory=list)
    conditional_scenarios: list[ConditionalScenario] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    source_coverage: SourceCoverage
    limitations: list[str] = Field(default_factory=list)
    generated_at_utc: datetime
    execution: ExecutionMetadata
    fixture_label: str | None = Field(default=None, max_length=80)

    @field_validator("analysis_as_of_utc", "generated_at_utc")
    @classmethod
    def result_times_are_aware_utc(cls, value: datetime) -> datetime:
        return _utc(value)
