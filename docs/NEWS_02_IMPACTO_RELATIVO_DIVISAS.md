# NEWS-02 — impacto relativo de noticias sobre pares Forex

## Estado y alcance

NEWS-02 corrige el contrato del laboratorio local de noticias. Es una herramienta de
investigación informativa: no crea órdenes, precios objetivo, probabilidades, señales
operativas ni decisiones de riesgo. NEWS-01 de fuentes todavía no está conectado
directamente; existe un adaptador puro para sus registros macro ya normalizados.

El corte `analysis_as_of_utc` requiere zona horaria. Una publicación solo puede aportar
hechos si su publicación, primera observación y recuperación están disponibles en el
corte. Una publicación de más de siete días se clasifica como antigua y no autoriza
hechos. Las agendas requieren timestamp con zona y estado verificado; una agenda pasada
sin publicación queda excluida. Revisiones observadas después del corte no sustituyen la
versión conocida entonces. La política de siete días es explícita y reproducible, no una
regla de trading.

## Diagnóstico del contrato anterior

`ConditionalScenario.currency` tenía dos lecturas posibles: divisa que origina la noticia
y divisa cuya relación con el par se describe. `_validate_proposal` primero exigía que el
par de la propuesta coincidiera con el símbolo analizado y que `currency` perteneciera a
ese par; después exigía que esa misma `currency` estuviera en las divisas de los eventos
citado. Por eso una propuesta que usara `EUR` para describir el efecto relativo en EURUSD
a partir de una noticia solamente USD era rechazada como `LLM_INVALID_SCENARIO_MAPPING`.
La rama concreta que causó las tres respuestas locales de Qwen no se puede determinar:
esas respuestas se analizaron en Windows, pero sus contenidos no se guardaron. No
atribuimos retrospectivamente un motivo exacto a esas tres inferencias.

El analizador determinista tenía la misma ambigüedad: producía una hipótesis por divisa
del evento, pero no separaba origen, contraparte, sentido hipotético del driver y signo
esperado del par.

## Contrato NEWS-02

La entrada conserva el esquema 1.0. El resultado usa el esquema 2.0:

- `driver_currency` procede de `currencies` en eventos elegibles.
- `pair`, `base_currency` y `quote_currency` salen de `SUPPORTED_PAIRS` en Python.
- `counterpart_currency` es la otra moneda del par y está marcada como contraparte
  relativa inferida; no se crea un hecho ni un evento para ella.
- `scenario_key` se construye en Python como `PAR:DRIVER:SENTIDO`; no puede inventarlo
  el LLM.
- `impact_origin` vale `DIRECT_EVENT+DERIVED_RELATIVE_PAIR_EFFECT`.
- `direction_hypothesis` puede ser `STRENGTHENS`, `WEAKENS` o `UNKNOWN`. Es una hipótesis
  LLM explícita y no calibrada, no una observación. Python conserva los dos escenarios
  condicionales aunque el modelo seleccione una hipótesis.
- `event_ids` y `evidence_ids` se añaden en Python desde los eventos que respaldan el
  driver. El modelo no puede crear IDs ni declarar una segunda confirmación.

| Driver que gana fuerza relativa frente a la contraparte | EURUSD | USDCAD | AUDUSD |
| --- | --- | --- | --- |
| USD | baja | sube | baja |
| EUR | sube | no aplica por sí sola | no aplica por sí sola |
| CAD | no aplica por sí sola | baja | no aplica por sí sola |
| AUD | no aplica por sí sola | no aplica por sí sola | sube |

Si el driver pierde fuerza relativa, el signo se invierte. Es una relación hipotética
manteniendo todo lo demás constante; una noticia no obliga al mercado a reaccionar así.
Una noticia USD no demuestra que EUR, CAD o AUD se debiliten frente a todas las monedas.

### Ejemplo sintético

La única evidencia del ejemplo es un evento sintético que declara `USD`; no hay noticia
EUR. El evento USD respalda el driver y los IDs permanecen USD en ambos escenarios:

Fragmento sintético del resultado; se muestran los campos de driver/escenario para
explicar el mapeo (la CLI añade también hechos, cobertura, límites y metadatos):

```json
{
  "schema_version": "2.0",
  "symbol": "EURUSD",
  "driver_assessments": [
    {
      "scenario_key": "EURUSD:USD:STRENGTHENS",
      "driver_currency": "USD",
      "direction_hypothesis": "UNKNOWN",
      "event_ids": ["SYNTH-USD-ONLY"],
      "evidence_ids": ["SYNTH-EVIDENCE-USD"]
    }
  ],
  "conditional_scenarios": [
    {
      "scenario_key": "EURUSD:USD:WEAKENS",
      "driver_currency": "USD",
      "pair": "EURUSD",
      "base_currency": "EUR",
      "quote_currency": "USD",
      "counterpart_currency": "EUR",
      "driver_direction_condition": "STRENGTHENS",
      "pair_direction": "DOWN",
      "impact_origin": "DIRECT_EVENT+DERIVED_RELATIVE_PAIR_EFFECT",
      "condition": "If USD relatively strengthens against EUR, all else equal.",
      "possible_effect": "EURUSD could fall under this conditional relative scenario; other information or repricing may alter or reverse the reaction.",
      "event_ids": ["SYNTH-USD-ONLY"],
      "evidence_ids": ["SYNTH-EVIDENCE-USD"]
    },
    {
      "driver_currency": "USD",
      "pair": "EURUSD",
      "base_currency": "EUR",
      "quote_currency": "USD",
      "counterpart_currency": "EUR",
      "driver_direction_condition": "WEAKENS",
      "pair_direction": "UP",
      "impact_origin": "DIRECT_EVENT+DERIVED_RELATIVE_PAIR_EFFECT",
      "condition": "If USD relatively weakens against EUR, all else equal.",
      "possible_effect": "EURUSD could rise under this conditional relative scenario; other information or repricing may alter or reverse the reaction.",
      "event_ids": ["SYNTH-USD-ONLY"],
      "evidence_ids": ["SYNTH-EVIDENCE-USD"]
    }
  ]
}
```

