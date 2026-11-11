# Cierre de Fase 1 — AgentTrading

**Decisión al 11/11/2026: `INCOMPLETE_PENDING_MANUAL`.** No se declara `PHASE1_COMPLETE` ni se inicia la Fase 2.

## Evidencias aprobadas

- 94 pruebas `test_phase1_*.py`: 93 PASS y una omisión de prueba POSIX no aplicable a Windows. Los bloqueos concurrentes multiproceso de Windows sí se probaron.
- 21/21 pruebas offline del validador y 375 comprobaciones look-ahead PASS.
- Ocho backtests portfolio/stateful completados; BASE se repitió en ambos modos y fue reproducible.
- MT5 REAL en solo lectura: terminal conectado, `tradeapi_disabled=True`; EURUSD, USDCAD y AUDUSD pasaron metadatos, riesgo hipotético, exposición y evaluación diaria.
- Ollama live PASS con `qwen3:4b`, JSON/Pydantic/evidencia válidos y guardas de respuestas inválidas.
- Cero órdenes enviadas y cero operaciones ejecutadas por el proceso. No se llamó a `order_send()` ni `order_check()`.

## Pendiente para una decisión de cierre

1. MT5 DEMO: no hay una cuenta DEMO disponible. El resultado de cuenta REAL es una validación independiente y no se presenta como DEMO.
2. DeepSeek: existe un PASS remoto del 11/11 en Python 3.12.14/Pydantic 2.14.2, pero el entorno Windows usa Python 3.13.14/Pydantic 2.13.5. No se repitió una llamada facturable; se requiere autorización para hacerlo en este runtime.
3. Regresión histórica: se explicaron los cambios de estado de expiración y una parte de las diferencias de selección por capacidad. Persisten selecciones sin testigo directo (portfolio 159; stateful 77) y no se pueden atribuir métricas antiguas que carecen de snapshots por operación.
4. Costes: las hipótesis de comisiones y slippage no se han contrastado con tarifas reales del bróker.

La prueba MT5 REAL está en [`reports/mt5_real_readonly_summary.json`](../reports/mt5_real_readonly_summary.json); la actualización histórica del 11/11 está en [`reports/mt5_history_refresh_summary.json`](../reports/mt5_history_refresh_summary.json) y los CSV versionados en su carpeta indicada. La matriz de resultados, cifras económicas y trazabilidad están en [`docs/FASE1_VALIDACION.md`](FASE1_VALIDACION.md) y [`docs/FASE1_RESULTADO_FINAL.md`](FASE1_RESULTADO_FINAL.md). El estado programático vigente está en [`reports/phase1_validation.json`](../reports/phase1_validation.json).

## Operaciones manuales necesarias

- Si se desea completar la validación MT5 original, conectar manualmente a una cuenta DEMO, mantener deshabilitado **Disable algorithmic trading via external Python API** y ejecutar `python scripts/validate_phase1_mt5.py`.
- Autorizar explícitamente una única prueba DeepSeek live nueva para el runtime local. No pegar API keys en el chat.
- Revisar las diferencias históricas pendientes con snapshots comparables o registrar su revisión antes de cambiar su estado.
- Tras resolver las comprobaciones, ejecutar `python scripts/validate_phase1.py`. El código de salida es 0 para `PHASE1_COMPLETE`, 2 para `INCOMPLETE_PENDING_MANUAL` y 1 para FAIL.

Hasta entonces, el sistema debe seguir exclusivamente en DRY RUN. Este cierre no habilita trading ni autoriza comenzar la Fase 2.
