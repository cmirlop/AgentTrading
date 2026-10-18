"""Registro persistente de identidad para zonas estructurales.

Primera fase: asigna IDs estables mientras los límites de la zona evolucionan.
No calcula fuerza, rechazos, rupturas ni decisiones de trading.

Uso futuro (siempre en orden cronológico)::

    registry = LevelRegistry("data/level_registry_usdcad.json")
    levels = registry.observe(levels)

En backtests usar una ruta NUEVA por ejecución y comenzar desde el instante
más antiguo. No reutilizar un registro construido con datos posteriores.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory


class LevelRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("version") != 1:
                raise ValueError("UNSUPPORTED_LEVEL_REGISTRY_VERSION")
        else:
            self.state = {
                "version": 1,
                "next_sequence": {},
                "last_analysis_time": {},
                "zones": [],
            }

    @staticmethod
    def _timestamp(value: str) -> datetime:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("ANALYSIS_TIME_MUST_HAVE_TIMEZONE")
        return timestamp.astimezone(timezone.utc)

    @staticmethod
    def _overlap_ratio(a: dict, b: dict) -> float:
        overlap = max(
            0.0,
            min(float(a["high"]), float(b["high"]))
            - max(float(a["low"]), float(b["low"])),
        )
        smaller_width = min(
            float(a["high"]) - float(a["low"]),
            float(b["high"]) - float(b["low"]),
        )
        return overlap / smaller_width if smaller_width > 0 else 0.0

    def _match(self, symbol: str, zone: dict, atr: float, used: set[str]):
        choices = []
        mid = (float(zone["low"]) + float(zone["high"])) / 2
        for previous in self.state["zones"]:
            if previous["symbol"] != symbol or previous["zone_id"] in used:
                continue
            overlap = self._overlap_ratio(zone, previous)
            previous_mid = (previous["low"] + previous["high"]) / 2
            midpoint_gap = abs(mid - previous_mid)
            if overlap >= 0.50 and midpoint_gap <= atr * 0.50:
                choices.append((overlap, -midpoint_gap, previous))
        return max(choices, key=lambda item: item[:2])[2] if choices else None

    def _next_id(self, symbol: str) -> str:
        sequence = self.state["next_sequence"].get(symbol, 1)
        self.state["next_sequence"][symbol] = sequence + 1
        # No codificamos SUPPORT/RESISTANCE en el ID: el rol puede invertirse.
        return f"{symbol}_Z{sequence:06d}"

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".level_registry_", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def observe(self, levels: dict) -> dict:
        """Asigna IDs y registra el snapshot. No modifica el dict original.

        Requiere salidas OK del detector en orden temporal. Si se intenta leer
        un momento anterior al último registrado, aborta para prevenir fuga
        de información posterior en un backtest.
        """
        if levels.get("status") != "OK":
            raise ValueError("LEVEL_DETECTOR_STATUS_NOT_OK")
        symbol = levels["symbol"]
        moment = self._timestamp(levels["analysis_time"])
        moment_iso = moment.isoformat()
        last_iso = self.state["last_analysis_time"].get(symbol)
        if last_iso is not None and moment < self._timestamp(last_iso):
            raise ValueError("NON_CHRONOLOGICAL_LEVEL_REGISTRY: use a fresh backtest registry")
        atr = float(levels["reference_atr_m15"])
        if atr <= 0:
            raise ValueError("REFERENCE_ATR_MUST_BE_POSITIVE")

        output = deepcopy(levels)
        used = set()
        assigned = {}
        for group in ("supports", "resistances", "active_zones"):
            for zone in output.get(group, []):
                if not float(zone["low"]) < float(zone["high"]):
                    raise ValueError("INVALID_ZONE_INTERVAL")
                previous = self._match(symbol, zone, atr, used)
                if previous is None:
                    previous = {
                        "zone_id": self._next_id(symbol),
                        "symbol": symbol,
                        "first_detected_at": moment_iso,
                        "initial_role": zone["role"],
                        "origin_timeframes": list(zone.get("timeframes", [])),
                    }
                    self.state["zones"].append(previous)
                previous.update({
                    "low": float(zone["low"]),
                    "high": float(zone["high"]),
                    "last_seen_at": moment_iso,
                    "last_role": zone["role"],
                    "current_timeframes": list(zone.get("timeframes", [])),
                })
                zone["zone_id"] = previous["zone_id"]
                zone["first_detected_at"] = previous["first_detected_at"]
                used.add(previous["zone_id"])
                assigned[(zone["role"], zone["low"], zone["high"])] = previous

        # Misma identidad también en nearest_support/nearest_resistance.
        for group in ("nearest_support", "nearest_resistance"):
            zone = output.get(group)
            if zone is None:
                continue
            previous = assigned.get((zone["role"], zone["low"], zone["high"]))
            if previous is not None:
                zone["zone_id"] = previous["zone_id"]
                zone["first_detected_at"] = previous["first_detected_at"]

        self.state["last_analysis_time"][symbol] = moment_iso
        self._save()
        return output


def _self_test():
    # Esta prueba no necesita datos Forex, Ollama ni dependencias externas.
    def snapshot(moment: str, zone: dict) -> dict:
        zone = dict(zone)
        if zone["role"] == "SUPPORT":
            supports, resistances = [zone], []
        else:
            supports, resistances = [], [zone]
        return {
            "status": "OK", "symbol": "USDCAD", "analysis_time": moment,
            "reference_atr_m15": 0.001,
            "supports": supports, "resistances": resistances,
            "active_zones": [],
            "nearest_support": zone if supports else None,
            "nearest_resistance": zone if resistances else None,
        }

    with TemporaryDirectory() as directory:
        filename = Path(directory) / "levels.json"
        registry = LevelRegistry(filename)
        start = snapshot("2026-04-08T08:00:00+00:00", {
            "role": "SUPPORT", "low": 1.38309, "high": 1.38368,
            "timeframes": ["H1"],
        })
        first = registry.observe(start)
        first_id = first["nearest_support"]["zone_id"]
        assert first["nearest_support"]["first_detected_at"] == "2026-04-08T08:00:00+00:00"
        assert "zone_id" not in start["nearest_support"]  # Entrada intacta.

        # Reinicio del proceso, desplazamiento de límites y cambio de rol.
        registry = LevelRegistry(filename)
        changed = registry.observe(snapshot("2026-04-08T09:00:00+00:00", {
            "role": "RESISTANCE", "low": 1.38315, "high": 1.38373,
            "timeframes": ["H1", "H4"],
        }))
        assert changed["nearest_resistance"]["zone_id"] == first_id
        assert changed["nearest_resistance"]["first_detected_at"] == "2026-04-08T08:00:00+00:00"
        assert registry.state["zones"][0]["origin_timeframes"] == ["H1"]

        # Un nivel distante recibe otro ID.
        distant = registry.observe(snapshot("2026-04-08T10:00:00+00:00", {
            "role": "RESISTANCE", "low": 1.38643, "high": 1.38708,
            "timeframes": ["H1", "H4", "D1"],
        }))
        assert distant["nearest_resistance"]["zone_id"] != first_id

        # Reutilizar un registro del futuro en el pasado debe fallar.
        try:
            registry.observe(start)
        except ValueError as error:
            assert "NON_CHRONOLOGICAL" in str(error)
        else:
            raise AssertionError("El registro acepto informacion futura")
        assert filename.is_file()
        print("OK: identidad estable, reinicio, cambio de rol, nueva zona y proteccion temporal")
        print("ID conservado:", first_id)


if __name__ == "__main__":
    _self_test()
