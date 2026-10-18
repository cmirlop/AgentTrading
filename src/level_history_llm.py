"""Payload breve y determinista de la biografia de una zona para Qwen.

Entrada: salida validada de build_level_history_report(). NO llama a Ollama.
No cambia strength, los niveles ni las reglas de trading.
La historia anterior a la deteccion usa retrospectivamente los LIMITES
ACTUALES y no equivale a conocimiento historico del detector.
"""
from __future__ import annotations

from datetime import datetime, timezone


_TFS = ("M15", "H1", "H4", "D1")
_OUTCOMES = ("REJECTION", "BREAK", "PENDING", "UNRESOLVED")
_ROLES = ("SUPPORT", "RESISTANCE", "UNKNOWN")


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("TIMESTAMP_MUST_HAVE_TIMEZONE")
    return stamp.astimezone(timezone.utc)


def _counts(summary: dict) -> dict:
    return {
        "contacts": int(summary["contact_count"]),
        "rejections": int(summary["confirmed_rejections"]),
        "breaks": int(summary["confirmed_breaks"]),
        "pending": int(summary["pending"]),
        "unresolved": int(summary["unresolved"]),
    }


def _market(snapshot: dict | None) -> dict | None:
    if snapshot is None:
        return None
    compact = {}
    for tf in _TFS:
        data = snapshot.get(tf)
        if not data:
            continue
        row = {
            "ema": data.get("ema"),
            "momentum": data.get("momentum"),
        }
        # Mayor detalle en las temporalidades relevantes para la interaccion.
        if tf in ("M15", "H1"):
            row["rsi14"] = data.get("rsi14")
            row["atr_relative"] = data.get("atr_relative")
        compact[tf] = row
    return compact or None


def _brief(event: dict, cutoff: datetime) -> dict:
    touched = _utc(event["contact_at"])
    resolved = _utc(event["resolved_at"]) if event.get("resolved_at") else None
    if touched > cutoff or (resolved is not None and (resolved < touched or resolved > cutoff)):
        raise ValueError("FUTURE_EVENT_IN_LLM_HISTORY")
    outcome = event["outcome"]
    if outcome not in _OUTCOMES or event["role_at_contact"] not in _ROLES:
        raise ValueError("INVALID_HISTORICAL_EPISODE")
    if outcome in ("REJECTION", "BREAK") and resolved is None:
        raise ValueError("CONFIRMED_EVENT_WITHOUT_RESOLUTION")
    return {
        "contact_at": touched.isoformat(),
        "historical_role": event["role_at_contact"],
        "outcome": outcome,
        "resolved_at": resolved.isoformat() if resolved else None,
    }


def build_level_history_llm_payload(
    report: dict,
    max_recent_events: int = 3,
    max_break_examples: int = 2,
) -> dict:
    """Condensa SOLO los episodios que existen en el informe ya validado.

    Los contextos de CONFIRMACION son descriptivos del pasado; NUNCA deben
    presentarse como informacion disponible al primer contacto.
    """
    if max_recent_events < 0 or max_break_examples < 0:
        raise ValueError("INVALID_HISTORY_LIMIT")
    cutoff = _utc(report["as_of"])
    detected_at = _utc(report["first_detected_at"])
    if detected_at > cutoff:
        raise ValueError("DETECTION_AFTER_ANALYSIS")
    role = report["current_role"]
    if role not in ("SUPPORT", "RESISTANCE"):
        raise ValueError("UNKNOWN_CURRENT_LEVEL_ROLE")

    role_stats = {}
    for phase, key in (
        ("retrospective_before_detection", "before_detection"),
        ("observed_since_detection", "since_detection"),
    ):
        group = report["by_role_and_phase"][key]
        role_stats[phase] = {
            historical_role: {
                "rejections": int(group[historical_role]["REJECTION"]),
                "breaks": int(group[historical_role]["BREAK"]),
            }
            for historical_role in ("SUPPORT", "RESISTANCE")
        }

    recents = report.get("recent_events", [])
    recents = sorted(recents, key=lambda e: _utc(e["contact_at"]))
    selected_events = recents[-max_recent_events:] if max_recent_events else []

    candidates = list(report.get("recent_break_examples", []))
    # Preferir episodios en que la zona desempenaba la misma funcion que hoy;
    # despues, preferir los mas recientes disponibles en el informe.
    candidates.sort(
        key=lambda e: (
            e["role_at_contact"] == role,
            _utc(e["contact_at"]),
        ),
        reverse=True,
    )
    selected_breaks = candidates[:max_break_examples] if max_break_examples else []

    break_examples = []
    for event in selected_breaks:
        item = _brief(event, cutoff)
        if item["outcome"] != "BREAK":
            raise ValueError("EXAMPLE_IS_NOT_BREAK")
        if event.get("market_at_contact") is None or event.get("market_at_confirmation") is None:
            raise ValueError("BREAK_CONTEXT_MISSING")
        item["market_at_first_contact"] = _market(event["market_at_contact"])
        item["market_at_break_confirmation"] = _market(event["market_at_confirmation"])
        break_examples.append(item)

    bounds = report["bounds"]
    low, high = float(bounds["low"]), float(bounds["high"])
    if not low < high:
        raise ValueError("INVALID_BOUNDS")

    return {
        "zone_id": report["zone_id"],
        "as_of": cutoff.isoformat(),
        "current_role": role,
        "zone_low": low,
        "zone_high": high,
        "origin_timeframes": list(report["current_timeframes"]),
        "first_seen_in_this_replay": detected_at.isoformat(),
        "counts": {
            "retrospective_before_detection": _counts(report["retrospective_history"]),
            "observed_since_detection": _counts(report["since_detection"]),
        },
        "outcomes_by_historical_role": role_stats,
        "recent_episodes": [_brief(e, cutoff) for e in selected_events],
        "historical_break_examples": break_examples,
        "interpretation_limits": [
            "Retrospective contacts use current zone boundaries; the zone may not have been known then.",
            "First detection is relative to the start of this registry replay.",
            "Break-confirmation indicators were NOT available at first contact.",
            "Historical counts are descriptive, not probabilities or trading recommendations.",
        ],
    }