En USDCAD, el mismo USD fortalece el numerador y el par sube; en AUDUSD, fortalece el
denominador y el par baja. Los cuatro resultados conservan `SYNTH-EVIDENCE-USD`.

La respuesta de Ollama permitida es mínima y cerrada. Un ejemplo válido sería:

```json
{
  "schema_version": "2.0",
  "driver_assessments": [
    {"driver_currency": "USD", "direction_hypothesis": "UNKNOWN"}
  ]
}
```

Python rechaza drivers sin soporte con `INVALID_DRIVER_EVIDENCE`, cobertura omitida con
`LLM_DRIVER_COVERAGE_INCOMPLETE`, JSON inválido con `LLM_INVALID_JSON` y campos o
versiones desconocidos con `LLM_RESPONSE_SCHEMA_INVALID`. La validación de salida vuelve
a calcular el signo: un par, contraparte o dirección alterados producen
`INVALID_RELATIVE_MAPPING`. En cualquier error semántico o de proveedor se usa el
escenario determinista marcado `FALLBACK`; JSON parseable no convierte un rechazo en
`PASS`.

## Fechas, duplicados y contenido no confiable

La entrada mantiene `event_id`, `evidence_id`, `first_seen_at_utc`, `retrieved_at_utc`,
`published_at_utc` o `scheduled_at_utc` y vínculos de duplicado/revisión. Toda evidencia
debe estar conocida al corte. Una versión observada después no puede reemplazar la
versión previa; metadata de revisión también se filtra por los timestamps del corte. Los
duplicados se agrupan incluso si el registro raíz llegó después del
corte, para no inflar `independent_event_groups`.

Los textos de fuente se pasan como datos JSON no confiables. El enlace de origen no se
envía al modelo ni se abre. El prompt no habilita herramientas. El resultado no contiene
titulares ni respuestas libres del LLM: este devuelve solo enums. Esto reduce la
superficie de inyección y hace que las reglas estructurales, y no una regex, sean la
defensa principal.

## Adaptador macro offline

`src/news_local/macro_adapter.py` adapta salidas ya normalizadas de
`src/macro_sources/core.py` y recibe el registro de fuentes que ya carga el llamador.
Comprueba ID/tipo/origen/currencies contra el registro y allowlist. Solo acepta
`NEWS_RELEASE` con publicación verificada y
`SCHEDULED_EVENT` con estado verificado, precisión de segundos y zona horaria. Omite
registros sin origen/hash/timestamps fiables y entrega códigos de exclusión. No realiza
solicitudes, no escribe `data/state/calendar.json`, no interpreta artículos, no inventa
`actual`, `forecast`, `previous` ni consenso, y no convierte una agenda en una noticia.
El `source_event_id` original queda preservado; los IDs y hashes derivados son estables
para esa fuente y ese registro.

No existe `src/news_intelligence/` en el `main` usado como base. El adaptador cubre el
contrato normalizado actual; la integración con otro módulo NEWS-01 queda pendiente de
revisar cuando ese módulo esté disponible.

## Validación reproducible

Desde la raíz del repositorio:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_news_local tests.test_news_02 -q
.\.venv\Scripts\python.exe -m compileall -q src/news_local scripts/run_news_local_ollama.py tests/test_news_local.py tests/test_news_02.py
.\.venv\Scripts\python.exe scripts/run_news_local_ollama.py --mode offline --fixture usd_only --symbol EURUSD
.\.venv\Scripts\python.exe scripts/run_news_local_ollama.py --mode mock-ollama --fixture usd_only --symbol EURUSD
```

CI ejecuta tests, compilación y matriz offline/mock para EURUSD, USDCAD y AUDUSD. La
condición de esta rama omite el job general que lanza backtests históricos. El modo
`live-ollama` no forma parte de CI.

## Estado y límites

- Tests y mocks no demuestran una inferencia real correcta de Qwen.
- No se ejecutó Ollama en remoto; no se hizo una inferencia local NEWS-02 en esta fase.
- La hipótesis direccional del LLM sigue sin calibrar y no constituye evidencia de ventaja.
- No se consultaron noticias actuales ni servicios externos.
- No se alteraron el modelo, `num_predict=32768` ni la configuración `think` existente.
- El NEWS local sigue aislado de `MacroAgent`, del riesgo y del motor de órdenes.

Declaraciones de alcance: `OLLAMA_LIVE_NOT_RUN_REMOTE`, `THINK_BEHAVIOR_UNCHANGED`,
`NO_PAID_API_CALLS`, `NO_TRADING_OR_BACKTEST_CHANGES`.
