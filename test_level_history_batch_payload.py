"""Comprueba batch -> LEVEL_EVIDENCE -> LevelThesisAgent -> payload LLM.

No llama a Ollama, no abre operaciones y detiene el batch en la primera
comparacion. Se ejecuta desde la raiz de AgentTrading.
"""

from pathlib import Path
from unittest.mock import patch
import runpy

from src.agents.regime_agent import RegimeAgent
from src.orchestration.agent_bus import AgentBus
import src.agents.level_thesis_agent as thesis_module


class _PruebaCompletada(BaseException):
    pass


class _PruebaFallida(BaseException):
    pass


contexto_capturado = {}
publicar_original = AgentBus.publish


def registrar_publicacion(self, agent, message_type, payload):
    if message_type == "LEVEL_EVIDENCE":
        contexto_capturado["level_evidence"] = payload
        contexto_capturado["analysis_time"] = self.context.analysis_time
    return publicar_original(self, agent, message_type, payload)


def regimen_simulado(self, context, bus):
    return bus.publish(
        agent="regime_agent",
        message_type="REGIME_ASSESSMENT",
        payload={
            "regime": "TREND",
            "valid": True,
            "confidence": 0.8,
            "evidence_used": [],
            "source": "TEST_WITHOUT_LLM",
        },
    )


def comprobar_payload(schema, system_prompt, payload):
    if schema.__name__ != "LevelHypothesisComparison":
        raise _PruebaFallida(f"Modelo inesperado: {schema.__name__}")

    level_evidence = contexto_capturado.get("level_evidence", {})
    history = payload.get("level_history")
    if not isinstance(history, dict):
        raise _PruebaFallida(
            "El informe no ha llegado a comparison_payload['level_history']. "
            "Comprueba que attach_history() se ejecuta antes de bus.publish('LEVEL_EVIDENCE')."
        )

    role = payload.get("active_level")
    zone = level_evidence.get("facts", {}).get(str(role).lower(), {})
    analysis_time = contexto_capturado.get("analysis_time")

    checks = {
        "ID de zona": history.get("zone_id") == zone.get("zone_id") and bool(zone.get("zone_id")),
        "rol": history.get("current_role") == role,
        "fecha exacta": history.get("as_of") == analysis_time,
        "limite inferior": history.get("zone_low") == float(zone["low"]) if "low" in zone else False,
        "limite superior": history.get("zone_high") == float(zone["high"]) if "high" in zone else False,
        "estadisticas historicas": isinstance(history.get("counts"), dict),
        "lista de episodios": isinstance(history.get("recent_episodes"), list),
    }
    for name, ok in checks.items():
        if not ok:
            raise _PruebaFallida(f"Fallo en {name}: historia={history} / zona={zone}")

    print("\nOK: informe historico recibido por LevelHypothesisComparison")
    print("Hora:", analysis_time)
    print("Zona:", history["zone_id"])
    print("Funcion:", role)
    print("Precio:", history["zone_low"], "-", history["zone_high"])
    print("Episodios recientes:", len(history["recent_episodes"]))
    print("OK: zona, rol y fecha coinciden; Ollama no se ha ejecutado")
    raise _PruebaCompletada


def main():
    batch_file = Path(__file__).resolve().parent / "run_level_thesis_batch.py"
    if not batch_file.is_file():
        raise FileNotFoundError(f"No se encuentra {batch_file}")

    with (
        patch.object(AgentBus, "publish", registrar_publicacion),
        patch.object(RegimeAgent, "run", regimen_simulado),
        patch.object(thesis_module, "structured_chat", comprobar_payload),
    ):
        try:
            runpy.run_path(str(batch_file), run_name="__main__")
        except _PruebaCompletada:
            return
        except _PruebaFallida as exc:
            raise SystemExit(f"ERROR: {exc}") from None

    raise SystemExit(
        "ERROR: no se llego a LevelHypothesisComparison. "
        "Comprueba si existen candidatos de nivel con el filtro de sesiones actual."
    )


if __name__ == "__main__":
    main()
