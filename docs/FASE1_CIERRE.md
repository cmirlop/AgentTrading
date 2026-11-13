# Cierre de Fase 1 — AgentTrading

**Decisión: PHASE1_COMPLETE.** La batería integral de Windows terminó con código 0 y todas las condiciones obligatorias admitidas por la incidencia quedaron verificadas. No se inició Fase 2 ni se habilitó trading real.

## Controles que cierran la fase

- 119 tests Phase 1: 118 PASS y un skip POSIX esperado.
- 21/21 comprobaciones offline y 375 controles de look-ahead PASS.
- Concurrencia y ciclo de vida de reservas validados en Windows, incluidos persistencia, expiración a 15 minutos, reinicio y límites de riesgo.
- MT5 REAL read-only PASS con evidencia reciente y huella compatible; tradeapi_disabled=true. La DEMO es NOT_APPLICABLE_NO_DEMO_ACCOUNT, según el alcance acordado en la incidencia.
- Ollama qwen3:4b live PASS con respuesta JSON/Pydantic y evidencia autorizada. El servicio, modelo, endpoint y manifiesto son accesibles; no se necesitó cambiar ACL, instalar ni descargar modelos.
- DeepSeek deepseek-flash PASS mediante evidencia vigente y de procedencia verificada. No se repitió la llamada facturable.
- Reconciliación del dataset congelado y los agregados históricos PASS: 487 cambios de selección atribuidos con evidencia por operación; 721 deltas agregados documentados; cero sin explicar.
- Ocho backtests válidos y BASE reproducible. La última pasada integral reutilizó las salidas solo tras validar huellas y ventana de vigencia.
- DRY RUN preservado. Ninguna orden fue enviada ni ejecutada.

## Límites documentados

- No se reconstruyó el subprocess exacto donde se detuvo la validación anterior: el informe previo no tenía checkpoints. El backtest histórico se mantiene como inferencia. La nueva instrumentación dejó constancia de progreso y la pasada final completó toda la batería.
- Los costes CONSERVATIVE y STRESS son escenarios de sensibilidad; no son tarifas confirmadas del bróker. BASE conserva las hipótesis originales de comisión y slippage cero no verificados. Ninguna cifra se ajustó para conseguir PASS.
- test_order_request.py y test_trade_planner.py no se ejecutan por seguridad y falta de DEMO; se marcan NOT_RUN_READ_ONLY_NO_DEMO, no PASS. Sus controles de riesgo relevantes están cubiertos por pruebas read-only y de unidad.

## Decisión operativa

Fase 1 completa y técnicamente se puede comenzar Fase 2 cuando el usuario lo decida. Esta tarea no inició trabajo de Fase 2. El sistema permanece en DRY RUN y no se debe conectar para trading real.

Evidencia autoritativa: [informe integral](../reports/phase1_validation.json), [resumen de ejecución](../reports/phase1_final_local/validation_summary.json), [conciliación histórica](../reports/phase1_final_local/baseline_aggregate_reconciliation.json) y [guía Ollama](OLLAMA_WINDOWS_DIAGNOSTICO.md).
