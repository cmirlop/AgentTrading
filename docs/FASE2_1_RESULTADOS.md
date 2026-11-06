# Fase 2.1 — resultados de auditoría estadística

Fecha de generación: 2026-11-06 UTC
Estado: `PHASE2_1_INCOMPLETE`
Conclusión de la estrategia: `INCONCLUSIVE`
Valor añadido de DeepSeek: `INCONCLUSIVE_INSUFFICIENT_DATA`

## 1. Resumen ejecutivo

Se reprodujeron, con el simulador actual, los escenarios BASE, CONSERVATIVE y STRESS para el dataset congelado y se ejecutaron ambos motores: portfolio y stateful. La referencia portfolio 65/LIMIT reproduce los valores documentados con una diferencia inferior a 0,01 EUR. También se evaluó por separado un dataset anterior de validación. No se mezclaron ambos periodos.

La lectura económica no es robusta a los costes ni al periodo. Para 65/LIMIT, el periodo principal da +25,62 EUR netos BASE, −27,53 EUR CONSERVATIVE y −74,39 EUR STRESS. El periodo anterior da pérdidas en los tres escenarios: −28,61, −119,25 y −188,87 EUR. El TEST temporal principal tiene resultados positivos en BASE y ligeramente positivos en CONSERVATIVE, pero contiene solo seis semanas independientes y no permite un intervalo de confianza fiable. El intervalo bootstrap de todo el periodo BASE cruza cero.

No existe un conjunto de decisiones DeepSeek verificables y emparejables con las oportunidades evaluadas. Por tanto, no se puede medir su valor añadido. No se hicieron llamadas a DeepSeek, Ollama ni MT5, y no se enviaron órdenes.

La evaluación está técnicamente incompleta: `pytest` no está disponible, las diferencias con algunas tablas históricas archivadas no están atribuidas, la preparación sobre `main` no pudo actualizarse por un fallo de red y la Fase 1 sigue `INCOMPLETE_PENDING_MANUAL`.

## 2. Repositorio y procedencia

- Commit de trabajo: `742ad936ab2049cc263fff8b67bef52cdf32b35a`.
- Rama: `codex/phase2-statistical-evaluation`.
- Worktree aislado: `/workspace/AgentTrading-phase2`.
- El commit de referencia pedido `ee10f99b3fa84a07c1fcb06eeb3820d5ef27c5d3` no estaba disponible localmente. `origin/main` estaba tres commits detrás del HEAD accesible; `git fetch origin` falló porque el proxy de este entorno rechazó la conexión.
- No se hicieron commit, push ni merge. Los cambios originales en el worktree de trabajo permanecen preservados.
- Python 3.12.14, pandas 3.1.0 y numpy 2.5.4. `pytest` no está instalado.
- Los informes de validación conservan commit, rama, estado del árbol, hashes y resultados observados. El árbol tiene cambios de investigación sin commit.

## 3. Calidad de los datos

### Dataset primario congelado

