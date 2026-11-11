# Fase 2.2.2 — resultados de históricos y calidad de datos

## Estado

**`PHASE2_2_2_COMPLETE` para auditoría offline y preparación de la recuperación.** La descarga no se ha realizado: **`HISTORY_BACKFILL_PREPARED`**, no `HISTORY_BACKFILL_COMPLETED`.

La revisión parte de `main` en el commit `2e61a68847ec82a0fd6420cad344640a037b7897` y se trabajó en `codex/phase2-2-2-history-data-quality`. El árbol de trabajo está modificado; no se hizo commit, push ni merge.

No se conectó a MT5, no se consultó Ollama ni DeepSeek, no se ejecutó backtest y no se enviaron órdenes.

## Auditoría de históricos

Se leyeron los 15 CSV antiguos de `data/market/` y los 15 CSV de la captura REAL read-only `data/market_history/mt5_real_readonly/20261109T202112.171333Z/`: **514.054 filas** en total.

Para todas las fuentes se verificaron UTC, orden cronológico, duplicados, alineación temporal, velas abiertas/futuras, campos OHLC y relaciones entre máximo, mínimo, apertura y cierre. No se encontraron timestamps inválidos, filas malformadas, duplicados, errores OHLC ni barras abiertas/futuras. Las comprobaciones de SHA-256 del manifiesto pasan en los 15 CSV nuevos al normalizar CRLF; el SHA-256 de los bytes del checkout difiere por finales de línea. También coinciden el número de filas y el último cierre declarado en el manifiesto. Los originales permanecieron intactos durante la auditoría.

Las fuentes antiguas y recientes **no se solapan en timestamps**, por lo que no se puede verificar una igualdad de precios entre ambas en un mismo instante. No hay conflictos de precio observados; el estado correcto es “sin puntos coincidentes para comparar”, no una afirmación de que los precios de cada fuente se confirmaron entre sí.

| Timeframe | Histórico anterior | Captura reciente | Puente entre ambas |
|---|---|---|---|
| M5 | EURUSD: 2025-06-06 04:15 a 2026-10-12 06:35 UTC; USDCAD: 2025-06-06 04:10 a 2026-10-12 06:35; AUDUSD: 2025-06-06 03:45 a 2026-10-12 06:35 | 2026-11-04 11:05 a 2026-11-09 20:15 | En los tres pares faltan las aperturas de vela 2026-10-12 06:40–2026-11-04 11:00 UTC: 6.677 posiciones de cadencia por par. Incluye cierres de mercado; los días laborables/festivos siguen pendientes de confirmar con calendario o datos. |
| M15 | 2024-09-24 07:00 a 2026-10-12 06:15 | 2026-10-26 12:30 a 2026-11-09 20:00 | En los tres pares faltan 2026-10-12 06:30–2026-10-26 12:15 UTC: 1.368 posiciones de cadencia por par. El horario de mercado de cada posición no se presupone. |
| H1 | 2024-09-09 00:00 a 2026-10-12 05:00 | 2026-10-12 06:00 a 2026-11-09 19:00 | Sin posiciones de cadencia vacías en el puente. |
| H4 | 2024-07-10 08:00 a 2026-10-12 00:00 | 2026-10-12 04:00 a 2026-11-09 16:00 | Sin posiciones de cadencia vacías en el puente. |
| D1 | 2023-07-17 a 2026-10-09 | 2026-10-12 a 2026-11-06 | Posiciones del 10 y 11 de octubre: sábado clasificado como cierre esperado candidato; domingo permanece pendiente de horario semanal. |

El informe enumera **77 intervalos candidatos de recuperación** con sus límites UTC exactos y **1.758 intervalos para revisión de calendario/horario**. Son intervalos detectados dentro de los conjuntos y sus puentes, no una declaración de que cada posición corresponda a una vela negociable. El plan deja los días de cierre, domingos, viernes y festivos sin calendario verificable pendientes de revisión. El modo `--dry-run` descompone el plan en 98 solicitudes con un máximo de 1.000 posiciones de cadencia y solapamiento de dos velas por lado.

