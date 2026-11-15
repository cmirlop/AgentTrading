"""Free official macro source adapters; research-only and offline by default."""

from .core import (
    MAX_FEED_BYTES,
    MAX_LIVE_REQUESTS,
    MacroSourceError,
    event_is_available_as_of,
    fetch_live_sources,
    normalize_feed,
    safe_calendar_preview,
)

__all__ = [
    "MAX_FEED_BYTES",
    "MAX_LIVE_REQUESTS",
    "MacroSourceError",
    "event_is_available_as_of",
    "fetch_live_sources",
    "normalize_feed",
    "safe_calendar_preview",
]
