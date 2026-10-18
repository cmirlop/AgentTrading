"""Puente causal entre registro de niveles, historial y payload para Qwen.

Fase de integración sin Ollama. El registro se alimenta con TODOS los
cierres M15 disponibles, incluso si el batch decide evaluar solo cuatro
horarios. Así first_detected_at no se desplaza artificialmente al primer
candidato que supera los filtros de sesión.

IMPORTANTE: usar un registro NUEVO en cada replay histórico; no reusar
uno poblado previamente con observaciones posteriores.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pandas as pd

from src.level_registry import LevelRegistry
from src.support_resistance import detect_support_resistance
from src.level_history import analyze_zone_events
from src.level_event_context import add_break_contexts
from src.level_history_report import build_level_history_report
from src.level_history_llm import build_level_history_llm_payload


def _utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError("ANALYSIS_TIME_REQUIRES_TIMEZONE")
    return ts.tz_convert("UTC")


class HistoricalLevelMemory:
    """Registro por cierres M15 + informe de la zona ACTIVA.

    Usar `get_registered_levels()` con horas no decrecientes.
    Después construir `build_level_evidence(levels)` normalmente;
    `attach_history()` añade el historial solo para AT_SUPPORT/AT_RESISTANCE.
    """

    def __init__(
        self,
        symbol: str,
        prepared_history: dict,
        registry_path: str | Path,
        replay_start,
    ) -> None:
        self.symbol = symbol
        self.history = prepared_history
        self.registry = LevelRegistry(registry_path)
        self.replay_start = _utc(replay_start)
        m15 = self.history["M15"]
        self.times = sorted({
            _utc(item)
            for item in m15.loc[
                m15["bar_close"] >= self.replay_start,
                "bar_close",
            ].tolist()
        })
        self.position = 0
        self.snapshots = {}
        self.last_requested = None

    def get_registered_levels(self, analysis_time) -> dict:
        """Avanza desde replay_start sin saltarse velas M15 intermedias."""
        moment = _utc(analysis_time)
        if moment < self.replay_start:
            raise ValueError("ANALYSIS_BEFORE_REPLAY_START")
        if self.last_requested is not None and moment < self.last_requested:
            raise ValueError("NON_CHRONOLOGICAL_HISTORY_REQUEST")
        self.last_requested = moment

        while self.position < len(self.times) and self.times[self.position] <= moment:
            close_time = self.times[self.position]
            levels = detect_support_resistance(
                symbol=self.symbol,
                analysis_time=close_time,
                prepared_history=self.history,
            )
            if levels.get("status") != "OK":
                raise ValueError(f"LEVEL_DETECTOR_NOT_OK_AT_{close_time.isoformat()}")
            self.snapshots[close_time] = self.registry.observe(levels)
            self.position += 1

        if moment not in self.snapshots:
            raise ValueError("ANALYSIS_NOT_AN_M15_CLOSE")
        return deepcopy(self.snapshots[moment])

    def attach_history(self, level_evidence: dict, analysis_time) -> dict:
        """Anexa JSON compacto sin cambiar IDs/umbrales de evidencia."""
        output = deepcopy(level_evidence)
        if output.get("status") != "OK":
            return output

        structure = output.get("market_structure")
        role = {
            "AT_SUPPORT": "SUPPORT",
            "AT_RESISTANCE": "RESISTANCE",
        }.get(structure)
        if role is None:
            return output

        moment = _utc(analysis_time)
        zone = output.get("facts", {}).get(role.lower())
        if not isinstance(zone, dict) or not zone.get("zone_id"):
            raise ValueError("ACTIVE_LEVEL_WITHOUT_REGISTERED_ZONE_ID")
        if not zone.get("first_detected_at"):
            raise ValueError("ACTIVE_LEVEL_WITHOUT_FIRST_DETECTED_AT")

        episodes = analyze_zone_events(zone, self.history["M15"], moment)
        enriched = add_break_contexts(episodes, self.history)
        report = build_level_history_report(zone, enriched)
        compact = build_level_history_llm_payload(report)

        if compact["as_of"] != moment.isoformat():
            raise ValueError("HISTORY_TIMESTAMP_MISMATCH")
        if compact["zone_id"] != zone["zone_id"]:
            raise ValueError("HISTORY_ZONE_ID_MISMATCH")
        if compact["current_role"] != role:
            raise ValueError("HISTORY_ROLE_MISMATCH")
        if (compact["zone_low"], compact["zone_high"]) != (float(zone["low"]), float(zone["high"])):
            raise ValueError("HISTORY_BOUNDS_MISMATCH")

        output["level_history_llm"] = compact
        return output


def _self_test() -> None:
    from tempfile import TemporaryDirectory
    from src.historical_state import prepare_symbol_history
    from src.level_evidence import build_level_evidence

    start = pd.Timestamp("2026-04-08T00:00:00Z")
    end = pd.Timestamp("2026-04-08T18:00:00Z")
    history = prepare_symbol_history("USDCAD")
    times = history["M15"].loc[
        history["M15"]["bar_close"].between(start, end), "bar_close"
    ].tolist()

    with TemporaryDirectory(prefix="level_history_bridge_test_") as temp:
        bridge = HistoricalLevelMemory(
            symbol="USDCAD", prepared_history=history,
            registry_path=Path(temp) / "registry.json", replay_start=start,
        )
        selected = None
        for t in times:
            levels = bridge.get_registered_levels(t)
            evidence = build_level_evidence(levels)
            if evidence["market_structure"] in ("AT_SUPPORT", "AT_RESISTANCE"):
                result = bridge.attach_history(evidence, t)
                payload = result["level_history_llm"]
                role = payload["current_role"]
                assert payload["zone_id"] == result["facts"][role.lower()]["zone_id"]
                assert payload["as_of"] == _utc(t).isoformat()
                selected = (t, role, payload["zone_id"])
                break
        assert selected is not None, "NO_ACTIVE_LEVEL_FOR_BRIDGE_TEST"
        print("OK: puente historico con nivel real y sin Ollama")
        print("Instante:", _utc(selected[0]).isoformat())
        print("Nivel:", selected[1], selected[2])
        print("OK: zona, rol y temporalidad coinciden; registro temporal eliminado")


if __name__ == "__main__":
    _self_test()
