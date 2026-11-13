# AgentTrading — Validación de Fase 1

**Estado al 13 de noviembre de 2026: INCOMPLETE_PENDING_MANUAL.** La validación integral separa correctamente los fallos de las comprobaciones pendientes. El informe global indica código de salida 2. No declarar PHASE1_COMPLETE ni iniciar la Fase 2.

La suite integral anterior terminó PASS antes de los últimos cambios de privacidad. La revalidación global posterior no produjo salida y fue interrumpida; por ello su resultado queda PENDING. Tras esos cambios sí finalizaron 44 pruebas focalizadas: procedencia (25), cliente live LLM en modo offline (7) y MT5 solo lectura (12), todas PASS. El JSON conserva por separado la evidencia integral previa y esta última tentativa.

## Entorno y procedencia

- Windows 10, build 19045; CPython 3.13.14 x64 desde el entorno virtual .venv.
- Commit base de la ejecución: 957bf6613ecdabd38a101db1471d1285917831ec.
- Rama: codex/phase1-autonomous-closure-20261113.
- El informe guarda hashes del código, configuración, dependencias y dataset. Las fuentes de backtest no cambiaron durante esta validación; la rama local contenía cambios de cierre al ejecutarla.
- Dataset congelado verificado desde Git: SHA-256 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3, 22.011 filas, del 7 de abril al 7 de octubre de 2026 UTC. El CSV de trabajo tiene otro SHA y no se usó ni modificó.
- Dependencias: MetaTrader5 5.0.6231, pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, Ollama SDK 0.6.3, OpenAI SDK 3.41.0 y python-dotenv 1.2.4.
- MT5 REAL se comprobó en modo de solo lectura; la cuenta DEMO no está disponible.
- Ollama está instalado y el modelo configurado es qwen3:4b. El servicio o el listado de modelos no respondió a la prueba actual.
- DeepSeek live se validó en este runtime con una única solicitud autorizada.

La evidencia agregada está en [reports/phase1_validation.json](../reports/phase1_validation.json). Los detalles financieros y trazas de cuenta están en reports/private, excluidos de Git.

## Resultados

| Comprobación | Estado | Evidencia |
|---|---|---|
| Suite Phase 1 | PENDING en la revalidación final | La ejecución integral anterior pasó con 98 pruebas (97 PASS, 1 omisión POSIX); la última tentativa no terminó. Las 44 pruebas focalizadas tras los cambios de privacidad sí pasaron. |
| Pruebas offline existentes | PASS en la ejecución integral anterior | 21/21; no se completó otra pasada integral después de la sanitización de informes. |
| Look-ahead | PASS en la ejecución integral anterior | 375 comprobaciones; el script no cambió en la última edición. |
| Concurrencia y reservas en Windows | PASS en la ejecución integral anterior | Multiproceso, bloqueos msvcrt, persistencia, caducidad y reinicios cubiertos por la suite previa; la revalidación integral final quedó pendiente. |
| MT5 REAL, solo lectura | PASS | tradeapi_disabled=true; datos, riesgo hipotético, exposición y control diario para EURUSD, USDCAD y AUDUSD. Cero órdenes y cero operaciones del proceso. |
| MT5 DEMO original | PENDING / no disponible | No existe cuenta DEMO local. La cuenta REAL no se etiqueta como DEMO. |
| Históricos MT5 | NOT_RUN en esta ejecución | Se conserva el snapshot del 11/11/2026; no se descargaron nuevas velas el 13/11. |
| Ollama live | PENDING | Servicio/listado no disponible (ConnectionError), 0 llamadas. |
| DeepSeek live | PASS | deepseek-flash; una solicitud, sin reintentos; JSON, Pydantic y evidencia permitida válidos. |
| Backtests | PASS | Ocho ejecuciones portfolio/stateful. BASE repetido dos veces por modo con métricas e IDs reproducibles. |
| Atribución de selecciones 1ef → 957 | PASS | 487 cambios de selección explicados; 0 diferencias técnicas de señal/plan y 0 pendientes de atribución. |
| Regresión contra agregados anteriores | PENDING | Faltan snapshots estables por operación para justificar todos los deltas de los informes agregados antiguos. |
| DRY RUN | PASS | No se habilitó trading ni se enviaron órdenes. |

## MT5 REAL y datos de mercado

El resumen público [reports/mt5_real_readonly_summary.json](../reports/mt5_real_readonly_summary.json) quedó MT5_REAL_READ_ONLY_PASS. El terminal confirmó cuenta REAL y tradeapi_disabled=true. EURUSD, USDCAD y AUDUSD pasaron metadatos, cálculo hipotético de riesgo, exposición agregada e historial diario.

Spreads puntuales observados: EURUSD 8 puntos, USDCAD 10 y AUDUSD 9. No son una garantía de coste futuro. El proceso leyó cero posiciones abiertas y cero órdenes pendientes al momento de la consulta, envió cero órdenes y ejecutó cero operaciones. No llamó a order_send ni order_check.

La prueba DEMO original sigue sin ejecutarse porque no hay una cuenta DEMO disponible. La prueba REAL de solo lectura no se presenta como DEMO.

El resumen de históricos de esta sesión indica NOT_RUN. Se conserva el snapshot previo de 15 archivos para tres pares y cinco timeframes, generado el 11/11/2026; no se sobrescribió el dataset congelado de backtest ni se actualizaron históricos en esta sesión.

## Ollama y DeepSeek

