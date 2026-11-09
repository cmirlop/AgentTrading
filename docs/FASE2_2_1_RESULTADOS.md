# Fase 2.2.1 — Resultados

## Estado

**PHASE2_2_1_COMPLETE** para la ingeniería, el plan técnico independiente y la validación offline.

La estrategia permanece **INCONCLUSIVE** y el valor añadido de DeepSeek permanece **INCONCLUSIVE_INSUFFICIENT_DATA**. El experimento prospectivo continúa **PROSPECTIVE_EXPERIMENT_BLOCKED**: esta fase no inició recolección automática ni realizó llamadas a proveedores.

La Fase 2.2.1 puede cerrarse porque sus entregables offline están implementados y validados. Eso no cierra la Fase 1 ni autoriza todavía a iniciar el experimento prospectivo.

## Trazabilidad de esta validación

- Repositorio: `AgentTrading`.
- Rama: `codex/phase2-2-1-independent-technical-plan`.
- Commit base verificado: `0ba7b5411d16ae4795a6cb3273dbcd746c48632d`.
- No se creó ningún commit y no se hizo push ni merge.
- Python: 3.12.14.
- Validación integral: 9 de noviembre de 2026, 07:17 UTC.
- Dataset congelado `data/backtest/technical_candidate_scan.csv`: SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.
- El dataset original no se modificó.
- Dependencias disponibles: pandas 2.2.3, NumPy 2.3.5 y Pydantic 2.13.4. Los paquetes `MetaTrader5`, `ollama` y `openai` no están instalados en este entorno.

El árbol contenía los cambios locales de esta tarea al ejecutar el validador (`working_tree_clean_before_validation=false`). El informe conserva rama, commit base, hashes de entradas y archivos relevantes, comandos, versiones y resultados. El estado actual sigue sin commit.

## Implementación

La referencia `TECHNICAL_REFERENCE_V1` reconstruye una oportunidad técnica sin consultar al LLM y crea el plan de comparación con el constructor técnico existente (umbral 65, entrada LIMIT). Se registra antes de guardar la solicitud LLM. Ambos comparadores reciben el mismo plan y mantienen estado de cartera separado.

El ledger persistente asocia oportunidad, plan técnico, consulta, respuesta, decisión final y resultado mediante identificadores verificables. La reconciliación exige procedencia, hashes y timestamps causales. El simulador diferencia cierre económico, momento en que el cierre se observa y momento de persistencia; una posición no libera capacidad antes de observar su cierre.

El demo sintético cubre respuestas aprobadas, rechazadas, inválidas, timeouts y decisiones de estrategia distinta. Sirve para validar el recorrido de ingeniería, no para estimar la eficacia del proveedor.

## Pruebas y validaciones

| Comprobación | Resultado | Evidencia |
|---|---|---|
| Pruebas específicas de Fase 2.2.1 | PASS | 13/13, `tests_phase2_2_1` |
| Regresión de Fase 2.2 | PASS | 28/28, `tests_phase2_2` |
| Suite offline completa | PASS con 1 omitida | 163 ejecutadas: 162 aprobadas, 0 fallidas, 1 omitida por no estar instalado MetaTrader5 |
| Look-ahead | PASS | `test_no_lookahead.py` |
| Auditoría histórica congelada | PASS como auditoría técnica | Sin errores de reconstrucción; resultados exploratorios, no confirmatorios |
| Escenarios BASE, CONSERVATIVE y STRESS | PASS como ejecuciones | Mismo umbral 65 y modo LIMIT; hipótesis de costes, no tarifas verificadas |
| Paridad del plan y comparación sintética | PASS | 5 eventos fixture; el mismo hash de plan para ambos comparadores |
| Reconciliación del ledger | PASS | Consultas, respuestas, planes, resultados y decisiones enlazados por request ID |
| Seguridad DRY RUN | PASS | 0 órdenes, 0 conexiones MT5 y 0 llamadas Ollama/DeepSeek |
| DeepSeek live | NOT_RUN_NOT_NEEDED | No se usó el presupuesto opcional de hasta 3 llamadas / USD 1 |
| MT5 demo | PENDING_MANUAL | No se conectó a MetaTrader; paquete no instalado |

Los comandos reproducibles principales son:

```bash
python scripts/validate_phase2_2_1.py
python scripts/run_phase2_2_offline_demo.py --output reports/phase2_2_1/synthetic_comparison.json
python scripts/audit_phase2_2_1_historical.py --output reports/phase2_2_1/historical_compatibility.json
```

El validador general vuelve a ejecutar las pruebas offline, comprobaciones look-ahead, auditoría histórica, los tres escenarios de backtest y el demo sintético. No ejecuta integración live.

