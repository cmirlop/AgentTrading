"""Prueba real del payload historico para Qwen, sin Ollama ni operaciones.

Reconstruye el registro de USDCAD cronologicamente hasta el 8 abril 2026
18:00 UTC y comprueba que el resumen no cambia al retirar las velas futuras.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.historical_state import prepare_symbol_history
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance
from src.level_history import analyze_zone_events
from src.level_event_context import add_break_contexts
from src.level_history_report import build_level_history_report
from src.level_history_llm import build_level_history_llm_payload


SYMBOL = "USDCAD"
START = pd.Timestamp("2026-04-08T00:00:00Z")
END = pd.Timestamp("2026-04-08T18:00:00Z")


def make_payload(zone, history):
    episodes = analyze_zone_events(zone, history["M15"], END)
    episodes = add_break_contexts(episodes, history)
    report = build_level_history_report(zone, episodes)
    return report, build_level_history_llm_payload(report)


def main():
    history = prepare_symbol_history(SYMBOL)
    m15 = history["M15"]
    times = m15.loc[m15["bar_close"].between(START, END), "bar_close"].tolist()
    assert len(times) == 73, f"Se esperaban 73 cierres M15: {len(times)}"
    assert (m15["bar_close"] > END).any(), "Faltan velas futuras para comprobar el corte"

    past_only = {
        tf: df.loc[df["bar_close"] <= END].copy()
        for tf, df in history.items()
    }

    with TemporaryDirectory(prefix="level_llm_historical_") as directory:
        registry = LevelRegistry(Path(directory) / "registry.json")
        observed = None
        for moment in times:
            levels = detect_support_resistance(
                symbol=SYMBOL,
                analysis_time=moment,
                prepared_history=history,
            )
            assert levels["status"] == "OK", (moment, levels)
            observed = registry.observe(levels)

        print("USDCAD | payload historico para Qwen hasta", END.isoformat())
        for label, key in (("SOPORTE", "nearest_support"), ("RESISTENCIA", "nearest_resistance")):
            zone = observed[key]
            assert zone is not None, label

            report, payload = make_payload(zone, history)
            report_cut, payload_cut = make_payload(zone, past_only)
            assert report == report_cut, f"El informe {label} utiliza informacion posterior"
            assert payload == payload_cut, f"El payload {label} utiliza informacion posterior"
            assert payload["zone_id"] == zone["zone_id"]
            assert payload["current_role"] == zone["role"]
            assert payload["as_of"] == END.isoformat()
            assert len(payload["recent_episodes"]) <= 3
            assert len(payload["historical_break_examples"]) <= 2
            for event in payload["recent_episodes"] + payload["historical_break_examples"]:
                assert pd.Timestamp(event["contact_at"]) <= END
                if event["resolved_at"]:
                    assert pd.Timestamp(event["resolved_at"]) <= END

            counts = payload["counts"]
            before = counts["retrospective_before_detection"]
            since = counts["observed_since_detection"]
            n_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            print(f"\n{label}: {payload['zone_id']} ({payload['current_role']})")
            print("Zona:", f"{payload['zone_low']:.5f}-{payload['zone_high']:.5f}")
            print("Origen:", payload["origin_timeframes"])
            print("Antes deteccion:", f"contactos={before['contacts']}", f"rechazos={before['rejections']}", f"rupturas={before['breaks']}")
            print("Desde deteccion:", f"contactos={since['contacts']}", f"rechazos={since['rejections']}", f"rupturas={since['breaks']}")
            print("Tamano JSON compacto:", n_chars, "caracteres")
            print("Episodios recientes:", len(payload["recent_episodes"]))
            print("Ejemplos ruptura:", len(payload["historical_break_examples"]))
            for event in payload["historical_break_examples"]:
                contact = event.get("market_at_first_contact") or {}
                confirmation = event.get("market_at_break_confirmation") or {}
                print(
                    "  ruptura:", event["contact_at"], "->", event["resolved_at"],
                    "RSI_M15_contacto=", (contact.get("M15") or {}).get("rsi14"),
                    "RSI_M15_confirmacion=", (confirmation.get("M15") or {}).get("rsi14"),
                )
            print("OK: datos reales compactados sin informacion futura")

    print("\nOK: resumenes historicos reales listos para integrar, sin Ollama")
    print("Registro temporal eliminado al terminar.")


if __name__ == "__main__":
    main()
