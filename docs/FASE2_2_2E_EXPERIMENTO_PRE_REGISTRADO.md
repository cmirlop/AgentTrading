# Protocolo preregistrado

El gate económico permanece NOT_RUN. No se ejecutará backtest hasta verificar calendario, ventanas MTF causales, paridad real de fuente, reglas de ejecución y costes/conversiones.

- Umbral 65, modo LIMIT.
- Escenarios hipotéticos BASE, CONSERVATIVE y STRESS.
- Floors de spread: 0, 5, 10 y 20 puntos MT5.
- Train/evaluation/out-of-sample/observation se fijan antes de observar PnL y según cobertura disponible.
- Warmup separado del rango evaluado; estabilización EMA200 calculada por el código: 692 barras.
- Sin optimización, DeepSeek ni otros LLM. Spread cero = OBSERVED_ZERO_UNVERIFIED; el bid/ask ya incluye spread, así que no se duplica.
- Sin conversión EUR verificada, el coste en EUR no se calcula.

Gate: reports/phase2_2_2e/exploratory_backtest_gate.json.
