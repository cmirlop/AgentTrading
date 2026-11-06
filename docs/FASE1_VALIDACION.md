# AgentTrading — Validación de Fase 1

Última actualización local: 5 de noviembre de 2026. Este documento refleja la validación en Windows del commit `d099610f03eeeef0576aa541187a635bbcb4d9f0`, rama `codex/phase1-local-closure`. El estado y las evidencias detalladas están en [`reports/phase1_validation.json`](../reports/phase1_validation.json) y [`docs/FASE1_RESULTADO_FINAL.md`](FASE1_RESULTADO_FINAL.md).

## Estado

La Fase 1 está **`INCOMPLETE_PENDING_MANUAL`**. Las pruebas offline, la seguridad DRY RUN, Ollama y la repetibilidad de los backtests pasaron. MT5 no confirmó una cuenta DEMO; la evidencia DeepSeek anterior corresponde a Python y Pydantic distintos; y las diferencias contra los informes históricos agregados no se pueden atribuir operación por operación porque esos informes no incluyen snapshots de operaciones.

No se inicia la Fase 2 ni se habilita trading real. No se invocó `mt5.order_send()` ni se abrió, cerró o modificó ninguna operación.

## Entorno de validación

- Sistema: Windows 10, build 19045.
- Python: `.venv\Scripts\python.exe`, CPython 3.13.14 de 64 bits.
- Dependencias registradas: MetaTrader5 5.0.6231, pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, Ollama SDK 0.6.3, OpenAI SDK 3.41.0 y python-dotenv 1.2.4.
- La librería MetaTrader5 y el terminal están disponibles localmente. La sesión está conectada, pero la cuenta no se pudo verificar como DEMO.
- Ollama 0.40.6 está instalado y el modelo configurado `qwen3:4b` está instalado.

## Pruebas offline y riesgo

La suite `python -m unittest discover -s tests -p "test_phase1_*.py"` ejecutó **85 pruebas: 84 PASS y una omisión POSIX específica**. Las pruebas de procesos `spawn` recorrieron el lock real de Windows con `msvcrt`; cubren concurrencia, reservas persistidas, token de propietario, caducidad, reinicio, cambio de día UTC, límites diarios y exposición agregada USD. Los casos de riesgo y deals MT5 que no podían ejecutarse contra una cuenta DEMO se probaron con fixtures, sin presentar esos fixtures como integración real.

`python test_no_lookahead.py` completó **375 comprobaciones PASS**. Las 21 pruebas offline históricas ejecutadas por el validador también pasaron. `test_ollama.py`, `test_order_request.py` y `test_trade_planner.py` permanecen identificadas como pruebas de integración separadas; Ollama se validó mediante su prueba live específica y las dos pruebas MT5 requieren una cuenta DEMO verificable.

La comprobación de seguridad encontró cero rutas de `order_send` en el código del proyecto. El flujo principal exige `tradeapi_disabled`; el validador MT5 se limita a lecturas de cuenta/terminal antes de la barrera DEMO y a `order_calc_profit()` hipotético si llega a superar las precondiciones.

## MT5

`scripts/validate_phase1_mt5.py` devolvió `PENDING` con código `ACCOUNT_NOT_VERIFIABLY_DEMO`. Se detuvo tras consultar `account_info()` y `terminal_info()`. No leyó símbolos, ticks, posiciones, órdenes ni historial, y no calculó riesgo de cuenta real. En la comprobación previa de solo lectura el terminal informó `tradeapi_disabled=True`.

El detalle, cuando existe, se guarda bajo `reports/private/`, excluido de Git. [`reports/phase1_mt5_validation.json`](../reports/phase1_mt5_validation.json) contiene únicamente el resumen seguro: estado, motivo, huellas de procedencia, cero órdenes y `real_execution_authorized=false`. Los informes previos fueron archivados antes de reemplazar el resumen.

