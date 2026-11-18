# Fase 2.2.2h — resultados

## Estado

**Ingeniería offline implementada; CI remoto pendiente de ejecutar en la PR.** La auditoría de datos y el manifiesto de procedencia pasan localmente. La paridad del pipeline está cubierta con datos sintéticos. La evaluación económica del histórico real permanece bloqueada y no se ejecutó.

| Comprobación | Estado | Evidencia |
|---|---|---|
| Integridad de las 15 series 2023+ | PASS | `reports/phase2_2_2h/public_2023_integrity.json`; 1.252.070 filas, 15/15 series y rangos iguales a fase g |
| Integridad de hashes fase g | PASS | Los 15 SHA declarados coinciden con la variante CRLF reconstruida desde los bytes LF actuales |
| Manifiesto ligado a bytes actuales | PASS | `reports/phase2_2_2h/public_2023_source_manifest.json`, validación independiente y rehash de las 15 fuentes |
| Paridad causal scan/backtest | PASS SINTÉTICO | Pruebas de cinco timeframes, warmup, DST, brecha, barra parcial y fingerprint; véase el informe de validación |
| Calendario histórico oficial | UNKNOWN | No hay fuente oficial fechada por símbolo configurada; loader real continúa fail-closed |
| Backtest económico del histórico público | NOT_RUN | Bloqueado por calendario y costes verificables pendientes; no se generaron candidatos ni PnL real |
| GitHub Actions | PENDING | El run anterior #7 falló en `setup-python` antes de los tests; la reparación se verificará en la PR |
| Llamadas externas / MT5 / órdenes / LLM | 0 | Trabajo realizado con archivos del repositorio, mocks y fixtures sintéticos |

La rama parte del commit `bf5828ef589479e3996b61dec119e9b2bab337a4` (`origin/main` verificado) y se llama `codex/phase2-2-2h-remote-dataset-ci`. El audit usa como `base_sha` el HEAD de ese commit y el corte fijo de fase g `2026-11-17T20:13:49.261352+00:00`; no sustituye ese corte por la fecha actual.

## Hallazgos e implementación

- El error de Actions se confirmó en el run `44248662116`: `actions/setup-python@v5` pidió cache de pip, pero el repo no contiene `requirements.txt` ni `pyproject.toml`. La corrección mínima elimina `cache: pip`; las dependencias offline que sí se necesitan se instalan explícitamente.
- La preparación de timestamps del adaptador histórico fallaba con pandas 2.2.3 al mezclar separadores ISO (`T` y espacio). Se especificó `format="mixed"`, sin cambiar el cálculo de indicadores.
- Un test de fase 2.2.2 aplicaba un corte antiguo del 15 de noviembre y esperaba los huecos de una captura previa. Ahora usa el `as_of_utc` congelado de fase g y comprueba que las fuentes actuales se solapan con los snapshots MT5 antiguos. No se reescribió ningún histórico.
- Se añadió una auditoría de streaming, determinista y de solo lectura. Verifica inventario 3×5, cabeceras, fechas UTC, orden, unicidad, rango, barra cerrada, OHLC, volúmenes, spreads, conteos, hashes, último corte y el scan congelado.
- El manifiesto registra hash bruto y canónico CRLF/LF bajo `CSV_LINE_ENDINGS_LF_V1`. La normalización toca exclusivamente finales de línea; no toca campos ni filas. El manifiesto puede cruzar Linux/Windows si el hash canónico y la relación con el SHA de fase g verifican la misma información y solo cambia LF/CRLF. El validador vuelve a leer el snapshot y reaudita los bytes, y rechaza fuentes cambiadas, manifiestos forjados (incluido un SHA bruto falsificado aunque se re-firme el JSON), rutas fuera de alcance, archivos ausentes, symlinks e inventarios incorrectos.
- El adaptador público 2023+ se integra con `validate_history_source_manifest` y `VerifiedResearchDataset.load_from_sources`. Sin un manifiesto de sesiones oficial, `load_public_2023_verified_dataset` devuelve `BROKER_CALENDAR_UNKNOWN`.
- El workflow ahora ejecuta explícitamente las pruebas f/g, auditoría pública, suites offline, compilación, look-ahead, privacidad y diff check. No instala MetaTrader5 ni accede a servicios externos.

## Dataset y hashes

La auditoría encontró exactamente 15 CSV, 1.252.070 filas agregadas y ningún timestamp, OHLC, volumen o campo de spread inválido. El scan congelado conserva SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.

