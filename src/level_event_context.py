"""Contexto técnico de las rupturas históricas de una zona de precio.

Paso 3.3, diagnóstico sin Ollama ni órdenes. Solo enriquece episodios BREAK
ya confirmados por `analyze_zone_events` con dos instantáneas de indicadores:
primer CONTACTO y cierre de CONFIRMACIÓN de la ruptura. Las franjas de precio históricas siguen
siendo retrospectivas y no prueban que el nivel fuese conocido entonces.

Uso futuro::
    events = analyze_zone_events(zone, prepared_history['M15'], analysis_time)
    enriched = add_break_contexts(events, prepared_history)

`prepared_history` proviene de `prepare_symbol_history`, que calcula
indicadores causales sobre series ordenadas cronológicamente.
"""

from __future__ import annotations

from copy import deepcopy
import math

import pandas as pd

from src.market_snapshot import _ema_structure, _rsi_zone, _volume_zone


TIMEFRAMES = ("M15", "H1", "H4", "D1")
_REQUIRED = {"bar_close", "close", "ema20", "ema50", "ema200", "rsi14", "atr14", "return_5"}


def _utc(value) -> pd.Timestamp:
    value = pd.Timestamp(value)
    if pd.isna(value) or value.tzinfo is None:
        raise ValueError("INVALID_OR_TIMEZONELESS_DATETIME")
    return value.tz_convert("UTC")


def _number(value, digits: int = 6):
    if value is None or pd.isna(value):
        return None
    result = float(value)
    return round(result, digits) if math.isfinite(result) else None


