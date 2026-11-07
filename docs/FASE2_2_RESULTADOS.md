# Fase 2.2 — resultados de ingeniería offline

## Estado

**PHASE2_2_COMPLETE — infraestructura experimental offline preparada.**

**PROSPECTIVE_EXPERIMENT_BLOCKED.** No se ha iniciado recopilación real ni se ha realizado ninguna llamada a DeepSeek/Ollama o conexión MT5.

- Investigación de estrategia: **INCONCLUSIVE**.
- Valor económico añadido de DeepSeek: **INCONCLUSIVE_INSUFFICIENT_DATA**.
- Fase 1: permanece `INCOMPLETE_PENDING_MANUAL` según la referencia del proyecto.
- La conclusión de ingeniería no afirma rentabilidad, mejora predictiva ni autorización para operar.

## Repositorio y procedencia

- Commit base verificado: `bcb46d87eb7c320569c20c8caf3dfb15abb66854`.
- Rama: `codex/phase2-2-prospective-experiment`.
- El árbol estaba limpio antes de iniciar el trabajo; al validar contiene los cambios de esta rama. No se hizo commit, push ni merge.
- Dataset congelado: `data/backtest/technical_candidate_scan.csv`.
- SHA-256 esperado y observado: `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.
- El CSV original y los informes previos de Fase 2.1/2.1.1 no se modificaron.
- El validador registra commit, rama, estado del árbol, Python, dependencias, hashes y comandos en `reports/phase2_2/validation_summary.json`.

## Hallazgo que bloquea el experimento económico

El rol canónico observado es `MARKET_ANALYST_STRATEGY_FAMILY` (`src.agents.market_analyst.StrategyAssessment`), con proveedor/modelo requeridos `deepseek` / `deepseek-flash` y el modelo reportado también verificado. El prompt productivo se conserva intacto; su SHA-256 es `4e54929356d993a6b633904d28c73dc1a2b70b591ee7498906dc0b44dfe198a3`.

Una familia válida distinta de `NONE` aprueba el candidato y mantiene la dirección técnica. `NONE` lo rechaza. `BUY`, `SELL`, `HOLD` y `WAIT` no pertenecen al esquema canónico y son inválidos; no se reinterpretan como órdenes. Los roles auxiliares se registran pero nunca se eligen después de ver los resultados.

Sin embargo, `run_dry_pipeline.py` usa la familia seleccionada por el modelo al construir el plan. No hay todavía un plan TECHNICAL ONLY independiente para la misma oportunidad, incluido cuando DeepSeek rechaza el candidato. El comparador exige `plan_strategy_source=TECHNICAL_ONLY_REFERENCE`; los planes actuales se etiquetan `MODEL_SELECTED_STRATEGY_FAMILY` y quedan `NO_EVALUABLE`. No se inventó una regla alternativa ni se eligió una familia fija porque cambiaría la estrategia.

## Cambios técnicos

- `compare_paired_policies()` ya no introduce un límite de posiciones propio: lee `config/risk_config.py` y rechaza overrides incompatibles.
- `src/paired_policy_simulator.py` ejecuta TECHNICAL ONLY, TECHNICAL + DEEPSEEK y ALWAYS HOLD con estados independientes. Reutiliza `calculate_position_size()` y `evaluate_plan_risk()`, con orden cronológico, slots y posiciones pendientes, exposición por divisa, riesgo agregado y diario, prioridad determinista, caducidad, costes etiquetados y reserva/liberación. Outcomes abiertos/ambiguos no se convierten en PnL cero cerrado; faltantes, eventos duplicados, planes LLM y outcomes no verificables producen `NO_EVALUABLE`.
- Riesgo reproducible offline: 2 posiciones simultáneas, riesgo por operación/cartera, límite diario y exposición por divisa se leen desde el motor existente. El capital de investigación sintético es fijo. Margen de broker y validación MT5 quedan `PENDING`.
- `src/event_ledger.py` incorpora rol/etapa, esquema 2.0, estados de cobertura, errores estructurados, modo estricto `RESEARCH_LOG_REQUIRED`, tabla append-only de contextos de plan/riesgo e idempotencia. El contexto almacena metadatos de mercado permitidos, sin equity, número de cuenta, posiciones ni volumen real. Conflictos no sustituyen observaciones previas.
- El ledger reconcilia población, consultas, respuestas, fallos, decisiones, contextos y outcomes. Eventos huérfanos o candidatos sin contexto hacen fallar la reconciliación. Los outcomes se agregan como registros separados.
- La salida emparejada valida rol, proveedor, modelo configurado y reportado, política, hash de prompt/payload, evidencias, tiempos UTC y decisión final. El contexto de plan se descarta si usa una vela posterior a la oportunidad.
- `run_market_agents.py`, `run_multiagent_analysis.py` y `run_dry_pipeline.py` pasan procedencia y registran contexto de plan/riesgo sin cambiar prompts, parámetros, decisión de trading ni ejecutar órdenes. La suite no activa esos flujos live.

## Validación ejecutada

Comando principal:

```powershell
python scripts/validate_phase2_2.py
```

Resultados reales registrados:

| Comprobación | Estado | Evidencia |
|---|---|---|
| Tests específicos Fase 2.2 | PASS | 28 ejecutados, 28 PASS |
| Suite offline de Fases 1, 2.1, 2.1.1 y 2.2 | PASS | 150 ejecutados: 149 PASS, 0 FAIL, 0 errores, 1 SKIPPED por dependencia MetaTrader5 ausente |
| Look-ahead histórico | PASS | 375 comprobaciones, tres símbolos |
| Integridad del baseline | PASS | SHA-256 coincide con referencia congelada |
| Paridad de riesgo offline | PASS | Slots, reservas, sizing, límite diario, exposición USD y estados independientes cubiertos por fixtures |
| Persistencia/concurrencia | PASS | Escritores multiproceso, idempotencia, conflicto, SQLite bloqueado y logging estricto probados |
| Reconciliación y cobertura | PASS | Fixture de 4 oportunidades: 9 consultas enviadas, 9 respuestas persistidas, 7 válidas, 2 errores, 4 contextos y outcomes, sin discrepancias |
| Demo end-to-end | PASS | Repetición idéntica; distingue mejora, degradación y sin cambio en fixtures |
| Órdenes reales | PASS | Escaneo AST de envío sin hallazgos; 0 órdenes enviadas |
| DeepSeek live | PENDING | No ejecutado por diseño; 0 llamadas reales |
| Ollama live | PENDING | No ejecutado por diseño; 0 llamadas reales |
| MT5 DEMO | PENDING | No se conectó ni leyó una cuenta |
| Coste y comparación confirmatoria | PENDING | No existe plan común TECHNICAL ONLY ni outcomes prospectivos; costes sintéticos no son tarifas del broker |

El comparador económico acepta únicamente el informe íntegro de población y reconciliación. Una lista de pares filtrados se rechaza, para que oportunidades sin cobertura, rechazadas o inválidas no desaparezcan de la evaluación.

La salida sintética de cartera incluye `TECHNICAL_ONLY` neto 0,00 EUR y `TECHNICAL_PLUS_DEEPSEEK` neto +3,00 EUR en cuatro eventos ficticios. Es una comprobación del evaluador, no un resultado económico de AgentTrading. La demo marca explícitamente `confirmatory_eligible=false`.

## Archivos entregados

- Documentos: `FASE2_2_PLAN.md`, `FASE2_2_POLITICA_DEEPSEEK.md`, este informe y actualización de `FASE2_PROTOCOLO_LLM.md`.
- Código: `src/research_policy.py`, `src/paired_policy_simulator.py`, ampliaciones de `event_ledger.py`, `llm_evaluation.py`, cliente LLM y puntos de instrumentación del pipeline.
- Pruebas: `tests/test_phase2_2.py` y fixtures actualizados para la validación de procedencia y pairing.
- Scripts: `scripts/run_phase2_2_offline_demo.py`, `scripts/validate_phase2_2.py`.
- Informes: `validation_summary.json`, `baseline_integrity.json`, `portfolio_parity.json`, `ledger_reliability.json`, `coverage_reconciliation.json`, `synthetic_experiment.json` y `prospective_readiness.json` en `reports/phase2_2/`.

## Qué falta antes de recopilar datos

1. Definir y congelar cómo genera TECHNICAL ONLY un plan de entrada/SL/TP independiente del modelo, y registrar ese plan para todos los candidatos, incluso los rechazados.
2. Realizar las verificaciones locales pendientes de Fase 1, especialmente MT5 DEMO y los proveedores requeridos.
3. Congelar horizonte confirmatorio UTC, costes y tratamiento de outcomes antes de observarlos.
4. Verificar los precios actuales de la API y aprobar explícitamente presupuesto/límites antes de cualquier consulta real. El gate implementado actualmente solo se prueba con mocks.
5. Asegurar calidad de spread y contexto de riesgo temporal adecuados para cada decisión. Los spreads cero históricos no verificados no se tratan como coste real cero.

No se inicia recopilación automática, no se ejecutan llamadas facturables y no se optimizan señales.
