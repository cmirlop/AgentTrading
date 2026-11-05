# Validación de Fase 1 — AgentTrading

## Alcance y estado

Esta fase estabiliza el límite de proveedores LLM, completa controles de riesgo verificables sin operar y hace explícitos los costes y ambigüedades del backtest. No modifica prompts estratégicos ni umbrales de señales. No ejecuta ni habilita órdenes reales.

**Estado de esta entrega: `INCOMPLETE_PENDING_MANUAL`.** Las comprobaciones offline y los replays históricos están cubiertos; la integración con un terminal MT5, una llamada local a Ollama y una llamada real a DeepSeek quedan pendientes porque requieren servicios externos. El informe con los resultados medidos está en `reports/phase1_validation.json`. La línea base capturada antes de los cambios está en `reports/phase1_baseline_metrics.json`.

## Arquitectura LLM

### Antes

- Algunos agentes utilizaban `src/agents/llm_client.py`; `MarketAnalystAgent` llamaba a Ollama directamente y fijaba Qwen.
- Había adaptadores DeepSeek duplicados y `integrar_deepseek.py` podía reescribir la selección de proveedor.
- La carga de `.env` podía reemplazar variables ya definidas por el proceso.

### Ahora

- Los agentes llaman mediante `structured_chat` en `src/agents/llm_client.py`, con `LLM_PROVIDER=ollama|deepseek`.
- `MarketAnalystAgent` también pasa por el cliente central. El único uso directo del SDK de Ollama está dentro del adaptador central.
- `src/agents/deepseek_client.py` es el adaptador único; `deepseek_client.py` conserva una fachada de compatibilidad.
- `integrar_deepseek.py` ya no reescribe código: valida la configuración y termina.
- Pydantic valida las respuestas; el JSON Schema restringe los IDs de evidencia y se vuelve a verificar después de la respuesta.
- Las llamadas de transporte no reintentan (`max_retries=0`). El agente de régimen conserva una única corrección explícita y acotada si la salida estructurada no valida; no es un retry automático de red.
- Se registran proveedor, modelo, timeout, esfuerzo, límite de tokens y opciones públicas. No se registran prompts, respuestas, claves ni razonamientos. Los errores salen como códigos seguros.
- Una respuesta fallida no se convierte en señal: `MarketAnalystAgent` devuelve `NO_TRADE`; los demás flujos rechazan o dejan la tesis sin resolver.
- `.env` usa `override=False`; el entorno explícito del proceso tiene precedencia.

### Configuración

Ollama local:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3:4b
OLLAMA_TIMEOUT_SECONDS=120
```

DeepSeek (guardar la clave en el gestor seguro del entorno o en un `.env` local excluido de Git):

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=<configurada fuera del chat>
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_MAX_TOKENS=16384
DEEPSEEK_TIMEOUT_SECONDS=180
```

Las pruebas de selección, JSON, evidencia, timeout, respuesta incompleta, clave ausente y precedencia del entorno utilizan mocks y no hacen llamadas facturables. Las pruebas reales de Ollama y DeepSeek figuran `PENDING`.

## Riesgo implementado

- `src/risk_engine.py` dimensiona el volumen con equity o balance configurable, riesgo porcentual, distancia al SL, `trade_tick_size`, `trade_tick_value_loss`, tamaño de contrato, límites y paso de volumen del símbolo.
- Si MT5 no ofrece tick value, se usa distancia × tamaño de contrato y conversión FX explícita. Los precios medios de pares se obtienen del runtime MT5. Si faltan metadatos o conversiones, se rechaza la propuesta.
- El volumen se redondea hacia abajo al paso del bróker. Si el mínimo ya supera el presupuesto, no se sube el lote: se rechaza.
- Se conserva el límite heredado de **5 EUR por operación**, combinado con un máximo porcentual configurable. Valores iniciales: riesgo por operación 0,5 % de equity; pérdida diaria 2 %; riesgo agregado de cartera 2 %; exposición bruta por divisa hasta 1× equity; máximo de 2 posiciones/pendientes.
- El riesgo agregado suma la pérdida al SL de posiciones, pendientes y nueva propuesta. La exposición se calcula como nocional bruto de divisa base y cotizada convertido a divisa de cuenta; así EURUSD, AUDUSD y USDCAD comparten exposición USD.
- `src/daily_risk_guard.py` calcula pérdida realizada neta del día más el riesgo íntegro al SL de posiciones, pendientes y propuestas DRY RUN ya aprobadas. Las aprobaciones reservan riesgo y nocional por divisa durante el día, por lo que el siguiente símbolo se evalúa contra las propuestas anteriores. La ventana está en UTC. El bloqueo y las reservas se persisten en `data/state/daily_risk_guard.json` hasta el siguiente día UTC; IDs también evitan repetir una aprobación idéntica.
- La lectura de deals del día y del riesgo de posiciones se agrega en `build_runtime_state.py`. Datos críticos ausentes, SL inexistente, conversión desconocida o estado persistido ilegible cierran la aprobación.
- `BREAK_EVEN_ENABLED=False`: no hay un gestor que mueva stops a break-even.
- `src/risk_preflight.py` aplica los mismos límites en el flujo multiagente y en `run_dry_pipeline.py`, antes de `order_check()`.
- Solo se mantiene `mt5.order_check()` como preflight DRY RUN. No hay llamadas `mt5.order_send()` en los flujos de producción.

