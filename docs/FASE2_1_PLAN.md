# Fase 2.1 — plan de evaluación preespecificado

Fecha de preparación: 2026-11-06 UTC
Commit disponible: `742ad936ab2049cc263fff8b67bef52cdf32b35a`
Rama de trabajo: `codex/phase2-statistical-evaluation`

## Preguntas e hipótesis

Se evaluarán P1–P7 del encargo sin modificar la lógica operativa. Las hipótesis nulas para las comparaciones económicas son que la expectativa neta no supera cero y que la diferencia emparejada entre una variante y TECHNICAL ONLY no es positiva. No se presupone que los datos permitan rechazarlas.

## Datos y población

La fuente primaria es `data/backtest/technical_candidate_scan.csv`, identificada antes de calcular resultados por SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`. Sus variables se tratarán como atributos de decisión. Los resultados se obtendrán solo de salidas reproducidas del simulador vigente y de los históricos M5 disponibles; no se inferirán etiquetas desde las características.

`data/backtest/validation_candidate_scan.csv` se mantendrá como una fuente histórica separada, con procedencia y hash propios. No se concatenará con la fuente primaria. Cualquier estudio de decisiones LLM usará únicamente registros conservados con procedencia identificable y una clave de evento coincidente; no se generarán respuestas nuevas.

## Comparaciones registradas

1. ALWAYS HOLD: cero operaciones y PnL cero.
2. TECHNICAL ONLY: configuración operativa vigente, con umbral actual 65, ejecución portfolio y sus reglas actuales.
3. TECHNICAL + QUALITY FILTER: solo umbrales ya existentes en la configuración (65, 75, 85 y 90), presentados descriptivamente. No se elegirá el mejor umbral mirando TEST.
4. TECHNICAL + DEEPSEEK: solo decisiones históricas válidas, atribuibles a un evento y con proveedor/modelo/resultado verificables. Si no hay suficientes pares, el resultado será inconcluso.
5. TECHNICAL + OLLAMA: solo si existen decisiones históricas equivalentes con procedencia verificable.

Todos los backtests usarán exactamente las mismas fechas, símbolos, entradas y reglas por escenario. Se reportarán BASE, CONSERVATIVE y STRESS. Los dos últimos son hipótesis de sensibilidad; BASE tampoco representa tarifas reales verificadas.

## Particiones temporales y purga

El particionado será cronológico por días UTC únicos: 60 % TRAIN, 20 % VALIDATION y 20 % TEST final, con redondeo determinista registrado en los artefactos. Las fronteras se fijarán solo a partir de timestamps, antes de comparar PnL. Se aplicará embargo de 24 horas alrededor de cada frontera. Además, se purgará cualquier operación cuyo intervalo desde señal hasta resolución/`release_time` cruce una frontera. Las operaciones sin intervalo final conocido quedarán como abiertas/pendientes y no se contabilizarán como cerradas.

El simulador no impone un máximo temporal de tenencia después del fill; por ello no se fingirá que un horizonte fijo resuelve la dependencia. La purga será por evento observado y los casos sin timestamps fiables se excluirán de la métrica confirmatoria, manteniéndolos visibles en conteos. Las particiones y pruebas walk-forward serán descriptivas si los historiales de ejecución no permiten volver a simular cada ventana sin contaminación.

El TEST permanecerá reservado: no se utilizará para escoger umbral, parámetros, hipótesis ni variante. Como el repositorio ya contiene análisis previos de estos periodos, la partición TEST se etiquetará como holdout cronológico exploratorio, no como confirmación verdaderamente inédita.

## Métricas

Métricas principales: PnL neto, expectativa neta por operación y en R, profit factor neto y drawdown máximo. Se publicarán también oportunidades, propuestas, aceptaciones/rechazos disponibles, ejecuciones, expiraciones, cierres, ambigüedades y operaciones abiertas/pendientes. Se separarán PnL bruto y costes.

Métricas secundarias: win rate de operaciones cerradas; exposición y duración solo cuando se puedan reconstruir sin supuestos añadidos; resultados por símbolo, periodo y régimen usando atributos disponibles antes de la decisión. Para LLM: cobertura, decisiones por clase, confusión y resultado económico contrafactual de HOLD, siempre separados de operaciones ejecutadas. No se inventarán probabilidades.

## Incertidumbre y criterios de conclusión

Se usarán intervalos de confianza bootstrap por bloques semanales UTC, muestreando semanas completas de cartera para conservar dependencia temporal y co-movimiento entre pares. Semilla fija `20261106`; si faltan suficientes bloques independientes, se reportarán estimaciones descriptivas sin afirmar significancia. Se advertirán comparaciones múltiples y sesgo de selección.

`PROMISING` requeriría expectativa neta positiva fuera de muestra bajo CONSERVATIVE, límite inferior del IC del 95 % por bloques por encima de cero, persistencia en walk-forward y ausencia de dependencia de un solo par o periodo. Si no se puede evaluar alguno de esos requisitos por datos insuficientes, se concluirá `INCONCLUSIVE`, no evidencia positiva. Una diferencia LLM favorable requiere evaluación emparejada en el mismo subconjunto y evidencia que no provenga solo de TRAIN/VALIDATION; en caso contrario será inconclusa.

## Reproducibilidad y seguridad

No se modificarán prompts, pesos, límites, stops, objetivos, criterios operativos ni configuración productiva. No se llamará a MT5, DeepSeek u Ollama; no se descargarán datos; no se abrirán operaciones. Cada resultado llevará commit, rama, estado de trabajo, configuración, semillas y hashes de entradas. Los tests usarán fixtures sintéticos y comprobarán que el pipeline de investigación no llama proveedores ni mecanismos de ejecución.

## Limitaciones conocidas al iniciar

El checkout accesible no contiene el commit de referencia `ee10f99b3fa84a07c1fcb06eeb3820d5ef27c5d3`. La `origin/main` local está tres commits detrás del HEAD `742ad93`; no se pudo actualizar el remoto porque el proxy del entorno rechazó la conexión. Para preservar el trabajo local, la rama de investigación se creó en un worktree aislado desde el commit disponible más reciente. La diferencia se mantendrá visible en todos los informes.
