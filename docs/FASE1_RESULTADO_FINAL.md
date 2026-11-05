# Resultado final de validación — Fase 1

**ESTADO FINAL FASE 1: `INCOMPLETE_PENDING_MANUAL`**

La validación automatizada no encontró fallos offline ni fallos de seguridad, pero faltan comprobaciones obligatorias. MT5 no acreditó cuenta DEMO, la evidencia DeepSeek no corresponde a las versiones Python/Pydantic instaladas y las diferencias históricas agregadas no pueden atribuirse por operación con los informes antiguos disponibles. No se declara `PHASE1_COMPLETE` ni se recomienda comenzar Fase 2.

## A. Entorno y trazabilidad

- Sistema: Windows 10 build 19045.
- Intérprete: `.venv\Scripts\python.exe`, CPython 3.13.14, 64 bits.
- Repositorio local: carpeta AgentTrading del entorno Windows.
- Commit comprobado: `d099610f03eeeef0576aa541187a635bbcb4d9f0`.
- Rama de trabajo: `codex/phase1-local-closure`.
- No se creó commit ni se hizo push o merge. Los cambios permanecen locales.
- Versiones instaladas: MetaTrader5 5.0.6231, pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, Ollama SDK 0.6.3, OpenAI SDK 3.41.0, python-dotenv 1.2.4.
- MT5: módulo y terminal disponibles; sesión conectada; cuenta no verificable como DEMO. La lectura de terminal indicó `tradeapi_disabled=True`.
- Ollama: servicio 0.40.6 instalado, modelo configurado e instalado `qwen3:4b`; su prueba live pasó.
- DeepSeek: el informe anterior de `deepseek-flash` se comprobó por hashes de código/configuración; no se realizó una nueva llamada.

### Archivos actualizados

- `.gitignore`
- `scripts/validate_phase1.py`
- `scripts/validate_phase1_mt5.py`
- `scripts/validate_phase1_llm_live.py`
- `tests/test_phase1_mt5_safety.py`
- `tests/test_phase1_validation_provenance.py`
- `docs/FASE1_VALIDACION.md`
- `docs/FASE1_CIERRE.md`
- `docs/FASE1_RESULTADO_FINAL.md`
- `reports/phase1_validation.json`
- `reports/phase1_mt5_validation.json` (resumen anonimizado)
- `reports/phase1_llm_live_ollama.json`

Los detalles MT5 se escriben bajo `reports/private/`, carpeta excluida de Git. Los informes anteriores se archivaron antes de actualizar sus destinos. Se preservaron los `.pyc` que ya estaban modificados localmente antes de esta revisión.

## B. Resultado por comprobación

| Comprobación | Estado | Evidencia |
|---|---|---|
| Suite Phase 1 | PASS | 86 pruebas en la ejecución final: 85 PASS y una omisión POSIX específica. |
| Pruebas offline anteriores | PASS | 21/21 completadas; no se ejecutaron como live las pruebas MT5 reservadas a DEMO. |
| Bloqueos/concurrencia Windows | PASS | `spawn` con procesos reales en Windows y ruta `msvcrt`; persistencia, reserva, token, caducidad, reinicio, cambio UTC y concurrencia cubiertos. |
| Riesgo por operación y exposición USD | PASS (offline) / PENDING (MT5 real) | Sizing, conversiones, límites y exposición cruzada pasaron con datos de prueba; faltan metadatos reales de cuenta DEMO. |
| Límite diario MT5 | PASS (lógica) / PENDING (historial real) | Permitir, bloquear por límite y fallar ante datos inválidos están diferenciados; no se leyó historial real por la barrera DEMO. |
| Integridad de historial | PASS (fixtures) / PENDING (historial DEMO) | Clasificación UTC y exclusión de depósitos/retiradas pasaron en pruebas offline. |
| Privacidad de informes MT5 | PASS | Resumen público whitelist, sin balance/equity/login/servidor; detalle privado ignorado por Git; texto sensible redactado. |
| MT5 demo | PENDING | `ACCOUNT_NOT_VERIFIABLY_DEMO`; no se ejecutaron lecturas operativas ni cálculos por símbolo. |
| Ollama live | PASS | `qwen3:4b`, respuesta JSON y Pydantic válidos, modelo exacto, evidencia `LIVE_PROBE`, guardas de respuestas inválidas PASS, sin señal operativa. |
| DeepSeek live | PENDING | El código del probe coincide por hash a través del merge; difieren `python_version` y `dependencies.pydantic`. La evidencia vieja no se reutilizó y no se hizo llamada facturable. |
| Backtests | PASS (ejecución) / PENDING (regresión agregada) | Ocho escenarios PASS sobre el dataset congelado; comparaciones económicas históricas quedan pendientes de atribución. |
| Reproducibilidad BASE | PASS | Portfolio y stateful BASE repetidos; métricas y operaciones con IDs estables idénticas. |
| Look-ahead | PASS | `test_no_lookahead.py`: 375 checks. |
| DRY RUN y seguridad | PASS | Escaneo sin llamadas `order_send`, `real_execution_authorized=false`, cero operaciones enviadas. |
| Estado global | **INCOMPLETE_PENDING_MANUAL** | Validaciones obligatorias externas e históricas pendientes; el código de salida documentado del validador para PENDING es 2. |