## Auditoría del histórico

El scan contiene 22.011 filas, de las cuales 10.284 cumplían la selección candidata histórica de score 65. La reconstrucción produjo:

| Estado de referencia | Oportunidades |
|---|---:|
| Sin candidato técnico | 11.724 |
| Plan técnico válido | 9.192 |
| Plan no evaluable | 1.095 |

Los 2.094 trades guardados del backtest estatal coincidieron en entrada, SL, TP y modo LIMIT. No se usaron resultados futuros para construir el plan. No obstante, el cálculo reconstruido no replica todos los campos congelados: hay 13 desacuerdos de pertenencia a candidatos, 1.200 valores distintos de `setup_quality`, 34 desacuerdos de bias y ninguno de alignment. No se ajustaron umbrales para ocultar estas diferencias.

En 7.004 planes válidos el spread de origen es cero sin verificación suficiente. Esos casos se identifican y limitan su elegibilidad; el cero no se reinterpreta como spread real. La auditoría histórica por eso sigue siendo exploratoria.

## Sensibilidad histórica a costes

Resultados EUR por símbolo, con ejecuciones independientes por símbolo. BASE usa los costes disponibles en la configuración (cero en estos datos); CONSERVATIVE y STRESS son supuestos explícitos del simulador, no precios verificados de un broker.

| Escenario | Símbolo | Órdenes ejecutadas | Cerradas | Costes EUR | PnL neto EUR | Expectativa neta R | Profit factor neto | Drawdown neto EUR |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| BASE | EURUSD | 277 | 251 | 0,00 | 15,05 | 0,1116 | 1,174 | 13,59 |
| BASE | USDCAD | 275 | 257 | 0,00 | 20,93 | 0,2304 | 1,382 | 7,34 |
| BASE | AUDUSD | 264 | 245 | 0,00 | -14,89 | -0,1269 | 0,823 | 24,11 |
| CONSERVATIVE | EURUSD | 277 | 251 | 24,18 | -6,86 | -0,0478 | 0,936 | 23,28 |
| CONSERVATIVE | USDCAD | 274 | 256 | 22,58 | 0,32 | 0,0074 | 1,010 | 10,89 |
| CONSERVATIVE | AUDUSD | 264 | 245 | 23,05 | -36,28 | -0,3004 | 0,644 | 40,17 |
| STRESS | EURUSD | 279 | 253 | 42,38 | -24,86 | -0,1746 | 0,788 | 34,86 |
| STRESS | USDCAD | 277 | 259 | 38,02 | -16,54 | -0,1717 | 0,795 | 21,01 |
| STRESS | AUDUSD | 265 | 246 | 40,26 | -55,88 | -0,4456 | 0,527 | 56,63 |

Las operaciones ambiguas se conservaron en los informes (63 en total por escenario en estos resultados). La sensibilidad muestra que los resultados dependen de los costes. No permite concluir rentabilidad ni que DeepSeek mejore la estrategia.

## Límites antes del experimento prospectivo

- La Fase 1 sigue `INCOMPLETE_PENDING_MANUAL`; hace falta completar MT5 demo y las comprobaciones locales de proveedores bajo el procedimiento de esa fase.
- No están congelados el periodo prospectivo UTC ni el presupuesto y la autorización de llamadas API.
- La frecuencia de spreads cero no verificados restringe qué resultados podrían considerarse confirmatorios.
- No hay decisiones DeepSeek prospectivas trazables suficientes para una comparación económica.
- No se recolectaron oportunidades automáticamente y no se deben generar llamadas nuevas hasta congelar protocolo, periodo, costes, riesgo, cobertura y presupuesto.
- La prueba específica de serialización MT5 fue omitida; no aparece como aprobada.

## Conclusión

La infraestructura offline ya puede registrar y enlazar de forma reproducible una oportunidad técnica, el plan común y los eventos de consulta/respuesta LLM; también puede rechazar registros sin procedencia y comparar políticas sintéticas con cartera independiente y cronología causal. Esto deja listo el andamiaje técnico para la futura evaluación.

**Todavía no es suficiente para iniciar la recopilación prospectiva real.** Faltan el cierre manual de Fase 1, congelar y autorizar explícitamente periodo y presupuesto, y establecer criterios de elegibilidad de costes/spreads. Hasta entonces, la conclusión estadística y el valor añadido de DeepSeek siguen inconclusos.

## Informes generados

Los resultados estructurados están en [`reports/phase2_2_1/validation_summary.json`](../reports/phase2_2_1/validation_summary.json). La carpeta incluye integridad del baseline, auditoría histórica, escenarios de costes, cronología, paridad de plan, reconciliación, comparación sintética, preparación prospectiva y salidas detalladas de los backtests.
