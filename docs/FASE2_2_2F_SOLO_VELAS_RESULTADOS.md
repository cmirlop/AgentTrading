# Fase 2.2.2f — Sincronización read-only de velas MT5

**Estado:** PARTIAL_PROVIDER_CALL_BUDGET

Corte UTC: `2026-11-15T21:20:30.715013+00:00`. Fecha local del PC: `2026-11-15`.

Cuenta REAL verificada y `tradeapi_disabled=True`: **PASS**. El proceso envió 0 órdenes y ejecutó 0 operaciones.

Consultas de barras MT5: **1500 / 1500**. Velas nuevas: **0**.

Dataset congelado intacto: **True**; SHA-256 canónico `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.

La tabla enumera exactamente los 15 pares símbolo/temporalidad. Los huecos son no confirmados: no se infiere cierre de mercado ni se fabrican velas.

| Símbolo | TF | Primera vela | Última vela cerrada | Filas | Nuevas | Huecos no confirmados | Estado |
|---|---|---|---|---:|---:|---:|---|
| EURUSD | M5 | 2024-11-15T15:30:00+00:00 | 2026-11-13T23:55:00+00:00 | 148704 | 0 | 112 | PARTIAL_PROVIDER_CALL_BUDGET |
| EURUSD | M15 | 2020-11-24T00:30:00+00:00 | 2026-11-13T23:45:00+00:00 | 148704 | 0 | 325 | PARTIAL_PROVIDER_CALL_BUDGET |
| EURUSD | H1 | 2002-11-29T10:00:00+00:00 | 2026-11-13T23:00:00+00:00 | 148704 | 0 | 1288 | PARTIAL_PROVIDER_CALL_BUDGET |
| EURUSD | H4 | 1971-01-04T00:00:00+00:00 | 2026-11-13T20:00:00+00:00 | 50556 | 0 | 8638 | COMPLETE_VS_MT5_OBSERVED |
| EURUSD | D1 | 1971-01-04T00:00:00+00:00 | 2026-11-13T00:00:00+00:00 | 14402 | 0 | 2980 | COMPLETE_VS_MT5_OBSERVED |
| USDCAD | M5 | 2024-12-19T21:20:00+00:00 | 2026-11-13T23:55:00+00:00 | 141718 | 0 | 108 | PARTIAL_PROVIDER_CALL_BUDGET |
| USDCAD | M15 | 2021-02-22T15:00:00+00:00 | 2026-11-13T23:45:00+00:00 | 142716 | 0 | 313 | PARTIAL_PROVIDER_CALL_BUDGET |
| USDCAD | H1 | 2003-11-12T06:00:00+00:00 | 2026-11-13T23:00:00+00:00 | 142716 | 0 | 1239 | PARTIAL_PROVIDER_CALL_BUDGET |
| USDCAD | H4 | 1993-04-28T00:00:00+00:00 | 2026-11-13T20:00:00+00:00 | 44858 | 0 | 2960 | COMPLETE_VS_MT5_OBSERVED |
| USDCAD | D1 | 1993-04-28T00:00:00+00:00 | 2026-11-13T00:00:00+00:00 | 8719 | 0 | 1771 | COMPLETE_VS_MT5_OBSERVED |
| AUDUSD | M5 | 2024-11-12T03:30:00+00:00 | 2026-11-13T23:55:00+00:00 | 149702 | 0 | 116 | PARTIAL_PROVIDER_CALL_BUDGET |
| AUDUSD | M15 | 2020-11-09T15:15:00+00:00 | 2026-11-13T23:45:00+00:00 | 149702 | 0 | 326 | PARTIAL_PROVIDER_CALL_BUDGET |
| AUDUSD | H1 | 2002-08-05T03:00:00+00:00 | 2026-11-13T23:00:00+00:00 | 150700 | 0 | 1311 | PARTIAL_PROVIDER_CALL_BUDGET |
| AUDUSD | H4 | 1993-04-27T00:00:00+00:00 | 2026-11-13T20:00:00+00:00 | 44863 | 0 | 2958 | COMPLETE_VS_MT5_OBSERVED |
| AUDUSD | D1 | 1993-04-27T00:00:00+00:00 | 2026-11-13T00:00:00+00:00 | 8719 | 0 | 1770 | COMPLETE_VS_MT5_OBSERVED |

Idempotencia de segundo pase: `True`. Evidencia privada (incluye chunks brutos y copias previas): `reports/private/phase2_2_2f/20261115T205056Z`.

No se ejecutaron señales, análisis, backtests ni llamadas LLM. Los datos de cuenta y el contenido bruto no se publican.
