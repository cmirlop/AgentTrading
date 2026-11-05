# Cierre de Fase 1.1 — AgentTrading

## Trazabilidad del trabajo

- Rama independiente: `codex/phase1-final-validation`.
- Commit base local de la rama: `1ef0faf7600b866236f75cb7c229fc8a3b66973e`.
- Commit verificado de GitHub `main`: `8ffe975251d3115f90ad263cc54c1ce7dbe99a43`.
- Árbol de GitHub `main`: `4b5951ecb79d7cea63c3afe8602f8861677f3634`; coincide con el árbol del commit base local. La diferencia es el commit de merge de GitHub, sin cambios de contenido.
- SHA-256 del dataset congelado `data/backtest/technical_candidate_scan.csv`: `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3` (22.011 filas; 7 de abril a 7 de octubre de 2026).
- No se creó commit ni se hizo push/merge. Los cambios permanecen sin commit en la rama indicada.

Antes de cambiar código se ejecutó la validación offline existente. Resultado: 34/34 pruebas Phase 1 y 21/21 pruebas offline anteriores pasaron; la integración MT5/Ollama/DeepSeek quedó pendiente. La captura está en `reports/phase1_pre_finalization_validation.json`.

## Problemas confirmados y correcciones

1. El PnL diario sumaba `profit`, `commission`, `swap` y `fee` de cualquier tipo de deal. Un depósito o retirada podía distorsionar la pérdida diaria. Se añadió una clasificación explícita; movimientos ajenos al trading se excluyen y el dato ausente, duplicado, desconocido o fuera de UTC bloquea nuevas propuestas.
2. El lock protegía escrituras, pero no la transacción leer/comprobar/reservar/guardar. Dos procesos podían aprobar reservas incompatibles. El bloqueo multiproceso ahora cubre toda la preflight, con `msvcrt` en Windows y `flock` en POSIX. La liberación requiere un token propietario y las reservas DRY RUN expiran a los 15 minutos.
3. `--refresh-offline` podía reutilizar replays sin verificar que correspondieran al mismo código/configuración/dataset. Ahora compara hashes exactos y exige evidencia de replay previo completa. Pruebas pendientes y resultados externos antiguos permanecen PENDING.
4. El simulador expiraba órdenes pendientes al final del dataset y asumía una secuencia en velas que cruzaban la expiración. Ahora registra `PENDING_AT_END`, `EXPIRED` o `AMBIGUOUS_EXPIRY_BAR` según la evidencia disponible y cobra costes conservadores a fills ambiguos.
5. El importe de slippage en EUR podía aparecer como cero si faltaba información para convertirlo. Ahora se muestra como desconocido.
6. El primer intento DeepSeek devolvió el código genérico `LLM_API_ERROR`. Tras añadir clasificación segura, el segundo confirmó `LLM_PROVIDER_CONNECTION_FAILED` sin respuesta HTTP; la clave tenía formato limpio y no fue rechazada. Se habilitó conectividad saliente y el intento siguiente pasó en `deepseek-flash`. No hubo reintentos automáticos. MT5 no tiene SDK/terminal disponible; el servicio Ollama no es accesible desde el entorno.

## Cambios y pruebas

Se modificaron `build_runtime_state.py`, `config/risk_config.py`, `run_dry_pipeline.py`, `scripts/validate_phase1.py`, `src/agents/decision_agent.py`, `src/agents/deepseek_client.py`, `src/agents/llm_client.py`, `src/agents/risk_agent.py`, `src/daily_risk_guard.py`, `src/historical_execution.py` y `src/risk_preflight.py`. Se actualizó `.gitignore` para excluir lockfiles y temporales de estado.

Se añadieron `src/mt5_deal_history.py`, `scripts/validate_phase1_mt5.py` y `scripts/validate_phase1_llm_live.py`. Las pruebas nuevas/modificadas cubren clasificación de deals, ganancias/pérdidas, depósitos/retiros, comisiones/swap/fee, historial incompleto, ventanas UTC, persistencia, expiración, liberación por token, escritura fallida y concurrencia real con procesos `spawn`; costes y bid/ask; expiración ambigua; selección de proveedor, JSON/evidencia inválidos, dependencia DeepSeek ausente y fallos sin señal; procedencia y reutilización por hash; rechazo de cuentas reales; BUY y SELL que se mantienen únicamente en estado DRY RUN; y escaneo AST de rutas de ejecución.