Los detalles completos por fuente, fila e intervalo están en [`history_gap_audit.json`](../reports/phase2_2_2/history_gap_audit.json) y en [`future_mt5_backfill_plan.json`](../reports/phase2_2_2/future_mt5_backfill_plan.json).

## Calidad de spreads

El campo original `spread` se conserva en puntos MT5. Un cero se clasifica como `OBSERVED_ZERO_UNVERIFIED`: es un valor presente, pero no hay evidencia suficiente para afirmar que sea una cotización real de spread cero. Los campos ausentes, inválidos y supuestos se mantienen separados; **no se imputó ningún spread**. OHLC puede aprobar aunque la calidad de ejecución no permita inferencias económicas.

Porcentaje de ceros, mostrado como **histórico anterior / captura reciente / unión temporal**:

| Par | M5 | M15 | H1 | H4 | D1 |
|---|---:|---:|---:|---:|---:|
| EURUSD | 94,3414 / 94,9744 / 94,3475 % | 94,9035 / 94,9546 / 94,9045 % | 95,8737 / 95,9514 / 95,8766 % | 99,9429 / 100 / 99,9449 % | 100 / 100 / 100 % |
| USDCAD | 13,8813 / 32,9231 / 14,0642 % | 23,1146 / 60,6458 / 23,8316 % | 31,3857 / 76,1134 / 33,0243 % | 41,7357 / 93,5484 / 43,5070 % | 62,0977 / 100 / 62,9802 % |
| AUDUSD | 92,9141 / 94,9744 / 92,9339 % | 94,8307 / 94,9546 / 94,8330 % | 95,8276 / 95,9514 / 95,8321 % | 99,9429 / 100 / 99,9449 % | 100 / 100 / 100 % |

La captura REAL read-only del 9 de noviembre registró 8 puntos EURUSD, 10 USDCAD y 9 AUDUSD. Son observaciones puntuales y **no se extrapolaron** a las velas históricas. Las distribuciones de spreads positivos por percentil, sesión UTC y mes están en [`spread_quality_audit.json`](../reports/phase2_2_2/spread_quality_audit.json).

Todos los pares/timeframes quedan con `execution_inference_eligible=false`: los ceros no están verificados y faltan series de costes completos del bróker. La investigación técnica sí puede usar ventanas con OHLC íntegro, atribuyendo la fuente y sin calcular indicadores a través de huecos aún no resueltos.

## Actualización incremental y seguridad

La herramienta [`backfill_market_history.py`](../scripts/backfill_market_history.py) funciona en `--dry-run` sin importar MetaTrader5 y solo muestra el plan. Descargar requerirá explícitamente `--download --confirm-real-read-only`; el plan y el entorno deben volver a verificarse entonces. La ruta de descarga reutiliza la allowlist y las guardas del validador read-only existente, exige cuenta REAL y `tradeapi_disabled=True` alrededor de cada lectura `copy_rates_range`, y rechaza cuentas no verificadas.

Las respuestas se validan por UTC, cadencia, OHLC, spread/volumen, duplicados y cierre de vela. Se guardan solo bajo `data/market_history/backfill_runs/<run-id>/`, con manifiesto SHA-256 y reanudación por chunks ya verificados. Conflictos conservan ambas variantes y bloquean cualquier afirmación de continuidad. La descarga nunca declara por sí sola el backfill terminado: necesita una nueva auditoría de todos los datos.

La opción de descarga **no se ejecutó** en esta fase. No hubo conexión a MT5: el dry-run confirmó `mt5_imported=false`, cero conexiones, cero órdenes y cero consultas de proveedor.

## Limpieza, baseline y pruebas

