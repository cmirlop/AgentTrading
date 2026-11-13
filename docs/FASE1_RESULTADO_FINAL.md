# Resultado final local — Fase 1 de AgentTrading

**ESTADO FINAL FASE 1: PHASE1_COMPLETE**

La validación integral local de Windows finalizó con código 0. Las métricas negativas después de costes se conservan; la conclusión solo certifica integridad de medición, seguridad y reproducibilidad. No se modificaron la estrategia, los parámetros, el dataset congelado ni los resultados para conseguir PASS. No se inició Fase 2.

## A. Entorno

- Sistema: Windows 10, build 19045.
- Python: CPython 3.13.14 x64 desde .venv.
- Rama de trabajo: codex/issue14-phase1-integrity.
- HEAD base examinado: ea2e60a10df4e9ae5f2e276d82cf343fd4a3ee91 (main tras PR #13). El informe conserva hashes de cada archivo validado y declara el árbol modificado durante la ejecución.
- Dependencias: pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, MetaTrader5 5.0.6231, Ollama SDK 0.6.3, OpenAI SDK 3.41.0 y python-dotenv 1.2.4.
- MT5 instalado y evidencia REAL read-only previa válida; no hay cuenta DEMO. La pasada integral no abrió sesión MT5.
- Ollama 0.40.6 responde en el endpoint local; qwen3:4b instalado y utilizado, sin sustitución silenciosa.
- DeepSeek disponible según evidencia vigente deepseek-flash; no se realizó llamada nueva.
- Rutas privadas, identidad de cuenta, balance, equity, historial identificable, claves y tokens no se incluyeron en los informes públicos.

Archivos modificados o creados para la incidencia: scripts/validate_phase1.py, scripts/reconcile_phase1_historical.py, tests/test_phase1_llm_live.py, tests/test_phase1_reconciliation.py, tests/test_phase1_runner.py, informes JSON públicos y esta documentación. El diagnóstico detallado de Ollama y los snapshots de operaciones están bajo reports/private/, excluidos de Git.

## B. Resultados

| Comprobación | Estado | Evidencia |
|---|---|---|
| Suite Phase 1 | PASS | 119 tests: 118 PASS, 1 skip específico de POSIX. Incluye Windows, trazabilidad, errores Ollama y pruebas adversariales de conciliación. |
| Pruebas offline | PASS | 21/21 scripts completados; se registran duración y código de salida por subprocess. |
| Concurrencia Windows | PASS | Locks y contención multiproceso, persistencia, expiración de reservas a 15 minutos, reinicio de proceso/día, bloqueo diario y exposición USD. |
| MT5 REAL read-only | PASS reutilizado | Evidencia con tradeapi_disabled=true, cuenta REAL, EURUSD/USDCAD/AUDUSD, metadatos, precios, riesgo hipotético, exposición e historial diario. Procedencia y antigüedad verificadas dentro de 24 h. No se realizaron llamadas MT5 en la pasada integral actual. |
| MT5 DEMO | NO APLICABLE | No existe cuenta DEMO. Se mantiene diferenciada de la validación REAL, que satisface el alcance read-only autorizado en la incidencia. |
| Riesgo monetario | PASS | Cálculos hipotéticos de compra/venta contrastados con el cálculo del bróker en la evidencia MT5 read-only. |
| Límite diario | PASS | Historial de deals clasificado; depósitos/retiros no compensan pérdidas operativas. Evidencia MT5 vigente y pruebas unitarias Windows. |
| Ollama | PASS | qwen3:4b, exactamente el modelo configurado; una sola inferencia local live, JSON/Pydantic válido, evidencia LIVE_PROBE, cero señal operativa. |
| DeepSeek | PASS reutilizado | deepseek-flash; JSON/Pydantic/evidencia permitida y llamada original documentada. Sin solicitud facturable nueva. |
| Backtests | PASS | Portfolio BASE/CONSERVATIVE/STRESS y Stateful BASE/CONSERVATIVE/STRESS, con BASE repetido dos veces por modalidad. Reutilización validada por hashes y ventana de vigencia. |
| Regresión histórica | PASS | Resúmenes reproducidos desde replay; IDs estables; 487 diferencias de selección explicadas; 721 deltas métricos agregados reconciliados; 0 inexplicados. |
| Look-ahead | PASS | 375 comprobaciones. |
| DRY RUN | PASS | Cero órdenes u operaciones ejecutadas por la validación; sin order_send() ni order_check(). |

Los tests test_order_request.py y test_trade_planner.py quedaron NOT_RUN_READ_ONLY_NO_DEMO porque intentarían order_check() o cambiarían el estado de selección de símbolos del terminal. No se presentan como PASS. test_ollama.py queda sustituido por la comprobación live real y no se repitió para evitar una segunda inferencia local innecesaria.

## C. Conciliación y resultados económicos

La reproducción histórica se fijó al blob Git del dataset congelado SHA-256 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3. El archivo de trabajo actual tiene SHA-256 341e8f12595a9e74bac2faeb739b8e7b1bdaa1ccdcbd35b26457354f686b31e0; es distinto, se mantuvo intacto y no se utilizó para sustituir el blob congelado.

Los commits comparados son baseline a00644a6b330b6235bea3f47cb17d2dfe39d4f5b, prefinalización 1ef0faf7600b866236f75cb7c229fc8a3b66973e y versión actual ea2e60a10df4e9ae5f2e276d82cf343fd4a3ee91. Los seis resúmenes históricos publicados (portfolio y stateful para cada versión) se reprodujeron desde snapshots con hashes. Baseline→prefinalización tuvo cinco transiciones documentadas de EXPIRED_AT_END a PENDING_AT_END, cero cambios de selección y cero métricas económicas comunes alteradas. Prefinalización→actualidad registró 274 cambios de selección portfolio y 213 stateful; cada uno tiene testigo. No hubo cambios de señal o plan. Se atribuyeron 721 deltas métricos agregados a operaciones, capacidad/selección, correcciones de expiración y reglas de coste. Quedaron cero diferencias inexplicadas. Los costes CONSERVATIVE/STRESS no tienen un baseline con tarifa anterior comparable y siguen etiquetados como hipótesis de sensibilidad.

Comparación Portfolio para umbral 65 / LIMIT:

| Escenario | Oportunidades | Señales | Planes aceptados/rechazados | Ejecutadas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa EUR | DD neto EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

El BASE conserva costes asumidos de comisión y slippage cero, no verificados como tarifa real del bróker. CONSERVATIVE y STRESS son sensibilidades y no representan costes confirmados. No se cambió ninguna regla para alterar estas cifras.

## D. Conclusión y operaciones pendientes

La Fase 1 está completada bajo los criterios de la incidencia. Los cambios corrigieron progreso y estados de la batería global, la distinción entre ejecución y reutilización, la validación histórica de agregados y la publicación segura de evidencia. El antiguo punto de interrupción exacto no puede recuperarse porque el informe previo no guardó checkpoints; la etapa de backtest era solo la hipótesis mejor respaldada. La ejecución actual sí terminó todas las fases.

Ollama no requirió una modificación persistente: el síntoma previo de acceso denegado no se reprodujo. El proceso usa la identidad con acceso al modelo, el endpoint de versión y listado responde correctamente, el manifiesto es legible y qwen3:4b pasó una validación live. No se borró ni descargó ningún modelo y no se cambiaron ACL.

No hay tareas manuales obligatorias para cerrar Fase 1. MT5 debe permanecer con tradeapi_disabled=true; no se usó para operar. Se puede comenzar Fase 2 técnicamente cuando el usuario lo decida; esta ejecución no comenzó esa fase.

- [Informe de la última validación](../reports/phase1_validation.json)
- [Resumen de estado](../reports/phase1_final_local/validation_summary.json)
- [Conciliación agregada segura](../reports/phase1_final_local/baseline_aggregate_reconciliation.json)
- [Resumen causal de selecciones](../reports/phase1_final_local/historical_causal_attribution_summary.json)
- [Evidencia MT5 REAL read-only](../reports/mt5_real_readonly_summary.json)
- [Validación live local de Ollama](../reports/phase1_final_local/ollama_live_20261113T1908Z_issue14_pass.json)
- [Evidencia DeepSeek reutilizada](../reports/phase1_llm_live_deepseek.json)
- [Diagnóstico y guía de Ollama](OLLAMA_WINDOWS_DIAGNOSTICO.md)
