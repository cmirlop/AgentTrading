# Política de investigación DeepSeek — versión candidata

## Identidad

- ID de política: `deepseek-strategy-family-v1`.
- Proveedor/modelo requeridos para una observación emparejable: `deepseek` / `deepseek-flash` (incluido el modelo reportado por el proveedor).
- Rol canónico: `MARKET_ANALYST_STRATEGY_FAMILY`.
- Etapa: `STRATEGY_FAMILY_ASSESSMENT`.
- Esquema: `src.agents.market_analyst.StrategyAssessment`.
- SHA-256 del prompt productivo preservado: `4e54929356d993a6b633904d28c73dc1a2b70b591ee7498906dc0b44dfe198a3`.
- El texto del prompt productivo no se ha cambiado.

El hash se calcula sobre la cadena literal del prompt con JSON canónico. Antes de un periodo confirmatorio se debe fijar también la versión de código y confirmar en el terminal local que proveedor y modelo reportados coinciden.

## Mapeo de decisiones

La salida técnica genera `WATCH_LONG`, `WATCH_SHORT` o `NO_TRADE`. `WATCH_LONG` identifica un candidato técnico largo; `WATCH_SHORT`, uno corto. No son equivalentes a órdenes `BUY`/`SELL`.

El esquema LLM productivo emite una familia de estrategia: `TREND_CONTINUATION`, `PULLBACK`, `BREAKOUT`, `REVERSAL`, `RANGE_FADE` o `NONE`.

- Una familia válida distinta de `NONE` aprueba el candidato técnico y conserva su dirección.
- `NONE` rechaza el candidato y la decisión final es `NO_TRADE`.
- `BUY`, `SELL`, `HOLD` y `WAIT` son inválidos para el esquema canónico. No se reinterpretan como una orden ni como un HOLD.
- Una respuesta Pydantic inválida, evidencia no autorizada, timeout, error de proveedor o ausencia de cobertura se conserva como tal y nunca se convierte en aprobación.
- La salida canónica no puede discrepar de dirección porque el esquema no contiene lado. Si una salida no canónica contiene otro lado, no se usa para esta política.

## Límite que impide la evaluación económica

Aunque la regla experimental propuesta trata la familia válida como una aprobación, el flujo productivo usa esa familia como `strategy` al construir el plan. Por tanto, el plan de entrada puede depender de DeepSeek. El comparador solo admite eventos cuyo plan provenga de una referencia TECHNICAL ONLY independiente (`plan_strategy_source=TECHNICAL_ONLY_REFERENCE`), idéntica para ambas políticas.

Los planes actualmente generados por `run_dry_pipeline.py` se etiquetan `MODEL_SELECTED_STRATEGY_FAMILY` y producen `NO_EVALUABLE`. No se ha elegido una familia fija alternativa porque eso sería una decisión estratégica y cambiaría el método técnico. La política aquí documentada no está congelada para confirmación; requiere aprobar y versionar un plan técnico independiente y recopilarlo para toda la población, incluidas las oportunidades rechazadas.

## Otros roles

Las respuestas de régimen, propuesta de estrategia multiagente, crítica y tesis de niveles se guardan con sus roles/etapas. No se seleccionan después de conocer outcomes. `DecisionAgent` es determinista y su resultado final se registra separado de cada salida LLM.

El fixture offline registra salidas `BUY`, `SELL`, `HOLD` y `WAIT` bajo un rol sintético no canónico para verificar que nunca se confunden con la respuesta de familia de estrategia.
