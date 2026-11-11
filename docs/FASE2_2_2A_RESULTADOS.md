# Fase 2.2.2a — cierre del circuito de recuperación histórica

## Estado

Estado de ingeniería: **`PHASE2_2_2A_COMPLETE`** según el validador offline. La ejecución real de recuperación sigue siendo **`HISTORY_BACKFILL_PREPARED`**; no se descargaron históricos en esta fase.

La base verificada y el HEAD de la rama son `2ffae736c8b5a561aa613d00d30dcef1584e671b` (`origin/main`); los cambios de esta fase siguen sin commit en `codex/phase2-2-2a-history-backfill-hardening`. No se hizo merge.

No se conectó a MetaTrader 5, no se hicieron llamadas a Ollama ni DeepSeek, no se descargaron datos externos, no se ejecutaron backtests ni órdenes.

## Cambios técnicos

- La auditoría acepta un único `--backfill-run`, valida el manifiesto y cada chunk contra el plan guardado, comprueba hashes y filas, detecta archivos ausentes o huérfanos, y preserva variantes con conflicto. Los estados de continuidad son `NOT_AUDITED`, `VERIFIED`, `PARTIAL` y `BLOCKED`; `status=COMPLETED` por sí solo no se considera prueba.
- Los manifiestos y planes nuevos usan esquema 2. Los hashes guardan SHA-256 de bytes y SHA-256 canónico. La política versionada `CSV_LINE_ENDINGS_LF_V1` normaliza exclusivamente CRLF/LF; los cambios de campos o filas alteran el hash canónico. Los formatos antiguos se rechazan expresamente.
- `run-id` queda limitado a caracteres seguros y nombres compatibles con Windows. Se rechazan traversal, nombres reservados y rutas con symlinks; las escrituras se confinan a `data/market_history/backfill_runs/<run-id>/`.
- La descarga sigue requiriendo los dos flags explícitos y conserva sus guardas read-only para cuenta REAL y `tradeapi_disabled=True`. `--dry-run` sigue siendo el modo predeterminado y no importa MT5.
- El ensayo sintético ejercita gap → plan → lectura mock → interrupción → reanudación → hashes → auditoría posterior, junto con respuestas vacías y conflictos.

## Evidencia de validación

El validador registra los comandos, conteos, tiempos, hashes de fuentes y baseline, estados de continuidad sintéticos, seguridad y limitaciones en [`validation_summary.json`](../reports/phase2_2_2a/validation_summary.json). El ensayo completo y la auditoría posterior sintética están en [`synthetic_end_to_end.json`](../reports/phase2_2_2a/synthetic_end_to_end.json) y [`post_backfill_audit.json`](../reports/phase2_2_2a/post_backfill_audit.json). Estos dos informes declaran que sus datos son sintéticos.

La auditoría local de partida, sus 30 hashes de fuente y el diagnóstico de spreads se conservan en [`audits/history_gap_audit.json`](../reports/phase2_2_2a/audits/history_gap_audit.json), [`audits/spread_quality_audit.json`](../reports/phase2_2_2a/audits/spread_quality_audit.json) y [`audits/future_mt5_backfill_plan.json`](../reports/phase2_2_2a/audits/future_mt5_backfill_plan.json). Repiten 77 intervalos candidatos y 1.758 intervalos pendientes de revisión de calendario. Los puentes más largos son M5, 12-oct-2026 06:40 a 4-nov-2026 11:00 UTC (6.677 posiciones por símbolo), y M15, 12-oct-2026 06:30 a 26-oct-2026 12:15 UTC (1.368 por símbolo). Son posiciones de cadencia, no una afirmación de que todas fueran velas negociables.

La paridad LF/CRLF se documenta en [`portability_test.json`](../reports/phase2_2_2a/portability_test.json); la protección de identificadores y symlinks, en [`path_safety_test.json`](../reports/phase2_2_2a/path_safety_test.json). El baseline congelado que se verifica es `data/backtest/technical_candidate_scan.csv`, SHA-256 `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.

Los datos originales no reciben escrituras. Las nuevas descargas se guardarán en un directorio de run nuevo; el auditor los lee como una fuente adicional y mantiene las variantes conflictivas con su procedencia. Los informes anteriores de Fase 2.2.2 no se reemplazan.

La validación ejecutó **35 pruebas: 35 PASS, 0 FAIL y 0 SKIPPED**. El modo dry-run produjo 98 chunks, no importó MT5 y registró cero conexiones, llamadas de proveedor u órdenes. El estado de la auditoría local base es `HISTORY_CONTINUITY_NOT_AUDITED`, porque todavía no existe un run real de backfill que se pueda auditar.

## Lectura de continuidad

Una auditoría base sin `--backfill-run` informa `HISTORY_CONTINUITY_NOT_AUDITED`: solo ha medido los históricos que ya existen. Con un run explícito, la continuidad queda `BLOCKED` si falla la integridad o hay conflicto; queda `PARTIAL` si hay chunks sin respuesta o intervalos/calendario sin verificar; solo queda `VERIFIED` si la evidencia del run y la cobertura de los intervalos evaluados lo permiten. La ausencia de posiciones vacías no demuestra por sí sola que el bróker haya entregado todas las velas negociables.

## Próximo uso en Windows

Desde la raíz del proyecto, con los CSV originales idénticos al plan y frente al terminal REAL en modo read-only, el flujo preparado es:

```powershell
python scripts/backfill_market_history.py --dry-run
python scripts/backfill_market_history.py --download --confirm-real-read-only --run-id backfill-20261109
python scripts/audit_market_history.py --backfill-run backfill-20261109
```

Revisar primero el `dry-run`, usar un `run-id` nuevo, y revisar el informe de auditoría posterior. Esta fase no ejecuta ni autoriza órdenes. La continuidad real no puede declararse antes de descargar y comprobar datos en el terminal del usuario.

## Limitaciones

- `HISTORY_BACKFILL_COMPLETED` no se declara: no hubo descarga real.
- La validación de MT5 REAL read-only y la disponibilidad del terminal todavía deben confirmarse en Windows al momento de la recuperación.
- Calendario de festivos, horarios de apertura/cierre y posibles omisiones del feed pueden dejar intervalos como pendientes aunque no haya gaps aritméticos.
- El cambio a esquema 2 exige generar un plan nuevo; planes/manifiestos de esquema 1 se rechazan para evitar una reanudación sin procedencia portable.
