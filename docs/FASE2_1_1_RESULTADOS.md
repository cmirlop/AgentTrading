# Resultados — Fase 2.1.1

## Estado

**PHASE2_1_1_COMPLETE**. La validación de ingeniería Fase 2.1 también queda **PHASE2_1_COMPLETE**, separada de las integraciones operativas aún pendientes de Fase 1. Las conclusiones de estrategia y DeepSeek son independientes:

- Investigación de estrategia: **INCONCLUSIVE**. No existe un periodo confirmatorio intacto.
- Valor añadido de DeepSeek: **INCONCLUSIVE_INSUFFICIENT_DATA**. Hay cero decisiones históricas DeepSeek emparejables con procedencia verificada.
- Fase 1: **INCOMPLETE_PENDING_MANUAL** para comprobaciones MT5 demo y proveedores live. No bloquea la auditoría offline.

La referencia Git verificada fue `main` en `6c326ee8d4d5ac17d4d47737a190e305cd85f6cf`. El trabajo se hizo en `codex/phase2-1-1-consolidation`; el árbol final contiene cambios sin commit. El informe de cierre de Fase 2.1 se escribió en `reports/phase2_1_1/phase2_1_framework/validation_summary.json`. El informe anterior `reports/phase2_1/validation_summary.json` se preservó.

## Archivos nuevos y modificados

- Nuevos: `src/spread_quality.py`, `src/warmup_audit.py`, `src/event_ledger.py`, `src/llm_evaluation.py`, `scripts/validate_phase2_1_1.py`, `tests/test_phase2_1_1.py`.
- Nuevos documentos: `docs/FASE2_1_1_PLAN.md`, `docs/FASE2_PROTOCOLO_LLM.md`, este informe.
- Nuevos informes y sensibilidades: `reports/phase2_1_1/validation_summary.json`, `spread_quality.json`, `spread_sensitivity.csv`, `warmup_comparison.json`, `baseline_integrity.json`, `llm_logging_validation.json`, además de los CSV/JSON de backtest en `reports/phase2_1_1/backtests/`.
- Modificados: validadores de Fase 2.1, cliente LLM unificado, simulador histórico, backtesters de cartera/stateful, pipeline DRY RUN y `run_market_agents.py`/`run_multiagent_analysis.py` para observabilidad. Se añadieron regresiones a pruebas de Fase 1 y 2.1.

No se cambiaron prompts productivos, pesos, umbrales, gestión de riesgo, tamaño de posición ni reglas de entrada/salida. Las sensibilidades ejecutaron únicamente el comparador existente `65/LIMIT`.

## Pruebas y validaciones

La ejecución completa se hizo con Python 3.12.14 y los históricos del repositorio. Comando reproducible:

```powershell
python scripts/validate_phase2_1_1.py
```

Resultados observados:

- **121** casos descubiertos por `unittest`: **120 PASS, 0 FAIL, 1 SKIPPED**. El omitido depende de MetaTrader5, que no está instalado en este entorno.
- **21** scripts independientes de comprobación offline: **21 PASS, 0 FAIL**; entre ellos el replay fake de DeepSeek y las 375 comprobaciones existentes de look-ahead.
- Tres scripts de integración no se ejecutaron: Ollama live y dos comandos MT5 que inicializan el terminal. MT5 demo, Ollama live y DeepSeek live quedan **PENDING**; ninguna API real fue llamada.
- Duración agregada de comandos de prueba: **61,407 s** en la ejecución registrada. El validador registra cada comando, su salida, duración, intérprete, dependencias, commit, estado del árbol y hashes de código/datos.
- Reproducción del baseline anterior: BASE `+25,62008 €`, CONSERVATIVE `−27,52673 €`, STRESS `−74,39422 €`; las diferencias frente a sus referencias son menores de `0,01 €`.
- Los archivos de trades y resúmenes BASE repetidos coinciden byte por byte para ambos motores.
- Seguridad: cero llamadas MT5/provider ejecutadas y cero órdenes. El escaneo de ejecución encontró cero referencias ejecutables de envío de órdenes.

