# Handoff local — siguiente paso

No hay que sincronizar ni reparar CSV en esta fase. El corte auditado es fijo: `2026-11-17T20:13:49.261352+00:00`. Los 15 CSV y el scan congelado deben conservar sus hashes.

Para desbloquear una evaluación confirmatoria con el usuario ante su ordenador Windows:

1. Obtener y documentar el calendario histórico oficial del bróker para la entidad legal, símbolos EURUSD/USDCAD/AUDUSD y el periodo 2023–2026, con zona horaria, sesiones semanales, festivos/excepciones y fechas de vigencia. El calendario deberá ser revisado e incorporado al allowlist por el procedimiento existente; no deducirlo de los huecos.
2. Obtener documentación verificable de costes históricos (spread por lado/unidad, comisión, swap y slippage) y política de conversión monetaria. No usar las cotizaciones instantáneas actuales como sustituto de costes históricos.
3. Si se necesita ampliar el periodo más allá del corte, revisar el plan de sincronización incremental existente y ejecutar, desde el terminal local, primero su modo `--dry-run`; revisar intervalos y luego iniciar una descarga autorizada de solo lectura. No alterar los 15 CSV publicados: los datos nuevos deben conservar versión, procedencia y hashes propios.
4. Ejecutar de nuevo la auditoría y validar hashes, timestamps, solapamientos y conflictos. Solo después de verificar calendario y costes puede prepararse la evaluación económica pre-registrada.

No se necesitan claves, credenciales, número de cuenta ni balances para estos pasos. Esta rama no conecta con MetaTrader5, no manda órdenes, no llama a DeepSeek/Ollama y no cambia estrategia o riesgo.

Comando remoto reproducible para verificar las fuentes publicadas:

```powershell
python scripts/audit_public_2023_history.py
```

La descarga incremental, si posteriormente se autoriza desde el terminal local, debe realizarse con el script existente de backfill y sus guardas; esta fase no la ejecutó. El backfill real sigue `NOT_RUN`.
