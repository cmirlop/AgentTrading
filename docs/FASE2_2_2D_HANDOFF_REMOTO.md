# Fase 2.2.2d — handoff a Codex remoto

Estado local: `PHASE2_2_2D_PARTIAL_CALENDAR_UNKNOWN`. Base `origin/main`: `1bcf5114ade42521d5b706d0200ac9af4c283791`; rama `codex/phase2-2-2d-local-intensive`; HEAD `1bcf5114ade42521d5b706d0200ac9af4c283791`. No se creó commit, push, PR ni merge. CI: `NOT_RUN`.

La sesión remota no tiene acceso a MetaTrader, API MT5, CSV brutos ni informes privados. No infieras sus contenidos. El calendario histórico oficial sigue `UNKNOWN`; ningún gap ni ventana de mercado puede inferirse de velas ausentes.

## Evidencia pública reproducible

- [Resultados locales](FASE2_2_2D_RESULTADOS.md)
- [Resumen consolidado](../reports/phase2_2_2d/validation_summary.json)
- [Integridad de fuentes y run](../reports/phase2_2_2d/source_and_run_integrity.json)
- [Calendario y fuentes](../reports/phase2_2_2d/broker_calendar_evidence.json)
- [Reconciliación](../reports/phase2_2_2d/broker_session_reconciliation.json)
- [Warmup y causalidad](../reports/phase2_2_2d/mtf_warmup_and_causality.json)
- [Cobertura](../reports/phase2_2_2d/coverage_and_optional_backfill.json)
- [Productor de scan](../reports/phase2_2_2d/scan_producer_validation.json)
- [Backtest/costes](../reports/phase2_2_2d/exploratory_costs_and_backtest.json)
- [Hashes de código/configuración y manifest](../reports/phase2_2_2d/remote_handoff_manifest.json)

Pruebas offline reproducibles con el Python del proyecto:

```powershell
python -m unittest discover -s tests -p "test_phase2_2_2*.py"
python -m unittest discover -s tests -p "test_phase1_backtest.py"
python -m unittest tests.test_mt5_real_readonly tests.test_phase1_mt5_safety
python test_no_lookahead.py
```

La última ejecución pasó 124 tests (omitidos: 0) y 375 checks no-look-ahead. El informe de ejecución solo contiene comandos/conteos; no vuelca datos de barras.

## Resultado y límites

- Calendario oficial con fechas efectivas 2023–2026: no encontrado. Los 1.850 registros (74+1.776) permanecen UNKNOWN; 0 resueltos.
- Fuentes previas: 47 archivos; run `phase222b-local-20261115`, 98 chunks, 18.175 filas, 0 conflictos; los hashes locales siguen coincidiendo. Dataset congelado canónico sin cambios.
- Se hicieron 0 llamadas de histórico MT5 nuevas y se añadieron 0 barras en esta fase. La validación read-only previa observó cuenta REAL y `tradeapi_disabled=True`; 0 órdenes y 0 operaciones. No hubo llamadas LLM.
- 0 ventanas MTF verificadas; no scan histórico completo ni paridad de candidatos; no backtest portfolio/stateful ni PnL nuevo.
- La evidencia privada se menciona únicamente por run-id y hash: reconciliación local `phase2-2-2d-local-20261115` SHA-256 `9c3fa6fb812a649256df9212a269d51f0d6efdd66deb2e1e8b8ee0712666a9a0`; fuente/run privado `phase222b-local-20261115` SHA-256 `8d3ec0042ae2c51919fdd2d4849c0548f0f3781541c3fd1e75b7ee3296342f30`.

## Backlog remoto priorizado

1. Ampliar CI y fixtures sintéticos de sesiones, DST, festivos, D1/H4, velas parciales y alineación.
2. Revisar la separación entre cadencia de reloj y sesiones del mercado, conservando ambas líneas base.
3. Revisar por pares el productor explícito de scan, causalidad, lineage y seguridad de escritura privada.
4. Comprobar diferencias estadísticas con fixtures/resúmenes agregados públicos; no usar CSV privados.
5. Documentar el pipeline reproducible y revisar hipótesis de spread/comisión/slippage.
6. Diseñar, sin ejecutar llamadas, un protocolo preregistrado técnico vs DeepSeek con presupuesto máximo, cohortes, datos permitidos y guardas.

## Backlog exclusivamente local

1. Obtener especificación oficial del bróker con vigencia histórica, festivos, timezone/DST, horarios por símbolo y límites de barras D1/H4/H1. No incluir datos de cuenta.
2. Cargar esa evidencia en el esquema fail-closed y validar su hash, fechas, cobertura por símbolo/TF y excepciones.
3. Si después de clasificar sesiones el warmup D1 aún es insuficiente, preparar un plan mínimo con `copy_rates_range`, cuenta REAL y `tradeapi_disabled=True` antes/después de cada lectura; máximo 150 llamadas por campaña y 2 campañas en total, sin cambiar la configuración.
4. Reauditar offline, volver a calcular ventanas y ejecutar el productor solo si hay ventanas MTF elegibles; después se podrán considerar backtests exploratorios.

## Dependencias

| Tarea | Remoto ahora | Requiere local/persona |
|---|---|---|
| Fixtures DST/calendario, tests, revisión del productor y documentación | Sí | No |
| Resolver cierres históricos y continuidad MTF | No | Documento oficial fechado del bróker |
| Leer datos del terminal o pedir histórico | No | PC con MT5 y guarda REAL/read-only |
| Backtest exploratorio del scan nuevo | No | Calendario y costes/ejecución verificados, scan causal nuevo |

## PROMPT REMOTO PARA COPIAR

> Revisa el commit/base indicado y solo los artefactos públicos de esta rama. No finjas acceso a MetaTrader, API MT5, barras privadas ni informes privados; no solicites ni uses credenciales; no llames a DeepSeek/Ollama ni a APIs facturables. Puedes revisar código, pruebas sintéticas, causalidad, hashes públicos y documentación. Conserva UNKNOWN_BROKER_CALENDAR, no inventes calendario ni resultados; no cambies estrategia, no ejecutes backtest con scan congelado como si fuera nuevo y no hagas commit, push, PR ni merge sin autorización explícita.