Resultado final medido: **68/68** pruebas `test_phase1_*.py` y **21/21** pruebas offline históricas PASS. El análisis/look-ahead completó 375 checks. Los ocho backtests acabaron PASS y las dos repeticiones BASE fueron idénticas. `--refresh-offline` volvió a ejecutar la suite y reutilizó los replays solo después de verificar los hashes (`validator_refresh_hash_check=PASS`).

## Resultados históricos

Se completaron ocho replays completos con el mismo dataset/configuración: portfolio BASE dos veces, CONSERVATIVE y STRESS; stateful BASE dos veces, CONSERVATIVE y STRESS. Los ocho terminaron PASS. Cada repetición BASE produjo métricas idénticas.

El total de oportunidades técnicas permaneció idéntico: **10.284** en portfolio. En stateful, la muestra `65 / LIMIT` conserva 3.362 oportunidades AUDUSD, 3.271 EURUSD y 3.651 USDCAD. El simulador actualizado cambia el estado de fills/expiración y la ocupación de slots, por lo que algunas oportunidades posteriores llegan ahora a validación de plan. El validador confirma que no cambiaron las oportunidades, rechaza cualquier diferencia en ese campo y clasifica explícitamente los cambios downstream de accepted/rejected como efectos de ejecución. No se cambiaron prompts, criterios de señal, pesos o umbrales.

Muestra `65 / LIMIT` de portfolio:

| Escenario | Oportunidades | Planes aceptados/rechazados | Ejecutadas / cerradas | Ambiguas | Bruto EUR | Costes EUR | Neto EUR |
|---|---:|---:|---:|---:|---:|---:|---:|
| BASE anterior | 10.284 | 1.708 / 246 | 670 / 641 | 28 | 23,94 | 0,00 | 23,94 |
| BASE actualizado | 10.284 | 1.737 / 266 | 686 / 628 | 28 | 25,62 | 0,00 | 25,62 |
| CONSERVATIVE actualizado | 10.284 | 1.736 / 266 | 685 / 627 | 28 | 26,12 | 53,64 | -27,53 |
| STRESS actualizado | 10.284 | 1.748 / 265 | 689 / 632 | 28 | 18,29 | 92,68 | -74,39 |

En BASE, cero costes adicionales significa solo que no hay tarifas de comisión/slippage verificadas; el spread histórico bid/ask sigue incorporado en los fills. Los valores CONSERVATIVE/STRESS son supuestos de sensibilidad, no tarifas confirmadas del bróker. El coste incurrido total, que incluye operaciones no cerradas, fue 58,58 EUR y 101,00 EUR respectivamente.

Muestra stateful `USDCAD / 65 / LIMIT`: BASE 3.651 oportunidades, 729 aceptados, 87 rechazados, 275 ejecutados, 257 resueltos, 18 ambiguos, bruto/neto 20,93 EUR; CONSERVATIVE bruto 21,42 EUR, costes de cerrados 21,10 EUR, neto 0,32 EUR; STRESS bruto 19,01 EUR, costes 35,55 EUR, neto -16,54 EUR. Los costes totales incurridos fueron 22,58 EUR y 38,02 EUR.

Los cuatro cotejos contra baseline/pre-cierre tienen diferencias solo en ejecución y efectos de ocupación. No son una coincidencia exacta: la tabla del informe guarda todas las diferencias y las etiqueta como esperadas; los campos de oportunidades son exactos. Los replays BASE portfolio/stateful repetidos coinciden sin diferencias. No se usaron las estadísticas para optimizar la estrategia.

## Estado de validaciones

