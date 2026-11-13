# Cierre de Fase 1 — AgentTrading

**Decisión al 13/11/2026: INCOMPLETE_PENDING_MANUAL.** La validación integral terminó sin fallos offline, pero no satisface todos los criterios de cierre. No iniciar Fase 2.

La revalidación integral tras los cambios de privacidad quedó interrumpida después de varios minutos sin salida. No se considera un nuevo PASS. Pasaron 44 pruebas focalizadas de los componentes modificados (25 procedencia, 7 LLM live offline y 12 MT5 readonly); la suite completa y las 21 pruebas offline conservan el resultado PASS de la ejecución integral anterior.

## Evidencias PASS

- En la ejecución integral anterior: 98 pruebas Phase 1, 97 PASS y una omisión POSIX específica. La última revalidación integral no terminó.
- 21/21 comprobaciones offline y 375 comprobaciones look-ahead.
- Concurrencia multiproceso y ciclo de vida de reservas validados en Windows.
- Ocho backtests portfolio/stateful PASS sobre el dataset congelado; BASE repetido por modo y reproducible.
- MT5 REAL en modo solo lectura: tradeapi_disabled=true, instrumentos EURUSD/USDCAD/AUDUSD, riesgo hipotético, exposición y límite diario PASS.
- DeepSeek live PASS con deepseek-flash, una solicitud autorizada y sin reintentos.
- 487 diferencias de selección entre las versiones 1ef0faf y 957bf66 atribuidas a la corrección documentada del simulador; cero cambios técnicos de señal/plan y cero selecciones pendientes en esa comparación.
- Cero órdenes enviadas y cero operaciones ejecutadas por los procesos de validación. No se llamó a order_send ni order_check.

## Pendientes que bloquean el cierre

1. Ollama live: el servicio/listado de modelos devolvió ConnectionError, calls_made=0. El intento de inicio local anterior falló por acceso denegado al directorio de manifiestos. No se modificaron permisos ni configuración.
2. Regresión global: el baseline antiguo y la prefinalización solo conservan agregados para parte de las comparaciones. Los deltas económicos antiguos aún no pueden enlazarse completamente con IDs estables. Se mantienen PENDING aunque la comparación de trazas 1ef → 957 atribuya todas las diferencias de selección.
3. MT5 DEMO: no hay cuenta DEMO. La cuenta REAL se validó de manera independiente y estrictamente de solo lectura; no se clasifica como DEMO.

El histórico de mercado no se actualizó en esta ejecución; sigue disponible el snapshot separado del 11/11/2026. No se modificó el CSV congelado.

## Acción manual

- Restaurar el acceso del servicio Ollama al directorio del modelo configurado qwen3:4b, comprobar el listado y ejecutar scripts/validate_phase1_llm_live.py --provider ollama.
- Resolver la comparación agregada antigua aportando snapshots estables por operación o documentando una revisión explícita de los deltas pendientes.
- Mantener MT5 con trading externo Python deshabilitado. No operar la cuenta.

El informe global es [reports/phase1_validation.json](../reports/phase1_validation.json); el código de salida registrado es 2 para pendiente, 1 para fallo y 0 para PHASE1_COMPLETE. El sistema sigue exclusivamente en DRY RUN.

Acción adicional: repetir la batería global offline tras confirmar que no quedan procesos de la ejecución interrumpida y registrar su finalización. No es necesario repetir DeepSeek ni volver a conectar con MT5 para esta revalidación.
