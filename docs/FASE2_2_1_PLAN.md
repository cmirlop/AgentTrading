# Fase 2.2.1 — plan técnico

## Propósito

Preparar una comparación offline entre `TECHNICAL_ONLY` y `TECHNICAL_PLUS_DEEPSEEK` en la que el único cambio sea la aprobación del plan por DeepSeek. La referencia no sustituye al planificador productivo y no altera señales, prompts, límites de riesgo ni operaciones.

## Referencia elegida

La política es `TECHNICAL_REFERENCE_V1`: reutiliza la regla técnica 65 existente y el constructor histórico `build_historical_trade_plan(..., mode="LIMIT")`. El constructor conserva la dirección técnica, la entrada EMA20 LIMIT, el stop del planificador histórico y el TP `TARGET_RISK_REWARD` configurado. El comparador reutiliza el motor de riesgo existente y ejecuta cada política con estado de cartera independiente.

Esta elección evita inventar una familia de estrategia. El plan se construye antes de consultar el LLM y se registra incluso cuando el resultado es no candidato, no evaluable o posteriormente rechazado por DeepSeek.

## Cambios previstos

1. Construir el plan desde el snapshot técnico, usando timestamps de cierre para verificar causalidad en M5/M15/H1/H4/D1.
2. Guardar un registro append-only por oportunidad y versión, con hash de plan, política, configuración, evidencia y calidad de datos.
3. Asociar cada consulta, respuesta y decisión final mediante un `request_id` único.
4. Exigir ledger reconciliado y hash de fuente actual antes de comparar políticas.
5. Usar cierre económico, observación del resultado y persistencia como tiempos distintos en el replay de cartera.
6. Validar la paridad, cronología, baseline, tests offline, backtests 65/LIMIT y seguridad DRY RUN.

## Límites

- No consultar MT5 ni proveedores en la validación principal.
- No enviar órdenes ni activar recopilación automática.
- El smoke test real de DeepSeek es opcional; si no añade información necesaria, registrar cero llamadas.
- No convertir resultados históricos exploratorios ni fixtures sintéticos en evidencia confirmatoria.

## Reproducción

Desde la raíz del repositorio:

```bash
python scripts/validate_phase2_2_1.py
```

El validador crea artefactos bajo `reports/phase2_2_1/`, mantiene intacto el dataset congelado y devuelve un código distinto de cero si falla una comprobación obligatoria.