## Estado independiente de Fase 2.1

Se ejecutó `scripts/validate_phase2_1.py` contra una carpeta de salida nueva, reutilizando los backtests previos de solo lectura. No se reemplazaron los informes históricos. La fase de ingeniería resultó `COMPLETE`; el estado de Fase 1 y la ausencia de pares DeepSeek aparecen como dependencias/resultados, no como fallos técnicos.

La conclusión de estrategia sigue `INCONCLUSIVE`: el periodo TEST disponible fue examinado anteriormente y no es confirmatorio. DeepSeek sigue `INCONCLUSIVE_INSUFFICIENT_DATA`.

## Calidad de spreads

Los CSV conservan el campo `spread` exportado desde `copy_rates_range`; no se encontró una conversión explícita de faltantes a cero en el flujo de importación. El simulador lo interpreta como puntos MT5 y lo multiplica por `SYMBOL_POINT`. Los CSV no guardan cotizaciones bid/ask separadas ni los metadatos históricos de dígitos del símbolo/broker necesarios para verificar independientemente cada conversión. Por tanto, los ceros se clasifican `OBSERVED_ZERO_UNVERIFIED`, sin presentarlos como spread real cero.

| Símbolo M5 | Ceros | Ausentes | Positivos observados | P95 puntos | P99 puntos | Máximo puntos |
|---|---:|---:|---:|---:|---:|---:|
| EURUSD | 94,341 % | 0 % | 5,659 % | 5 | 40 | 200 |
| USDCAD | 13,881 % | 0 % | 86,119 % | 7 | 68 | 300 |
| AUDUSD | 92,914 % | 0 % | 7,086 % | 5 | 52 | 300 |

El diagnóstico completo cubre los 15 CSV (3 símbolos × 5 timeframes), percentiles, extremos, sesiones UTC, bid/ask disponible y hashes de origen. Ningún CSV histórico fue modificado.

### Sensibilidad económica BASE

Pisos predefinidos de 5, 10 y 20 puntos son **supuestos de investigación**, no tarifas de broker. El control es el simulador actual con piso 0. La sensibilidad conserva el spread en los precios ask/bid y modifica fills y salidas; BASE no modela comisión ni slippage, por lo que `transaction_costs_eur` es 0 y no se descuenta el spread por segunda vez.

| Motor | Piso hipotético | Planes | Ejecutadas | Cerradas | Expiradas | Ambiguas | PnL bruto/neto € | Expectativa neta €/cierre | PF neto | DD máx. € | Operaciones que tocaron algún cero no verificado | Con piso aplicado |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Cartera | 0 | 1.737 | 686 | 628 | 1.050 | 57 | 25,62 | 0,0408 | 1,118 | 10,94 | 82,50 % | 0 % |
| Cartera | 5 | 1.777 | 685 | 622 | 1.091 | 62 | 16,04 | 0,0258 | 1,074 | 11,44 | 82,22 % | 99,94 % |
| Cartera | 10 | 1.822 | 680 | 617 | 1.141 | 62 | 2,01 | 0,0033 | 1,009 | 17,66 | 81,50 % | 99,95 % |
| Cartera | 20 | 1.904 | 656 | 573 | 1.247 | 82 | −35,17 | −0,0614 | 0,840 | 41,08 | 81,09 % | 99,95 % |
| Stateful | 0 | 2.124 | 816 | 753 | 1.308 | 63 | 21,09 | 0,0280 | 1,079 | 17,92 | 83,10 % | 0 % |
| Stateful | 5 | 2.163 | 810 | 740 | 1.353 | 70 | 7,62 | 0,0103 | 1,029 | 18,03 | 83,08 % | 100 % |
| Stateful | 10 | 2.205 | 803 | 731 | 1.402 | 72 | −7,39 | −0,0101 | 0,973 | 21,30 | 82,63 % | 100 % |
| Stateful | 20 | 2.272 | 770 | 673 | 1.502 | 97 | −42,99 | −0,0639 | 0,836 | 49,38 | 82,09 % | 100 % |

