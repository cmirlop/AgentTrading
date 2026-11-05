# AgentTrading — Validación de Fase 1

## Alcance y estado

Esta fase endurece la integración LLM, el control de riesgo y la simulación histórica sin optimizar prompts, señales, pesos ni umbrales. El sistema continúa en simulación/DRY RUN: el flujo principal solo usa `mt5.order_check()` y el código de producción no contiene llamadas directas, alias ni llamadas dinámicas detectadas a `mt5.order_send()`.

El estado actual es **`INCOMPLETE_PENDING_MANUAL`**. En la investigación de DeepSeek, el primer intento devolvió `LLM_API_ERROR`; tras añadir clasificación segura, el siguiente se identificó como `LLM_PROVIDER_CONNECTION_FAILED` por la red saliente restringida, no por rechazo de clave. Al habilitar red, la siguiente solicitud pasó con `deepseek-flash`, JSON válido y evidencia `LIVE_PROBE`. No hubo reintentos automáticos. MT5 demo y Ollama siguen pendientes. El detalle final está en [`reports/phase1_validation.json`](../reports/phase1_validation.json); la línea base tomada antes de Fase 1.1 está en [`reports/phase1_pre_finalization_validation.json`](../reports/phase1_pre_finalization_validation.json), y las métricas base anteriores en [`reports/phase1_baseline_metrics.json`](../reports/phase1_baseline_metrics.json).

## Arquitectura LLM

Antes de Fase 1, algunos agentes llamaban a Ollama directamente y otras rutas usaban clientes DeepSeek duplicados. Ahora todos los agentes pasan por `src/agents/llm_client.py`, que selecciona `LLM_PROVIDER=ollama|deepseek` y entrega respuestas Pydantic validadas. Los IDs de evidencia se limitan al conjunto incluido en el payload y se verifican de nuevo después de la respuesta.

El adaptador DeepSeek usa una sola solicitud, `max_retries=0`, no imprime claves ni cuerpos de respuesta y comprueba respuesta completa, JSON y esquema. La carga de `.env` conserva la precedencia del entorno explícito (`override=False`). El SDK se importa de forma opcional para que las pruebas offline puedan correr sin instalar `openai`; si falta el SDK o la clave, la llamada falla cerrada con un código controlado.

Configuración habitual:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen3:4b
OLLAMA_TIMEOUT_SECONDS=120
```

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_MAX_TOKENS=16384
DEEPSEEK_TIMEOUT_SECONDS=180
# La clave se carga desde un gestor seguro del entorno; no se guarda en Git.
```

Las pruebas de interfaz usan proveedores falsos. La validación final usa el Python del proyecto (`pandas 3.1.0`, `numpy 2.5.4`, `pydantic 2.14.2`, `ollama 0.6.3`, `openai 3.41.0`); el SDK MT5 no está instalado. DeepSeek respondió correctamente a la solicitud de diagnóstico autorizada. Ollama no tuvo un servicio accesible.

## Pérdidas diarias y reservas

`src/mt5_deal_history.py` clasifica los tipos de deal de MT5. Solo suma los campos registrados `profit`, `commission`, `swap` y `fee` de deals BUY/SELL y deals de comisión vinculados a una orden o posición. Cada ticket se cuenta una vez. Excluye balance, crédito, cargos, correcciones, bonos, intereses, dividendos, impuestos y operaciones canceladas. Un tipo desconocido, historial no disponible, ticket duplicado, marca de tiempo fuera de la ventana UTC, comisión sin vínculo o dato monetario ausente invalida el cálculo y bloquea propuestas nuevas.

La ventana diaria se expresa en UTC. El bloqueo por pérdida alcanzada persiste a través de reinicios y se reinicia al cambiar el día UTC. Se evita añadir comisiones estimadas encima de los deals observados.

`src/daily_risk_guard.py` bloquea la transacción completa **leer → comprobar → reservar → guardar** entre procesos: `msvcrt` en Windows y `fcntl.flock` en POSIX. El estado se valida, escribe a un archivo temporal único, sincroniza y publica mediante reemplazo atómico. Estado corrupto, bloqueo fallido o escritura fallida deniegan la reserva. Las reservas llevan token de propietario y eventos de ciclo de vida; una liberación necesita el token que recibió su proceso. Las reservas DRY RUN vencen tras **15 minutos** (`DAILY_RISK_RESERVATION_TTL_MINUTES`) y se registran como expiradas. La ventana diaria cancela las reservas del día anterior.

`src/risk_preflight.py` mantiene bajo ese mismo bloqueo los controles diarios, los límites de cartera, la exposición compartida por divisa, los slots y la reserva final. La liberación después de un rechazo posterior también requiere el token. Se conservan los límites configurados existentes; el cambio solo hace atómica su aplicación.

## Simulador, costes e integridad histórica

`src/historical_execution.py` usa la cotización bid/ask histórica incorporada en las condiciones de fill y salida; no resta el spread una segunda vez. La comisión se aplica por lado y el slippage por lado, con conversión consistente a R y a EUR cuando existe el riesgo monetario necesario. Si no se puede convertir el slippage a EUR, el coste se informa como desconocido en vez de cero.

| Escenario | Spread histórico | Comisión round turn | Slippage por lado | Interpretación |
|---|---:|---:|---:|---|
| BASE | 1,00× | 0 EUR/lote | 0 puntos | El spread histórico bid/ask sí se aplica; comisión/slippage no verificados quedan omitidos. |
| CONSERVATIVE | 1,25× | 7 EUR/lote | 1 punto | Supuesto de sensibilidad, no tarifa de bróker. |
| STRESS | 2,00× | 10 EUR/lote | 3 puntos | Supuesto adverso, no tarifa de bróker. |

