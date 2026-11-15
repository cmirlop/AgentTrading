# Handoff remoto — Fase 2.2.2e

Base origin/main observada: 6d1f0f7e3e696aa3b773bed997b9d813f1eba7dc; rama codex/phase2-2-2e-local-macro-and-backtest-integration; no se creó commit, push, PR ni merge. Suite offline: PASS. GitHub Actions no se ejecutó localmente. Dataset canónico: 0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3.

## Trabajo remoto priorizado

1. Revisar workflow offline, parser macro y contrato VerifiedResearchDataset; ejecutar CI sin MetaTrader5 ni secretos.
2. Revisar legalidad, robots y cuotas oficiales antes de ampliar endpoints.
3. Mantener scans y backtests reales bloqueados hasta recibir el calendario oficial fechado.

Prompt remoto: “Revisa esta rama contra main. Ejecuta solo offline-research-tests.yml, sin MetaTrader5, secretos ni APIs facturables. Comprueba privacidad, hashes, paridad sintética y diff-check. No cambies estrategia, parámetros ni resultados. No hagas merge con fallos y mantén el backtest económico NOT_RUN hasta validar el documento histórico del bróker.”

MT5 y los chunks son locales, privados y excluidos de Git. Cero órdenes, lecturas de histórico y llamadas LLM en esta fase.

Preguntas a soporte: entidad legal que rige la cuenta; documento histórico con rango de vigencia; zona de servidor, horarios por símbolo, festivos y DST; límites de etiqueta D1/H4/H1; archivo que cubra 2023-07-17 a 2026-11-11.
