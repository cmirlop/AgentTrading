# NEWS-LOCAL-01 — adaptador Ollama local y validación

## Alcance

Esta pieza prepara un contrato y un laboratorio independiente bajo `src/news_local/`.
No implementa conectores de noticias, calendario real, persistencia ni integración con
agentes de producción. Todos los ejemplos llevan `SYNTHETIC_FIXTURE_NOT_LIVE_NEWS`.

El corte temporal exige zona horaria. Un evento publicado solo aporta hechos si fue
publicado, visto por primera vez y recuperado antes o en el corte. Una agenda futura se
presenta sin resultado ni consenso.

## Arquitectura

- `models.py` define contratos Pydantic versionados y cerrados ante campos extra: entrada 1.0 y resultado NEWS-02 2.0.
- `analyzer.py` aplica causalidad, agrupa revisiones y duplicados y valida referencias.
- `providers.py` implementa `NewsSummarizer` y el adaptador local de Ollama.
- `fixtures.py` contiene fixtures sintéticos de publicación, agenda, duplicados, inyección y drivers aislados.
- `scripts/run_news_local_ollama.py` ofrece una CLI offline por defecto.

El cliente global `src/agents/llm_client.py` se deja intacto porque registra consultas
en el ledger compartido y permite el proveedor remoto. Este adaptador solo permite HTTP
a `127.0.0.1` o `localhost` en el puerto 11434. Un `OLLAMA_HOST` externo bloquea
la ejecución. La CLI comprueba `/api/tags` y puede realizar como máximo una inferencia
`/api/chat`, sin descargas, redirecciones ni reintentos. El modelo usa `OLLAMA_MODEL`
y, si no existe, `config.agent_config.OLLAMA_MODEL`.

Los textos de fuente se envían como datos JSON no confiables, separados del prompt de
sistema. Se aplica un bloqueo previo a patrones conocidos y aislamiento de rol/datos;
la seguridad no depende solo de la expresión regular. Python conserva hechos, valores,
agenda, referencias, pares y efectos relativos. El modelo devuelve exclusivamente una
hipótesis tipada de dirección del driver (`STRENGTHENS`, `WEAKENS` o `UNKNOWN`).

## Comandos PowerShell

Desde la raíz del repositorio:

```powershell
./.venv/Scripts/python.exe -m unittest tests.test_news_local -v
./.venv/Scripts/python.exe scripts/run_news_local_ollama.py --mode offline
./.venv/Scripts/python.exe scripts/run_news_local_ollama.py --mode mock-ollama
```

Fixtures: `scheduled`, `published`, `usd_only`, `eur_only`, `cad_only`, `aud_only`,
`duplicate_revision`, `injection`, `unavailable` y `late_first_seen`. Para otro par:

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

## Puente offline para NEWS-01

`src/news_local/macro_adapter.py` transforma registros ya normalizados por
`src/macro_sources/core.py` a `NewsAnalysisInput`. No descarga, escribe archivos ni
inventa `actual`, `forecast`, `previous` o timestamps. Omite registros con hora de
publicación/programación no verificada y entrega un informe de exclusiones sin titulares.
El ID original se conserva como `source_event_id`; los IDs internos se vinculan a la fuente
y al registro. La llamada pública del laboratorio sigue siendo
`NewsSummarizer.summarize()`. No conecta escenarios a `MacroAgent`, riesgo u órdenes ni
altera el cliente LLM compartido.

En `main` no existe `src/news_intelligence/`; el adaptador cubre el contrato normalizado
macro actual y deja pendiente cualquier integración directa futura con NEWS-01.

## Validación rápida local

- HEAD base de la validación: `6f494c1`. Python 3.13.14.
- `python -m unittest tests.test_news_local -q`: PASS, 26 pruebas, 0 fallos, 0 omitidas.
- `python -m compileall -q src/news_local scripts/run_news_local_ollama.py`: PASS.
- El modelo local `qwen3:4b` estaba instalado. Con timeout de 1800 segundos, se probaron
  límites de 8192, 16384 y 32768 tokens (una inferencia por etapa). Las tres respuestas
  fueron JSON parseable, pero quedaron FALLBACK con `LLM_INVALID_SCENARIO_MAPPING; duraron
  99996, 99975 y 100121 ms. Ninguna agotó el timeout.
- El límite configurado final es 32768 tokens. El rechazo procede de la validación del
  escenario, no de un timeout ni de JSON incompleto. La causa exacta de esas tres
  respuestas no se puede reconstruir porque el contenido del modelo no se registró.
- Ese resultado es histórico de la validación local NEWS-01. No se repitió una inferencia
  local en NEWS-02 y no se considera demostración del contrato nuevo.

No se copió contenido de inferencia ni se alteraron la instalación, el modelo o variables
persistentes de Ollama.