El hash bruto del snapshot fase g se generó con bytes CRLF en Windows. El checkout actual contiene LF. Para las 15 series, el SHA actual no coincide como hash de bytes, pero la reconstrucción de CRLF da coincidencia exacta; la auditoría lo reporta como `CRLF_EOL_EQUIVALENT`. El hash bruto y el canónico de los CSV actuales son iguales entre sí porque los archivos usan LF.

| Símbolo | TF | Filas | Spread cero | Mediana positiva reportada |
|---|---:|---:|---:|---:|
| EURUSD | M5 | 289.633 | 94,702952 % | 17 |
| EURUSD | M15 | 96.552 | 95,141478 % | 14 |
| EURUSD | H1 | 24.140 | 95,816073 % | 9 |
| EURUSD | H4 | 6.035 | 99,917150 % | 3 |
| EURUSD | D1 | 1.005 | 100,000000 % | — |
| USDCAD | M5 | 289.623 | 50,446270 % | 1 |
| USDCAD | M15 | 96.551 | 54,807304 % | 1 |
| USDCAD | H1 | 24.140 | 60,455675 % | 1 |
| USDCAD | H4 | 6.035 | 65,998343 % | 1 |
| USDCAD | D1 | 1.005 | 68,358209 % | 1 |
| AUDUSD | M5 | 289.620 | 94,015261 % | 17 |
| AUDUSD | M15 | 96.551 | 95,048213 % | 17 |
| AUDUSD | H1 | 24.140 | 95,745650 % | 11 |
| AUDUSD | H4 | 6.035 | 99,884010 % | 2 |
| AUDUSD | D1 | 1.005 | 100,000000 % | — |

Los valores positivos están expresados en las unidades enteras del campo exportado, no en pips ni en precio. No se verificó la unidad contra metadatos del bróker; la mediana no se presenta como coste monetario. Un cero se clasifica `OBSERVED_ZERO_UNVERIFIED`; no se reemplaza ni se interpreta como spread real cero.

Se observaron 3.167 saltos de cadencia, igual al recuento de intervalos no confirmados de fase g. Son discontinuidades de timestamps, **no** una cuenta de velas perdidas. El informe conserva duración y distribución por timeframe, mes y día UTC; todos los intervalos siguen `UNKNOWN`. No se declaró ningún cierre de fin de semana o festivo sin calendario oficial.

## Pruebas

Comando reproducible principal: `python scripts/validate_phase2_2_2h.py`. Ejecuta las suites offline, compilación, look-ahead, privacidad, auditoría determinista, diff check y verificación de que el scan/CSV congelado no cambiaron; escribe `reports/phase2_2_2h/validation_summary.json`. También se ejecutaron sus subcomandos individualmente para aislar resultados.

Resultados capturados: fase 2.2.2 relacionada **91/91 PASS**; auditoría pública **13/13 PASS**; f **11/11 PASS**; g **16/16 PASS**; macro/dataset/paridad **16/16 PASS**; backtest fase 1 **16/16 PASS**; seguridad MT5 **21 PASS y 1 SKIPPED explícito** por fixture de symlink/junction no disponible en una prueba heredada; look-ahead **375/375 PASS**; privacidad **PASS**; compileall **PASS**. En total: **185 pruebas**, **184 PASS**, **0 FAIL**, **1 SKIPPED**. El validador principal reejecuta estas comprobaciones y registra cada comando, versión y resultado observado.

La prueba sintética de paridad usa las cinco temporalidades, más de 200 barras de warmup, la transición DST del 8 de marzo de 2026, una discontinuidad preservada, barras parciales, fingerprint compartido y rechazo de fingerprint distinto. Sus resultados no se mezclan con resultados económicos del histórico real.

## Resultados y bloqueos

- **PASS:** integridad actual del snapshot, manifiesto de procedencia, normalización limitada a CRLF/LF y fixtures negativos.
- **PASS:** el baseline congelado no cambió; ningún CSV de mercado fue escrito por la herramienta.
- **PASS SINTÉTICO:** el flujo scan → estado → plan → simulación usa el mismo dataset y fingerprint, con controles temporales.
- **UNKNOWN:** calendario oficial del bróker fechado por símbolo; los 3.167 saltos permanecen sin clasificar.
- **PENDING:** el run de GitHub Actions sobre esta rama. La reparación local no se etiqueta como CI verde hasta observar un run con conclusión `success`.
- **NOT_RUN:** backtest económico real, DeepSeek/Ollama y cualquier conexión o consulta MT5.

La evidencia completa y por fila está en `reports/phase2_2_2h/public_2023_integrity.json`; los hashes y la cobertura del manifiesto están en `reports/phase2_2_2h/public_2023_source_manifest.json`. La síntesis ejecutable está en `reports/phase2_2_2h/validation_summary.json`.