Los valores de 0,5 %, 2 % y 1× son límites configurables explícitos para control operativo; no se eligieron para mejorar resultados históricos. El runtime MT5 real aún necesita validación manual.

## Hipótesis del simulador

`src/historical_execution.py` acepta tres escenarios:

| Escenario | Spread | Comisión round-turn | Slippage por lado | Tratamiento |
|---|---:|---:|---:|---|
| BASE | Spread histórico de cada vela | 0 EUR/lote | 0 puntos | Spread bid/ask aplicado en el lado de ejecución; no se vuelve a descontar. Comisión/slippage no están verificados y quedan omitidos. |
| CONSERVATIVE | 1,25× spread histórico | 7 EUR/lote | 1 punto | Hipótesis de sensibilidad, no coste verificado de bróker. |
| STRESS | 2× spread histórico | 10 EUR/lote | 3 puntos | Hipótesis adversa, no coste verificado de bróker. |

En las salidas se separan PnL bruto, costes de trades cerrados, costes incurridos en todas las operaciones llenadas y PnL neto. Los costes de operaciones ambiguas o abiertas se muestran como incurridos, pero no se les inventa PnL neto.

Si el mismo OHLC puede representar entrada y salida, o toca TP y SL en una vela, se clasifica como `AMBIGUOUS_ENTRY_BAR` o `AMBIGUOUS_BOTH`; no se adjudica resultado favorable. Las velas posteriores a la expiración no se consultan para resolver la orden. Los informes incluyen oportunidades, planes aceptados/rechazados, fills, expiraciones, cierres, resultados ambiguos/abiertos, PnL bruto/neto, R, win rate, profit factor, expectativa y drawdown. El backtest de cartera agrega resultados por símbolo, sesión UTC aproximada y modo de entrada.

Las tesis `HOLD`/`BREAK`/`UNCLEAR` no se cuentan como órdenes ejecutadas. No se repitieron decisiones LLM en este replay.

## Archivos modificados y añadidos

- Proveedores: `src/agents/llm_client.py`, `src/agents/deepseek_client.py`, `src/agents/market_analyst.py`, `deepseek_client.py`, `integrar_deepseek.py`.
- Riesgo/runtime: `config/risk_config.py`, `src/risk_engine.py`, `src/daily_risk_guard.py`, `src/risk_preflight.py`, `src/trade_planner.py`, `src/risk_validator.py`, `src/agents/risk_agent.py`, `build_runtime_state.py`, `run_dry_pipeline.py`.
- Simulación: `config/backtest_cost_config.py`, `src/historical_execution.py`, `run_portfolio_backtest.py`, `run_stateful_backtest.py`.
- Regresión/tests: `tests/test_phase1_llm.py`, `tests/test_phase1_risk.py`, `tests/test_phase1_risk_validator.py`, `tests/test_phase1_backtest.py`, `test_deepseek_integration.py`, `test_multiagent_second_stage.py`, `scripts/validate_phase1.py`.
- Trazabilidad: `reports/phase1_baseline_metrics.json`, `reports/phase1_validation.json`.

El fixture de `test_multiagent_second_stage.py` decía “critic sin objeciones” pero enviaba `VETO`; se corrigió a `NO_OBJECTION`. La lógica del agente no se alteró por ese ajuste.

## Validaciones y resultados