En ambos motores el resultado cae con supuestos de spread mayores y pasa a negativo a 20 puntos. También cambian los fills, expiraciones y ambigüedades. Esta dependencia económica impide tratar los ceros actuales como una fuente de costes suficientemente verificada.

## Calentamiento de indicadores

Se inspeccionaron 60 timestamps fijos y equiespaciados del scan congelado (20 por símbolo), recortando cada input al cierre de la última vela disponible en el instante de decisión. Las ventanas fueron 200, 400, 700 y 800 barras; no se escogieron por PnL.

EMA200 tiene `α = 2/201`. Reducir el peso de inicialización por debajo de 0,1 % requiere **692 barras** por el cálculo analítico. Frente a la referencia de 800 barras:

- Ventana 200: cambios de relación precio/EMA en M5 3/60, M15 1/60, H1 0/60, H4 0/60, D1 3/60; cambió el bias en 1 evento, la alineación en 1, la calidad del setup en 15 y el score general en 17.
- Ventana 400: cambios de relación en M15 0/60 y D1 1/60; calidad de setup en 4 eventos, score general en 4 y pertenencia al umbral 75 en 1 evento.
- Ventana 700: no cambió bias, alineación, calidad, score ni pertenencia a 65/75/85/90 en la muestra.
- La reconstrucción de 800 barras frente a los valores congelados cambió setup quality en 2/60 y score general en 3/60; bias y alineación no cambiaron. Se registra como diferencia de reconstrucción aún no atribuida.

El cálculo actual expone EMA200 tras 200 observaciones y, por tanto, puede mostrar valores antes del criterio de estabilización. No se añadió una puerta operativa silenciosa: el cambio afectaría señales y queda para revisión de impacto separada.

## Registro y emparejamiento LLM

Se añadió un ledger SQLite en modo WAL con transacciones `BEGIN IMMEDIATE`, IDs estables versionados, clave UTC, protección append-only y deduplicación/conflicto de payload. La instrumentación corre dentro del cliente estructurado existente y solo cuando el llamador enlaza una oportunidad. Registra proveedor/modelo, hash de prompt/payload, IDs de evidencia permitidos, campos de decisión seleccionados, validación Pydantic, error, timestamps, latencia y tokens disponibles. No guarda prompts/respuestas completos, secretos ni identificadores de cuenta. Los outcomes se añaden como filas independientes y se rechazan si su timestamp es anterior a la decisión.

Los tests con mocks verificaron BUY, SELL, HOLD, WAIT, evidencia inválida, timeout, hash, timestamps, reinicio, persistencia, datos append-only, outcomes separados y escritores concurrentes reales. El fixture emparejó cuatro eventos y validó las interfaces `TECHNICAL_ONLY`, `TECHNICAL_PLUS_LLM` y `ALWAYS_HOLD`; son resultados sintéticos, no evidencia de trading.

La infraestructura no fabrica decisiones históricas. La comparación productiva solo será elegible cuando cada evento tenga una decisión técnica, una única llamada LLM verificable y una decisión final del sistema en el mismo ID. Eventos con múltiples llamadas LLM por oportunidad quedan excluidos hasta definir una asignación preregistrada por rol.

## Pendiente y siguiente experimento

No falta infraestructura offline para empezar a recolectar trazas de las consultas que el flujo ya ejecuta. Sí faltan decisiones prospectivas verificables y outcomes de ambas políticas, procedencia completa del spread/bid-ask, muestra confirmatoria futura, y las pruebas operativas separadas de Fase 1. El siguiente experimento —sin ejecutarlo aquí— debe congelar el protocolo de `docs/FASE2_PROTOCOLO_LLM.md`, registrar todas las oportunidades DRY RUN y definir por adelantado el periodo confirmatorio, escenario de costes y reglas de cartera.

Para repetir todo en Windows desde la raíz, con el entorno virtual activo:

```powershell
python scripts/validate_phase2_1_1.py
python -m unittest discover -s tests -p 'test_*.py' -v
python test_no_lookahead.py
```

La validación final no inició MT5, no consultó Ollama ni DeepSeek y no hizo commit, push o merge.
