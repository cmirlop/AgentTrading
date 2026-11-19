# NEWS-03 — continuación en Codex remoto

La rama `codex/news-03-local-integration` contiene un ejecutable de investigación acotada y pruebas offline. El pipeline reutiliza `src/macro_sources/core.py` para descargar y normalizar feeds, `src/news_local/macro_adapter.py` para convertirlos al contrato NEWS-02 y `NewsSummarizer` para el resultado estructurado. No está conectado a decisiones ni flujos productivos de `MacroAgent`.

## Qué puede continuar en remoto

- Revisar el diff y completar pruebas de normalización RSS/Atom/ICS con fixtures, incluyendo duplicados, revisiones, agendas y texto hostil.
- Revisar licencias, condiciones de uso y límites publicados para cada fuente antes de ampliar la frecuencia de recopilación.
- Investigar por qué BCE y RBA devolvieron `URLError` desde el equipo local; el snapshot conserva las fuentes fallidas sin inferir que no hubo noticias.
- Mejorar la cobertura de fechas y política de antigüedad sin introducir scraping nuevo ni datos económicos inferidos.
- Ejecutar el job focalizado de GitHub Actions. El workflow cubre la rama principal y las ramas de trabajo; el job no contiene backtests históricos.
- Preparar un contrato de lectura para `MacroAgent` y revisar su integración por separado, manteniendo el laboratorio aislado de sus decisiones productivas.
- Añadir informes agregados o deduplicación más amplia usando fixtures; no publicar titulares ni respuestas completas.

## Qué requiere el equipo local

- Repetir verificaciones de conectividad con los feeds desde Windows, respetando el máximo de solicitudes y sin eludir bloqueos.
- Ejecutar `--mode local` solo cuando se quiera autorizar una inferencia adicional con Ollama. Esta sesión ya consumió su única inferencia live.
- Si se requiere confirmar un cambio del modelo local, verificar en ese equipo `qwen3:4b`, `num_predict=32768` y la configuración de `think` sin alterarlos.

## Comandos remotos seguros

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_news_local tests.test_news_02 tests.test_news_research -q
.\.venv\Scripts\python.exe -m compileall -q src/news_local src/macro_sources scripts/run_news_local_ollama.py scripts/run_news_research.py tests/test_news_local.py tests/test_news_02.py tests/test_news_research.py
.\.venv\Scripts\python.exe scripts\run_news_research.py --mode offline --fixture usd_only --symbol EURUSD
```

La repetición offline no requiere red ni Ollama. Los comandos `collect` y `local` sí consultan respectivamente fuentes HTTP y el Ollama local; usa `collect` con el límite de cuatro orígenes y reserva `local` para una solicitud explícita. Los datos completos de feeds y resultados permanecen en `reports/private/news_local/`.
