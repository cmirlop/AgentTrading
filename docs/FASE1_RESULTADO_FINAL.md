# Resultado final local — Fase 1 de AgentTrading

**ESTADO FINAL FASE 1: INCOMPLETE_PENDING_MANUAL**

La validación integral anterior se ejecutó el 13/11/2026 en Windows. Tras añadir redacción de rutas locales a los informes, la suite global final no terminó y fue interrumpida después de varios minutos sin salida. Las pruebas focalizadas de los componentes afectados sí pasaron: 25 de procedencia, 7 de cliente live LLM offline y 12 de MT5 readonly. El resultado global queda pendiente; no se declara PHASE1_COMPLETE ni se recomienda iniciar la Fase 2.

## A. Entorno y cambios

- Sistema: Windows 10, build 19045.
- Python: CPython 3.13.14 x64, .venv.
- Commit base de validación: 957bf6613ecdabd38a101db1471d1285917831ec.
- Rama: codex/phase1-autonomous-closure-20261113.
- Dataset congelado usado en backtests: SHA-256 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3. El CSV de trabajo local tenía otro hash y no se alteró.
- Dependencias comprobadas: MetaTrader5 5.0.6231, pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, Ollama 0.6.3, OpenAI 3.41.0 y python-dotenv 1.2.4.
- Archivos de implementación y prueba cambiados: scripts/validate_phase1.py, scripts/validate_phase1_llm_live.py, tests/test_phase1_validation_provenance.py y tests/test_phase1_llm_live.py. También se actualizaron los informes públicos de validación y estos documentos.
- Las trazas financieras y de operaciones permanecen en reports/private, excluidas de Git.

## B. Resultados

| Comprobación | Estado | Evidencia |
|---|---|---|
| Tests Phase 1 | PENDING en la revalidación final | La ejecución integral anterior: 98 ejecutadas; 97 PASS, 1 omisión POSIX. Última revalidación integral interrumpida; 44 pruebas focalizadas posteriores PASS. |
| Offline existente | PASS en la ejecución integral anterior | 21/21; no se completó otra pasada integral tras la sanitización. |
| Concurrencia Windows | PASS en la ejecución integral anterior | Locks multiproceso, persistencia, expiración y reinicios cubiertos; sin nueva pasada integral. |
| Look-ahead | PASS en la ejecución integral anterior | 375 comprobaciones; no se completó otra pasada global. |
| MT5 REAL solo lectura | PASS | Cuenta REAL; tradeapi_disabled=true; tres símbolos; riesgo monetario y límite diario PASS. |
| MT5 DEMO | PENDING / no disponible | No hay cuenta DEMO; no se imita ni se marca como PASS. |
| Riesgo y exposición MT5 | PASS | EURUSD, USDCAD y AUDUSD; cálculos hipotéticos, conversión, exposición y evaluación de historial. |
| Históricos MT5 | NOT_RUN | Snapshot separado del 11/11 conservado; ninguna descarga ni actualización en esta ejecución. |
| Ollama | PENDING | Servicio/listado unavailable (ConnectionError); 0 llamadas; modelo configurado qwen3:4b. |
| DeepSeek | PASS | deepseek-flash; una solicitud autorizada, cero reintentos; JSON/Pydantic/evidencia válidos. |
| Backtests | PASS | Ocho ejecuciones portfolio/stateful; BASE repetido dos veces en cada modalidad. |
| Regresión de selección 1ef → 957 | PASS | 487 diferencias explicadas; cero cambios de señal o plan; cero diferencias sin atribución. |
| Regresión contra agregados anteriores | PENDING | Falta evidencia por operación para probar todos los deltas del baseline antiguo y la prefinalización. |
| DRY RUN | PASS | 0 órdenes enviadas y 0 operaciones ejecutadas por los procesos. |

Informe global: [reports/phase1_validation.json](../reports/phase1_validation.json). Su estado es INCOMPLETE_PENDING_MANUAL y el código de salida registrado es 2.

## C. Resultados económicos

Corte común: umbral 65 / LIMIT. BASE usa costes de comisión y slippage cero no verificados; CONSERVATIVE y STRESS son hipótesis. Los números negativos se mantienen sin modificación.

### Portfolio

| Escenario | Oportunidades | Señales | Planes A/R | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Costes cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa EUR | DD EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

### Stateful por instrumento

| Escenario | Par | Ejecutadas/cerradas | Bruto EUR | Costes cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa EUR | DD EUR |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| BASE | EURUSD | 277 / 251 | 15,05 | 0 / 0 | 15,05 | 1,174 | 0,060 | 13,59 |
| BASE | USDCAD | 275 / 257 | 20,93 | 0 / 0 | 20,93 | 1,382 | 0,081 | 7,34 |
| BASE | AUDUSD | 264 / 245 | -14,89 | 0 / 0 | -14,89 | 0,823 | -0,061 | 24,11 |
| CONSERVATIVE | EURUSD | 277 / 251 | 15,05 | 21,91 / 24,18 | -6,86 | 0,936 | -0,027 | 23,28 |
| CONSERVATIVE | USDCAD | 274 / 256 | 21,42 | 21,10 / 22,58 | 0,32 | 1,010 | 0,001 | 10,89 |
| CONSERVATIVE | AUDUSD | 264 / 245 | -14,89 | 21,39 / 23,05 | -36,28 | 0,644 | -0,148 | 40,17 |
| STRESS | EURUSD | 279 / 253 | 13,57 | 38,43 / 42,38 | -24,86 | 0,788 | -0,098 | 34,86 |
| STRESS | USDCAD | 277 / 259 | 19,01 | 35,55 / 38,02 | -16,54 | 0,795 | -0,064 | 21,01 |
| STRESS | AUDUSD | 265 / 246 | -18,51 | 37,37 / 40,26 | -55,88 | 0,527 | -0,227 | 56,63 |

La repetibilidad BASE pasó en el mismo dataset congelado. Los cambios técnicos de señal y plan entre las fuentes 1ef0faf y 957bf66 fueron cero. La comparación causal por oportunidades explica los 487 cambios de selección mediante la corrección de expiración/estado final.

El comparador más amplio contra los informes agregados antiguos sigue PENDING: los archivos de baseline y prefinalización no contienen suficientes IDs estables para demostrar individualmente cada diferencia histórica. Esto impide marcar toda la regresión PASS.

## D. Conclusión y acciones manuales

Se implementaron la verificación de procedencia por contenido normalizado entre LF/CRLF, la reutilización de evidencia histórica por hashes, la comprobación causal de cada raíz en su candidato inmediato, y un estado/código global que distingue fallo de pendiente. Los informes financieros detallados siguen privados.

Para resolver los pendientes:

1. Corregir manualmente el acceso del servicio Ollama a su directorio de modelos, confirmar que qwen3:4b está visible y ejecutar scripts/validate_phase1_llm_live.py --provider ollama. No descargar ni sustituir el modelo sin autorización.
2. Aportar snapshots por operación del baseline antiguo o completar una revisión explícita de los agregados pendientes. No convertirlos en PASS automáticamente.
3. No usar una cuenta REAL para reemplazar la validación DEMO, si el cierre oficial exige DEMO.

El validador retorna 0 para PHASE1_COMPLETE, 2 para INCOMPLETE_PENDING_MANUAL y 1 para FAIL. La Fase 1 no está completa. Mantener DRY RUN; no comenzar Fase 2 todavía. Para cerrar también la revalidación técnica local, vuelve a ejecutar la batería global offline y confirma que finaliza; no hace falta repetir MT5 ni DeepSeek.
