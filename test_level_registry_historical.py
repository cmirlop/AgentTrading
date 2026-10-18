"""Prueba de integración del detector y LevelRegistry, sin Ollama.

Usa dos instantes históricos consecutivos y un registro temporal descartable.
No modifica el registro de producción ni los datos de mercado.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from src.historical_state import prepare_symbol_history
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance


SYMBOL = "USDCAD"
TIMES = (
    "2026-04-08T17:45:00+00:00",
    "2026-04-08T18:00:00+00:00",
)


def zones_by_id(snapshot):
    result = {}
    for group in ("supports", "resistances", "active_zones"):
        for zone in snapshot[group]:
            zone_id = zone["zone_id"]
            assert zone_id not in result, f"ID duplicado en snapshot: {zone_id}"
            result[zone_id] = zone
    return result


def describe_nearest(snapshot, label):
    zone = snapshot.get(label)
    if zone is None:
        return "Ninguna"
    return (
        f"{zone['zone_id']} | {zone['role']} | "
        f"{zone['low']:.5f}-{zone['high']:.5f} | "
        f"detectada={zone['first_detected_at']}"
    )


def main():
    history = prepare_symbol_history(SYMBOL)

    # Al usar un directorio temporal no contaminamos replay futuros.
    with TemporaryDirectory(prefix="level_registry_test_") as folder:
        registry = LevelRegistry(Path(folder) / "registry.json")
        observations = []

        for moment in TIMES:
            raw = detect_support_resistance(
                symbol=SYMBOL,
                analysis_time=moment,
                prepared_history=history,
            )
            assert raw["status"] == "OK", (moment, raw)
            assert "zone_id" not in (raw.get("nearest_support") or {})

            recorded = registry.observe(raw)
            observations.append(recorded)

            print(f"\n{moment}")
            print("Soporte:    ", describe_nearest(recorded, "nearest_support"))
            print("Resistencia:", describe_nearest(recorded, "nearest_resistance"))

        first, second = map(zones_by_id, observations)
        shared_ids = set(first) & set(second)

        assert shared_ids, "No se conserva ninguna identidad entre los dos instantes"
        for zone_id in shared_ids:
            assert second[zone_id]["first_detected_at"] == TIMES[0], zone_id

        # El registro nunca debe escribir en el dict original del detector.
        assert registry.state["last_analysis_time"][SYMBOL] == TIMES[1]
        print(f"\nOK: {len(shared_ids)} zonas conservan su ID entre ambos análisis")
        print("Registro temporal eliminado al terminar la prueba.")


if __name__ == "__main__":
    main()
