# Protocolo preregistrado: TECHNICAL ONLY frente a TECHNICAL + DEEPSEEK

## Pregunta

¿La política técnica más una decisión DeepSeek, aplicada a las mismas oportunidades que la política técnica sola, cambia la expectativa neta por oportunidad bajo el mismo plan de riesgo, costes y restricciones de cartera?

Este protocolo debe fecharse y congelarse antes de recopilar el periodo confirmatorio. No se debe cambiar a la vista de esos resultados.

## Población y unidad de análisis

- Unidad: oportunidad estable definida por símbolo, cierre UTC de decisión y versión/ordinal de oportunidad.
- Se incluyen todas las oportunidades elegibles, decisiones HOLD/WAIT, rechazos, respuestas inválidas, errores, timeouts y ausencia de cobertura del proveedor.
- Todos los instrumentos se asignan por timestamp de decisión, no por la fecha de cierre de la operación.
- La misma oportunidad se conserva aunque una política la descarte.

## Políticas comparadas

- `TECHNICAL_ONLY`: aplica la decisión técnica congelada y el simulador/gestor de cartera acordado.
- `TECHNICAL_PLUS_DEEPSEEK`: usa DeepSeek como filtro de la decisión técnica, sobre el mismo evento y con el mismo plan de entrada/salida. Cualquier regla para resolver desacuerdos debe congelarse antes del periodo confirmatorio.
- `ALWAYS_HOLD`: referencia de abstención con cero operaciones y cero PnL.

Todas las políticas procesan los eventos cronológicamente y aplican los mismos límites de posiciones, una posición activa por símbolo, exposición, riesgo y costes. No se suman operaciones simultáneas incompatibles.

## Métrica primaria

Diferencia emparejada de expectativa neta por oportunidad, después de costes, entre `TECHNICAL_PLUS_DEEPSEEK` y `TECHNICAL_ONLY`. El denominador es la población preregistrada de oportunidades, incluyendo abstenciones como cero y conservando por separado errores, órdenes no ejecutadas, ambigüedades y datos faltantes. La política de tratamiento de outcomes abiertos/ambiguos debe cerrarse antes del experimento.

Escenario primario de coste: `CONSERVATIVE` actual, salvo que antes del periodo de observación se documente otro escenario con origen verificable. Los pisos de spread hipotéticos no se pueden describir como tarifas reales. `BASE`, `CONSERVATIVE`, `STRESS` y las sensibilidades se presentan por separado.

## Métricas secundarias

PnL neto, expectativa en R, drawdown, profit factor, tasa de operación, frecuencia de aceptación/rechazo, cobertura del proveedor, errores/timeouts, operaciones perdedoras evitadas, operaciones ganadoras descartadas, reducción de exposición, y resultados por instrumento y sesión.

## Separación temporal

1. **Desarrollo:** depuración de instrumentación y revisión de calidad; no se declara valor predictivo.
2. **Validación:** pruebas de esquema, integridad temporal, capacidad de emparejamiento y simulador. Se permiten correcciones de ingeniería, no ajuste de estrategia.
3. **Confirmatorio:** intervalo UTC futuro fijado por fecha antes de observar sus resultados. No se cambian prompt, modelo, parámetros, umbrales, filtros, costes ni selección de eventos durante ese intervalo.

Los periodos confirmatorios no se usan para escoger el tamaño de ventana, el modelo, el prompt o reglas de decisión. Cualquier modificación reinicia una nueva versión y requiere otro periodo confirmatorio.

## Estadística e incertidumbre

- Re-muestreo bootstrap por bloques semanales UTC; se remuestrea el mismo bloque temporal conjuntamente para todos los pares para conservar dependencia entre instrumentos.
- Se informa el número de bloques, el tamaño efectivo de muestra y el intervalo de confianza. El número de operaciones por sí solo no determina suficiencia.
- Antes de iniciar el periodo confirmatorio, usar la variabilidad observada solo en desarrollo para fijar una precisión objetivo del intervalo y un horizonte de observación. No detener el experimento al observar un resultado favorable.
- Una sola métrica primaria. Las conclusiones secundarias múltiples se identifican como exploratorias o ajustan multiplicidad mediante Holm.
- Sin muestra confirmatoria válida, o con precisión insuficiente, la conclusión es `INCONCLUSIVE`.
- `PROMISING` requiere límite inferior del intervalo de la expectativa neta conservadora superior a cero, consistencia en todos los folds preregistrados y PnL neto positivo en al menos dos instrumentos. `NOT_PROMISING` requiere intervalo confirmatorio conservador por completo en cero o debajo y expectativa no positiva. Otros casos son `INCONCLUSIVE`.

## Procedencia y seguridad

Los eventos de decisión contienen timestamps UTC, hash de payload/prompt, proveedor/modelo, configuración, código, IDs de evidencia permitidos, estado Pydantic, latencia, tokens disponibles, error y decisión final del sistema. El outcome se añade como evento separado, fechado cuando se conoce. Nunca se completa el histórico con decisiones DeepSeek inventadas o sin procedencia verificable.

La recopilación es DRY RUN. No se envían órdenes, ni demo ni reales. Las llamadas DeepSeek solo se realizarían fuera de la suite offline, bajo una autorización explícita y el presupuesto acordado por el usuario.
