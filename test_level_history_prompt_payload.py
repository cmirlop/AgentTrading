"""Verifica memoria historica Y sus instrucciones en el prompt sin usar Ollama.

Reutiliza el test de integracion del batch; detiene la ejecucion en cuanto
LevelHypothesisComparison recibe el primer payload.
"""

import json
import test_level_history_batch_payload as prueba_integracion


_comprobar_payload_original = prueba_integracion.comprobar_payload


def comprobar_prompt_y_memoria(schema, system_prompt, payload):
    if schema.__name__ != "LevelHypothesisComparison":
        raise prueba_integracion._PruebaFallida(
            f"Esquema inesperado: {schema.__name__}"
        )

    history = payload.get("level_history")
    if not isinstance(history, dict):
        raise prueba_integracion._PruebaFallida(
            "No llega la memoria historica al payload"
        )

    instructions = {
        "bloque de memoria": "HISTORICAL LEVEL MEMORY:",
        "historico retrospectivo": "retrospective_before_detection",
        "desde primera deteccion": "observed_since_detection",
        "contexto al primer contacto": "market_at_first_contact",
        "contexto de confirmacion": "market_at_break_confirmation",
        "IDs de evidencia autorizados": "allowed_evidence_ids",
        "comparacion HOLD/BREAK": "HOLD versus BREAK",
    }
    for label, fragment in instructions.items():
        if fragment not in system_prompt:
            raise prueba_integracion._PruebaFallida(
                f"Falta instruccion '{label}' en comparison_prompt: {fragment!r}"
            )

    counts = history.get("counts", {})
    if not all(
        isinstance(counts.get(key), dict)
        for key in ("retrospective_before_detection", "observed_since_detection")
    ):
        raise prueba_integracion._PruebaFallida(
            "La memoria no separa los periodos anteriores y posteriores"
        )

    for key in ("recent_episodes", "historical_break_examples"):
        if not isinstance(history.get(key), list):
            raise prueba_integracion._PruebaFallida(
                f"Campo ausente o incorrecto: {key}"
            )

    if not isinstance(payload.get("allowed_evidence_ids"), list):
        raise prueba_integracion._PruebaFallida(
            "Falta la lista de evidencias actuales autorizadas"
        )

    history_chars = len(json.dumps(history, ensure_ascii=False))
    print("[PROMPT CHECK] Instrucciones de historia: 7/7 presentes")
    print("[PROMPT CHECK] Caracteres del prompt:", len(system_prompt))
    print("[PROMPT CHECK] Caracteres de la memoria:", history_chars)
    print("[PROMPT CHECK] Rupturas en memoria:", len(history["historical_break_examples"]))
    print("[PROMPT CHECK] No se ha llamado a Ollama")

    # Conserva todas las comprobaciones previas de zona, rol y fecha.
    return _comprobar_payload_original(schema, system_prompt, payload)


prueba_integracion.comprobar_payload = comprobar_prompt_y_memoria

if __name__ == "__main__":
    prueba_integracion.main()