`.gitignore` excluye `__pycache__/`, `*.py[cod]` y `*$py.class`. Los 66 archivos compilados que estaban seguidos por Git se retiraron del seguimiento; el contenido local no se borró. No se modificaron los CSV originales ni `data/backtest/technical_candidate_scan.csv`.

El SHA-256 del baseline permanece `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.

La validación ejecutó siete comandos: auditoría, cinco módulos de pruebas y dry-run. **86 pruebas, 86 superadas, 0 fallidas y 0 omitidas**, sin conexión a MT5, sin proveedor LLM y sin backtests. El resumen verificable está en [`validation_summary.json`](../reports/phase2_2_2/validation_summary.json).

En una primera pasada, una prueba existente rechazó `copy_rates_range` porque su lista esperada de métodos read-only aún no incluía el método requerido para esta recuperación. Se añadió ese método a la expectativa de la prueba y se repitió la validación completa; la pasada final es la que queda registrada en el resumen.

## Reproducción futura en Windows

Desde la raíz del repositorio, con el entorno virtual activado:

```powershell
python scripts/validate_phase2_2_2.py
python scripts/backfill_market_history.py --dry-run
```

Cuando el usuario esté frente al terminal y autorice expresamente la recuperación, revisar primero el plan y las fechas de calendario. Después, en la cuenta REAL con `tradeapi_disabled=True` confirmado, iniciar un run versionado:

```powershell
python scripts/backfill_market_history.py --download --confirm-real-read-only --run-id backfill-20261109
```

El comando de descarga solo recupera históricos en archivos nuevos; no cambia órdenes ni la configuración de la cuenta. Tras completar, ejecutar de nuevo `python scripts/audit_market_history.py` y revisar conflictos, hashes y continuidad antes de declarar `HISTORY_BACKFILL_COMPLETED`. La herramienta puede devolver `PREPARED_WITH_REVIEW` cuando falten respuestas o haya conflictos; no se deben resolver esos casos copiando datos sobre los originales.

## Limitaciones

- La recuperación M5/M15 está preparada pero no ejecutada; por tanto, no se declara continuidad histórica completa.
- Sin calendario verificado del bróker, los huecos de fines de semana, horarios semanales y festivos no se convierten automáticamente en errores ni se descartan como cierres.
- Las fuentes no se solapan temporalmente; no existe comparación de precio entre fuentes en un mismo timestamp.
- Los spreads no son aptos para confirmar rentabilidad. No se ejecutó análisis de sensibilidad ni backtest en esta fase.
- La captura nueva termina el 9 de noviembre de 2026 y no contiene velas posteriores.
- La Fase 1 conserva sus tareas manuales pendientes documentadas en [`FASE1_CIERRE.md`](FASE1_CIERRE.md). La prueba DEMO sigue siendo no aplicable al entorno actual; la validación REAL read-only independiente se completó el 9 de noviembre. No hay autorización de ejecución de órdenes.

## Nota de seguimiento — Fase 2.2.2a

El trabajo posterior de endurecimiento del backfill se documenta en [`FASE2_2_2A_RESULTADOS.md`](FASE2_2_2A_RESULTADOS.md). El informe nuevo de auditoría puede incorporar exactamente un run explícito, verificar plan, manifiesto y hashes canónicos/raw de cada chunk, preservar conflictos y recalcular la continuidad. La política portable solo normaliza finales CRLF/LF. Los planes y manifiestos previos de esquema 1 se rechazan expresamente; hay que crear un plan nuevo de esquema 2 para una recuperación posterior.

La prueba completa de ciclo usa datos y un proveedor MT5 sintéticos. No es una recuperación real ni modifica las conclusiones históricas anteriores. El estado real permanece `HISTORY_BACKFILL_PREPARED`; esta nota no declara `HISTORY_BACKFILL_COMPLETED`. La validación final de esta fase está en [`phase2_2_2a/validation_summary.json`](../reports/phase2_2_2a/validation_summary.json), sin sobrescribir informes de Fase 2.2.2.