def _prepared_view(data: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    missing = _REQUIRED.difference(data.columns)
    if missing:
        raise ValueError(f"MISSING_MARKET_COLUMNS: {sorted(missing)}")
    cols = sorted(_REQUIRED | ({"volume_ratio"} & set(data.columns)))
    view = data[cols].copy()
    view["bar_close"] = pd.to_datetime(view["bar_close"], utc=True)
    return (
        view.loc[view["bar_close"] <= cutoff]
        .sort_values("bar_close")
        .drop_duplicates("bar_close", keep="last")
        .reset_index(drop=True)
    )


def _snapshot(view: pd.DataFrame, at: pd.Timestamp) -> dict:
    position = int(view["bar_close"].searchsorted(at, side="right") - 1)
    if position < 0:
        return {"bar_close": None, "available": False}

    row = view.iloc[position]
    ema20 = _number(row["ema20"], 8)
    ema50 = _number(row["ema50"], 8)
    ema200 = _number(row["ema200"], 8)
    atr = _number(row["atr14"], 8)
    return5 = _number(row["return_5"], 8)

    ema_structure = _ema_structure(ema20, ema50, ema200)

    if return5 is None:
        momentum = "UNKNOWN"
    elif return5 > 0:
        momentum = "POSITIVE"
    elif return5 < 0:
        momentum = "NEGATIVE"
    else:
        momentum = "FLAT"

    # Baseline SOLO con ATRs ya observados, no calculados con velas futuras.
    atr_series = pd.to_numeric(
        view.iloc[max(0, position - 19): position + 1]["atr14"],
        errors="coerce",
    ).dropna()
    atr_relative = None
    if atr is not None and atr > 0 and len(atr_series) >= 10:
        baseline = float(atr_series.mean())
        if baseline > 0 and math.isfinite(baseline):
            atr_relative = round(atr / baseline, 3)

    result = {
        "bar_close": _utc(row["bar_close"]).isoformat(),
        "available": True,
        "ema_structure": ema_structure,
        "rsi14": _number(row["rsi14"], 2),
        "rsi_zone": _rsi_zone(_number(row["rsi14"], 4)),
        "return5_direction": momentum,
        "return5_pct": _number(
            None if return5 is None else return5 * 100, 4
        ),
        "atr14": atr,
        "atr_vs_recent_mean": atr_relative,
    }
    if "volume_ratio" in view.columns:
        volume_ratio = _number(row["volume_ratio"], 3)
        result["tick_volume_vs_sma20"] = volume_ratio
        result["volume_zone"] = _volume_zone(volume_ratio)
    return result


def add_break_contexts(report: dict, prepared_history: dict) -> dict:
    """Añade contexto de BREAK al primer contacto y a su confirmación.

    No altera el reporte original ni rellena eventos sin ruptura confirmada.
    En cada instante/TF toma la última vela CERRADA, nunca la abierta.
    El contexto del primer contacto es observable después del cierre M15
    de la vela de contacto; no es una señal predictiva antes de ese cierre.
    """
    cutoff = _utc(report["analysis_time"])
    views = {
        tf: _prepared_view(prepared_history[tf], cutoff)
        for tf in TIMEFRAMES
    }
    enriched = deepcopy(report)
    for event in enriched["events"]:
        if event["outcome"] != "BREAK":
            continue
        when_raw = event.get("resolved_at")
        if not when_raw:
            raise ValueError("BREAK_WITHOUT_RESOLUTION_TIMESTAMP")
        when = _utc(when_raw)
        contact_at = _utc(event["contact_at"])
        if when > cutoff or when < contact_at:
            raise ValueError("INVALID_BREAK_RESOLUTION_TIME")
        event["context_at_first_contact"] = {
            "observed_at": contact_at.isoformat(),
            "timeframes": {
                tf: _snapshot(views[tf], contact_at) for tf in TIMEFRAMES
            },
            "interpretation": "CONTEXT_OBSERVABLE_AFTER_CONTACT_BAR_CLOSED",
        }
        event["context_at_break_confirmation"] = {
            "confirmed_at": when.isoformat(),
            "timeframes": {tf: _snapshot(views[tf], when) for tf in TIMEFRAMES},
            "interpretation": "TECHNICAL_CONTEXT_ONLY_NOT_CAUSAL_PROOF",
        }
    enriched.setdefault("limitations", []).extend([
        "BREAK_CONTEXT_USES_CAUSAL_CLOSED_BARS_AT_FIRST_CONTACT_AND_CONFIRMATION",
        "CONTACT_CONTEXT_AVAILABLE_ONLY_AT_CONTACT_BAR_CLOSE",
    ])
    return enriched


def _self_test():
    # Velas ficticias; el comportamiento debe respetar cierres de cada TF.
    origin = pd.Timestamp("2026-04-08T00:00:00Z")

    def build(tf_minutes, count, earliest):
        rows = []
        for i in range(count):
            when = origin + pd.Timedelta(minutes=earliest + tf_minutes * i)
            rows.append({
                "bar_close": when, "close": 1.10 + 0.001 * i,
                "ema20": 1.08, "ema50": 1.07, "ema200": 1.06, "rsi14": 50 + i,
                "atr14": 0.001 + i * 0.00001, "return_5": 0.001,
                "volume_ratio": 1.0 + 0.01 * i,
            })
        return pd.DataFrame(rows)

    history = {
        "M15": build(15, 15, 15),
        "H1": build(60, 5, 0),
        "H4": build(240, 3, 0),
        "D1": build(1440, 2, 0),
    }
    report = {
        "analysis_time": "2026-04-08T01:30:00Z",
        "events": [
            {"outcome": "BREAK", "contact_at": "2026-04-08T00:15:00Z",
             "resolved_at": "2026-04-08T00:45:00Z"},
            {"outcome": "PENDING", "contact_at": "2026-04-08T01:15:00Z",
             "resolved_at": None},
        ],
    }
    result = add_break_contexts(report, history)
    event = result["events"][0]
    snapshots = event["context_at_break_confirmation"]["timeframes"]
    before = event["context_at_first_contact"]["timeframes"]
    assert before["M15"]["bar_close"] == "2026-04-08T00:15:00+00:00"
    assert before["H1"]["bar_close"] == "2026-04-08T00:00:00+00:00"
    assert snapshots["M15"]["bar_close"] == "2026-04-08T00:45:00+00:00"
    assert snapshots["H1"]["bar_close"] == "2026-04-08T00:00:00+00:00"
    assert snapshots["H4"]["bar_close"] == "2026-04-08T00:00:00+00:00"
    assert snapshots["D1"]["bar_close"] == "2026-04-08T00:00:00+00:00"
    assert "context_at_break_confirmation" not in result["events"][1]
    assert "context_at_first_contact" not in result["events"][1]
    assert "context_at_break_confirmation" not in report["events"][0]
    assert "context_at_first_contact" not in report["events"][0]

    # Eliminar explícitamente las velas posteriores no cambia el informe.
    trimmed = {
        tf: df.loc[df["bar_close"] <= origin + pd.Timedelta(minutes=90)].copy()
        for tf, df in history.items()
    }
    assert result == add_break_contexts(report, trimmed)

    # Las velas de H1/H4/D1 todavía abiertas no se incorporan.
    report_2 = deepcopy(report)
    report_2["events"][0]["resolved_at"] = "2026-04-08T01:15:00Z"
    latest = add_break_contexts(report_2, history)["events"][0]
    assert latest["context_at_break_confirmation"]["timeframes"]["H1"]["bar_close"] == "2026-04-08T01:00:00+00:00"

    print("OK: contexto en contacto y ruptura, sin velas futuras ni TF abiertos")
    print("M15 primer contacto:", before["M15"]["bar_close"])
    print("M15 ruptura:", snapshots["M15"]["bar_close"])
    print("H1 disponible:", snapshots["H1"]["bar_close"])
    print("H4 disponible:", snapshots["H4"]["bar_close"])
    print("D1 disponible:", snapshots["D1"]["bar_close"])


if __name__ == "__main__":
    _self_test()
