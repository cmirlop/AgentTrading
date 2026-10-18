"""Resumen compacto de la biografía histórica de una zona, sin Ollama.

Entrada: una zona registrada por LevelRegistry y el informe devuelto por
add_break_contexts(analyze_zone_events(...), prepared_history).

Las estadísticas anteriores a first_detected_at son RETROSPECTIVAS: no prueban
que el detector conociese entonces esos límites. El informe no da señales de
trading ni estima probabilidades predictivas.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

TIMEFRAMES = ("M15", "H1", "H4", "D1")
PHASES = {"BEFORE_DETECTION": "before_detection", "SINCE_DETECTION": "since_detection"}
ROLES = ("SUPPORT", "RESISTANCE", "UNKNOWN")
OUTCOMES = ("REJECTION", "BREAK", "PENDING", "UNRESOLVED")


def _utc(text: str) -> datetime:
    instant = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("TIMESTAMP_MUST_HAVE_TIMEZONE")
    return instant.astimezone(timezone.utc)


def _compact_snapshot(context: dict | None, cutoff: datetime) -> dict | None:
    if context is None:
        return None
    result = {}
    for tf in TIMEFRAMES:
        snap = context.get("timeframes", {}).get(tf)
        if not snap or not snap.get("available"):
            continue
        closed_at = _utc(snap["bar_close"])
        if closed_at > cutoff:
            raise ValueError("FUTURE_TECHNICAL_CONTEXT")
        result[tf] = {
            "ema": snap.get("ema_structure"),
            "rsi14": snap.get("rsi14"),
            "momentum": snap.get("return5_direction"),
            "atr_relative": snap.get("atr_vs_recent_mean"),
        }
    return result


def build_level_history_report(
    zone: dict,
    enriched_events: dict,
    max_recent_events: int = 3,
    max_break_examples: int = 2,
) -> dict:
    """Resume eventos conocidos hasta analysis_time; no modifica las entradas.

    Los ejemplos BREAK incorporan información al primer contacto y a la
    confirmación, etiquetada de forma separada. No presupone causalidad.
    """
    if max_recent_events < 0 or max_break_examples < 0:
        raise ValueError("NEGATIVE_REPORT_LIMIT")
    if zone["zone_id"] != enriched_events["zone_id"]:
        raise ValueError("ZONE_ID_MISMATCH")
    as_of = _utc(enriched_events["analysis_time"])
    first_detected = _utc(zone["first_detected_at"])
    if first_detected > as_of:
        raise ValueError("DETECTION_AFTER_ANALYSIS")
    bounds = enriched_events["bounds_used"]
    if float(zone["low"]) != float(bounds["low"]) or float(zone["high"]) != float(bounds["high"]):
        raise ValueError("ZONE_BOUNDS_MISMATCH")

    events = sorted(enriched_events["events"], key=lambda e: _utc(e["contact_at"]))
    breakdown = {
        name: {role: {outcome: 0 for outcome in OUTCOMES} for role in ROLES}
        for name in PHASES.values()
    }
    validated = []
    for event in events:
        contact = _utc(event["contact_at"])
        if contact > as_of:
            raise ValueError("FUTURE_CONTACT")
        resolved = _utc(event["resolved_at"]) if event.get("resolved_at") else None
        if resolved is not None and (resolved < contact or resolved > as_of):
            raise ValueError("INVALID_RESOLUTION_TIME")
        role, outcome, phase = event["role_at_contact"], event["outcome"], event["phase"]
        expected_phase = "BEFORE_DETECTION" if contact < first_detected else "SINCE_DETECTION"
        if phase != expected_phase or phase not in PHASES:
            raise ValueError("INVALID_EVENT_PHASE")
        if role not in ROLES or outcome not in OUTCOMES:
            raise ValueError("INVALID_EVENT_TYPE")
        if outcome in ("BREAK", "REJECTION") and resolved is None:
            raise ValueError("CONFIRMED_EVENT_WITHOUT_RESOLUTION")
        # Nunca incorporar indicadores de confirmación a un evento pendiente.
        at_contact = _compact_snapshot(event.get("context_at_first_contact"), contact)
        at_confirmation = _compact_snapshot(event.get("context_at_break_confirmation"), resolved or as_of)
        if outcome != "BREAK" and (at_contact is not None or at_confirmation is not None):
            raise ValueError("UNEXPECTED_BREAK_CONTEXT")
        breakdown[PHASES[phase]][role][outcome] += 1
        validated.append({
            "contact_at": contact.isoformat(),
            "phase": phase,
            "role_at_contact": role,
            "outcome": outcome,
            "resolved_at": resolved.isoformat() if resolved else None,
            "market_at_contact": at_contact,
            "market_at_confirmation": at_confirmation,
        })

    # Detecta discrepancias entre conteos y la lista de episodios.
    for name in PHASES.values():
        s = enriched_events[name]
        group = breakdown[name]
        observed = {k: sum(by_role[k] for by_role in group.values()) for k in OUTCOMES}
        expected = {
            "REJECTION": s["confirmed_rejections"],
            "BREAK": s["confirmed_breaks"],
            "PENDING": s["pending"],
            "UNRESOLVED": s["unresolved"],
        }
        if observed != expected or sum(observed.values()) != s["contact_count"]:
            raise ValueError("INCONSISTENT_EVENT_COUNTS")

    def brief(event):
        return {k: event[k] for k in (
            "contact_at", "phase", "role_at_contact", "outcome", "resolved_at"
        )}

    breaks = [e for e in validated if e["outcome"] == "BREAK"]
    return {
        "zone_id": zone["zone_id"],
        "as_of": as_of.isoformat(),
        "first_detected_at": first_detected.isoformat(),
        "current_role": zone.get("role"),
        "bounds": deepcopy(bounds),
        "current_timeframes": list(zone.get("timeframes", [])),
        "retrospective_history": deepcopy(enriched_events["before_detection"]),
        "since_detection": deepcopy(enriched_events["since_detection"]),
        "by_role_and_phase": breakdown,
        "recent_events": [brief(e) for e in (validated[-max_recent_events:] if max_recent_events else [])],
        "recent_break_examples": [
            {**brief(e), "market_at_contact": e["market_at_contact"],
             "market_at_confirmation": e["market_at_confirmation"]}
            for e in (breaks[-max_break_examples:] if max_break_examples else [])
        ],
        "limitations": [
            "RETROSPECTIVE_HISTORY_USES_CURRENT_ZONE_BOUNDS",
            "FIRST_DETECTION_IS_RELATIVE_TO_REGISTRY_REPLAY_START",
            "EVENT_RULES_DIAGNOSTIC_NOT_CALIBRATED_PROBABILITIES",
            "BREAK_EXAMPLES_ARE_DESCRIPTIVE_NOT_PREDICTIVE",
        ],
    }


def _self_test() -> None:
    zone = {"zone_id": "USDCAD_Z000004", "first_detected_at": "2026-04-08T01:00:00Z",
            "role": "RESISTANCE", "low": 1.38527, "high": 1.38557, "timeframes": ["D1"]}
    snap_contact = {"timeframes": {"M15": {"available": True,
        "bar_close": "2026-04-08T00:15:00Z", "ema_structure": "BEARISH",
        "rsi14": 30, "return5_direction": "NEGATIVE", "atr_vs_recent_mean": 1.2}}}
    snap_break = {"timeframes": {"M15": {"available": True,
        "bar_close": "2026-04-08T00:45:00Z", "ema_structure": "BEARISH",
        "rsi14": 20, "return5_direction": "NEGATIVE", "atr_vs_recent_mean": 1.3}}}
    report = {"zone_id": zone["zone_id"], "analysis_time": "2026-04-08T02:00:00Z",
              "bounds_used": {"low": zone["low"], "high": zone["high"]},
              "before_detection": {"contact_count": 1, "confirmed_rejections": 0,
                                   "confirmed_breaks": 1, "pending": 0, "unresolved": 0},
              "since_detection": {"contact_count": 2, "confirmed_rejections": 1,
                                  "confirmed_breaks": 0, "pending": 1, "unresolved": 0},
              "events": [
                  {"contact_at": "2026-04-08T00:15:00Z", "phase": "BEFORE_DETECTION",
                   "role_at_contact": "SUPPORT", "outcome": "BREAK",
                   "resolved_at": "2026-04-08T00:45:00Z",
                   "context_at_first_contact": snap_contact,
                   "context_at_break_confirmation": snap_break},
                  {"contact_at": "2026-04-08T01:30:00Z", "phase": "SINCE_DETECTION",
                   "role_at_contact": "RESISTANCE", "outcome": "REJECTION",
                   "resolved_at": "2026-04-08T01:45:00Z"},
                  {"contact_at": "2026-04-08T02:00:00Z", "phase": "SINCE_DETECTION",
                   "role_at_contact": "RESISTANCE", "outcome": "PENDING",
                   "resolved_at": None},
              ]}
    original = deepcopy(report)
    result = build_level_history_report(zone, report)
    assert report == original
    assert result["retrospective_history"]["confirmed_breaks"] == 1
    assert result["since_detection"]["pending"] == 1
    assert result["by_role_and_phase"]["before_detection"]["SUPPORT"]["BREAK"] == 1
    assert result["recent_break_examples"][0]["market_at_contact"]["M15"]["rsi14"] == 30
    assert result["recent_break_examples"][0]["market_at_confirmation"]["M15"]["rsi14"] == 20
    assert len(result["recent_events"]) == 3

    future = deepcopy(report)
    future["events"][0]["context_at_first_contact"]["timeframes"]["M15"]["bar_close"] = "2026-04-08T01:00:00Z"
    try:
        build_level_history_report(zone, future)
    except ValueError as error:
        assert str(error) == "FUTURE_TECHNICAL_CONTEXT"
    else:
        raise AssertionError("Se aceptaron indicadores posteriores al primer contacto")
    print("OK: informe compacto con historia, episodios y dos contextos de ruptura")
    print("OK: sin datos futuros, sin mezclar etapas y sin mutar el informe original")
    print("Zona:", result["zone_id"])
    print("Rupturas antes / desde deteccion:",
          result["retrospective_history"]["confirmed_breaks"],
          "/", result["since_detection"]["confirmed_breaks"])


if __name__ == "__main__":
    _self_test()
