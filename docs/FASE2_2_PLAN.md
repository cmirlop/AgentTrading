# Fase 2.2 — plan, auditoría y restricciones metodológicas

## Revisión de repositorio

- Repositorio: `https://github.com/cmirlop/AgentTrading.git`.
- Rama de trabajo: `codex/phase2-2-prospective-experiment`.
- `main` remoto verificado mediante `git fetch origin main`: `bcb46d87eb7c320569c20c8caf3dfb15abb66854` (merge de Fase 2.1.1).
- El árbol estaba limpio antes de crear esta rama. No había modificaciones locales ajenas.
- No se crearon commits ni se publicó nada durante la inspección.

## Hallazgos confirmados al inicio

1. `compare_paired_policies()` tenía `max_open_positions=3` codificado y únicamente aplicaba slots y una posición por símbolo. No llamaba a `calculate_position_size()`, `evaluate_plan_risk()` ni `evaluate_portfolio_limits()`; no era un comparador económico con paridad de riesgo.
2. `record_llm_call()` devolvía silenciosamente ante falta de contexto y capturaba cualquier excepción SQLite. `structured_chat()` también silenciaba el fallo del logger.
3. El ledger solo conservaba tablas para oportunidades, respuestas, decisiones y outcomes. No permitía reconciliar oportunidades detectadas, exclusiones, consultas enviadas o huecos de cobertura.
4. El ledger no tenía un campo explícito para rol/etapa del agente. El flujo `run_market_agents.py` tiene una salida estructurada `MarketAnalysis`; `run_multiagent_analysis.py` puede producir varias respuestas (régimen, estrategia y crítica) para el mismo evento. `DecisionAgent` calcula una decisión determinista y no es un modelo LLM.
5. Las taxonomías de señales técnicas (`WATCH_LONG`, `WATCH_SHORT`, `NO_TRADE`) y salidas del modelo (`BUY`, `SELL`, `HOLD`, `WAIT`, etc.) no deben compararse por igualdad literal.
6. El riesgo productivo está configurado en `config/risk_config.py`: 2 posiciones, 0,5 % por operación y tope legado de 5 EUR, 2 % cartera, 2 % pérdida diaria, exposición bruta por divisa limitada a 1× equity, reservas DRY RUN con TTL de 15 minutos. `src/risk_preflight.py` y `src/risk_engine.py` ofrecen lógica reutilizable; una comparación requiere contexto monetario, metadatos y conversiones.

## Diseño de esta fase

- La comparación económica invocará la ruta de dimensionamiento y preflight existente por separado para cada política, desde el mismo capital y la misma secuencia causal. Si faltan datos críticos, devolverá `NO_EVALUABLE` y no computará PnL como cero.
- Las llamadas LLM llevarán rol y etapa explícitos. El rol canónico observado es `MARKET_ANALYST_STRATEGY_FAMILY`; las demás llamadas se almacenan pero no se seleccionan retrospectivamente.
- Los eventos de cobertura serán append-only e incluirán detección, elegibilidad/exclusión, consulta prevista/enviada, respuesta recibida/guardada, decisión final y outcome.
- El logging ofrecerá estado estructurado y modo estricto `RESEARCH_LOG_REQUIRED`. El modo estricto impedirá iniciar una consulta sin evento persistido y hará que respuestas no persistidas sean inelegibles; nunca reintentará la consulta.
- El presupuesto se implementará como gate/mocks offline, con límites de solicitudes/tokens/coste, pero desactivado para proveedores reales.
- La demo y el validador usarán fixtures sintéticos, sin MT5, Ollama, DeepSeek, órdenes, descargas o tareas programadas.

## Hallazgo metodológico confirmado

`MarketAnalystAgent` genera una familia de estrategia y `run_dry_pipeline.py` pasa `candidate["technical"]["strategy"]` al planificador. Por eso el plan operativo actual depende de la salida LLM. No hay un plan técnico contrafactual independiente disponible para comparar ambos métodos sobre el mismo plan.

El rol canónico observado se registra como `MARKET_ANALYST_STRATEGY_FAMILY`. La propuesta de política permite que `NONE` rechace el candidato y que una familia válida lo apruebe sin cambiar la dirección técnica. `BUY`, `SELL`, `HOLD` y `WAIT` no pertenecen a esa taxonomía y se consideran inválidos. Aun así, el planificador usa la familia elegida por el modelo; por eso una aprobación no basta para una comparación económica justa.

El simulador exige `plan_strategy_source=TECHNICAL_ONLY_REFERENCE`. El flujo actual registra sus planes como `MODEL_SELECTED_STRATEGY_FAMILY` y son no evaluables para confirmar valor económico. No se sustituye por una familia fija ni se inventa una regla técnica: eso cambiaría la estrategia fuera del alcance. Para empezar un periodo confirmatorio se necesita un plan técnico independiente, congelado antes de observar sus resultados y registrado también para candidatos rechazados por DeepSeek.

## Restricciones de salida

La preparación offline y la autorización de recopilación son estados separados. La evaluación prospectiva permanece bloqueada hasta definir el plan TECHNICAL ONLY común, validar Fase 1 en el entorno local, congelar política y horizonte, verificar costes y precios de API, y contar con autorización y presupuesto explícitos.
