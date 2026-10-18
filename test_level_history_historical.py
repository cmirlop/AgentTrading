"""Prueba historica de LevelHistoryAnalyzer con zonas reales de USDCAD.

Reconstruye cronologicamente el registro del 8 de abril hasta las 18:00 UTC,
y analiza las zonas mas cercanas usando exclusivamente M15 cerradas hasta
ese instante. No emplea Ollama, no ejecuta operaciones ni persiste cambios.

Los contactos anteriores son RETROSPECTIVOS: las fronteras de zona son las
del instante de analisis; no prueban que la zona existiera entonces.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.historical_state import prepare_symbol_history
from src.level_history import analyze_zone_contacts
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance


SYMBOL = "USDCAD"
START = pd.Timestamp("2026-04-08T00:00:00+00:00")
ANALYSIS_TIME = pd.Timestamp("2026-04-08T18:00:00+00:00")


def verify_and_print(label: str, zone: dict, m15: pd.DataFrame) -> None:
    if zone is None:
        print(f"{label}: no hay zona cercana")
        return

    report = analyze_zone_contacts(zone, m15, ANALYSIS_TIME)
    detection = pd.Timestamp(zone["first_detected_at"])
    expected_bars = int((m15["bar_close"] <= ANALYSIS_TIME).sum())

    assert report["zone_id"] == zone["zone_id"]
    assert report["closed_bars_examined"] == expected_bars
    assert pd.Timestamp(report["analysis_time"]) == ANALYSIS_TIME

    before = report["before_detection"]["contact_times"]
    since = report["since_detection"]["contact_times"]
    assert report["before_detection"]["contact_count"] == len(before)
    assert report["since_detection"]["contact_count"] == len(since)
    assert all(pd.Timestamp(t) < detection for t in before)
    assert all(detection <= pd.Timestamp(t) <= ANALYSIS_TIME for t in since)

    # No debe cambiar nada si los datos futuros siguen presentes.
    # El historial preparado contiene velas posteriores a ANALYSIS_TIME.
    assert (m15["bar_close"] > ANALYSIS_TIME).any(), (
        "Se necesitan velas futuras en el dataset para comprobar el filtro"
    )
    past_only = m15.loc[m15["bar_close"] <= ANALYSIS_TIME].copy()
    assert analyze_zone_contacts(zone, past_only, ANALYSIS_TIME) == report, (
        "El informe cambio al eliminar velas futuras"
    )

    print(f"\n{label}: {zone['zone_id']} ({zone['role']})")
    print(f"Precio: {zone['low']:.5f} - {zone['high']:.5f}")
    print(f"Origen: {', '.join(zone.get('timeframes', []))}")
    print("Primera deteccion:", zone["first_detected_at"])
    print("M15 cerradas revisadas:", report["closed_bars_examined"])
    print("Contactos anteriores (retrospectivos):", len(before))
    print("Ultimos anteriores:", before[-5:])
    print("Contactos desde deteccion:", len(since))
    print("Ultimos posteriores:", since[-5:])
    print("OK: clasificacion temporal y filtro de velas futuras")


def main() -> None:
    history = prepare_symbol_history(SYMBOL)
    m15 = history["M15"]
    times = m15.loc[
        (m15["bar_close"] >= START)
        & (m15["bar_close"] <= ANALYSIS_TIME),
        "bar_close",
    ].tolist()
    assert len(times) == 73, f"Cierres M15 inesperados: {len(times)}"

    # Registro NUEVO y temporal: evita transportar identidades del futuro.
    with TemporaryDirectory(prefix="test_zone_history_") as temp:
        registry = LevelRegistry(Path(temp) / "registry.json")
        latest = None
        for moment in times:
            raw = detect_support_resistance(
                symbol=SYMBOL,
                analysis_time=moment,
                prepared_history=history,
            )
            assert raw["status"] == "OK", (moment, raw)
            latest = registry.observe(raw)

        assert latest is not None
        print("USDCAD | informe de contactos hasta", ANALYSIS_TIME.isoformat())
        print("Instantes M15 del replay:", len(times))
        verify_and_print("SOPORTE", latest["nearest_support"], m15)
        verify_and_print("RESISTENCIA", latest["nearest_resistance"], m15)

    print("\nOK: LevelHistoryAnalyzer probado sobre niveles reales, sin Ollama")
    print("Registro temporal eliminado al terminar.")


if __name__ == "__main__":
    main()
