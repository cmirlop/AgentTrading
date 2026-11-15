# Fase 2.2.2b — resultados de históricos MT5

**Estado final:** `PHASE2_2_2B_COMPLETE_WITH_PENDING_GAPS`

## Entorno y Git

- Sistema: Windows-10-10.0.19045-SP0
- Python: 3.13.14 (.venv/Scripts/python.exe)
- HEAD de partida: `a36c2e3236deb877ca9664ca3ca67580eaba48e7`
- Rama: `codex/phase2-2-2b-local-real-history-audit`
- Cambios: rama de trabajo local; sin commit, push, PR ni merge.
- SDK MT5 disponible: True; versión 5.0.6231

## Validación y campaña read-only

- Tests offline: 40/40 PASS; fallidos 0; omitidos 0.
- Tests del finalizador: 5/5 PASS; fallidos 0; omitidos 0.
- Cuenta verificada: **REAL**; terminal conectado y `tradeapi_disabled=True`.
- Lecturas MT5: 98/98 (límite 150). Run `phase222b-local-20261115`.
- Velas recibidas en chunks: 18175; respuestas vacías 0; exclusiones de velas abiertas/futuras 0.
- Integridad de chunks: PASS; conflictos de precio 0.
- Órdenes enviadas: **0**. Operaciones ejecutadas por el proceso: **0**. Llamadas LLM: **0**.
- La campaña solo llamó `copy_rates_range`, con comprobación REAL/read-only antes y después de cada lectura. No ejecutó backtests.

### Velas leídas por instrumento y timeframe

| Símbolo | TF | Peticiones | Filas | Timestamps únicos | Primera UTC | Última vela cerrada UTC |
|---|---:|---:|---:|---:|---|---|
| AUDUSD | D1 | 6 | 22 | 22 | 2023-12-21T00:00:00+00:00 | 2026-01-02T00:00:00+00:00 |
| AUDUSD | H1 | 4 | 16 | 16 | 2024-12-24T22:00:00+00:00 | 2026-01-02T01:00:00+00:00 |
| AUDUSD | H4 | 4 | 16 | 16 | 2024-12-24T16:00:00+00:00 | 2026-01-02T04:00:00+00:00 |
| AUDUSD | M15 | 6 | 1008 | 1004 | 2024-12-24T23:30:00+00:00 | 2026-10-26T12:45:00+00:00 |
| AUDUSD | M5 | 12 | 4994 | 4970 | 2025-12-24T23:50:00+00:00 | 2026-11-04T11:10:00+00:00 |
| EURUSD | D1 | 6 | 22 | 22 | 2023-12-21T00:00:00+00:00 | 2026-01-02T00:00:00+00:00 |
| EURUSD | H1 | 4 | 16 | 16 | 2024-12-24T22:00:00+00:00 | 2026-01-02T01:00:00+00:00 |
| EURUSD | H4 | 4 | 16 | 16 | 2024-12-24T16:00:00+00:00 | 2026-01-02T04:00:00+00:00 |
| EURUSD | M15 | 6 | 1008 | 1004 | 2024-12-24T23:30:00+00:00 | 2026-10-26T12:45:00+00:00 |
| EURUSD | M5 | 12 | 4994 | 4970 | 2025-12-24T23:50:00+00:00 | 2026-11-04T11:10:00+00:00 |
| USDCAD | D1 | 6 | 22 | 22 | 2023-12-21T00:00:00+00:00 | 2026-01-02T00:00:00+00:00 |
| USDCAD | H1 | 4 | 16 | 16 | 2024-12-24T22:00:00+00:00 | 2026-01-02T01:00:00+00:00 |
| USDCAD | H4 | 4 | 16 | 16 | 2024-12-24T16:00:00+00:00 | 2026-01-02T04:00:00+00:00 |
| USDCAD | M15 | 7 | 1011 | 1007 | 2024-12-23T00:00:00+00:00 | 2026-10-26T12:45:00+00:00 |
| USDCAD | M5 | 13 | 4998 | 4974 | 2025-11-07T23:50:00+00:00 | 2026-11-04T11:10:00+00:00 |

## Históricos, spreads y preparación

- Fuentes originales: 47 archivos; hashes raw/canónicos sin cambios: PASS.
- Dataset congelado: SHA canónico esperado `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`; intacto antes y después: True.
- Gaps candidatos: 77 antes y 74 después; revisión de calendario: 1761 antes y 1776 después.
- Posiciones ausentes de cadencia en intervalos de recuperación + calendario: 236313 antes y 218523 después; disminución neta 17790, nuevas 0. No equivalen a velas negociables.
- Continuidad: `HISTORY_CONTINUITY_PARTIAL`; pendientes 74; conflictos de solapamiento 0.
- Las filas de spread cero conservan el estado `OBSERVED_ZERO_UNVERIFIED`; no se trataron como coste cero. La auditoría no demuestra bid/ask histórico ni verifica comisión o slippage.
- Investigación técnica: ELIGIBLE_ONLY_WITHIN_CONTIGUOUS_WINDOWS; usar solo ventanas con OHLC válido y sin atravesar gaps.
- Backtest exploratorio con sensibilidad a costes: ELIGIBLE_WITH_CONTIGUOUS_WINDOWS_AND_EXPLICIT_COST_SENSITIVITY; no ejecutado en esta tarea.
- Evaluación económica confirmatoria: BLOCKED.

### Intervalos pendientes por instrumento y timeframe

| Símbolo | TF | Gaps candidatos | Revisión de calendario |
|---|---:|---:|---:|
| EURUSD | D1 | 6 | 171 |
| EURUSD | H1 | 4 | 113 |
| EURUSD | H4 | 4 | 122 |
| EURUSD | M15 | 4 | 111 |
| EURUSD | M5 | 6 | 75 |
| USDCAD | D1 | 6 | 171 |
| USDCAD | H1 | 4 | 113 |
| USDCAD | H4 | 4 | 122 |
| USDCAD | M15 | 5 | 111 |
| USDCAD | M5 | 7 | 74 |
| AUDUSD | D1 | 6 | 171 |
| AUDUSD | H1 | 4 | 113 |
| AUDUSD | H4 | 4 | 122 |
| AUDUSD | M15 | 4 | 111 |
| AUDUSD | M5 | 6 | 76 |

## Evidencias

- `reports/phase2_2_2b/validation_summary.json`
- `reports/phase2_2_2b/preflight.json` y `source_inventory.json`
- `reports/phase2_2_2b/initial_gap_audit.json` y `post_backfill_audit.json`
- `reports/phase2_2_2b/download_summary.json` y `spread_quality_real.json`
- `reports/phase2_2_2b/dataset_readiness.json` y `future_research_manifest.json`
- La descarga bruta permanece local bajo `data/market_history/backfill_runs/` y está excluida de Git.

## Pendientes y siguiente trabajo

Persisten huecos cuyo carácter de sesión, festivo, DST o ausencia del feed no se puede decidir sin un calendario verificable del bróker. El saldo neto pasó de 77 a 74 gaps candidatos y de 1.761 a 1.776 revisiones de calendario; la campaña no interpola ni reclasifica esos huecos. El siguiente trabajo es conciliar el calendario del bróker y preparar sensibilidad de costes en ventanas continuas. La evaluación confirmatoria queda bloqueada hasta disponer de spreads bid/ask históricos y costes de comisión/slippage verificados. La comparación técnico/DeepSeek queda aplazada a una fase posterior autorizada; aquí no hubo llamadas LLM.
