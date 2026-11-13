# Ollama Windows: diagnóstico y recuperación segura

## Resultado local de esta validación

- Ollama 0.40.6 estaba instalado y el proceso que atendía el puerto local coincidía con la identidad de usuario de la sesión.
- El host local efectivo respondió en el puerto 11434; /api/version y /api/tags devolvieron HTTP 200.
- qwen3:4b estaba instalado y coincidía con OLLAMA_MODEL.
- El manifiesto del modelo era legible para la identidad del proceso; la ACL efectiva concedía acceso suficiente. Los logs examinados no mostraron errores actuales HTTP 500 ni acceso denegado.
- Una única validación live local terminó PASS con JSON/Pydantic y evidencia LIVE_PROBE. El proveedor no generó ninguna señal operativa.
- El fallo previo de manifiesto no pudo reproducirse. No se cambiaron ACL, registro, servicio, host persistente ni instalación; no se borraron ni descargaron modelos.

Evidencia pública: [informe live](../reports/phase1_final_local/ollama_live_20261113T1908Z_issue14_pass.json) y [resumen de la batería integral](../reports/phase1_validation.json). El diagnóstico local detallado queda en reports/private/issue14/ollama_windows_diagnostics.json, excluido de Git.

## Guía si reaparece un HTTP 500 o ConnectionError

1. Identifica qué proceso responde en el host/puerto configurado y bajo qué identidad de Windows se ejecuta. Compara esa identidad con el usuario que instaló el modelo.
2. Revisa OLLAMA_HOST y OLLAMA_MODELS en el contexto de ese proceso. No publiques sus valores si contienen rutas privadas; no asumas que el servicio y una terminal interactiva heredan el mismo entorno.
3. Consulta /api/version y /api/tags. Si no hay conexión, corrige primero la discrepancia de host, puerto o contexto del proceso. No cambies la cuenta del terminal MetaTrader.
4. Si el servidor devuelve HTTP 500 con error de lectura del manifiesto, usa la ruta indicada solo para diagnóstico local. Verifica la ACL del directorio preciso y la identidad del proceso. Restaura únicamente los permisos mínimos de lectura/listado que necesita esa identidad sobre los manifiestos; concede escritura solo donde la actualización del modelo la requiera.
5. Si el proceso corre bajo otra identidad, ejecútalo bajo el usuario propietario del modelo o configura explícitamente el directorio correcto para ese contexto. Conserva manifiestos y blobs existentes.
6. Comprueba que el nombre listado sea exactamente qwen3:4b; no sustituyas el modelo configurado. Tras recuperar el acceso, ejecuta una única prueba live de proveedor y revisa que haya JSON válido, Pydantic PASS, evidencia permitida y ninguna señal operativa.

No uses Everyone:F, resets recursivos de ACL, borrado de manifiestos/blobs, reinstalación ni descargas de modelos como primer intento. No registres rutas privadas, tokens ni claves en informes destinados a Git.

## Cobertura de errores

Las pruebas unitarias simulan un HTTP 500 de manifiesto, PermissionError y un host inaccesible. Verifican que la prueba queda PENDING sin exponer el cuerpo de error, la ruta o URL privada, y que el resultado no crea señal ni operación. Una falta real de acceso sigue bloqueando el PASS live; un mock no sustituye la comprobación local.
