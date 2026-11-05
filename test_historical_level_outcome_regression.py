"""Regresiones para la clasificación causal HOLD/BREAK con velas OHLC."""

import pandas as pd

from src.historical_level_outcome import evaluate_level_thesis


ANALYSIS_TIME = "2026-01-01T00:00:00+00:00"


def _history(*rows):
    frame = pd.DataFrame(
        rows,
        columns=["time", "bar_close", "open", "high", "low", "close"],
    )
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame["bar_close"] = pd.to_datetime(frame["bar_close"], utc=True)
    return {"M5": frame}


def _levels():
    return {
        "analysis_time": ANALYSIS_TIME,
        "reference_atr_m15": 0.001,
        "nearest_support": {"low": 1.2990, "high": 1.3000},
        "nearest_resistance": None,
    }


def _evaluate(*rows):
    return evaluate_level_thesis(
        thesis={"thesis": "SUPPORT_HOLD"},
        levels=_levels(),
        prepared_history=_history(*rows),
        horizon_minutes=180,
    )


def main():
    break_event_time = _evaluate(
        (
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:05:00Z",
            1.3002,
            1.3004,
            1.2988,
            1.2988,
        ),
        (
            "2026-01-01T00:05:00Z",
            "2026-01-01T00:10:00Z",
            1.2988,
            1.2990,
            1.2980,
            1.2985,
        ),
    )
    assert break_event_time["actual_outcome"] == "BREAK", break_event_time
    assert break_event_time["event_time"] == "2026-01-01T00:10:00+00:00", break_event_time
    assert break_event_time["event_bar"]["bar_open_time"] == "2026-01-01T00:05:00+00:00", break_event_time
    assert break_event_time["event_bar"]["bar_close_time"] == break_event_time["event_time"], break_event_time

    recent_touch_time = _evaluate(
        ("2025-12-31T23:55:00Z", "2026-01-01T00:00:00Z", 1.2995, 1.2998, 1.2989, 1.2992),
        ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", 1.3002, 1.3004, 1.2988, 1.2988),
        ("2026-01-01T00:05:00Z", "2026-01-01T00:10:00Z", 1.2988, 1.2990, 1.2980, 1.2985),
    )
    assert recent_touch_time["touch_time"] == "2026-01-01T00:00:00+00:00", recent_touch_time

    no_contact_hold = _evaluate(
        ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", 1.3002, 1.3006, 1.3002, 1.3004),
    )
    assert no_contact_hold["actual_outcome"] == "UNRESOLVED", no_contact_hold
    assert no_contact_hold["correct"] is None, no_contact_hold

    touch_then_confirmed_hold = _evaluate(
        ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", 1.3002, 1.3006, 1.2995, 1.3004),
        ("2026-01-01T00:05:00Z", "2026-01-01T00:10:00Z", 1.3004, 1.3006, 1.3003, 1.3005),
    )
    assert touch_then_confirmed_hold["actual_outcome"] == "HOLD", touch_then_confirmed_hold
    assert touch_then_confirmed_hold["event_time"] == "2026-01-01T00:10:00+00:00", touch_then_confirmed_hold

    print("OK: BREAK/HOLD requieren dos cierres M5; las mechas solas no cambian el resultado")


if __name__ == "__main__":
    main()
