# Smoke remoto DeepSeek — resultados

**Fecha UTC:** 2026-11-11 · **Estado:** `DEEPSEEK_REMOTE_LIVE_PASS`
**Alcance:** una solicitud diagnóstica, sin datos de mercado ni cuenta, en la rama `codex/phase1-deepseek-remote-live-smoke`, basada en `fc4f9da0f3c42a461f55993adf47bd1269237339`.

## Resultado

Se realizó exactamente **1 intento** a `https://api.deepseek.com`, ruta Chat Completions, con el alias solicitado y reportado `deepseek-flash`. La respuesta terminó con `finish_reason=stop`, fue JSON válido, pasó `ProbeAnswer` de Pydantic y usó únicamente el ID permitido `LIVE_PROBE`.

El probe no contenía precios, recomendación, señal ni plan operativo. No se ejecutaron `run_cycle.py`, pipelines, backtests ni consultas a Ollama. No hubo conexiones a MT5 ni operaciones.

## Presupuesto y coste estimado

La documentación oficial de DeepSeek se consultó el `2026-11-11T15:46:44Z`. El informe conserva URLs, alias, modelo de servicio documentado, divisa, tiers de tarifa y valores usados. Se calculó el coste con los precios pico publicados y entrada sin caché: `0.30 USD/1M` tokens de entrada y `1.20 USD/1M` tokens de salida.

La cota de entrada contó bytes UTF-8 de ambos mensajes completos, incluido el esquema JSON generado, usando cuatro tokens por byte más 64 tokens de framing: **3.024 tokens**. Con salida limitada a 256 tokens, el coste preflight máximo fue **0.0012144 USD**, inferior al límite autorizado de 0.25 USD. La configuración fue `max_retries=0`, timeout de 30 segundos, `temperature=0` y thinking desactivado.

DeepSeek informó 203 tokens de entrada y 17 de salida. Aplicando las tarifas pico, el coste estimado es **0.000081300 USD**. Es una estimación de tarifas públicas, no una conciliación contra la factura del proveedor. El modelo no reportó tokens de razonamiento por separado.

## Pruebas y trazabilidad

Antes del intento y de nuevo dentro del comando del smoke se ejecutaron cinco módulos offline: **72 PASS, 0 FAIL, 0 SKIPPED**. Cubren presupuesto, una única petición y ausencia de reintentos, respuestas malformadas, evidencia inventada, respuesta incompleta, timeout, 401/404/429, esquema, ledger y sanitización del informe.

El registro de la llamada quedó en un SQLite privado bajo `reports/private/phase1_deepseek_remote/`, excluido de Git. El resumen valida una oportunidad diagnóstica inelegible, una consulta planeada/enviada, una respuesta válida, cero oportunidades elegibles, cero decisiones del sistema y cero órdenes registradas. No guarda el secreto en el informe público; no publica prompt ni respuesta bruta.

El informe público [`../reports/phase1_deepseek_remote/validation_summary.json`](../reports/phase1_deepseek_remote/validation_summary.json) incluye SHA del dataset congelado, hashes de fuentes, runtime, rama, commit base, hashes de prompt/payload, resultado, tokens, coste estimado y contadores de seguridad. El dataset mantuvo su SHA-256 esperado `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.

## Cambios técnicos

- `src/agents/deepseek_client.py`: callback opcional de conteo de intento inmediatamente antes de la petición; metadatos incluyen `finish_reason`.
- `src/agents/llm_client.py`: propaga el callback y metadato sin alterar el comportamiento de llamadas existentes.
- `src/deepseek_remote_smoke.py`: payload ficticio, cota de tokens, tarifas verificadas y barrera de presupuesto de una petición.
- `scripts/validate_deepseek_remote_smoke.py`: wrapper con pruebas previas, permiso explícito, ledger privado aislado y reporte público sanitizado.
- `tests/test_deepseek_remote_smoke.py`: pruebas mock del presupuesto, proveedor, respuesta, evidencia, errores y ledger.
- `docs/FASE1_CIERRE.md`: añade el resultado y mantiene los pendientes de Fase 1.

## Comandos de reproducción

La suite offline puede repetirse sin consumir API:

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "tests"
python -m unittest -v test_deepseek_remote_smoke test_phase1_llm_live test_phase1_llm test_phase1_validation_provenance test_phase2_2
```

El comando live ya se ejecutó y consumió la única petición autorizada:

```powershell
python scripts/validate_deepseek_remote_smoke.py --authorize-single-request
```

**No volver a ejecutar el comando live** bajo esta autorización. Una futura validación requeriría nueva autorización explícita y un nuevo preflight de precios/limites.

## Límites y pendientes

La Fase 1 permanece `INCOMPLETE_PENDING_MANUAL`. El smoke se hizo en CPython `3.12.14`, Pydantic `2.14.2` y OpenAI SDK `3.41.0`; no valida Windows/Python `3.13.14`, el terminal MT5 ni el proveedor local Ollama. Tampoco mide precisión, rentabilidad, robustez estadística ni valor añadido de DeepSeek. La validación histórica y sus diferencias no atribuibles siguen pendientes; no se alteraron estrategia, prompts productivos, límites de riesgo ni dataset congelado.

No se hizo commit, push o merge. El informe de validación del probe se generó sobre la rama aislada; la base `main` no se modificó.
