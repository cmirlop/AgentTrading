"""Replay de identidad de niveles durante todo el 8 de abril, SIN Ollama.

No opera, no modifica la fuerza de los niveles y usa un registro temporal.
Importante: este test solo prueba seguimiento cronologico de identidades;
no clasifica toques, rechazos ni rupturas.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.historical_state import prepare_symbol_history
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance


SYMBOL = "USDCAD"
START = pd.Timestamp("2026-04-08T00:00:00+00:00")
END = pd.Timestamp("2026-04-09T00:00:00+00:00")
CHECK_TIMES = {
    "2026-04-08T17:45:00+00:00",
    "2026-04-08T18:00:00+00:00",
}


def describe(zone):
    if not zone:
        return "NINGUNO"
    return (
        f"{zone['zone_id']} ({zone['role']}) "
        f"{zone['low']:.5f}-{zone['high']:.5f} "
        f"desde={zone['first_detected_at']}"
    )


def main():
    history = prepare_symbol_history(SYMBOL)
    times = (
        history["M15"]
        .loc[
            (history["M15"]["bar_close"] >= START)
            & (history["M15"]["bar_close"] < END),
            "bar_close",
        ]
        .tolist()
    )
    assert len(times) > 0, "No hay velas M15 para el dia solicitado"
    assert times == sorted(times), "Las velas no estan en orden cronologico"

    observed = {}  # IDs vistos: rol y primera deteccion
    new_ids = 0
    reobservations = 0
    role_changes = 0

    with TemporaryDirectory(prefix="level_registry_day_") as folder:
        registry = LevelRegistry(Path(folder) / "registry.json")
        for time in times:
            raw = detect_support_resistance(
                symbol=SYMBOL,
                analysis_time=time,
                prepared_history=history,
            )
            assert raw["status"] == "OK", (str(time), raw)
            result = registry.observe(raw)
            local_ids = set()

            for group in ("supports", "resistances", "active_zones"):
                for zone in result.get(group, []):
                    zid = zone["zone_id"]
                    assert zid not in local_ids, f"ID duplicado en {time}: {zid}"
                    local_ids.add(zid)
                    if zid not in observed:
                        assert zone["first_detected_at"] == time.isoformat(), (time, zone)
                        observed[zid] = (zone["role"], zone["first_detected_at"])
                        new_ids += 1
                    else:
                        previous_role, first_detected = observed[zid]
                        assert zone["first_detected_at"] == first_detected, zid
                        reobservations += 1
                        if previous_role != zone["role"]:
                            role_changes += 1
                        observed[zid] = (zone["role"], first_detected)

            if time.isoformat() in CHECK_TIMES:
                print(f"\n{time.isoformat()}")
                print("Soporte:    ", describe(result.get("nearest_support")))
                print("Resistencia:", describe(result.get("nearest_resistance")))

        assert registry.state["last_analysis_time"][SYMBOL] == times[-1].isoformat()
        assert reobservations > 0, "No se ha conservado ningun nivel"
        print("\nRESUMEN REPLAY REGISTRO")
        print("Velas M15 evaluadas:", len(times))
        print("Zonas distintas registradas:", new_ids)
        print("Reobservaciones con ID conservado:", reobservations)
        print("Cambios de rol observados:", role_changes)
        print("OK: registro cronologico del dia completo, sin Ollama")
        print("Registro temporal eliminado al terminar.")


if __name__ == "__main__":
    main()