La evaluación diaria MT5 participa ahora en el estado global. Permitir propuestas y bloquearlas por alcanzar el límite son resultados válidos del control; datos faltantes o errores de cálculo hacen fallar la evaluación. La integración real de metadatos, conversión de divisas, `order_calc_profit()`, deals, exposición y límite diario permanece pendiente hasta verificar DEMO.

## Ollama y DeepSeek

La validación live de Ollama terminó **PASS** con `qwen3:4b`: modelo configurado y utilizado coincidentes, respuesta JSON válida, Pydantic PASS, evidencia limitada a `LIVE_PROBE` y guardas de esquema/evidencia inválidos PASS. El primer intento agotó el timeout; después se inició la instalación existente y la prueba final pasó con una solicitud local. No se cambió el modelo y no se creó señal operativa.

La evidencia live previa de DeepSeek informó **PASS** con `deepseek-flash`, una solicitud y sin reintentos. La huella de archivo del probe se comprobó contra el blob equivalente del merge, de modo que el SHA o la rama por sí solos no la invalidan. Sin embargo, aquella prueba utilizó Python 3.12.14 y Pydantic 2.14.2; este equipo ejecuta Python 3.13.14 y Pydantic 2.13.5. Por ello la prueba actual queda **PENDING**. No se realizó ninguna llamada facturable nueva.

## Backtests y regresión histórica

Los ocho escenarios portfolio/stateful —BASE dos veces por modo, CONSERVATIVE y STRESS— terminaron PASS. Las dos repeticiones BASE coinciden en métricas agregadas y en comparación de operaciones con ID estable. El dataset congelado usado fue el blob de `data/backtest/technical_candidate_scan.csv` en el commit `1ef0faf7600b866236f75cb7c229fc8a3b66973e`, verificado por SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3` (22.011 filas). Se ejecutó desde una copia temporal; el CSV local, cuyo hash es distinto, no se reemplazó.

La comparación detectó un cambio de fuente en `src/historical_execution.py`, registrado entre los hashes `03022d66350fddc5f6a8d0d83da5745893bed96155fad71224635563245d584a` y `0a9f36b496429f0ce577a237e9f6ffab36a0dd7d29f5fe7d8b9fa3caa7d70531`. El cambio añade validación de barras y planes, corrige el borde de expiración, diferencia órdenes pendientes al fin de datos y calcula costes para fills ambiguos. La huella lo clasifica como corrección documentada del simulador; las oportunidades técnicas no variaron.

Los cotejos agregados contra baseline/pre-cierre contienen 111, 256, 175 y 492 diferencias respectivamente. No se consideran esperadas automáticamente: los snapshots anteriores no contienen IDs estables por operación, así que no es posible asignar cada cambio de entrada, estado, salida o coste a la corrección. Estos cotejos se mantienen PENDING para revisión explícita. El informe completo registra cada diferencia y su campo.

Las tablas económicas comparables por escenario, con las hipótesis de coste claramente separadas de tarifas reales, están en [`docs/FASE1_RESULTADO_FINAL.md`](FASE1_RESULTADO_FINAL.md); la matriz completa por umbral/modo está en el JSON de validación.

## Ejecución manual pendiente

Conectar manualmente el terminal MT5 a una cuenta DEMO, mantener deshabilitado el trading por API Python y ejecutar:

```powershell
python scripts/validate_phase1_mt5.py
```

No se debe pegar ninguna clave en el chat. Para actualizar DeepSeek será necesaria autorización explícita de una llamada API; cargar la clave únicamente desde el almacén seguro ya configurado y usar:

```powershell
python scripts/validate_phase1_llm_live.py --provider deepseek --authorize-deepseek
```

Cuando se disponga de logs de operaciones del replay anterior o de una reproducción de la versión anterior sobre el mismo dataset, revisar las diferencias antes de pasar la regresión histórica a PASS. Después ejecutar `python scripts/validate_phase1.py` para refrescar el estado global. La Fase 2 no debe comenzar mientras cualquiera de estos puntos siga pendiente.
