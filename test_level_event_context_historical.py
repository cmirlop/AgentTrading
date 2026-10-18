"""Prueba real de contexto técnico en rupturas históricas de USDCAD.

Usa la zona D1 más cercana a 2026-04-08 18:00 UTC, reconstruyendo su
registro cronológicamente desde las 00:00. No usa Ollama ni ejecuta órdenes.

El contexto se calcula en la CONFIRMACIÓN de cada ruptura, no en el primer
contacto; por tanto es descriptivo, NO una señal predictiva de entrada.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from src.historical_state import prepare_symbol_history
from src.level_event_context import add_break_contexts
from src.level_history import analyze_zone_events
from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance

SYMBOL = "USDCAD"
START = pd.Timestamp("2026-04-08T00:00:00Z")
END = pd.Timestamp("2026-04-08T18:00:00Z")
CONTACT_TO_INSPECT = pd.Timestamp("2026-04-08T02:15:00Z")
BREAK_TO_INSPECT = pd.Timestamp("2026-04-08T02:45:00Z")


def main() -> None:
    history = prepare_symbol_history(SYMBOL)
    m15 = history["M15"]
    times = m15.loc[
        (m15["bar_close"] >= START) & (m15["bar_close"] <= END),
        "bar_close",
    ].tolist()
    assert len(times) == 73, f"Esperados 73 cierres M15; hay {len(times)}"
    assert (m15["bar_close"] > END).any(), "Sin velas futuras para prueba de corte"

    with TemporaryDirectory(prefix="level_break_context_20260408_") as tmp:
        registry = LevelRegistry(Path(tmp) / "registry.json")
        current = None
        for instant in times:
            detected = detect_support_resistance(
                symbol=SYMBOL,
                analysis_time=instant,
                prepared_history=history,
            )
            assert detected["status"] == "OK", (instant, detected)
            current = registry.observe(detected)

        zone = current["nearest_resistance"]
        assert zone is not None, "No se detecto resistencia al cierre"
        assert "D1" in zone["timeframes"], zone

        original_report = analyze_zone_events(zone, m15, END)
        enriched = add_break_contexts(original_report, history)

        # Los contextos no deben modificar el informe original.
        assert all(
            "context_at_break_confirmation" not in ev
            for ev in original_report["events"]
        )

        breaks = [ev for ev in enriched["events"] if ev["outcome"] == "BREAK"]
        assert len(breaks) == (
            enriched["before_detection"]["confirmed_breaks"]
            + enriched["since_detection"]["confirmed_breaks"]
        )
        assert all("context_at_break_confirmation" in ev for ev in breaks)
        assert all(
            "context_at_break_confirmation" not in ev
            for ev in enriched["events"] if ev["outcome"] != "BREAK"
        )

        # Cada TF debe contener una vela cerrada no posterior a la ruptura.
        for ev in breaks:
            resolved = pd.Timestamp(ev["resolved_at"])
            contexts = ev["context_at_break_confirmation"]["timeframes"]
            for tf, snap in contexts.items():
                if snap["available"]:
                    assert pd.Timestamp(snap["bar_close"]) <= resolved, (ev, tf)

        # Independencia del futuro: quitar todas las velas posteriores a END
        # no debe cambiar las cifras ni los contextos ya conocidos.
        past_only = {
            tf: df.loc[df["bar_close"] <= END].copy()
            for tf, df in history.items()
        }
        assert enriched == add_break_contexts(original_report, past_only), (
            "El contexto cambia cuando eliminamos velas posteriores al analisis"
        )

        selected = [
            ev for ev in breaks
            if pd.Timestamp(ev["contact_at"]) == CONTACT_TO_INSPECT
            and pd.Timestamp(ev["resolved_at"]) == BREAK_TO_INSPECT
        ]
        assert len(selected) == 1, (
            "No se encontro la ruptura esperada del 8 de abril", selected,
        )
        event = selected[0]
        details = event["context_at_break_confirmation"]["timeframes"]

        print(f"USDCAD | {zone['zone_id']} | {zone['low']:.5f}-{zone['high']:.5f}")
        print("Analisis hasta:", END.isoformat())
        print("Rupturas historicas enriquecidas:", len(breaks))
        print("Episodio seleccionado:", event["contact_at"], "->", event["resolved_at"])
        print("Papel historico al llegar:", event["role_at_contact"])
        for tf in ("M15", "H1", "H4", "D1"):
            snap = details[tf]
            print(
                f"{tf}: cierre={snap['bar_close']} "
                f"EMA={snap.get('ema_structure')} "
                f"RSI={snap.get('rsi14')} "
                f"retorno5_pct={snap.get('return5_pct')} "
                f"ATR_relativo={snap.get('atr_vs_recent_mean')}"
            )
        print("OK: contexto historico real confirmado, solo velas cerradas y sin futuro")
        print("Registro temporal eliminado al terminar")


if __name__ == "__main__":
    main()
