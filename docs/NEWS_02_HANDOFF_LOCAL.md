# NEWS-02 — handoff para validación local Windows

Desde PowerShell, en la raíz del repositorio:

```powershell
git status
git fetch origin
git switch codex/news-02-relative-currency-impact
git pull --ff-only

# Validación offline; no contacta Ollama.
.\.venv\Scripts\python.exe -m unittest tests.test_news_local tests.test_news_02 -q
.\.venv\Scripts\python.exe -m compileall -q src/news_local scripts/run_news_local_ollama.py tests/test_news_local.py tests/test_news_02.py
.\.venv\Scripts\python.exe scripts/run_news_local_ollama.py --mode offline --fixture usd_only --symbol EURUSD
.\.venv\Scripts\python.exe scripts/run_news_local_ollama.py --mode mock-ollama --fixture usd_only --symbol EURUSD

# Una sola inferencia local recomendada, solo después de revisar el estado anterior.
$env:NEWS_LOCAL_OLLAMA_TIMEOUT_SECONDS = "1800"
.\.venv\Scripts\python.exe scripts/run_news_local_ollama.py --mode live-ollama --fixture usd_only --symbol EURUSD --output reports/private/news_local/news02_ollama_eurusd.json
```

Si la rama no existe localmente, usar una sola vez
`git switch --track origin/codex/news-02-relative-currency-impact` en lugar de `git switch`.
La última línea hace una solicitud a `/api/tags` y como máximo una inferencia a Ollama
local. No la repitas automáticamente. El fixture es sintético: esta prueba valida el
contrato de salida, no noticias reales ni desempeño comercial.

No se añade `think: false`, no se cambia `qwen3:4b` y no se cambia `num_predict=32768`.
El resultado `PASS` local requiere que la respuesta real de Ollama cumpla el esquema 2.0;
una prueba offline o mock no demuestra esa integración.
