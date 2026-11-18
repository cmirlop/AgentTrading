# NEWS-LOCAL-01 — adaptador Ollama local y validación

## Alcance

Esta pieza prepara un contrato y un laboratorio independiente bajo `src/news_local/`.
No implementa conectores de noticias, calendario real, persistencia ni integración con
agentes de producción. Todos los ejemplos llevan `SYNTHETIC_FIXTURE_NOT_LIVE_NEWS`.

El corte temporal exige zona horaria. Un evento publicado solo aporta hechos si fue
publicado, visto por primera vez y recuperado antes o en el corte. Una agenda futura se
presenta sin resultado ni consenso.

## Arquitectura

- `models.py` define contratos Pydantic versionados y cerrados ante campos extra.
- `analyzer.py` aplica causalidad, agrupa revisiones y duplicados y valida referencias.
- `providers.py` implementa `NewsSummarizer` y el adaptador local de Ollama.
- `fixtures.py` contiene seis escenarios sintéticos fijos.
- `scripts/run_news_local_ollama.py` ofrece una CLI offline por defecto.

El cliente global `src/agents/llm_client.py` se deja intacto porque registra consultas
en el ledger compartido y permite el proveedor remoto. Este adaptador solo permite HTTP
a `127.0.0.1` o `localhost` en el puerto 11434. Un `OLLAMA_HOST` externo bloquea
la ejecución. La CLI comprueba `/api/tags` y puede realizar como máximo una inferencia
`/api/chat`, sin descargas, redirecciones ni reintentos. El modelo usa `OLLAMA_MODEL`
y, si no existe, `config.agent_config.OLLAMA_MODEL`.

Los textos de fuente se envían como datos JSON no confiables, separados del prompt de
sistema. Una coincidencia con patrones de inyección bloquea Ollama antes de contactar
el servidor. Los hechos, valores numéricos y agenda se producen fuera del LLM; su salida
solo puede proponer escenarios condicionales que después se validan externamente.

## Comandos PowerShell

Desde la raíz del repositorio:

```powershell
./.venv/Scripts/python.exe -m unittest tests.test_news_local -v
./.venv/Scripts/python.exe scripts/run_news_local_ollama.py --mode offline
./.venv/Scripts/python.exe scripts/run_news_local_ollama.py --mode mock-ollama
```

Fixtures: `scheduled`, `published`, `duplicate_revision`, `injection`,
`unavailable` y `late_first_seen`. Para otro par:

```powershell
./.venv/Scripts/python.exe scripts/run_news_local_ollama.py --mode offline --fixture published --symbol USDCAD
```

Prueba live explícita, solo si Ollama y el modelo ya existen:

```powershell
./.venv/Scripts/python.exe scripts/run_news_local_ollama.py --mode live-ollama --fixture published
```

Si runtime o modelo no están disponibles, queda `PENDING` con `PENDING_LOCAL_RUNTIME`.
El informe no incluye la respuesta del modelo. `--output` solo acepta rutas bajo
`reports/private/news_local/` o `reports/news_local_01/`. No se escribe por defecto.

## Matriz real, simulada y pendiente

| Modo | Ollama | Qué demuestra |
| --- | --- | --- |
| `offline` | Sin llamada | Contrato, causalidad y proveedor determinista |
| `mock-ollama` | Simulado | Parser, referencias y reglas de salida |
| `live-ollama` | Loopback real | Integración observada; PASS requiere inferencia real válida |

Un PASS offline o mock no prueba que Ollama esté disponible. Las hipótesis son
cualitativas, con incertidumbre amplia y no calibrada. No hay fuentes actuales,
probabilidades, señales, PnL, decisiones de riesgo ni integración con MetaTrader 5.

## Diagnóstico seguro

Si live queda pendiente, consulta [OLLAMA_WINDOWS_DIAGNOSTICO.md](OLLAMA_WINDOWS_DIAGNOSTICO.md).
Comprueba identidad del proceso, host/puerto y `/api/tags`. No cambies ACL, registro,
firewall, instalación, servicio ni archivos de modelos para hacer pasar la prueba.

## Handoff para NEWS-01 remoto

Cuando exista `src/news_intelligence/`, un adaptador allí debe mapear sus eventos a
`NewsAnalysisInput` preservando `even_t_id`, `evidence_id`, `first_seen_at_utc`,
`retrieved_at_utc`, publicación/programación y relaciones de revisión/duplicado. Invoca
`NewsSummarizer.summarize()` desde un punto explícito; conserva las referencias y límites
del resultado. No conectes los escenarios a `MacroAgent`, riesgo u órdenes ni cambies el
cliente LLM compartido.

No existe `src/news_intelligence/` en esta copia local. GitHub quedó inaccesible por
la red restringida, así que no se simuló esa integración.

## Validación rápida local

- HEAD base de la validación: `6f494c1`. Python 3.13.14.
- `python -m unittest tests.test_news_local -q`: PASS, 26 pruebas, 0 fallos, 0 omitidas.
- `python -m compileall -q src/news_local scripts/run_news_local_ollama.py`: PASS.
- El modelo local `qwen3:4b` estaba instalado. Con timeout de 1800 segundos, se probaron
  límites de 8192, 16384 y 32768 tokens (una inferencia por etapa). Las tres respuestas
  fueron JSON parseable, pero quedaron FALLBACK con `LLM_INVALID_SCENARIO_MAPPING; duraron
  99996, 99975 y 100121 ms. Ninguna agotó el timeout.
- El límite configurado final es 32768 tokens. El rechazo procede de la validación del
  escenario, no de un timeout ni de JSON incompleto.
- CI: sí; `.github/workflows/offline-research-tests.yml` ejecuta explícitamente la suite en
  el job offline habitual para push y PR. No se ha observado una ejecución remota de Actions.
- El diff YAML contiene solo el nuevo paso, con indentación del bloque `steps` existente;
  la comprobación estructural pasó y `git diff --check` no reporta errores. No hay un
  parser YAML instalado y no se ejecutó GitHub Actions.

No se copió contenido de inferencia ni se alteraron la instalación, el modelo o variables
persistentes de Ollama.
