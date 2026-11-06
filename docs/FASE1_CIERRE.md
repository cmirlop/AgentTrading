# Cierre local de Fase 1 — AgentTrading

**Estado: `INCOMPLETE_PENDING_MANUAL`.** La validación offline y la ejecución simulada pasaron; faltan una sesión MT5 DEMO verificable, una prueba DeepSeek con las dependencias Python actuales y la atribución de diferencias históricas agregadas.

La revisión se hizo en Windows 10 build 19045 con Python 3.13.14, en la rama `codex/phase1-local-closure` y sobre el commit `d099610f03eeeef0576aa541187a635bbcb4d9f0`. No se creó commit, no se hizo push ni merge. La Fase 2 no debe comenzar todavía.

## Resultado de las comprobaciones

| Comprobación | Estado | Evidencia |
|---|---|---|
| Suite `test_phase1_*.py` | PASS | 85 pruebas; 84 PASS y una omisión específica POSIX. Locks y procesos concurrentes se probaron en Windows. |
| 21 pruebas offline históricas | PASS | Ejecutadas por `scripts/validate_phase1.py`; ninguna falló. |
| Look-ahead | PASS | `test_no_lookahead.py`: 375 checks. |
| Riesgo diario y exposición USD | PASS (lógica) / PENDING (MT5 real) | Casos offline, procesos, persistencia y deals mock PASS; lectura de cuenta DEMO no ejecutada. |
| Seguridad DRY RUN | PASS | Cero llamadas `order_send` en el proyecto; no hubo órdenes ni señales operativas. |
| MT5 demo | PENDING | `ACCOUNT_NOT_VERIFIABLY_DEMO`; la validación se detuvo antes de leer mercado, posiciones, órdenes o historial. |
| Ollama live | PASS | `qwen3:4b`; JSON/Pydantic/evidencia válidos; guardas de respuesta inválida PASS. |
| DeepSeek live | PENDING | El PASS anterior se verificó por contenido y código, pero usó Python 3.12.14/Pydantic 2.14.2 frente a 3.13.14/2.13.5 actuales. No hubo llamada nueva. |
| Ocho escenarios de backtest | PASS | Portfolio y stateful BASE×2, CONSERVATIVE y STRESS sobre dataset congelado verificado. |
| Repetibilidad BASE | PASS | Igualdad de agregados y operaciones con identificadores estables en ambos modos. |
| Regresión contra métricas anteriores | PENDING | Las diferencias agregadas no se presumieron esperadas; faltan snapshots históricos por operación para atribuirlas. |

## Correcciones incluidas

1. Los informes MT5 detallados se guardan en `reports/private/`, excluido de Git. El informe público contiene estado, motivo seguro y hashes de procedencia, no balance, equity, servidor ni número de cuenta. Los informes existentes se archivaron antes de generar el resumen nuevo.
2. La reutilización de evidencia compara el contenido del código, configuración, dependencias, runtime y dataset; la rama o el SHA de merge no invalidan un resultado si los hashes pertinentes coinciden. Las reutilizaciones quedan registradas.
3. `daily_loss_limit_evaluation` forma parte del estado global. Tanto permitir propuestas como bloquear por alcanzar el límite son comportamientos válidos; la falta de datos o un error de cálculo hace fallar la comprobación.
4. Las regresiones históricas separan cambios técnicos, correcciones de simulador registradas y diferencias económicas no atribuibles. La corrección de `src/historical_execution.py` está asociada a hashes antes/después. Como los informes de origen son agregados, sus cambios económicos siguen PENDING.

## Comparativa económica

Los siguientes cortes mantienen fijo el umbral/modo `65 / LIMIT`; no son una selección para optimizar estadísticas. El spread histórico bid/ask ya está incorporado. Los costes BASE de comisión/slippage son cero porque no hay tarifas reales verificadas; CONSERVATIVE y STRESS son hipótesis de sensibilidad, no cotizaciones del bróker.

### Portfolio `65 / LIMIT`

| Escenario | Oportunidades | Señales | Planes aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | DD neto EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

### Stateful `65 / LIMIT`

Cada fila corresponde a un par. La matriz completa por umbral/modo está en `reports/phase1_validation.json`.

| Escenario | Par | Oportunidades | Señales/planes aceptados | Rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | DD neto EUR |
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

## Revisión histórica

El dataset congelado `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3` se recuperó del commit `1ef0faf7600b866236f75cb7c229fc8a3b66973e` y se verificó antes de usarlo. La copia se creó en un directorio temporal; el archivo de trabajo local no se modificó.

Las oportunidades técnicas se mantuvieron invariantes. El validador detectó una corrección documentada del simulador: validación estricta de barras/planes, manejo del borde de expiración, estado pendiente al fin del dataset y costes para operaciones ambiguas con fill. Sin embargo, los informes anteriores no incluyen operaciones con IDs estables. Las diferencias agregadas frente a baseline/pre-cierre son 111, 256, 175 y 492; permanecen sin atribución individual y no se marcan como PASS.

## Intervención manual

- Conectar MT5 manualmente a una cuenta DEMO. El script no cambiará de cuenta. Mantener deshabilitado el trading vía API Python y volver a ejecutar `python scripts/validate_phase1_mt5.py`.
- Autorizar expresamente una única llamada DeepSeek en el futuro si se quiere actualizar esa evidencia. Cargar la clave desde un almacén seguro; no pegarla en el chat.
- Revisar la regresión agregada con snapshots por operación de la versión anterior y la actual sobre el mismo dataset, o reproducir la versión anterior para obtenerlos.

La evidencia, estados exactos, huellas y diferencias están en [`docs/FASE1_RESULTADO_FINAL.md`](FASE1_RESULTADO_FINAL.md) y [`reports/phase1_validation.json`](../reports/phase1_validation.json). No comenzar Fase 2 mientras MT5, DeepSeek o la revisión histórica sigan pendientes.