### MT5: motivo y límite de las pruebas

El script consultó `account_info()` y `terminal_info()` únicamente para aplicar la condición obligatoria de seguridad. Al no confirmar DEMO, devolvió `PENDING` con `ACCOUNT_NOT_VERIFIABLY_DEMO` y terminó antes de consultar EURUSD, USDCAD o AUDUSD, posiciones, órdenes, deals, balance/equity, o `order_calc_profit()`. No cambió de cuenta ni modificó permisos.

Por tanto, el riesgo monetario con tick value/contract size del bróker, la exposición agregada real, el límite diario calculado desde deals reales y el control de historial MT5 no se consideran PASS en esta ejecución.

### Ollama y DeepSeek

La instalación existente de Ollama se inició para probar el modelo configurado; el primer intento expiró y la validación posterior pasó con `qwen3:4b` sin sustituirlo. La prueba produjo una respuesta JSON válida y verificó Pydantic, IDs permitidos y rechazo de esquema/evidencia inválidos. No generó ninguna señal.

El PASS DeepSeek anterior usó Python 3.12.14 y Pydantic 2.14.2; este entorno usa Python 3.13.14 y Pydantic 2.13.5. Aunque los hashes de código/configuración pertinentes coinciden y el SHA/branch de merge no se usa como motivo de invalidez, los cambios de runtime y Pydantic impiden reutilizar el PASS. No se pidió ni se hizo una nueva llamada API.

## C. Comparativa de backtests

