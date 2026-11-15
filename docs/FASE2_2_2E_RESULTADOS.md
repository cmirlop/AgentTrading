# Resultados de Fase 2.2.2e

**Estado:** PHASE2_2_2E_ENGINEERING_COMPLETE_WITH_DATA_BLOCKERS

## Entorno y seguridad

- Windows, Python 3.13.14; base main 6d1f0f7e3e696aa3b773bed997b9d813f1eba7dc; rama codex/phase2-2-2e-local-macro-and-backtest-integration.
- Árbol de trabajo inicial limpio. No se creó commit, push, PR ni merge.
- Run privado phase222b-local-20261115: 98/98 chunks verificados, 18175 filas del run previo; hashes fuente sin discrepancias: PASS.
- Dataset congelado canónico SHA-256: 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3; intacto: True.
- MetaTrader5 Python disponible: 5.0.6231; terminal no conectado ni leído en esta fase.
- orders_sent=0; market_history_requests=0; llm_provider_calls=0.

## Resultados

| Comprobación | Estado | Evidencia |
|---|---|---|
| Tests offline | PASS | 145 pruebas unittest; reports/phase2_2_2e/offline_test_results.json |
| Fuentes macro gratuitas | MACRO_PARTIAL_COVERAGE | 4 peticiones; macro_source_audit.json |
| Calendario del bróker | BROKER_CALENDAR_UNKNOWN | 0 de 1.850 resueltos; no hay documento histórico oficial |
| Continuidad MTF | REAL_MTF_WINDOWS_UNVERIFIED | 0 ventanas certificadas; EMA200 estabiliza a 692 barras |
| Paridad scan/backtest | SCAN_BACKTEST_PIPELINE_PARITY_PASS_SYNTHETIC | Integración sintética y rechazo por fingerprint/periodo |
| Costes | PASS_HYPOTHESIS_ONLY | Spread bid/ask sin doble cargo; costes EUR reales sin verificar |
| Backtest económico nuevo | BACKTEST_NOT_RUN | Calendario y ventanas MTF sin certificar |
| Look-ahead | PASS | 375 controles |
| Operaciones | PASS | Cero llamadas MT5, órdenes y ejecuciones en esta fase |

## Pruebas ejecutadas

- 2.2.2a-d offline suite: **PASS** (91 tests).
- macro parser and safety: **PASS** (13 tests).
- verified dataset/backtest parity: **PASS** (3 tests).
- historical backtest engine: **PASS** (16 tests).
- MT5 read-only and order safety: **PASS** (22 tests).
- look-ahead: **PASS**.
- Python compile: **PASS**.

## Comparativa económica

No se ejecutó backtest económico nuevo, por lo que no hay resultados comparables. BASE, CONSERVATIVE y STRESS siguen siendo hipótesis; no se modificó estrategia ni se seleccionó ventana por PnL.

## Conclusión

La ingeniería queda preparada para revisión si las pruebas pasan. La fase no certifica el calendario del bróker, ventanas MTF reales ni ventaja de trading. Los 1.850 registros siguen UNKNOWN. Se necesita el documento histórico oficial del bróker para evaluar si existe una ventana causal elegible.

Ingeniería preparada ≠ calendario certificado ≠ ventaja de trading demostrada.