La prueba actual de Ollama está en [reports/phase1_final_local/ollama_live_current_runtime.json](../reports/phase1_final_local/ollama_live_current_runtime.json). qwen3:4b sigue configurado, pero la conexión o listado de modelos devolvió ConnectionError antes de generar una respuesta; calls_made=0. En el intento local anterior, el servidor devolvió HTTP 500 por acceso denegado al directorio de manifiestos. Se detuvo el proceso temporal. No se cambiaron permisos, configuración ni instalación.

El resumen DeepSeek [reports/phase1_llm_live_deepseek.json](../reports/phase1_llm_live_deepseek.json) tiene PASS en Python 3.13.14/Pydantic 2.13.5. Usó exactamente deepseek-flash, una solicitud y cero reintentos. La respuesta JSON pasó Pydantic y utilizó evidencia permitida; no produjo ninguna señal operativa. El coste estimado por uso fue USD 0,000078; el límite preventivo de la prueba fue USD 0,25. No se necesita otra llamada para este cierre.

## Regresión histórica

Las trazas completas privadas se generaron con el dataset congelado y las fuentes de los commits 1ef0faf7600b866236f75cb7c229fc8a3b66973e y 957bf6613ecdabd38a101db1471d1285917831ec. Los IDs estables cubren 56.474 oportunidades por lado y modo; no faltan trazas de operaciones. Se observaron cero cambios técnicos de señal o de campos de plan.

Se identificaron 274 diferencias de selección portfolio y 213 stateful. Las 487 se enlazaron a correcciones documentadas de expiración/estado final, incluidas las cascadas de capacidad; quedan 0 selecciones sin causa en esa comparación. El detalle se conserva en reports/private/phase1_final_closure; el resumen seguro está en [historical_causal_attribution_summary.json](../reports/phase1_final_local/historical_causal_attribution_summary.json).

La comparación adicional contra el baseline agregado antiguo y el informe de prefinalización permanece PENDING. Hay métricas agregadas que difieren (111 portfolio y 256 stateful contra baseline; 175 portfolio y 492 stateful contra prefinalización), pero los informes históricos no contienen snapshots estables suficientes para atribuir individualmente cada delta económico. Las trazas 1ef → 957 justifican los cambios de selección, pero no se usan como sustituto de evidencia para todos esos agregados. No se marca PASS por inferencia.

## Backtests económicos

Corte comparable: umbral 65 / LIMIT. BASE no aplica costes de comisión/slippage verificados; CONSERVATIVE y STRESS son hipótesis de sensibilidad. Los resultados negativos se mantienen sin ajustes.

### Portfolio

| Escenario | Oportunidades | Señales | Planes aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa EUR | DD EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

### Stateful por instrumento

| Escenario | Par | Señales | Aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa EUR | DD EUR |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | EURUSD | 710 | 710 / 124 | 277 / 251 | 26 | 15,05 | 0 / 0 | 15,05 | 1,174 | 0,060 | 13,59 |
| BASE | USDCAD | 729 | 729 / 87 | 275 / 257 | 18 | 20,93 | 0 / 0 | 20,93 | 1,382 | 0,081 | 7,34 |
| BASE | AUDUSD | 685 | 685 / 114 | 264 / 245 | 19 | -14,89 | 0 / 0 | -14,89 | 0,823 | -0,061 | 24,11 |
| CONSERVATIVE | EURUSD | 710 | 710 / 124 | 277 / 251 | 26 | 15,05 | 21,91 / 24,18 | -6,86 | 0,936 | -0,027 | 23,28 |
| CONSERVATIVE | USDCAD | 728 | 728 / 87 | 274 / 256 | 18 | 21,42 | 21,10 / 22,58 | 0,32 | 1,010 | 0,001 | 10,89 |
| CONSERVATIVE | AUDUSD | 685 | 685 / 114 | 264 / 245 | 19 | -14,89 | 21,39 / 23,05 | -36,28 | 0,644 | -0,148 | 40,17 |
| STRESS | EURUSD | 712 | 712 / 127 | 279 / 253 | 26 | 13,57 | 38,43 / 42,38 | -24,86 | 0,788 | -0,098 | 34,86 |
| STRESS | USDCAD | 736 | 736 / 90 | 277 / 259 | 18 | 19,01 | 35,55 / 38,02 | -16,54 | 0,795 | -0,064 | 21,01 |
| STRESS | AUDUSD | 686 | 686 / 114 | 265 / 246 | 19 | -18,51 | 37,37 / 40,26 | -55,88 | 0,527 | -0,227 | 56,63 |

## Pendiente y decisión

1. Ollama: iniciar o reparar manualmente el servicio con acceso de lectura/escritura correcto a su directorio de modelos, comprobar que qwen3:4b aparece en la lista y ejecutar de nuevo el validador Ollama. No se requiere descargar ni sustituir el modelo.
2. Regresión: aportar snapshots por operación del baseline antiguo o revisar y aceptar explícitamente los deltas agregados pendientes. Hasta entonces, la regresión global no pasa.
3. MT5 DEMO: no hay cuenta DEMO; el resultado REAL de solo lectura sigue separado y no se etiqueta como DEMO.

El informe actual registra INCOMPLETE_PENDING_MANUAL y exit_code=2. No comenzar Fase 2 hasta resolver las comprobaciones pendientes. El sistema permanece en DRY RUN.

La revalidación global más reciente (`python scripts/validate_phase1.py`) fue interrumpida tras permanecer varios minutos sin salida; no se capturó un nuevo código de salida ni se actualizó su conjunto de resultados. El informe registra la tentativa como pendiente. No se atribuye PASS a esa ejecución.