Se ejecutaron portfolio BASE dos veces, CONSERVATIVE y STRESS, y stateful BASE dos veces, CONSERVATIVE y STRESS. El dataset congelado se recuperó como blob Git del commit `1ef0faf7600b866236f75cb7c229fc8a3b66973e`, SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`, 22.011 filas. Se validó el hash antes de escribir una copia temporal. El archivo local del workspace, con SHA `341e8f12595a9e74bac2faeb739b8e7b1bdaa1ccdcbd35b26457354f686b31e0`, quedó intacto.

Las tablas siguientes fijan `65 / LIMIT` como corte comparativo común, no como optimización. El JSON contiene la matriz completa por umbral/modo.

#### Portfolio

| Escenario | Oportunidades | Señales | Planes aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | Drawdown neto EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

#### Stateful por par

| Escenario | Par | Oportunidades | Señales/aceptados | Rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | Drawdown neto EUR |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | EURUSD | 3.271 | 710 | 124 | 277 / 251 | 26 | 15,05 | 0 / 0 | 15,05 | 1,174 | 0,060 | 13,59 |
| BASE | USDCAD | 3.651 | 729 | 87 | 275 / 257 | 18 | 20,93 | 0 / 0 | 20,93 | 1,382 | 0,081 | 7,34 |
| BASE | AUDUSD | 3.362 | 685 | 114 | 264 / 245 | 19 | -14,89 | 0 / 0 | -14,89 | 0,823 | -0,061 | 24,11 |
| CONSERVATIVE | EURUSD | 3.271 | 710 | 124 | 277 / 251 | 26 | 15,05 | 21,91 / 24,18 | -6,86 | 0,936 | -0,027 | 23,28 |
| CONSERVATIVE | USDCAD | 3.651 | 728 | 87 | 274 / 256 | 18 | 21,42 | 21,10 / 22,58 | 0,32 | 1,010 | 0,001 | 10,89 |
| CONSERVATIVE | AUDUSD | 3.362 | 685 | 114 | 264 / 245 | 19 | -14,89 | 21,39 / 23,05 | -36,28 | 0,644 | -0,148 | 40,17 |
| STRESS | EURUSD | 3.271 | 712 | 127 | 279 / 253 | 26 | 13,57 | 38,43 / 42,38 | -24,86 | 0,788 | -0,098 | 34,86 |
| STRESS | USDCAD | 3.651 | 736 | 90 | 277 / 259 | 18 | 19,01 | 35,55 / 38,02 | -16,54 | 0,795 | -0,064 | 21,01 |
| STRESS | AUDUSD | 3.362 | 686 | 114 | 265 / 246 | 19 | -18,51 | 37,37 / 40,26 | -55,88 | 0,527 | -0,227 | 56,63 |

BASE, CONSERVATIVE y STRESS se mantienen tal como salen, incluidos los resultados negativos tras costes. El spread bid/ask histórico ya forma parte de los fills. Las comisiones/slippage de BASE no tienen tarifa verificada; los costes CONSERVATIVE/STRESS son supuestos de sensibilidad, no tarifas de bróker.

#### Diferencias históricas y causa

Las oportunidades técnicas se mantuvieron invariantes. Una diferencia de fuente está registrada como corrección documentada de simulador en `src/historical_execution.py`, con hashes anterior y actual. Añade validación estricta de barras/planes, tratamiento del borde de expiración, estado `PENDING_AT_END` y costes para fills ambiguos. No modifica el código de selección de señales.

Los cotejos agregados baseline/pre-cierre contienen 111, 256, 175 y 492 diferencias. Los informes anteriores carecen de snapshots por operación; por ello no se afirma que cada diferencia provenga de la corrección. Todas conservan causa no atribuida y estado PENDING, y requieren revisión explícita antes de pasar la regresión a PASS.

## D. Conclusión y pasos manuales

Se corrigieron privacidad de informes, procedencia tras merge, evaluación global del límite diario y clasificación estricta de regresiones. Se ejecutaron las pruebas offline, la detección look-ahead, las validaciones Ollama/MT5 y los backtests descritos. No se optimizaron prompts, umbrales, pesos ni selección técnica.

Para desbloquear la Fase 1:

1. Conectar manualmente MT5 a una cuenta DEMO; no cambiar de cuenta de forma automática. Mantener deshabilitado el trading por API Python y ejecutar `python scripts/validate_phase1_mt5.py`. El script solo avanzará a las lecturas de mercado/historial si acredita DEMO.
2. Autorizar expresamente una llamada live nueva a DeepSeek para `deepseek-flash`, porque Python/Pydantic difieren del entorno de la evidencia anterior. Cargar la clave desde el almacén seguro configurado; no compartirla en el chat.
3. Revisar las diferencias históricas con snapshots por operación de la versión anterior y actual en el mismo dataset, o reproducir la versión anterior para generarlos.
4. Ejecutar de nuevo `python scripts/validate_phase1.py`. El validador devuelve código 2 si hay PENDING, 1 ante FAIL y 0 solo para `PHASE1_COMPLETE`.

Hasta que esos puntos pasen, **no comenzar Fase 2**. No se enviaron órdenes y el sistema permanece en DRY RUN.
