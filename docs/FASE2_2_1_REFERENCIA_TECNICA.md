# Referencia técnica independiente — `TECHNICAL_REFERENCE_V1`

## Decisión de diseño

La referencia elegida es la política 65/LIMIT ya usada por los backtests técnicos del proyecto. No se seleccionó una familia fija nueva ni una familia según su PnL. La regla de oportunidad existente requiere `setup_quality >= 65`, `alignment != CONFLICT` y `bias != NEUTRAL`. La dirección sigue la tendencia técnica (`BULLISH` → BUY, `BEARISH` → SELL).

El constructor usa `src.historical_trade_planner.build_historical_trade_plan` con `mode="LIMIT"`. El planificador propone la EMA20 M15 como entrada, valida que el LIMIT esté al lado correcto del mercado, calcula el stop con la regla técnica/ATR que ya tenía el constructor y calcula el objetivo con `TARGET_RISK_REWARD`. Se usan los metadatos y precios del snapshot histórico disponible al timestamp de decisión.

## Flujo productivo y flujo experimental

El flujo productivo actual puede seguir usando la familia devuelta por `MarketAnalystAgent` para su plan posterior. La referencia nueva es solo para investigación: se guarda en `technical_plans` antes de comenzar la consulta LLM. El evaluador emparejado ignora el plan que derive de la familia LLM y toma el plan técnico inmutable del ledger.

```text
snapshot de mercado cerrado
  → evaluación técnica existente
  → TECHNICAL_REFERENCE_V1 y plan/hash
  → [TECHNICAL_ONLY] o [TECHNICAL_PLUS_DEEPSEEK]
```

Las dos políticas reciben los mismos campos del plan y las mismas entradas de riesgo y costes. El filtro DeepSeek solo permite o rechaza la dirección técnica; no crea ni modifica el plan. Cada comparador conserva su propio capital, slots, exposición y estado diario.

## Causalidad y elegibilidad

El constructor valida que cada vela disponible en M5, M15, H1, H4 y D1 cierre como máximo en `decision_time_utc`. También rechaza un estado con símbolo/timestamp no coincidente, spread ausente o inválido, indicadores incompletos, geometría de plan inválida o rechazo del planificador histórico. Un cero de spread se conserva como `OBSERVED_ZERO_UNVERIFIED` y el plan no se marca elegible confirmatoriamente.

Si una oportunidad no cumple la regla, se conserva como `NO_TECHNICAL_CANDIDATE`; si faltan datos o el plan histórico lo rechaza, se conserva como `TECHNICAL_PLAN_NOT_EVALUABLE` con un motivo. Ninguna de esas filas se elimina silenciosamente de la población.

## Procedencia del plan

Cada fila registra la versión, hash de política, hash del plan, configuración de riesgo, timestamp de construcción, timestamp máximo del mercado, dirección, orden, entrada, SL, TP, evidencia técnica y calidad del spread. La tabla es append-only y la clave `(event_id, policy_version)` hace idempotentes las repeticiones; un contenido distinto para la misma versión produce conflicto explícito.

La reconciliación requiere una referencia exacta por oportunidad, versión y hash de política; valida el hash del plan/configuración, los timestamps y la geometría. La comparación requiere de nuevo el SQLite de origen y verifica tanto su hash lógico como el hash del reporte de emparejamiento. Los eventos LLM se vinculan por `request_id` único, incluyendo las consultas enviadas sin respuesta y respuestas sin solicitud.

## Cronología económica

El simulador distingue aceptación, colocación, fill, expiración, cierre económico, observación y persistencia. Una posición se liquida y libera solo cuando el cierre es observable. La hora de escritura no mantiene una operación cerrada ocupando un slot. PnL y límites diarios cambian cuando el resultado está disponible causalmente. Una posición ambigua, abierta o no resoluble sigue consumiendo recursos y no se convierte en PnL cero.

## Integridad histórica

El CSV de referencia congelado es `data/backtest/technical_candidate_scan.csv`, SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`. La reconstrucción puede diferir de ese scan cuando se recalculan indicadores con otro calentamiento; esas diferencias se informan, no se arreglan forzando la selección. Se contrastan los niveles de entrada, stop y objetivo con los trades guardados para threshold 65 y modo LIMIT. El histórico sigue siendo exploratorio y no se considera confirmatorio.

## Seguridad

El módulo de referencia no importa cliente LLM ni MetaTrader5. La integración offline usa mocks deterministas. El código de validación no ejecuta `order_send`, no accede a cuentas, no inicia recolección prospectiva y no hace llamadas de proveedor. El plan productivo puede seguir siendo model-driven, pero el contrafactual de investigación queda fijo antes de la respuesta.
