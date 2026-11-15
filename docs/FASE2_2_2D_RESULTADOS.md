# Fase 2.2.2d — resultados locales

**Estado calculado:** `PHASE2_2_2D_PARTIAL_CALENDAR_UNKNOWN`  
**Base/HEAD:** `1bcf5114ade42521d5b706d0200ac9af4c283791`  
**Rama:** `codex/phase2-2-2d-local-intensive` — sin commit, push, PR ni merge.

## Etapas

| Etapa | Estado | Evidencia / bloqueo |
|---|---|---|
| 0. Preflight | PASS | Python virtual 3.13; rama local desde `origin/main`; 47 fuentes y 98 chunks reauditorizados. |
| 1. Calendario de bróker | PARTIAL_CALENDAR_UNKNOWN | No se encontró calendario oficial con vigencia 2023–2026; 0 de 74 candidatos de recuperación y 0 de 1.776 revisiones resueltos. |
| 2. Sesiones, warmup y MTF | PARTIAL_CALENDAR_UNKNOWN | 159.133 decisiones M15 auditadas; 0 ventanas MTF verificadas; 0 look-ahead rechazados. |
| 3. Cobertura/recuperación | PASS_NO_NEW_CAMPAIGN_REQUIRED | 0 campañas/llamadas nuevas; no se descargó histórico porque no resolvería cierres desconocidos. |
| 4. Productor de scan | BLOCKED_NO_VERIFIED_MTF_WINDOWS | Código y fixtures sintéticos listos; scan real bloqueado por 0 ventanas MTF verificadas. |
| 5. Costes/backtest | BACKTEST_BLOCKED | Portfolio/stateful y sensibilidad no ejecutados: faltan calendario, scan nuevo y costes/ejecuciones validados. |
| 6. Validación local | PASS | 124 tests, 375 comprobaciones look-ahead, privacy y static scan. |
| 7. Handoff remoto | READY | [Guía de handoff](FASE2_2_2D_HANDOFF_REMOTO.md). |

## Pruebas ejecutadas

| Suite | Estado | Tests / comprobaciones |
|---|---:|---:|
| `phase2_2_2_abcd_offline` | PASS | 86 |
| `phase1_historical_simulator` | PASS | 16 |
| `phase1_mt5_readonly_safety` | PASS | 22 |
| `no_lookahead` | PASS | 375 |

Tests omitidos: 0. Las pruebas son offline y sintéticas donde corresponde. No se ejecutaron backtests económicos, LLMs ni consultas de barras MT5. La validación MT5 separada leyó metadatos, cotizaciones observadas e historial de deals en cuenta REAL con `tradeapi_disabled=True`; no leyó velas de precio.

## Calendario y conciliación histórica

