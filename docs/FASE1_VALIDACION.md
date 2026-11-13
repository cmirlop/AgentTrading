# AgentTrading — Validación de Fase 1

**Estado: PHASE1_COMPLETE.** La validación global finalizó en Windows con código 0. La Fase 1 está cerrada técnicamente; no se inició trabajo de Fase 2.

## Evidencia vigente

| Control | Estado | Evidencia |
|---|---|---|
| Suite Phase 1 | PASS | 119 tests ejecutados; 118 PASS y 1 omitido por ser específico de POSIX. |
| Pruebas offline existentes | PASS | 21/21 scripts finalizaron correctamente. |
| Concurrencia y reservas en Windows | PASS | Locks multiproceso, persistencia, expiración, reinicio de proceso/día, límite diario y exposición USD en la suite Phase 1. |
| MT5 REAL solo lectura | PASS reutilizado | Evidencia con tradeapi_disabled=true, metadatos, riesgo, exposición e historial, dentro de 24 h y con procedencia verificada. No se conectó MT5 en la pasada integral. |
| MT5 DEMO | NO APLICABLE | No existe cuenta DEMO. No se presenta la REAL como DEMO; la ruta REAL read-only permitida por la incidencia pasó. |
| Ollama / qwen3:4b | PASS | Una validación local live, JSON/Pydantic/evidencia LIVE_PROBE; sin señal operativa. La pasada integral verificó y reutilizó la evidencia por hashes y vigencia. |
| DeepSeek / deepseek-flash | PASS reutilizado | Evidencia vigente y con huella compatible; no se hizo una nueva llamada facturable. |
| Regresión histórica | PASS | Conciliación por operaciones, hashes y dataset congelado; 721 deltas agregados explicados, 0 sin explicar. |
| Backtests | PASS | Ocho resultados de portfolio/stateful; BASE repetido por modalidad. Reutilización en la pasada integral comprobada por código, configuración, dependencias, dataset y antigüedad. |
| Look-ahead | PASS | 375 comprobaciones. |
| DRY RUN / operaciones | PASS | Cero órdenes u operaciones ejecutadas por las validaciones; no se llamó a order_send ni order_check. |

Las pruebas test_order_request.py y test_trade_planner.py se clasifican NOT_RUN_READ_ONLY_NO_DEMO: requerirían order_check() o alterar la selección de símbolos, fuera del alcance autorizado. test_ollama.py se reemplaza por la validación live de qwen3:4b; no se cuenta como PASS independiente.

## Dataset y trazabilidad

Las reproducciones históricas usaron el blob Git verificado con SHA-256 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3. El CSV de trabajo del checkout tiene SHA-256 distinto (341e8f12595a9e74bac2faeb739b8e7b1bdaa1ccdcbd35b26457354f686b31e0) y no fue modificado ni utilizado para sustituir el dataset congelado. El informe diferencia ambos hashes.

La validación integral anterior a esta incidencia se había interrumpido sin checkpoint por fase. Los logs no permiten recuperar el último subprocess exacto; el backtest histórico era la hipótesis más probable, no un hecho. El runner ahora registra inicio, progreso, fin, duración y estado de cada proceso. La ejecución final actual sí terminó completamente.

## Informes

- [Informe global](../reports/phase1_validation.json)
- [Resumen de la última ejecución](../reports/phase1_final_local/validation_summary.json)
- [Conciliación agregada pública](../reports/phase1_final_local/baseline_aggregate_reconciliation.json)
- [Diagnóstico y guía de Ollama Windows](OLLAMA_WINDOWS_DIAGNOSTICO.md)

Los informes privados y las trazas financieras de cuenta permanecen excluidos de Git.
