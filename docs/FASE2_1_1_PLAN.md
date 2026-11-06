# Fase 2.1.1 — plan de consolidación

## Alcance

Esta fase corrige el estado de cierre estadístico de Fase 2.1, termina la auditoría offline, examina la calidad de los spreads y la estabilidad de indicadores, y añade trazabilidad prospectiva para oportunidades y consultas LLM. No cambia señales, umbrales, prompts, riesgo, entradas o salidas.

La Fase 1 se informa por separado. Sus validaciones MT5 demo y proveedores live no bloquean el cierre de ingeniería estadística offline. La ausencia de decisiones DeepSeek verificables se informa como resultado de investigación.

## Cambios previstos

1. Conclusiones calculadas con reglas documentadas y funciones puras: ingeniería, estrategia y valor LLM son estados independientes.
2. Ejecución de pruebas `unittest` y scripts offline existentes; MT5 y Ollama live quedan fuera. DeepSeek live no se invoca.
3. Diagnósticos de spread por instrumento, timeframe y sesión UTC. Los ceros se etiquetan como no verificados. Se ejecutan sensibilidades BASE con pisos hipotéticos de 5, 10 y 20 puntos, además del control de 0, sin cambiar los escenarios existentes.
4. Reconstrucción causal de EMA y score técnico con ventanas fijas de 200, 400, 700 y 800 barras. EMA200 usa un criterio de estabilización derivado del peso residual de su inicialización al 0,1 %: `ceil(log(0,001)/log(1 - 2/201)) + 1` barras. El análisis no usa resultados para escoger la ventana.
5. Registro SQLite transaccional append-only para oportunidades, llamadas LLM, decisiones del sistema y resultados futuros separados. El logger guarda hashes, campos estructurados permitidos y procedencia; no guarda prompts, respuestas completas, secretos ni datos de cuenta.
6. Emparejamiento por evento y comparador offline `TECHNICAL_ONLY`, `TECHNICAL_PLUS_LLM` y `ALWAYS_HOLD`, con límite de posiciones y exclusión de eventos sin procedencia o resultado elegible.

## Criterios de cierre

La fase puede completarse con estadística inconclusa y sin integraciones live si pasan el código, las pruebas offline, integridad del baseline, auditorías de spread/warmup, sensibilidad histórica, registro sintético, emparejamiento y controles DRY RUN. Los estados se reportan como PASS, FAIL, PENDING o NOT_APPLICABLE.

Una recomendación de bloqueo productivo por warmup requiere su propio informe de impacto y aprobación. En esta fase se informa el problema, pero no se modifica silenciosamente el flujo de señales.

## Seguridad y reproducción

Comando principal:

```powershell
python scripts/validate_phase2_1_1.py
```

La validación utiliza históricos locales congelados y fixtures sintéticos. No conecta MT5, Ollama ni DeepSeek y no llama a mecanismos de envío de órdenes. Los resultados completos se escriben en `reports/phase2_1_1/` sin reemplazar los artefactos congelados de `data/backtest/`.
