# NEWS-03 — resultados de la sesión local

**Estado:** `NEWS03_PARTIAL`  
**Commit base:** `82f33e8ed3561365c98f298cd61162d2a9554dff` (`main`, NEWS-02)  
**Rama de trabajo:** `codex/news-03-local-integration`  
**Repositorio remoto:** `origin/main` seguía en el commit base tras `git fetch`; no había cambios nuevos.  
**Inicio:** 2026-11-19 18:59:43 UTC.  
**Duración hasta el cierre técnico:** aproximadamente 23 minutos.  

## Resultado

| Comprobación | Estado | Evidencia |
|---|---|---|
| Tests NEWS-02 | PASS | Línea base: `python -m unittest tests.test_news_local tests.test_news_02 -q`, 62 tests. Cierre: NEWS-LOCAL + NEWS-02 + NEWS-03, 69 tests, `OK`. |
| Ollama live | PASS | Una inferencia sintética; `qwen3:4b`, esquema 2.0, JSON válido, 106434 ms, un request de generación. El driver fue USD y Python derivó EURUSD DOWN si USD se fortalece y UP si se debilita. |
| Recopilación RSS/Atom | PARTIAL | Cuatro peticiones, sin reintentos. Fed y Banco de Canadá respondieron HTTP 200; BCE y RBA fallaron con `URLError`. BLS ICS no se consultó porque sigue inactivo en el registro. |
| Adaptador macro | PASS | Siete pruebas nuevas; conversión normalizada a NEWS-02, límites privados, fuente inaccesible y matriz USD/EUR/CAD/AUD para los tres pares. |
| Pipeline end-to-end | PARTIAL | Captura real acotada y replay determinista desde snapshot privado. Captura: 21 eventos recibidos, 20 aceptados por el adaptador, 4 exclusiones de fuente/registro, 3 eventos elegibles para NEWS-02 y 17 descartados por antiguos. El reporte conserva `PARTIAL` por cobertura incompleta. |
| Impacto relativo | PASS | NEWS-02 comprueba USD en EURUSD, USDCAD y AUDUSD, además de EUR/CAD/AUD en su par. Los escenarios son condicionales y no señales de operación. |
| Seguridad y privacidad | PASS | Los snapshots, respuestas RSS y resultados quedaron en `reports/private/`, ignorado por Git. No se usó MT5 ni DeepSeek; no se cambiaron estrategias, riesgo, `think`, modelo ni límite de salida. |
| CI | PENDING | El workflow ahora ejecuta NEWS-LOCAL/NEWS-02/NEWS-03 en un job focalizado para `main` y otras ramas. No se lanzó una ejecución de GitHub Actions desde esta sesión. No se añadieron backtests históricos a ese job. |
| DRY RUN | PASS | No se importó ni conectó MT5 y no se enviaron órdenes. |

## Ollama

Se comprobó que el servicio local tenía instalado `qwen3:4b`. La única inferencia real usó el fixture sintético `usd_only`, par `EURUSD`, timeout configurado de 240 segundos y el límite existente `num_predict=32768`. El resultado fue `PASS`, proveedor `ollama`, esquema `2.0`, respuesta JSON válida y sin código de error. Solo propuso el driver USD; no inventó un evento EUR. Python calculó ambos escenarios relativos.

No se hicieron inferencias reales adicionales. El adaptador local puede analizar posteriormente un snapshot con `--mode local`, pero esa segunda llamada requiere ejecutarse explícitamente fuera de esta sesión. Los parámetros de `think` no se modificaron.

## Fuentes y procedencia

La captura se realizó el 2026-11-19 a las 19:14:21 UTC con el máximo permitido de cuatro solicitudes. Estados observados:

| Fuente registrada | Estado | HTTP | Eventos de feed | SHA-256 del feed |
|---|---:|---:|---:|---|
| `FED_PRESS_RSS` | FETCH_OK | 200 | 20 | `e06ed627f6fa071162b75adb70e830b2f5c343a52b30aa0e345055023dd3aaf5` |
| `BANK_OF_CANADA_PRESS_RSS` | FETCH_OK | 200 | 1 | `314e84b247a44d4bbb8f22a8ed6c910565a695050a9d8c56e47dda6e434cd4d0` |
| `ECB_PRESS_RSS` | UNAVAILABLE | — | 0 | — |
| `RBA_MEDIA_RSS` | UNAVAILABLE | — | 0 | — |
| `BLS_RELEASE_ICS` | NOT_FETCHED_BY_POLICY | — | 0 | — |

El snapshot normalizado se guardó en `reports/private/news_local/macro_source_snapshot_20261119T191421Z.json`. El análisis NEWS-02 real-fuente y el replay están en la misma carpeta privada. El replay hizo cero peticiones y conservó el corte temporal original. No se publicaron títulos, cuerpos, URLs de artículos ni razonamientos del modelo.

El informe de la recopilación marcó `PARTIAL`: `ECB_PRESS_RSS` y `RBA_MEDIA_RSS` devolvieron `URLError`; no se reintentaron. Los 17 eventos elegibles para adaptación pero antiguos se descartaron por `SOURCE_STALE_AT_CUTOFF`. El evento de Banco de Canadá no autorizó un hecho por falta de fecha de publicación verificable. Una RSS no es un calendario económico completo.

## Ejecución reproducible en PowerShell

Desde la raíz del repositorio:

```powershell
.\.venv\Scripts\python.exe scripts\run_news_research.py --mode offline --fixture usd_only --symbol EURUSD

.\.venv\Scripts\python.exe scripts\run_news_research.py --mode collect --symbol EURUSD --request-limit 4 --output reports/private/news_local/news_research_eurusd.json

.\.venv\Scripts\python.exe scripts\run_news_research.py --mode offline --symbol EURUSD --snapshot reports/private/news_local/macro_source_snapshot_20261119T191421Z.json
```

`collect` obtiene y normaliza fuentes del registro y realiza el análisis determinista aunque Ollama no esté disponible. Los feeds y el snapshot se guardan solo en el directorio privado. Para autorizar una inferencia local sobre un snapshot guardado:

```powershell
$env:NEWS_LOCAL_OLLAMA_TIMEOUT_SECONDS = "240"
.\.venv\Scripts\python.exe scripts\run_news_research.py --mode local --symbol EURUSD --snapshot reports/private/news_local/macro_source_snapshot_20261119T191421Z.json
```

Ese último comando **no se ejecutó** durante NEWS-03. `local` llama a Ollama; `offline` nunca lo hace. No se deben mover snapshots con noticias a informes públicos.

## Verificaciones y límites

- Tests finales: `python -m unittest tests.test_news_local tests.test_news_02 tests.test_news_research -q` → 69 tests, `OK`.
- Compilación focalizada de `src/news_local`, `src/macro_sources/core.py`, ambos ejecutables y sus tests → OK.
- `git diff --check` → OK.
- `reports/private/` está ignorado por Git.
- Los fallos de fuente y de validación no se presentan como `PASS`; la captura real terminó `PARTIAL`.
- No se evaluaron rentabilidad, precisión predictiva ni utilidad económica.
- No se hizo commit, push ni merge.