TP y SL simultáneos se clasifican como ambiguos. Un fill en una vela que cruza el vencimiento también es ambiguo porque no se conoce el orden intrabar. Una orden aún pendiente al final del dataset queda `PENDING_AT_END`; no se etiqueta como expirada sin que haya vencido. Operaciones abiertas y ambiguas no reciben un PnL inventado; los costes ya incurridos se informan aparte. Las velas se exigen cronológicas, únicas y con cierres no anteriores a su apertura. Las pruebas de look-ahead existentes ejecutaron 375 comprobaciones.

Las comparaciones preservan las métricas técnicas antes/después y hacen explícito el efecto de corregir estados de órdenes: el estado de fills/expiración puede cambiar la ocupación de slots y, en consecuencia, cuántas oportunidades posteriores llegan a validación de plan. El total de oportunidades debe permanecer idéntico. En el replay, portfolio mantuvo 10.284 oportunidades. En stateful, la muestra `65 / LIMIT` registró 3.362 para AUDUSD, 3.271 para EURUSD y 3.651 para USDCAD; cada fila se comparó antes/después y no hubo diferencias de oportunidades. Esas diferencias de ejecución no se tratan como una mejora predictiva.

## Validación y procedencia

`scripts/validate_phase1.py` registra commit, rama, estado del árbol al iniciar/cerrar, SHA-256 del CSV congelado, archivos de simulación/configuración relevantes, Python, dependencias, fecha UTC y comandos/resultados. Ejecuta pruebas offline, parseo/importación, análisis AST de rutas de envío y los backtests portfolio/stateful BASE dos veces, CONSERVATIVE y STRESS. La repetición BASE debe ser exacta. La comparación técnica exige que las oportunidades no varíen; solo cambios de estado de ejecución y sus efectos de ocupación pueden diferir.

La validación final ejecutó **69/69** pruebas `test_phase1_*.py`, **21/21** pruebas offline históricas y los ocho backtests completos bajo el Python del proyecto. Las dos repeticiones BASE fueron idénticas y las oportunidades se mantuvieron invariantes. MT5 y Ollama siguen PENDING; DeepSeek está PASS. No se omitió ninguna prueba requerida como PASS.

`--refresh-offline` reutiliza un replay únicamente si coinciden los hashes del dataset, código relevante, configuración del backtest y métricas baseline, y existe evidencia histórica completa. Si no coinciden, falla en lugar de atribuir el replay anterior a la versión actual. Los externos deben tener estado, fecha reciente y procedencia coincidente; `PENDING` nunca cuenta como PASS.

Las pruebas MT5 y LLM no se ejecutan desde la suite offline. Sus scripts son `scripts/validate_phase1_mt5.py` y `scripts/validate_phase1_llm_live.py`. El primero solo lee una cuenta DEMO verificable y calcula pruebas hipotéticas; no llama `order_check()` ni `order_send()`. El segundo hace una solicitud diagnóstica pequeña; DeepSeek exige además el flag `--authorize-deepseek` para esa invocación.

## Comandos reproducibles en Windows 11

Desde la raíz, con el entorno virtual del proyecto activo:

```powershell
python scripts/validate_phase1.py
python -m unittest discover -s tests -p "test_phase1_*.py"
python scripts/validate_phase1.py --refresh-offline
```

Para las integraciones manuales:

```powershell
# MT5 abierto en cuenta DEMO; deshabilitar el trading algorítmico vía Python
python scripts/validate_phase1_mt5.py

# Ollama encendido y el modelo configurado disponible
ollama list
python scripts/validate_phase1_llm_live.py --provider ollama
```

Para DeepSeek, cargar la clave desde el almacén seguro de Windows configurado por el usuario, sin imprimirla. Ejemplo con PowerShell SecretManagement:

```powershell
$env:DEEPSEEK_API_KEY = Get-Secret -Name DEEPSEEK_API_KEY -AsPlainText
python scripts/validate_phase1_llm_live.py --provider deepseek --authorize-deepseek
Remove-Item Env:\DEEPSEEK_API_KEY
```

El último comando realiza una solicitud API única y de bajo límite de tokens. Durante el diagnóstico hubo dos intentos fallidos (error genérico y después conexión bloqueada) y uno exitoso tras habilitar la red: 192 tokens de entrada y 17 de salida. La clave estaba definida, nunca se registró y el cliente no reintentó ninguna solicitud. `--refresh-offline` no vuelve a llamar MT5, Ollama o DeepSeek.

## Archivos de Fase 1.1

- Riesgo/deals: `src/mt5_deal_history.py`, `src/daily_risk_guard.py`, `src/risk_preflight.py`, `build_runtime_state.py`, `config/risk_config.py`.
- LLM y DRY RUN: `src/agents/llm_client.py`, `src/agents/deepseek_client.py`, agentes de riesgo/decisión y `run_dry_pipeline.py`.
- Simulación: `src/historical_execution.py`, `run_portfolio_backtest.py`, `run_stateful_backtest.py`, `config/backtest_cost_config.py`.
- Validación: `scripts/validate_phase1.py`, `scripts/validate_phase1_mt5.py`, `scripts/validate_phase1_llm_live.py`, pruebas `tests/test_phase1_*.py`.
- Evidencia: `reports/phase1_validation.json`, `reports/phase1_pre_finalization_validation.json`, `reports/phase1_mt5_validation.json`, `reports/phase1_llm_live_*.json`.

No se inició Fase 2, no se cambiaron prompts o umbrales estratégicos y no se enviaron órdenes reales.