def _self_test():
    import json
    phase = lambda c, r, b: {
        "contact_count": c, "confirmed_rejections": r,
        "confirmed_breaks": b, "pending": 0, "unresolved": c-r-b,
    }
    role = lambda r, b: {"REJECTION": r, "BREAK": b, "PENDING": 0, "UNRESOLVED": 0}
    empty = role(0, 0)
    example = {
        "contact_at": "2026-04-08T01:15:00+00:00",
        "phase": "BEFORE_DETECTION", "role_at_contact": "SUPPORT",
        "outcome": "BREAK", "resolved_at": "2026-04-08T01:45:00+00:00",
        "market_at_contact": {
            "M15": {"ema": "BEARISH", "rsi14": 40, "momentum": "NEGATIVE", "atr_relative": 1.1},
            "H1": {"ema": "MIXED", "rsi14": 52, "momentum": "NEGATIVE", "atr_relative": 1.0},
        },
        "market_at_confirmation": {
            "M15": {"ema": "BEARISH", "rsi14": 30, "momentum": "NEGATIVE", "atr_relative": 1.3},
        },
    }
    report = {
        "zone_id": "USDCAD_Z000004", "as_of": "2026-04-08T03:00:00+00:00",
        "first_detected_at": "2026-04-08T02:00:00+00:00", "current_role": "RESISTANCE",
        "bounds": {"low": 1.38527, "high": 1.38557},
        "current_timeframes": ["D1"],
        "retrospective_history": phase(2, 1, 1),
        "since_detection": phase(1, 1, 0),
        "by_role_and_phase": {
            "before_detection": {"SUPPORT": role(0, 1), "RESISTANCE": role(1, 0), "UNKNOWN": empty},
            "since_detection": {"SUPPORT": empty, "RESISTANCE": role(1, 0), "UNKNOWN": empty},
        },
        "recent_events": [example, {
            "contact_at": "2026-04-08T02:30:00+00:00", "phase": "SINCE_DETECTION",
            "role_at_contact": "RESISTANCE", "outcome": "REJECTION",
            "resolved_at": "2026-04-08T02:45:00+00:00",
        }],
        "recent_break_examples": [example],
    }
    original = json.dumps(report, sort_keys=True)
    result = build_level_history_llm_payload(report)
    assert original == json.dumps(report, sort_keys=True), "SE MODIFICO EL INFORME ORIGINAL"
    assert result["counts"]["retrospective_before_detection"]["breaks"] == 1
    assert result["counts"]["observed_since_detection"]["breaks"] == 0
    assert result["historical_break_examples"][0]["market_at_first_contact"]["M15"]["rsi14"] == 40
    assert result["historical_break_examples"][0]["market_at_break_confirmation"]["M15"]["rsi14"] == 30
    assert result["recent_episodes"][-1]["outcome"] == "REJECTION"
    clone = json.loads(json.dumps(report))
    clone["recent_events"][-1]["resolved_at"] = "2026-04-08T03:15:00+00:00"
    try:
        build_level_history_llm_payload(clone)
    except ValueError as ex:
        assert str(ex) == "FUTURE_EVENT_IN_LLM_HISTORY"
    else:
        raise AssertionError("ACEPTO UN EVENTO CON INFORMACION FUTURA")
    print("OK: resumen historico breve, roles separados, sin mutar datos ni usar futuro")
    print("Zona:", result["zone_id"])
    print("Tamano JSON:", len(json.dumps(result, ensure_ascii=False)), "caracteres")
    print("Ejemplos de ruptura:", len(result["historical_break_examples"]))


if __name__ == "__main__":
    _self_test()
