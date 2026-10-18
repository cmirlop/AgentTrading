"""Prueba de episodios historicos de zonas de USDCAD, sin Ollama.

Reconstruye LevelRegistry en orden cronologico hasta 2026-04-08 18:00 UTC.
Clasifica eventos de las dos zonas mas cercanas, respetando el corte temporal.
Las fronteras de la zona son retrospectivas para contactos anteriores a su
primera deteccion; el informe NO demuestra que la zona existiera entonces.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.historical_state import prepare_symbol_history
from src.level_history import analyze_zone_contacts, analyze_zone_events
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance

SYMBOL = "USDCAD"
START = pd.Timestamp("2026-04-08T00:00:00+00:00")
END = pd.Timestamp("2026-04-08T18:00:00+00:00")


def inspect_zone(label: str, zone: dict, m15: pd.DataFrame) -> None:
    assert zone is not None, f"{label}: no se encontro zona"
    report = analyze_zone_events(zone, m15, END)
    contacts = analyze_zone_contacts(zone, m15, END)
    past_only = m15.loc[m15["bar_close"] <= END].copy()

    assert report == analyze_zone_events(zone, past_only, END), (
        f"{label}: el informe cambia al eliminar velas futuras"
    )
    assert report["zone_id"] == zone["zone_id"]
    assert report["bounds_used"] == contacts["bounds_used"]

    seen = 0
    for phase in ("before_detection", "since_detection"):
        summary = report[phase]
        total = (
            summary["confirmed_rejections"]
            + summary["confirmed_breaks"]
            + summary["pending"]
            + summary["unresolved"]
        )
        assert summary["contact_count"] == total, (label, phase, summary)
        assert summary["contact_count"] == contacts[phase]["contact_count"], (
            label, phase, "Difieren episodios y contactos", summary
        )
        seen += summary["contact_count"]

    assert seen == len(report["events"])
    detection = pd.Timestamp(zone["first_detected_at"])
    for event in report["events"]:
        contact_time = pd.Timestamp(event["contact_at"])
        assert contact_time <= END
        assert event["role_at_contact"] in ("SUPPORT", "RESISTANCE", "UNKNOWN")
        assert event["outcome"] in ("REJECTION", "BREAK", "PENDING", "UNRESOLVED")
        expected_phase = (
            "BEFORE_DETECTION" if contact_time < detection else "SINCE_DETECTION"
        )
        assert event["phase"] == expected_phase
        if event["resolved_at"] is not None:
            resolution_time = pd.Timestamp(event["resolved_at"])
            assert contact_time <= resolution_time <= END

    print(f"\n{label}: {zone['zone_id']} ({zone['role']})")
    print(f"Zona: {zone['low']:.5f} - {zone['high']:.5f}")
    print("Primera deteccion:", zone["first_detected_at"])
    for phase, heading in (
        ("before_detection", "ANTES DE DETECTARSE (RETROSPECTIVO)"),
        ("since_detection", "DESDE SU DETECCION"),
    ):
        s = report[phase]
        print(f"  {heading}:")
        print(
            f"    contactos={s['contact_count']} "
            f"rechazos={s['confirmed_rejections']} "
            f"rupturas={s['confirmed_breaks']} "
            f"pendientes={s['pending']} "
            f"sin_resolver={s['unresolved']}"
        )
    print("  Ultimos 5 episodios:")
    for e in report["events"][-5:]:
        print(
            "   ", e["contact_at"],
            e["role_at_contact"], "->", e["outcome"],
            "resuelto:", e["resolved_at"] or "-"
        )
    print("OK: recuentos consistentes y sin informacion futura")


def main() -> None:
    history = prepare_symbol_history(SYMBOL)
    m15 = history["M15"]
    times = m15.loc[
        (m15["bar_close"] >= START) & (m15["bar_close"] <= END),
        "bar_close",
    ].tolist()
    assert len(times) == 73, f"Se esperaban 73 cierres M15, hay {len(times)}"
    assert (m15["bar_close"] > END).any(), "Faltan datos futuros para probar filtrado"

    with TemporaryDirectory(prefix="zone_events_20260408_") as tmp:
        registry = LevelRegistry(Path(tmp) / "registry.json")
        observed = None
        for moment in times:
            levels = detect_support_resistance(
                symbol=SYMBOL, analysis_time=moment, prepared_history=history,
            )
            assert levels["status"] == "OK", (moment, levels)
            observed = registry.observe(levels)

        print("USDCAD | episodios historicos hasta", END.isoformat())
        inspect_zone("SOPORTE MAS CERCANO", observed["nearest_support"], m15)
        inspect_zone("RESISTENCIA MAS CERCANA", observed["nearest_resistance"], m15)

    print("\nOK: episodios reales clasificados, sin Ollama ni operaciones")
    print("Registro temporal eliminado al terminar")


if __name__ == "__main__":
    main()
