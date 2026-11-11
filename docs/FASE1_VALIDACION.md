# AgentTrading — Validación de Fase 1

**Estado al 11 de noviembre de 2026: `INCOMPLETE_PENDING_MANUAL`.** La validación local terminó las pruebas offline, look-ahead, backtests reproducibles, MT5 real en modo solo lectura y Ollama. No se declara `PHASE1_COMPLETE`: falta evidencia DeepSeek vigente en el runtime local y quedan diferencias de selección histórica sin testigo suficiente.

No se inició la Fase 2 ni se habilitó trading. En la comprobación MT5 se verificó `tradeapi_disabled=True`; el proceso envió cero órdenes y ejecutó cero operaciones. No se llamó a `order_send()` ni a `order_check()`.

## Entorno y procedencia

- Windows 10, build 19045; CPython 3.13.14 x64 desde `.venv\Scripts\python.exe`.
- Commit base comprobado: `c6fb7de6ee58cfdb12552b7062dbd188f771a169`; rama `codex/phase1-final-local-real-readonly-closure`. Los cambios de esta revisión siguen sin commit.
- Dependencias relevantes: MetaTrader5 5.0.6231, pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, Ollama SDK 0.6.3, OpenAI SDK 3.41.0 y python-dotenv 1.2.4.
- Terminal MT5 y módulo Python accesibles. Se verificó una cuenta REAL con el bloqueo de trading externo activo. No hay cuenta DEMO disponible; la validación DEMO original no se considera superada.
- Ollama está instalado con `qwen3:4b`; se usó el servidor local existente para una prueba y se detuvo al terminar.
- DeepSeek conserva evidencia remota del 11/11, pero en CPython 3.12.14/Pydantic 2.14.2. No se hizo una nueva llamada facturable.
- Dataset congelado de los backtests: SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`, 22.011 filas, período 2026-04-07 a 2026-10-07 UTC. Se recuperó desde el blob Git y se ejecutó desde una copia temporal. El CSV local no se sobrescribió.

La procedencia y el resumen global están en [`reports/phase1_validation.json`](../reports/phase1_validation.json) y [`reports/phase1_final_local/validation_summary.json`](../reports/phase1_final_local/validation_summary.json). El resumen público de la prueba MT5 no contiene número de cuenta, servidor, balance ni equity.

## Pruebas ejecutadas

| Comprobación | Estado | Evidencia |
|---|---|---|
| Suite `test_phase1_*.py` | PASS | 94 pruebas; 93 pasaron y una prueba específica de fallo de lock POSIX se omitió en Windows. Los procesos concurrentes sí recorrieron el lock de Windows con `msvcrt`. |
| Pruebas offline existentes | PASS | 21/21 comprobaciones del validador. |
| Look-ahead | PASS | `test_no_lookahead.py`: 375 comprobaciones. |
| MT5 REAL, solo lectura | PASS | `tradeapi_disabled=True`; EURUSD, USDCAD y AUDUSD; metadatos, cálculo hipotético de riesgo, exposición y control diario. Cuenta DEMO: no aplicable/no disponible. |
| Historial MT5 | PASS | 15 archivos (3 pares × M5, M15, H1, H4, D1), 7.998 velas descargadas a un snapshot nuevo, orden y formato validados; actualización 11/11/2026 20:23Z. |
| Ollama live | PASS | Una solicitud a `qwen3:4b`; JSON/Pydantic válidos, evidencia permitida y guardas de respuesta inválida. Sin señal operativa. |
| DeepSeek live local | PENDING | No se repitió la llamada. La evidencia remota reciente usa otro Python/Pydantic; necesita autorización explícita para una nueva llamada facturable en este runtime. |
| Backtests | PASS | Ocho ejecuciones portfolio/stateful: BASE dos veces por modo, CONSERVATIVE y STRESS; BASE reproducible por métricas e IDs estables. |
| Atribución histórica | PENDING | Los planes emparejados no cambian; los estados de ejecución atribuibles a expiración se explican. Permanecen selecciones sin testigo directo y comparaciones agregadas históricas no atribuibles. |
| Seguridad DRY RUN | PASS | El proceso de validación no envió órdenes ni ejecutó operaciones. |

La suite también comprobó concurrencia multiproceso en Windows, persistencia y caducidad de reservas, reinicio de proceso y día UTC, límites de pérdida y exposición USD. Una omisión POSIX se refiere a una inyección de fallo específica de `fcntl`; no sustituye las pruebas reales de `msvcrt` en Windows.

## MT5 REAL de solo lectura

La comprobación independiente [`reports/mt5_real_readonly_summary.json`](../reports/mt5_real_readonly_summary.json) acabó `MT5_REAL_READ_ONLY_PASS`. Tipo de cuenta informado: REAL; `tradeapi_disabled=true`. Se leyeron únicamente datos permitidos y se calcularon operaciones hipotéticas. `order_send` y `order_check` no se usaron. El proceso registró cero órdenes y cero operaciones ejecutadas.

Los tres símbolos pasaron metadatos y evaluación de riesgo. El spread observado durante la última consulta fue EURUSD 9, USDCAD 9 y AUDUSD 9 puntos. Es una observación puntual, no una garantía de coste ejecutable. El cálculo de riesgo y la evaluación diaria del historial MT5 pasaron en la cuenta REAL, pero esto no transforma la validación DEMO original en PASS.

La actualización del 11/11 guardó 15 archivos separados bajo `data/market_history/mt5_real_readonly/20261111T202300.097058Z/` con 7.998 velas cerradas descargadas y un máximo de 991 filas por archivo. Es un snapshot nuevo de las últimas velas disponibles, no una unión con el histórico local anterior; no sobrescribe ni modifica el CSV congelado de backtest. El manifiesto SHA-256 está en ese directorio y su hash es `1b671bd07e91160434fdd9e2a1c7b3c2c6346f36710e3d05bf218bc1a7d3d56e`; el resumen está en [`reports/mt5_history_refresh_summary.json`](../reports/mt5_history_refresh_summary.json). Las últimas velas cerradas comunes fueron M5 20:15Z, M15 20:00Z, H1 19:00Z, H4 16:00Z del 11/11 y D1 00:00Z del 10/11. La comprobación directa encontró cero duplicados, cero marcas fuera de orden y cero filas con OHLC/spread/tick-volume inválidos. Sin embargo, 6.516 de 7.998 filas tienen spread cero; el spread histórico no permite estimar costes de forma fiable en esas filas. Se excluyeron velas abiertas; los huecos detectados corresponden a cierres de mercado y la calidad de cada serie pasó.

La prueba solicitada fue de lectura segura sobre la cuenta REAL. No equivale a una prueba DEMO del validador Fase 1.

## Ollama y DeepSeek

La prueba local de Ollama está en [`reports/phase1_final_local/ollama_live_20261111T1900Z.json`](../reports/phase1_final_local/ollama_live_20261111T1900Z.json). El modelo configurado y el informado por el proveedor coincidieron (`qwen3:4b`); respuesta JSON, esquema Pydantic, evidencia `LIVE_PROBE` y guardas negativas pasaron. Un ledger temporal aislado registró una respuesta de diagnóstico, cero oportunidades elegibles y cero órdenes.

El resumen remoto existente de DeepSeek informa PASS para `deepseek-flash`, una petición y sin reintentos, en CPython 3.12.14/Pydantic 2.14.2. La máquina local ejecuta CPython 3.13.14/Pydantic 2.13.5; por ello, la evidencia no cubre este runtime. No se ejecutó otra llamada. Estado local: PENDING.

## Regresión histórica

La comparación usó operaciones identificadas de forma estable por SHA-256 de símbolo, umbral, modo y hora de señal. No detectó cambios de dirección, puntuación ni campos del plan para IDs emparejados. La corrección del simulador cambia la clasificación en la barra de expiración; sus efectos directos se registraron por estado de ejecución. Los cambios de fuente en los dos backtests se clasificaron mediante huellas verificadas como cambios de auditoría/configuración no señal y la corrección documentada de `src/historical_execution.py`.

La comparación directa de snapshots BASE registró:

| Modo | IDs coincidentes | Solo versión anterior | Solo versión actual | Diferencias de plan | Selecciones con causa directa de cascada | Selecciones aún pendientes |
|---|---:|---:|---:|---:|---:|---:|
| Portfolio | 7.628 | 87 | 187 | 0 | 115 | 159 |
| Stateful | 8.926 | 48 | 165 | 0 | 136 | 77 |

Entre los IDs emparejados, las transiciones documentadas corresponden a resultados que pasaron a `AMBIGUOUS_EXPIRY_BAR`; no se atribuyeron automáticamente las selecciones restantes. El dataset antiguo de métricas agregadas carece de snapshots por operación, así que esas diferencias siguen PENDING. Detalle privado por operación: `reports/private/phase1_final_local/historical_regression_attribution_detail.json` (excluido de Git). Resumen seguro: [`reports/phase1_final_local/historical_regression_attribution.json`](../reports/phase1_final_local/historical_regression_attribution.json).

## Costes y límites de los backtests

Los resultados siguientes fijan `65 / LIMIT` para una comparación común. BASE supone comisión y slippage cero, valores no verificados con tarifas del bróker. CONSERVATIVE y STRESS son escenarios hipotéticos de sensibilidad, no cotizaciones. El spread histórico se usa, aunque los valores cero observados limitan su fiabilidad. No se ajustaron estrategia, pesos, prompts ni umbrales.

### Portfolio

| Escenario | Oportunidades | Señales | Planes aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | DD neto EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

### Stateful por instrumento

| Escenario | Par | Oportunidades | Señales | Aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | DD neto EUR |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | EURUSD | 3.271 | 710 | 710 / 124 | 277 / 251 | 26 | 15,05 | 0 / 0 | 15,05 | 1,174 | 0,060 | 13,59 |
| BASE | USDCAD | 3.651 | 729 | 729 / 87 | 275 / 257 | 18 | 20,93 | 0 / 0 | 20,93 | 1,382 | 0,081 | 7,34 |
| BASE | AUDUSD | 3.362 | 685 | 685 / 114 | 264 / 245 | 19 | -14,89 | 0 / 0 | -14,89 | 0,823 | -0,061 | 24,11 |
| CONSERVATIVE | EURUSD | 3.271 | 710 | 710 / 124 | 277 / 251 | 26 | 15,05 | 21,91 / 24,18 | -6,86 | 0,936 | -0,027 | 23,28 |
| CONSERVATIVE | USDCAD | 3.651 | 728 | 728 / 87 | 274 / 256 | 18 | 21,42 | 21,10 / 22,58 | 0,32 | 1,010 | 0,001 | 10,89 |
| CONSERVATIVE | AUDUSD | 3.362 | 685 | 685 / 114 | 264 / 245 | 19 | -14,89 | 21,39 / 23,05 | -36,28 | 0,644 | -0,148 | 40,17 |
| STRESS | EURUSD | 3.271 | 712 | 712 / 127 | 279 / 253 | 26 | 13,57 | 38,43 / 42,38 | -24,86 | 0,788 | -0,098 | 34,86 |
| STRESS | USDCAD | 3.651 | 736 | 736 / 90 | 277 / 259 | 18 | 19,01 | 35,55 / 38,02 | -16,54 | 0,795 | -0,064 | 21,01 |
| STRESS | AUDUSD | 3.362 | 686 | 686 / 114 | 265 / 246 | 19 | -18,51 | 37,37 / 40,26 | -55,88 | 0,527 | -0,227 | 56,63 |

Los resultados negativos después de costes se conservan tal cual.

## Pendiente y decisión

1. Para cerrar las comprobaciones restantes de MT5 Fase 1, se requiere una cuenta DEMO y ejecutar `python scripts/validate_phase1_mt5.py` con trading vía API externa deshabilitado. La cuenta REAL no debe cambiarse automáticamente ni usarse para enviar órdenes.
2. Autorizar expresamente una llamada DeepSeek live nueva para el runtime local; cargar la clave solo desde el almacén seguro configurado.
3. Resolver o revisar explícitamente las selecciones históricas sin testigo directo. Los resultados agregados antiguos no bastan para justificar cada diferencia.
4. Verificar tarifas de comisión/slippage si se desean costes BASE comparables al bróker; los escenarios actuales no representan tarifas verificadas.

El validador distingue PENDING de FAIL: código 2 para `INCOMPLETE_PENDING_MANUAL`, código 1 ante FAIL y código 0 solo para `PHASE1_COMPLETE`. **La Fase 1 no está completa; no comenzar la Fase 2 todavía.**