| Comprobación | Estado | Evidencia |
|---|---|---|
| Selección de proveedor, JSON y evidencia LLM offline | PASS | Suite mock `test_phase1_*.py`; no hay llamadas facturables. |
| DeepSeek live | PASS | Modelo reportado `deepseek-flash`; JSON/Pydantic y evidencia `LIVE_PROBE` válidos; 1.636 s, 192 tokens de entrada y 17 de salida; una solicitud, sin reintentos ni órdenes. |
| Ollama live | PENDING | Servicio/SDK/modelo no disponible en este entorno; cero solicitudes. |
| Riesgo por operación y cartera | PASS | Pruebas offline de sizing, límites, conversiones y exposición compartida USD. |
| Pérdida diaria/deals MT5 | PASS | Deals mock clasificados, suma neta determinista, fail-closed, UTC y bloqueo persistente. |
| Concurrencia de reservas | PASS | Procesos reales `spawn`, presupuesto conjunto, duplicado, token, expiración, reinicio y escritura. |
| Riesgo diario MT5 real | PENDING | Sin terminal; comprobación demo de lectura requiere Windows/MT5. |
| Costes BASE/CONSERVATIVE/STRESS | PASS | Ocho backtests completos; hipótesis y tarifas no verificadas etiquetadas. |
| TP/SL y expiración intrabar ambigua | PASS | Pruebas de ambigüedad y regresión histórica. |
| Look-ahead | PASS | `test_no_lookahead.py`: 375 comprobaciones. |
| Regresión y reproducibilidad | PASS | Oportunidades sin cambios; BASE repetido idéntico; diferencias de ejecución justificadas y registradas. |
| Seguridad DRY RUN | PASS | AST sin llamadas `order_send`, gates `tradeapi_disabled`, pruebas BUY/SELL y `real_execution_authorized=false`. |
| Fase 1 completa | PENDING | DeepSeek pasó; MT5 demo y Ollama siguen PENDING. No cumple todavía los criterios de cierre. |

El informe automatizado final es `reports/phase1_validation.json`. La captura de cada integración está en `reports/phase1_mt5_validation.json`, `reports/phase1_llm_live_ollama.json` y `reports/phase1_llm_live_deepseek.json`; las tres indican PENDING y no guardan número de cuenta ni claves.

## Limitaciones y ejecución manual pendiente

Las pruebas de concurrencia usaron procesos `spawn` en el host Linux; la ruta Windows `msvcrt` requiere ejecución de la suite en Windows 11. MT5, sus metadatos reales y `order_calc_profit()` no pudieron verificarse en este entorno. Los costes reales dependen de datos del bróker. El histórico está congelado y no sustituye validación prospectiva.

Desde PowerShell en Windows 11, en la raíz del proyecto y con `.venv` activo:

```powershell
python scripts/validate_phase1.py
python -m unittest discover -s tests -p "test_phase1_*.py"
python scripts/validate_phase1_mt5.py
ollama list
python scripts/validate_phase1_llm_live.py --provider ollama
```

MT5 debe estar conectado a **DEMO** y configurado para deshabilitar trading algorítmico vía API Python. Para DeepSeek, cargar `DEEPSEEK_API_KEY` desde un almacén seguro previamente configurado y ejecutar solo cuando se autorice conscientemente una llamada API única:

```powershell
$env:DEEPSEEK_API_KEY = Get-Secret -Name DEEPSEEK_API_KEY -AsPlainText
python scripts/validate_phase1_llm_live.py --provider deepseek --authorize-deepseek
Remove-Item Env:\DEEPSEEK_API_KEY
```

El error se resolvió habilitando conectividad saliente para el intento autorizado; la clave no estaba siendo rechazada. Después de completar MT5 demo y Ollama, ejecutar `python scripts/validate_phase1.py --refresh-offline` para verificar procedencia y actualizar el informe sin repetir replays ni hacer llamadas externas. No comenzar Fase 2 hasta completar esas integraciones y revisar el resultado en demo.

**Evaluación:** la parte offline/histórica está completada y DeepSeek live pasó. MT5 demo y Ollama siguen pendientes, por lo que la Fase 1 global no puede declararse `PHASE1_COMPLETE`. Estado: **`INCOMPLETE_PENDING_MANUAL`**. El sistema no habilita órdenes reales.