`data/backtest/technical_candidate_scan.csv`: 22.011 filas, EURUSD, USDCAD y AUDUSD; 2026-04-07 18:15–2026-10-07 18:15 UTC; 13 variables de contexto técnico conocidas al decidir; cero valores ausentes, duplicados exactos o claves símbolo/timestamp duplicadas. SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`, coincidente con el hash de referencia.

### Dataset histórico separado

`data/backtest/validation_candidate_scan.csv`: 35.679 filas, los mismos tres pares; 2025-06-10 07:00–2026-04-07 18:00 UTC; cero valores ausentes y duplicados. SHA-256 `8c5bd365d54545e34fb1c46340a8a3173d98e4aaa5ed8cf47d772251dc042a52`. Se mantuvo separado del primario.

### Barras y calidad del spread

Hay históricos M5, M15, H1, H4 y D1 para cada uno de los tres pares, con timestamps UTC explícitos. En la revisión ejecutada no se encontraron timestamps duplicados ni celdas ausentes. Los CSV de barras M5 tienen aproximadamente 100.538 registros por par, desde junio de 2025 hasta el 12 de octubre de 2026. No hay datos originales de ticks.

La cobertura de spread del histórico M5 no es suficientemente fiable para interpretar BASE como coste real: el spread es cero en 94,34 % de EURUSD y 92,91 % de AUDUSD; sus percentiles 95 son 5 puntos y los máximos 200/300. USDCAD contiene 13,88 % de ceros, percentil 95 de 7 y máximo 300. La unidad procede de la interpretación del simulador y no se verificó contra el bróker.

Los escenarios CONSERVATIVE/STRESS son hipótesis explícitas, no tarifas del bróker: CONSERVATIVE aplica spread ×1,25, 7 EUR/lote ida y vuelta y 1 punto de slippage por lado; STRESS ×2, 10 EUR/lote y 3 puntos por lado.

### Decisiones LLM y otras fuentes

Se inventariaron 90 fuentes locales entre datasets, barras, logs y reportes. Se encontraron 13 archivos de decisiones guardadas, pero ninguno comparte una oportunidad primaria con procedencia completa de proveedor, modelo y prompt. Los 11 eventos del replay multiagente solapados con la ventana están marcados genéricamente como LLM/WAIT, sin atribución verificable a DeepSeek. Los reportes de prueba de conexión del proveedor no son decisiones de trading y se excluyeron.

## 4. Auditoría de leakage y reconstrucción

Las columnas de oportunidad incluidas como features son conocidas al momento de decisión; el chequeo rechazó columnas de resultados futuros. Las etiquetas de ejecución se calcularon con outputs actuales del simulador, separados de esas variables.

La comprobación preexistente `test_no_lookahead.py` pasó 375 verificaciones para EURUSD, USDCAD y AUDUSD. El dataset congelado se reconstruyó con 22.011 eventos y los mismos IDs, pero algunas puntuaciones cambiaron al extender el historial de calentamiento de indicadores. Las barras de precio/spread que se solapan no cambiaron; se añadieron 329.168 barras anteriores sumadas entre los archivos de mercado. Cambiaron, entre otras, 1.278 filas de `d1_score`, 1.200 de `setup_quality` y 1.298 de `overall_score`; variaron entre 13 y 20 pertenencias a cada umbral existente. La lógica técnica inspeccionada se mantiene igual que en el snapshot usado para producir el CSV. Para mantener una comparación homogénea, los resultados primarios usan el CSV congelado, no la reconstrucción reciente.

Esto detecta sensibilidad de inicialización/warmup y limita la reproducción exacta del proceso que generó originalmente el scan; no constituye por sí mismo evidencia de uso de datos futuros.

## 5. Baseline y escenarios de costes

Portfolio, umbral actual 65, modo LIMIT, sobre el periodo primario:

| Escenario | Propuestas | Ejecutadas | Cerradas | Ambiguas | Expiradas | Abiertas | Bruto EUR | Costes añadidos EUR | Neto EUR | Expectativa neta EUR | PF neto | DD máximo EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 1.737 | 686 | 628 | 57 | 1.050 | 2 | 25,62 | 0,00 | 25,62 | 0,0408 | 1,118 | 10,94 |
| CONSERVATIVE | 1.736 | 685 | 627 | 57 | 1.050 | 2 | 26,12 | 53,64 | −27,53 | −0,0439 | 0,890 | 36,69 |
| STRESS | 1.748 | 689 | 632 | 56 | 1.058 | 2 | 18,29 | 92,68 | −74,39 | −0,1177 | 0,735 | 77,59 |

El PnL bruto difiere entre escenarios porque cambiar spread y slippage también modifica ejecuciones y precios de entrada/salida. El spread bid/ask está incorporado en la simulación; `transaction_cost_eur` representa comisión y slippage modelados. No se volvió a descontar el spread como coste separado. BASE tiene comisión y slippage añadidos iguales a cero y una serie de spreads incompleta; por eso su PnL neto no acredita costes reales.

La reproducción coincide con la referencia previa: BASE +25,62008 frente a +25,62; CONSERVATIVE −27,52673 frente a −27,53; STRESS −74,39422 frente a −74,39. La diferencia máxima absoluta es 0,00423 EUR.

En el dataset anterior, portfolio 65/LIMIT produjo BASE bruto/neto −28,61 EUR; CONSERVATIVE bruto −32,27, costes añadidos 86,98 y neto −119,25; STRESS bruto −38,63, costes 150,24 y neto −188,87. Los conteos y el detalle están en `reports/phase2_1/validation_period_comparison.csv`.

El motor stateful también se ejecutó en los tres escenarios y se conservan sus resultados por símbolo. Sus operaciones por símbolo no equivalen a una cartera multi-par con restricciones agregadas, así que no se sumaron como resultado de portfolio.

## 6. Comparación de estrategias y calidad

ALWAYS HOLD se registra como abstención con cero operaciones y cero PnL. TECHNICAL ONLY usa la configuración de 65/LIMIT vigente. Los umbrales preexistentes 75, 85 y 90 se presentan de forma descriptiva; no se eligió ninguno según TEST.

En portfolio BASE, los netos observados fueron +25,62 EUR para 65, +16,43 para 75, +10,65 para 85 y +24,43 para 90. En el periodo histórico anterior, fueron −28,61, −6,61, −21,70 y −3,33 EUR, respectivamente. Los calendarios de ejecución y ocupación de slots pueden cambiar entre umbrales; no es una comparación causal aislada. No se selecciona un umbral ganador.

Por símbolo, BASE 65/LIMIT en el periodo primario fue: AUDUSD −10,29 EUR (201 cierres), EURUSD +14,12 (202) y USDCAD +21,79 (225). Son hallazgos exploratorios de una sola ventana; no prueban persistencia por par. Las tablas de símbolo, mes y agrupaciones de régimen/sesión están en `symbol_analysis.csv`, `period_analysis.csv` y `regime_analysis.csv`. Esas estratificaciones no tienen suficiente tamaño independiente para tratarlas como confirmatorias.

## 7. Partición temporal y fuera de muestra

Se ordenaron los 132 días UTC únicos de la ventana primaria: 79 TRAIN, 26 VALIDATION y 27 TEST antes del embargo. La división deja 13.137, 4.032 y 4.338 filas y marca 504 filas dentro de los embargos. Hay un embargo de 24 horas en las fronteras; además se purgan operaciones cuyo intervalo señal–resolución/`release_time` cruza una partición o embargo.

El corte de entrenamiento es 2026-07-27 UTC; TEST empieza el 2026-09-01 UTC. El TEST queda protegido de selección en el script, pero no es un holdout confirmatorio: la ventana se había explorado antes y la configuración principal del proyecto la define como `EXPLORATION_DAYS`.

| Escenario TEST 65/LIMIT | Ejecutadas | Cerradas | Ambiguas | Abiertas | Neto EUR | Expectativa EUR/cierre | PF neto | Bloques semanales independientes | IC 95 % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| BASE | 142 | 128 | 13 | 2 | +11,69 | +0,0913 | 1,287 | 6 | insuficiente |
| CONSERVATIVE | 141 | 127 | 13 | 2 | +1,35 | +0,0106 | 1,029 | 6 | insuficiente |
| STRESS | 144 | 130 | 13 | 2 | −7,88 | −0,0606 | 0,853 | 6 | insuficiente |

Se generaron seis cortes walk-forward descriptivos. Las muestras por ventana tampoco bastan para afirmar consistencia estadística. El archivo `time_split.json` contiene conteos, purgas, métricas por partición y esos cortes.

## 8. Incertidumbre estadística

Se usó bootstrap por bloques semanales UTC, 2.000 repeticiones y semilla `20261106`. En el conjunto primario completo:

| Escenario | Cierres | Semanas | Expectativa neta media EUR | IC 95 % por bloques EUR |
|---|---:|---:|---:|---:|
| BASE | 628 | 27 | +0,0408 | [−0,0163; +0,0943] |
| CONSERVATIVE | 627 | 27 | −0,0439 | [−0,1014; +0,0109] |
| STRESS | 632 | 27 | −0,1177 | [−0,1796; −0,0588] |

El IC BASE incluye cero; CONSERVATIVE también incluye cero y tiene expectativa puntual negativa. STRESS es negativo bajo la hipótesis de coste adversa definida. El resultado TEST no dispone de diez bloques semanales, por lo que el programa lo marca como `INSUFFICIENT_DATA` y no publica un IC. Las semanas tampoco son una garantía de independencia total entre pares. Estas cifras no demuestran una ventaja positiva fuera de muestra.

## 9. Valor añadido de DeepSeek

Conclusión obligatoria: `INCONCLUSIVE_INSUFFICIENT_DATA`.

- Decisiones guardadas revisadas: 13 archivos.
- Decisiones con evento primario coincidente: 0.
- Decisiones atribuibles a DeepSeek con modelo/prompt verificables: 0.
- Pares comparables TECHNICAL ONLY vs DeepSeek: 0.
- Los 11 eventos del replay que coinciden son WAIT y no identifican el proveedor.

No se puede calcular cobertura, matriz de confusión, rendimiento contrafactual de rechazos o impacto de DeepSeek en PnL, drawdown y exposición. No se infiere la respuesta del modelo desde resultados futuros. No se realizaron llamadas nuevas.

## 10. Pruebas y reproducibilidad

- 14 tests unitarios nuevos de Fase 2.1: PASS.
- Prueba existente `test_no_lookahead.py`: PASS, 375 verificaciones.
- AST/compilación sintáctica de los tres archivos Python de Fase 2.1: PASS.
- Reproducción BASE repetida, portfolio y stateful: PASS; salidas idénticas para cada motor.
- Reproducción de los tres escenarios contra la referencia documentada: PASS dentro de 0,01 EUR.
- Análisis de columnas de features sin resultados futuros y escaneo de seguridad de órdenes: PASS.
- Suite general `pytest`: PENDING porque pytest no está instalado en el Python disponible.
- Atribución de reportes históricos archivados: PENDING. Los resultados antiguos difieren de los generados con el simulador actual y no se encontró suficiente procedencia para explicar todas las diferencias.
- `scripts/validate_phase2_1.py` terminó y escribió `validation_summary.json`; devuelve estado global incompleto al conservar comprobaciones obligatorias pendientes.

## 11. Riesgos de sesgo y limitaciones heredadas

- Dataset de candidatos congelado, no ticks originales; spreads ausentes/irregulares en EURUSD y AUDUSD.
- Comisiones/slippage verificables del bróker no disponibles.
- Warmup histórico extendido altera algunas puntuaciones reproducidas aun cuando los precios solapados coinciden.
- Ventana principal previamente explorada; TEST no es confirmatorio y solo tiene seis semanas independientes.
- Dependencia temporal, exposición simultánea y correlación entre pares reducen el tamaño efectivo.
- Logs LLM sin provenance completa, sin solapamiento elegible y sin evaluación emparejada.
- Diferencias con ciertos reportes archivados siguen sin atribución.
- La validación de Fase 1 continúa `INCOMPLETE_PENDING_MANUAL` por MT5 Demo, proveedor LLM local y demás controles indicados en su informe.
- El checkout disponible no permitió verificar HEAD actualizado de `main`; no hubo conectividad para `git fetch`.

## 12. Conclusiones

**P1 — Expectativa después de costes:** BASE es positivo en la ventana primaria, pero los escenarios con costes hipotéticos son negativos; el escenario BASE no tiene spreads suficientemente fiables y el IC incluye cero. No se acredita expectativa positiva.

**P2 — Consistencia por periodo/par/régimen:** no. El periodo anterior pierde dinero en los tres escenarios; los pares y meses difieren. Los subgrupos son exploratorios.

**P3 — Fuera de muestra:** resultado puntual positivo en TEST BASE y CONSERVATIVE casi plano, pero muestra insuficiente, ventana previamente examinada e intervalos de TEST no calculables. No es evidencia confirmatoria.

**P4 — Filtros de calidad:** cambian exposición y número de operaciones y no son consistentes entre periodos. Estos datos no justifican seleccionar un filtro.

**P5/P6 — DeepSeek y compensación por errores/costes:** no evaluables; cero pares elegibles.

**P7 — Fase 2.2:** la auditoría sugiere priorizar una mejor instrumentación y una muestra prospectiva emparejada. No justifica optimizar entradas ni declarar que la estrategia tiene ventaja estadística.

Estado final: `PHASE2_1_INCOMPLETE`; estrategia `INCONCLUSIVE`; DeepSeek `INCONCLUSIVE_INSUFFICIENT_DATA`.

## 13. Recomendaciones para futura investigación (sin iniciarla aquí)

1. Descartar la búsqueda de umbrales por rendimiento retrospectivo y la selección del umbral 90 porque luce mejor en un escenario: sería selección por resultado.
2. Descartar inferir valor de DeepSeek de logs sin modelo/prompt/proveedor o de ejemplos positivos elegidos manualmente.
3. Instrumentar desde ahora un registro inmutable de cada oportunidad y cada salida LLM: ID estable, timestamp UTC, hash del payload, proveedor/modelo/prompt, evidencias, respuesta, validez y decisión final. Mantener la misma política de muestreo para HOLD y BUY/SELL.
4. Recoger suficientes bloques temporales intactos y datos bid/ask/ticks con metadatos de origen, sin cambiar la estrategia durante el periodo de observación.
5. Antes de cualquier comparación futura, registrar protocolo, población y métrica primaria. Comparar pares emparejados en el mismo evento y reportar cobertura y contrafactuales aparte de operaciones ejecutadas.
6. Investigar en un protocolo separado la sensibilidad al calentamiento de indicadores y atribuir los reportes archivados con datos/configuración/commit exactos antes de usarlos como evidencia.
7. Mantener validación prospective/paper-only y DRY RUN. No pasar a trading real sobre la base de este análisis.

## 14. Reproducción

Desde la raíz del worktree, con el entorno Python del proyecto:

```bash
python -m unittest discover -s tests -p 'test_phase2_1.py' -v
python test_no_lookahead.py
python scripts/validate_phase2_1.py
```

Para volver a ejecutar los backtests (escribe artefactos solo dentro de `reports/phase2_1/backtests/`):

```bash
python run_portfolio_backtest.py --scan technical_candidate_scan.csv --cost-scenario BASE --name portfolio_BASE_repeat --output-dir reports/phase2_1/backtests
python run_stateful_backtest.py --scan data/backtest/technical_candidate_scan.csv --cost-scenario BASE --name stateful_BASE_repeat --output-dir reports/phase2_1/backtests
python scripts/validate_phase2_1.py --run-backtests --run-validation-period
```

No se necesitan credenciales ni MT5 para la validación offline. No se ejecutaron las integraciones live.