Línea base antes de cambios: **20 PASS, 3 FAIL, 1 PENDING**. Los fallos observados eran el fixture contradictorio de segunda etapa y la ausencia del paquete/terminal MT5 en `test_order_request.py` y `test_trade_planner.py`. `test_ollama.py` requería servicio local y no se ejecutó.

La suite Phase 1 ejecuta offline las pruebas de proveedor, riesgo, preflight, costes, ambigüedad y reproducibilidad. El validador vuelve a ejecutar las pruebas offline anteriores, analiza imports/sintaxis, busca `mt5.order_send`, ejecuta ambos backtests BASE y compara contra el snapshot previo con el mismo CSV congelado. También repite el portfolio BASE para verificar reproducibilidad y ejecuta CONSERVATIVE.

Resultados medidos en esta entrega:

- **34/34** pruebas Phase 1 offline pasan; **21/21** pruebas históricas offline pasan. No hubo fallos offline en la validación final.
- El replay de cartera BASE repitió sus métricas en dos ejecuciones. Los **8** escenarios comparables BASE coinciden con la línea base, y las **24** combinaciones stateful también: cero diferencias en señales tomadas, cierres, ambiguas, PnL bruto, profit factor y drawdown comparables.
- `test_no_lookahead.py` completó **375** comprobaciones. La regresión de clasificación de niveles también pasó.
- Muestra portfolio `65/LIMIT`: BASE, 1.708 planes aceptados, 641 cerrados, 28 ambiguos y PnL bruto/neto de **23,94 EUR** con costes explícitos cero. CONSERVATIVE, 1.708 aceptados, 639 cerrados, 28 ambiguos, PnL bruto **23,72 EUR**, costes de cerrados **54,68 EUR**, PnL neto **−30,96 EUR** y costes incurridos totales **55,92 EUR**. La diferencia de fills/reservas viene del spread ampliado; las oportunidades técnicas del CSV no cambian.
- STRESS está cubierto por la prueba unitaria de escenario y el comando reproducible, pero no se ejecutó sobre todo el histórico en esta validación.
- El snapshot técnico evaluado abarca 2026-04-07 18:15 UTC a 2026-10-07 18:15 UTC, 22.011 filas, SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.
- La captura inicial fue 20 PASS, 3 FAIL y 1 PENDING. Se corrigió el fixture contradictorio multiagente. Las dos pruebas que necesitan MT5 y la que requiere Ollama siguen `PENDING`; no se les atribuye PASS.

Comandos desde la raíz del repositorio con el entorno Python del proyecto:

```bash
python scripts/validate_phase1.py
python scripts/validate_phase1.py --refresh-offline
python -m unittest discover -s tests -p "test_phase1_*.py"
python test_multiagent_second_stage.py
python test_no_lookahead.py
python run_portfolio_backtest.py --scan technical_candidate_scan.csv --name local_base --cost-scenario BASE
python run_portfolio_backtest.py --scan technical_candidate_scan.csv --name local_conservative --cost-scenario CONSERVATIVE
python run_portfolio_backtest.py --scan technical_candidate_scan.csv --name local_stress --cost-scenario STRESS
python run_stateful_backtest.py --scan data/backtest/technical_candidate_scan.csv --name local_stateful --cost-scenario BASE
```

No se marca como PASS una validación externa omitida. El detalle y los datos numéricos completos están en `reports/phase1_validation.json`, incluida la comparación antes/después, hash del dataset, duración y salidas de cada prueba.

## Limitaciones pendientes

- Ejecutar `build_runtime_state.py`, el planificador y `order_check()` con MT5 conectado en una cuenta demo; confirmar tick values, conversión, deals de hoy, posiciones/pendientes y el interruptor `tradeapi_disabled`.
- Validar la conexión local a Ollama y, cuando se configure la clave fuera del chat, una llamada real DeepSeek. La suite offline cubre ambos contratos mediante mocks.
- Conseguir costes históricos reales de comisión y slippage del bróker para sustituir las hipótesis CONSERVATIVE/STRESS.
- No se evaluó performance predictiva ni se comparó estrategia con/sin IA; eso pertenece a fases posteriores.
- Fase 1 no se declara completada mientras sigan pendientes las integraciones manuales señaladas en el informe.

## Trazabilidad

Commit base: `a00644a6b330b6235bea3f47cb17d2dfe39d4f5b`. Los cambios están en `codex/phase1-hardening` y quedan sin commit; no se hizo push al remoto. El estado final exacto del árbol se captura en `reports/phase1_validation.json`.