Las fuentes públicas actuales encontradas no acreditan fechas efectivas ni excepciones históricas. El horario Forex genérico no se aplicó retrospectivamente. Resultado verificable: **0 intervalos históricos resueltos**; los 1.850 registros heredados (74 `RECOVERY_CANDIDATE` + 1.776 `CALENDAR_REVIEW`) permanecen `UNKNOWN`. La documentación pública puede consultarse en [horarios IC](https://www.ic.com/en/trading-pricing/trading-hours), [centro de ayuda IC](https://www.ic.com/en/help-resources/help-centre) y las [referencias oficiales MQL5](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py). Las fuentes y hashes están en `broker_calendar_evidence.json`.

## Continuidad estricta y warmup

Las ventanas de esta tabla son segmentos de cadencia UTC estricta, no sesiones del bróker verificadas.

| Símbolo | TF | Barras únicas válidas | Segmentos estrictos | Segmentos ≥200 | Segmentos ≥692 | Máx. barras/segmento | MTF verificado |
|---|---|---:|---:|---:|---:|---:|---:|
| EURUSD | M5 | 107035 | 82 | 82 | 76 | 1440 | 0 |
| EURUSD | M15 | 53045 | 116 | 110 | 0 | 480 | 0 |
| EURUSD | H1 | 13532 | 118 | 0 | 0 | 120 | 0 |
| EURUSD | H4 | 3639 | 127 | 0 | 0 | 30 | 0 |
| EURUSD | D1 | 861 | 178 | 0 | 0 | 5 | 0 |
| USDCAD | M5 | 107035 | 82 | 82 | 76 | 1440 | 0 |
| USDCAD | M15 | 53044 | 117 | 110 | 0 | 480 | 0 |
| USDCAD | H1 | 13532 | 118 | 0 | 0 | 120 | 0 |
| USDCAD | H4 | 3639 | 127 | 0 | 0 | 30 | 0 |
| USDCAD | D1 | 861 | 178 | 0 | 0 | 5 | 0 |
| AUDUSD | M5 | 107035 | 83 | 82 | 76 | 1440 | 0 |
| AUDUSD | M15 | 53044 | 116 | 110 | 0 | 480 | 0 |
| AUDUSD | H1 | 13532 | 118 | 0 | 0 | 120 | 0 |
| AUDUSD | H4 | 3639 | 127 | 0 | 0 | 30 | 0 |
| AUDUSD | D1 | 861 | 178 | 0 | 0 | 5 | 0 |

**D1:** el segmento estricto más largo es de 5 barras; no se afirman 200/692 barras D1 continuas. No se fijó un nuevo rango de evaluación; por ello, barras D1 anteriores a ese rango quedan sin cálculo. No hay look-ahead detectado en 159.133 decisiones auditadas.

## Cobertura, MT5 y seguridad

- Fuentes previas: 47 archivos (45 CSV), 540.143 filas auditadas; run `phase222b-local-20261115`: 98/98 chunks, 18.175 filas, 0 conflictos. Sus hashes actuales coinciden con el informe de integridad previo.
- Dataset congelado: SHA-256 canónico `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`; sin cambios.
- Esta fase: 0 llamadas `copy_rates_*`, 0 barras nuevas, 0 campañas MT5; una comprobación read-only separada confirmó cuenta REAL y `tradeapi_disabled=True`.
- Cotizaciones observadas en la comprobación previa: EURUSD 18 puntos, USDCAD 52, AUDUSD 58; eran antiguas (~40,6 h) y las dos últimas superaban su límite configurado. No son aprovechables como precio de riesgo actual.
- Riesgo hipotético BUY/SELL: los cálculos del motor coincidieron con `order_calc_profit` con diferencia relativa ≤0,034%; esto no valida precio fresco ni calendario.
- **0 órdenes enviadas, 0 operaciones ejecutadas por el proceso y 0 llamadas LLM.** No se usó `order_check`.

## Scan y backtest

El productor valida el manifiesto explícito, hashes, cierres, conflictos, lineage y salida privada. El adaptador de frames coincide exactamente con la ruta existente para el mismo CSV EURUSD y todos los TF. No se ejecutó scan histórico completo ni paridad de candidatos porque el calendario falla cerrado. No se generó `scan_from_local_history.csv`.

Portfolio/stateful y spread floors 0/5/10/20 quedaron `NOT_RUN`: 0 ventanas MTF verificadas, sin scan nuevo, horarios de ejecución/cierre desconocidos, spreads históricos cero no verificados y comisión/slippage sin validar. No se reportan PnL, PF ni conclusiones de rentabilidad.

## Archivos y evidencia

Código y pruebas locales:

- `scripts/validate_mt5_real_readonly.py`
- `src/historical_state.py`
- `src/phase2_2_2d_sessions.py`
- `src/verified_history_scan.py`
- `scripts/scan_from_verified_history.py`
- `scripts/run_phase2_2_2d_offline_validation.py`
- `scripts/finalize_phase2_2_2d_reports.py`
- `tests/test_mt5_real_readonly.py`
- `tests/test_phase2_2_2d_sessions.py`
- `tests/test_phase2_2_2d_scan.py`
- `reports/phase2_2_2d/`
- `docs/FASE2_2_2D_RESULTADOS.md`
- `docs/FASE2_2_2D_HANDOFF_REMOTO.md`
- `docs/FASE2_2_2D_CALENDARIO_INPUT.md`

Informes públicos: [preflight](../reports/phase2_2_2d/preflight.json), [integridad](../reports/phase2_2_2d/source_and_run_integrity.json), [calendario](../reports/phase2_2_2d/broker_calendar_evidence.json), [reconciliación](../reports/phase2_2_2d/broker_session_reconciliation.json), [MTF/warmup](../reports/phase2_2_2d/mtf_warmup_and_causality.json), [cobertura](../reports/phase2_2_2d/coverage_and_optional_backfill.json), [scan](../reports/phase2_2_2d/scan_producer_validation.json), [backtest](../reports/phase2_2_2d/exploratory_costs_and_backtest.json), [validación](../reports/phase2_2_2d/validation_summary.json), [handoff manifest](../reports/phase2_2_2d/remote_handoff_manifest.json).

La evidencia sensible queda local y se referencia en handoff solo mediante run-id y SHA-256. No se publicaron precios, identidad de terminal/cuenta ni rutas absolutas.

## Qué falta

1. Aportar el documento oficial con fechas efectivas y festivos descrito en [FASE2_2_2D_CALENDARIO_INPUT.md](FASE2_2_2D_CALENDARIO_INPUT.md).
2. Con ese documento, clasificar de nuevo los 1.850 intervalos y calcular sesiones/warmup MTF; solo si los D1 anteriores siguen insuficientes, preparar una recuperación acotada separada.
3. Ejecutar scan y backtests exploratorios únicamente cuando el calendario, la continuidad, los costes y las ventanas de ejecución/cierre pasen sus puertas.

No se declara Fase 2.2.2d completa ni se habilita trading real.
