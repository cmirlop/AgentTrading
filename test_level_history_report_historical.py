"""Prueba del informe compacto de dos niveles reales de USDCAD (sin Ollama).

Reconstruye el registro en orden cronológico hasta 2026-04-08 18:00 UTC,
clasifica el historial disponible, añade contextos técnicos de rupturas y
construye el resumen para un futuro payload LLM. No ejecuta operaciones.
"""
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.historical_state import prepare_symbol_history
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance
from src.level_history import analyze_zone_events
from src.level_event_context import add_break_contexts
from src.level_history_report import build_level_history_report

SYMBOL = "USDCAD"
START = pd.Timestamp("2026-04-08T00:00:00Z")
END = pd.Timestamp("2026-04-08T18:00:00Z")


def make_report(zone, prepared, cutoff=END):
    episodes = analyze_zone_events(zone, prepared["M15"], cutoff)
    enriched = add_break_contexts(episodes, prepared)
    return build_level_history_report(zone, enriched)


def describe(label, report):
    earlier = report["retrospective_history"]
    newer = report["since_detection"]
    print(f"\n{label}: {report['zone_id']} ({report['current_role']})")
    print(f"Zona: {report['bounds']['low']:.5f} - {report['bounds']['high']:.5f}")
    print("Primera deteccion:", report["first_detected_at"])
    print("Temporalidades:", report["current_timeframes"])
    print(
        "RETROSPECTIVO:",
        f"contactos={earlier['contact_count']}",
        f"rechazos={earlier['confirmed_rejections']}",
        f"rupturas={earlier['confirmed_breaks']}",
    )
    print(
        "DESDE DETECCION:",
        f"contactos={newer['contact_count']}",
        f"rechazos={newer['confirmed_rejections']}",
        f"rupturas={newer['confirmed_breaks']}",
    )
    print("Ultimos episodios:")
    for event in report["recent_events"]:
        print(
            " ", event["contact_at"], event["role_at_contact"],
            "->", event["outcome"],
        )
    print("Ejemplos recientes de ruptura:")
    for event in report["recent_break_examples"]:
        contact = event["market_at_contact"] or {}
        confirmation = event["market_at_confirmation"] or {}
        cm15 = contact.get("M15", {})
        fm15 = confirmation.get("M15", {})
        print(
            " ", event["contact_at"], "->", event["resolved_at"],
            f"RSI_M15_contacto={cm15.get('rsi14')}",
            f"RSI_M15_confirmacion={fm15.get('rsi14')}",
        )


def main():
    prepared = prepare_symbol_history(SYMBOL)
    m15 = prepared["M15"]
    moments = m15.loc[
        m15["bar_close"].between(START, END), "bar_close"
    ].tolist()
    assert len(moments) == 73, f"Se esperaban 73 cierres, hay {len(moments)}"
    assert (m15["bar_close"] > END).any(), "No hay velas futuras para probar el corte"

    with TemporaryDirectory(prefix="level_report_20260408_") as folder:
        registry = LevelRegistry(Path(folder) / "registry.json")
        observed = None
        for moment in moments:
            found = detect_support_resistance(
                symbol=SYMBOL, analysis_time=moment, prepared_history=prepared
            )
            assert found["status"] == "OK", (moment, found)
            observed = registry.observe(found)

        assert observed is not None
        past_only = {
            tf: df.loc[df["bar_close"] <= END].copy()
            for tf, df in prepared.items()
        }
        print("USDCAD | informes historicos hasta", END.isoformat())

        for label, key in (
            ("SOPORTE MAS CERCANO", "nearest_support"),
            ("RESISTENCIA MAS CERCANA", "nearest_resistance"),
        ):
            zone = observed[key]
            assert zone is not None, f"No se detecto {label}"
            report = make_report(zone, prepared)
            report_past_only = make_report(zone, past_only)
            assert report == report_past_only, (
                label, "INFORME CAMBIA AL RETIRAR VELAS FUTURAS"
            )
            assert report["as_of"] == END.isoformat()
            assert report["zone_id"] == zone["zone_id"]
            assert (
                report["retrospective_history"]["contact_count"]
                + report["since_detection"]["contact_count"]
            ) >= len(report["recent_events"])
            assert len(report["recent_break_examples"]) <= 2
            for event in report["recent_break_examples"]:
                assert pd.Timestamp(event["resolved_at"]) <= END
                assert event["market_at_contact"] is not None
                assert event["market_at_confirmation"] is not None
            describe(label, report)
            print("OK: informe compacto consistente y sin velas futuras")

    print("\nOK: informes reales listos para revisar, sin Ollama ni operaciones")
    print("Registro temporal eliminado al terminar")


if __name__ == "__main__":
    main()
