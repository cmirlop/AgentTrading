# Resultado final local — Fase 1 de AgentTrading

**ESTADO FINAL FASE 1: `INCOMPLETE_PENDING_MANUAL`**

La validación se ejecutó sobre Windows y terminó todas las pruebas offline, el control look-ahead, los ocho backtests, MT5 REAL en modo estrictamente de solo lectura y Ollama live. Persisten validaciones manuales necesarias para poder declarar el cierre completo. No comenzar Fase 2.

## A. Entorno y archivos

- Sistema: Windows 10, build 19045.
- Python: `.venv\Scripts\python.exe`, CPython 3.13.14 x64.
- Commit base exacto: `c6fb7de6ee58cfdb12552b7062dbd188f771a169`.
- Rama: `codex/phase1-final-local-real-readonly-closure`; trabajo local sin commit, push ni merge.
- Dependencias relevantes: MetaTrader5 5.0.6231, pandas 3.0.6, NumPy 2.5.3, Pydantic 2.13.5, Ollama SDK 0.6.3 y OpenAI SDK 3.41.0.
- MT5 local y terminal: disponibles; conexión REAL verificada bajo `tradeapi_disabled=True`.
- Ollama: instalado, `qwen3:4b` disponible; el probe live pasó y el servidor temporal se detuvo.
- DeepSeek: existe evidencia remota reciente PASS, pero fue generada en Python 3.12.14/Pydantic 2.14.2. No hubo llamada nueva desde esta validación.
- Dataset congelado: SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`, 22.011 filas, periodo 2026-04-07 a 2026-10-07 UTC. El archivo local distinto quedó intacto.

Cambios de código y pruebas principales: `scripts/validate_phase1.py`, `scripts/validate_mt5_real_readonly.py`, `scripts/validate_phase1_llm_live.py`, `tests/test_phase1_validation_provenance.py`, `tests/test_mt5_real_readonly.py` y `tests/test_phase1_llm_live.py`. También se actualizaron informes de validación y sus resúmenes. La lista completa está en `git status`; no se creó commit.

## B. Resultados

| Comprobación | Estado | Evidencia |
|---|---|---|
| Suite Phase 1 | PASS | 94 pruebas: 93 PASS y una omisión POSIX específica. |
| Pruebas offline | PASS | 21/21 comprobaciones del validador. |
| Concurrencia Windows | PASS | Multiproceso real y locks `msvcrt`, persistencia y ciclo de vida de reservas. |
| Look-ahead | PASS | 375 checks. |
| MT5 REAL solo lectura | PASS | Cuenta REAL, `tradeapi_disabled=True`, sin órdenes ni operaciones del proceso. DEMO no disponible/no aplicable. |
| Datos del bróker | PASS | EURUSD, USDCAD y AUDUSD; spread observado 9, 9 y 9 puntos, respectivamente. |
| Riesgo monetario y límite diario | PASS | Cálculos hipotéticos contrastados con MT5 y evaluación del historial diario. |
| Históricos MT5 | PASS | 15 archivos para tres pares y cinco timeframes; 7.998 velas descargadas en snapshot independiente el 11/11/2026, como máximo 991 por archivo. |
| Ollama live | PASS | `qwen3:4b`; JSON, Pydantic, evidencia y guardas negativas. Una solicitud diagnóstica. |
| DeepSeek live local | PENDING | No se repitió una llamada facturable; el PASS remoto usa otro Python/Pydantic. |
| Backtests | PASS | BASE×2, CONSERVATIVE y STRESS en portfolio y stateful. Repetibilidad BASE PASS. |
| Regresión histórica | PENDING | Cambios de expiración explicados; selección restante y agregados antiguos no plenamente atribuibles. |
| Seguridad DRY RUN | PASS | `order_send` y `order_check` no se invocaron; 0 órdenes y 0 operaciones ejecutadas por el proceso. |

## C. Comparativa económica

Corte común `65 / LIMIT`. Los costes BASE de comisión y slippage son cero, sin verificar con tarifas del bróker; CONSERVATIVE y STRESS son hipótesis de sensibilidad. En el último histórico MT5, 6.516 de 7.998 barras tienen spread cero, de modo que el spread no permite contrastar tarifas de coste en esas barras.

### Portfolio

| Escenario | Oportunidades | Señales | Aceptados/rechazados | Ejecutadas/cerradas | Ambiguas | Bruto EUR | Coste cerrado/incurrido EUR | Neto EUR | PF neto | Expectativa neta EUR | Drawdown neto EUR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASE | 10.284 | 1.737 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0 / 0 | 25,62 | 1,118 | 0,041 | 10,94 |
| CONSERVATIVE | 10.284 | 1.736 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 / 58,58 | -27,53 | 0,890 | -0,044 | 36,69 |
| STRESS | 10.284 | 1.748 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 / 101,00 | -74,39 | 0,735 | -0,118 | 77,59 |

Stateful BASE neto: EURUSD +15,05 EUR, USDCAD +20,93 EUR, AUDUSD -14,89 EUR. CONSERVATIVE: -6,86 / +0,32 / -36,28 EUR. STRESS: -24,86 / -16,54 / -55,88 EUR. Expectativa, PF, operaciones, costes y drawdown completos por instrumento están en [`docs/FASE1_VALIDACION.md`](FASE1_VALIDACION.md) y en `reports/phase1_validation.json`. Se mantienen los resultados negativos observados.

La comparación por ID estable encontró cero cambios de dirección, puntuación o campos del plan en las operaciones emparejadas. En portfolio, 7.628 IDs coinciden, 87 son exclusivos de la versión anterior y 187 de la actual; 159 selecciones quedan pendientes de testigo directo. En stateful, 8.926 coinciden, 48 son anteriores, 165 actuales y 77 selecciones siguen pendientes. Los estados que cambiaron hacia `AMBIGUOUS_EXPIRY_BAR` corresponden a la corrección documentada del simulador; no se atribuyeron automáticamente las demás diferencias.

## D. Conclusión y tareas manuales

Se corrigieron la privacidad de los informes, la reutilización de evidencia tras merge por huellas verificables, la inclusión del control diario en el resultado global y la clasificación estricta de las regresiones. Se ejecutaron las pruebas registradas arriba. No se optimizó la estrategia.

La Fase 1 sigue pendiente. Para cerrar:

1. MT5 DEMO no está disponible. Si se requiere satisfacer el validador DEMO original, conectar una cuenta DEMO manualmente y ejecutar `python scripts/validate_phase1_mt5.py` con el trading vía API externa deshabilitado. La cuenta REAL ya fue comprobada en modo solo lectura y no debe operar.
2. Autorizar expresamente una llamada DeepSeek live en el runtime local actual; la clave se carga desde el almacén seguro, nunca en el chat.
3. Revisar las selecciones históricas restantes con snapshots comparables o documentar su disposición explícita.
4. Confirmar tarifas de comisión/slippage para sustituir hipótesis si se necesitan costes de bróker.

El estado global queda `INCOMPLETE_PENDING_MANUAL`; el validador retorna código 2 para PENDING, 1 para FAIL y 0 solo para `PHASE1_COMPLETE`. No se recomienda comenzar Fase 2. El sistema permanece en DRY RUN.

Informes verificables: [`reports/phase1_validation.json`](../reports/phase1_validation.json), [`reports/phase1_final_local/validation_summary.json`](../reports/phase1_final_local/validation_summary.json), [`reports/mt5_real_readonly_summary.json`](../reports/mt5_real_readonly_summary.json) y [`reports/phase1_final_local/integration_evidence.json`](../reports/phase1_final_local/integration_evidence.json).
