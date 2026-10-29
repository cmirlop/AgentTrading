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
    no_contact_hold = _evaluate(
        ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", 1.3002, 1.3006, 1.3002, 1.3004),
    )
    assert no_contact_hold["actual_outcome"] == "UNRESOLVED", no_contact_hold
    assert no_contact_hold["correct"] is None, no_contact_hold

    same_bar_touch_and_hold = _evaluate(
        ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z", 1.3002, 1.3006, 1.2995, 1.3004),
    )
    assert same_bar_touch_and_hold["actual_outcome"] == "AMBIGUOUS", same_bar_touch_and_hold
    assert same_bar_touch_and_hold["correct"] is None, same_bar_touch_and_hold

    print("OK: HOLD sin contacto queda UNRESOLVED y contacto+confirmacion intrabar queda AMBIGUOUS")


if __name__ == "__main__":
    main()
