# Fase 2.2.2c — calendario, ventanas y costes

**Estado final:** **PHASE2_2_2C_PARTIAL_CALENDAR_UNKNOWN**

Esta fase verificó offline el backfill 2.2.2b y separó los tramos de barras contiguas de los cierres de mercado que no pueden demostrarse con el calendario disponible. No se ejecutó un backtest sobre barras nuevas.

## Entorno y Git

- main remoto al comenzar: 2f4fe82c1bb3e7d5c88d8d302cc0362e7c8a9bbc (merge de PR #16).
- Rama de trabajo: codex/phase2-2-2c-calendar-windows-cost-sensitivity.
- Python: entorno virtual del repositorio, Python 3.13.
- El run privado phase222b-local-20261115 existe localmente. No se publicó su contenido.
- No se creó commit, no se hizo push ni merge.

## Integridad de fuentes

- El verificador recalculó la integridad del manifiesto y de los **98/98 chunks**: **PASS**, 18.175 filas, hashes raw y canónicos coincidentes con el run y el plan congelado.
- Se auditaron **47 fuentes**: 45 CSV de mercado y 2 manifiestos de snapshots; **540.143 filas** de fuentes originales.
- Hashes raw/canónicos de las fuentes sin cambios; cero conflictos de OHLC en solapamientos y cero conflictos entre el backfill y sus fuentes.
- data/backtest/technical_candidate_scan.csv no se modificó. Su SHA-256 canónico sigue siendo 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3.
- Los informes contienen hashes, recuentos y timestamps, no precios OHLC ni identificadores de cuenta. Los CSV brutos y el manifiesto del run permanecen bajo el directorio local excluido de Git.

## Calendario y clasificación de huecos

No hay identidad de bróker ni especificación oficial de sesiones con vigencia, zona horaria y regla DST en los artefactos autorizados. No se infirieron cierres por día de semana, fin de semana, DST o festivo.

| Clase | Intervalos | Resultado |
|---|---:|---|
| Cierre de sesión verificado | 0 | Sin evidencia oficial específica del bróker |
| Sesión abierta con barra ausente verificada | 0 | Sin calendario/feed verificable |
| Sesión abierta, feed incierto | 0 | Sin evidencia de sesión abierta |
| Calendario del bróker indeterminado | 1.850 registros | 74 candidatos de recuperación y 1.776 grupos de revisión |
| Conflicto o dato inválido | 0 | La integridad OHLC pasó |

Los 74 candidatos y los 1.776 grupos se conservan como categorías de auditoría separadas; algunos describen intervalos de cadencia relacionados y no se suman como huecos únicos. El detalle por símbolo/timeframe y los intervalos UTC están en reports/phase2_2_2c/gap_classification.json.

## Ventanas contiguas y MTF

Se construyeron ventanas con timestamps UTC, cadencia exacta, fuente y SHA-256 por tramo. Un hueco no se cruza con rolling windows ni se rellena. Variantes con OHLC distinto se excluyen; la procedencia de variantes equivalentes se conserva. El spread nunca se elige silenciosamente entre fuentes.

El mínimo funcional es **200 barras contiguas**, tomado de EMA200 min_periods=200 en src/indicators.py. El informe distingue **691 barras** para el criterio existente de estabilización EMA200 al 0,1% en src/warmup_audit.py.

| Timeframe | Barras únicas por par | Tramos contiguos por par | Tramo más largo | Tramos ≥200 barras | Tramos ≥691 |
|---|---:|---:|---:|---:|---:|
| M5 | 107.035 | 82–83 | 1.440 | 82 | 76 |
| M15 | 53.044–53.045 | 116–117 | 480 | 110 | 0 |
| H1 | 13.532 | 118 | 120 | 0 | 0 |
| H4 | 3.639 | 127 | 30 | 0 | 0 |
| D1 | 861 | 178 | 5 | 0 | 0 |

Los recuentos por par, extremos UTC y procedencia de cada tramo están en eligible_windows.json. El análisis comprobó **159.133 decisiones M15**. Ninguna dispone de 200 barras cerradas y contiguas en las cinco temporalidades requeridas por el estado técnico intradía. H1, H4 y D1 quedan segmentadas por huecos cuyo estado de calendario no está probado; no se atravesaron.

Cada decisión M15 usa la hora de cierre de esa barra. Una barra de otra temporalidad solo se admite si su cierre es menor o igual a la decisión y no está obsoleta. Se detectaron **0 look-ahead**; las pruebas verifican el corte causal. No hay ventana MTF completa ni ventana apta para simular ejecuciones.

El campo de spread tiene ceros sin verificar y observaciones positivas. Estas últimas no prueban por sí solas una cotización bid/ask ejecutable. No se verificaron comisión, slippage ni conversión de costes a EUR.

## Backtest y costes

**NEW_BAR_BACKTEST_BLOCKED — no se ejecutó backtest con el run recuperado.**

run_portfolio_backtest.py y run_stateful_backtest.py consumen un argumento --scan precomputado. El estado histórico existente carga data/market y no tiene un productor verificado que convierta los nuevos snapshots/chunks en un scan con paridad de señales, MTF causal y warmup suficiente. Reutilizar el scan congelado no demostraría un backtest de las barras de noviembre.

La sensibilidad BASE, CONSERVATIVE, STRESS y floors de 0/5/10/20 puntos quedó **PENDING_NOT_EXECUTED**. La configuración contiene supuestos de comisión/slippage que no están verificados con el bróker; por tanto no se publican métricas PnL, profit factor ni una conclusión económica. La comprobación de costes confirma que el spread ya está incorporado en precios bid/ask del simulador y no se debe descontar dos veces.

## Pruebas, privacidad y seguridad

- Suite offline 2.2.2a/b/c y simulador histórico: **77/77 PASS**, 0 fallidas, 0 omitidas.
- Los 16 tests nuevos cubren evidencia incompleta/DST, fuentes y hashes, run ausente/rutas fuera de la carpeta local, conflictos, cortes de ventanas, barras cerradas MTF, warmup, selección independiente de PnL, censura de operaciones, conversión puntos/precio/lotes, doble spread y privacidad.
- Los 16 tests de ejecución histórica existentes pasaron; incluyen spread bid/ask aplicado una sola vez, órdenes sin fill, resultados ambiguos y costes EUR no calculables cuando falta conversión.
- El análisis hizo **0 conexiones MT5, 0 solicitudes de mercado, 0 llamadas LLM, 0 órdenes y 0 modificaciones de posiciones**.
- No se ejecutó el backtest congelado de regresión.

## Cómo repetir offline

Ejecuta: .venv\Scripts\python.exe scripts\analyze_phase2_2_2c.py --run-dir data\market_history\backfill_runs\phase222b-local-20261115

El modo acepta el directorio del run dentro de data/market_history/backfill_runs/, exige manifest.json y chunks/, y bloquea una ruta externa, un manifiesto faltante o contenido/hash modificado. No conecta a MT5 ni a proveedores LLM.

## Artefactos

- reports/phase2_2_2c/validation_summary.json
- reports/phase2_2_2c/source_integrity.json
- reports/phase2_2_2c/broker_calendar_evidence.json
- reports/phase2_2_2c/gap_classification.json
- reports/phase2_2_2c/eligible_windows.json
- reports/phase2_2_2c/mtf_causality_audit.json
- reports/phase2_2_2c/dataset_and_backtest_readiness.json
- reports/phase2_2_2c/cost_sensitivity.json
- reports/phase2_2_2c/next_phase_gates.json
- reports/phase2_2_2c/offline_test_results.json

## Siguiente fase propuesta

1. Obtener la especificación oficial y fechada del calendario del bróker para cada símbolo, incluidos zona horaria, DST, festivos y cambios de horario.
2. Reconciliar con esa evidencia los 74 candidatos y 1.776 grupos sin asumir que todo intervalo vacío corresponde a cierre.
3. Si la evidencia permite ventanas MTF con warmup suficiente, crear y validar un productor de scan desde barras contra los componentes técnicos deterministas existentes.
4. Verificar bid/ask histórico, comisión por lote/vuelta, slippage y conversión a EUR antes de ejecutar la sensibilidad preregistrada.

La evaluación económica confirmatoria y cualquier experimento prospectivo siguen bloqueados. Esta fase no inicia la siguiente ni habilita trading.
