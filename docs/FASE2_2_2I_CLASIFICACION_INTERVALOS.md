# Fase 2.2.2i — clasificación de intervalos de cadencia

## Estado

**INVENTORY_AND_CLASSIFICATION_COMPLETE.** Se reconstruyeron y etiquetaron los 3.167 intervalos observados. **OFFICIAL_BROKER_SESSION_COVERAGE_COMPLETE: UNKNOWN** y **ALL_GAPS_VERIFIED: false**: no se identificó la entidad jurídica del bróker ni se encontró un calendario histórico oficial aplicable a la cuenta y a los tres pares. Ningún intervalo recibió una etiqueta `VERIFIED_*`.

Commit base del repositorio: `9a3a04c3923749284cf52edd732bf0bec2e20dfb` (merge de Fase 2.2.2h, PR #19). Rama: `codex/phase2-2-2i-gap-classification`. El SHA de commit final se publicará en el PR después de crear el commit. Snapshot congelado: `2026-11-17T20:13:49.261352+00:00`; `base_sha` del informe H: `af79c8455cf2d9754759629a39f4e4a2f41e78d4`.

El auditor volvió a leer los 15 CSV, verificó sus hashes contra el manifiesto y volvió a calcular los huecos: integridad `PASS`, 1.252.070 filas y **3.167 discontinuidades**, igual al informe H. Los 15 hashes raw y canónicos están en `reports/phase2_2_2i/integrity_recheck.json`. El SHA-256 del manifiesto fuente es `4edb381ab3cffc83bf407186555ab2f972a379a708f9a856e07a016dffb13157`. El `technical_candidate_scan.csv` conserva el hash histórico `0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`.

## Recuento

| Resultado por intervalo | Recuento | Interpretación |
|---|---:|---|
| `SUPPORTED_WEEKEND_PATTERN_BROKER_UNVERIFIED` | 2.969 | Patrón de timestamp compatible con el fin de semana; no demuestra el horario del bróker. |
| `SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED` | 130 | El intervalo cruza fechas de calendarios públicos fechados; no demuestra cierre ni falta de cotización. |
| `UNRESOLVED_OPEN_SESSION_GAP` | 68 | Hueco intradía o de día laborable sin evidencia suficiente para separar sesión, feed o disponibilidad de cotizaciones. |
| `VERIFIED_*` | 0 | No hay evidencia oficial del proveedor aplicable. |
| `MIXED_PARTIAL_EVIDENCE` | 0 | No hay cobertura de calendario del proveedor que permita afirmar qué porción está verificada. |
| **Total** | **3.167** | La suma coincide exactamente con el inventario reconstruido. |

Los 3.167 registros se agrupan en **247 episodios candidatos**. Los episodios son una herramienta descriptiva para revisar una sola ventana temporal; no prueban una causa común. Hay 198 episodios con algún registro de patrón semanal, 14 con contexto de festivo y 36 episodios con gaps sin resolver. Algunos episodios contienen más de una etiqueta por diferencias de borde, de modo que esos recuentos de episodios no son sumables.

### Por símbolo

| Símbolo | Festivo contextual | Patrón semanal | Sin resolver | Total |
|---|---:|---:|---:|---:|
| EURUSD | 43 | 990 | 18 | 1.051 |
| USDCAD | 45 | 989 | 24 | 1.058 |
| AUDUSD | 42 | 990 | 26 | 1.058 |
| **Total** | **130** | **2.969** | **68** | **3.167** |

### Por timeframe

| Timeframe | Festivo contextual | Patrón semanal | Sin resolver | Total |
|---|---:|---:|---:|---:|
| M5 | 34 | 593 | 55 | 682 |
| M15 | 24 | 594 | 13 | 631 |
| H1 | 24 | 594 | 0 | 618 |
| H4 | 24 | 594 | 0 | 618 |
| D1 | 24 | 594 | 0 | 618 |
| **Total** | **130** | **2.969** | **68** | **3.167** |

### Por año UTC del inicio del intervalo

| Año | Festivo contextual | Patrón semanal | Sin resolver | Total |
|---|---:|---:|---:|---:|
| 2023 | 33 | 749 | 43 | 825 |
| 2024 | 18 | 780 | 16 | 814 |
| 2025 | 30 | 780 | 0 | 810 |
| 2026 | 49 | 660 | 9 | 718 |
| **Total** | **130** | **2.969** | **68** | **3.167** |

Los recuentos mensuales, por tipo observacional, motivo, procedencia y confianza se encuentran en `reports/phase2_2_2i/classification_summary.json`.

## Método y límites de la evidencia

La extracción compara aperturas UTC adyacentes con la cadencia nominal M5/M15/H1/H4/D1. `elapsed_seconds` es el tiempo entre las aperturas observadas; `gap_seconds` es el tiempo que excede una cadencia nominal. Un salto de cadence no se llama automáticamente “vela faltante”. Los IDs estables incluyen el símbolo, timeframe, extremos observados y hash canónico de su CSV.

La clase observacional se guarda aparte de la interpretación y del estado de prueba del proveedor. Para los patrones semanales se exigió apertura previa viernes, reanudación domingo/lunes y una distancia entre 36 y 96 horas. La fecha pública se aplica como contexto en UTC, sin conocer el huso del servidor. El intervalo completo permanece en `remaining_unknown_portion`, también cuando una etiqueta `SUPPORTED_*` describe un patrón reconocible.

Los festivos usados son un extracto local fijo y con hash dentro de `source_evidence_registry.json`: calendarios federales de EE. UU. 2023–2026, Inglaterra y Gales 2023–2026, el calendario CRA canadiense de 2026 y fechas de estado/territorio australianas de 2026. La propia CRA indica que algunas fechas canadienses dependen de provincia o territorio; Fair Work indica que las fechas australianas dependen de estado o territorio. Un festivo civil solo ofrece contexto. No demuestra que FX o el bróker dejaran de cotizar.

Fuentes públicas consultadas:

- [BIS, Implementation of the FX Global Code](https://www.bis.org/speeches/20171004-implementation-fx-global-code): contexto de mercado OTC y actividad a través de centros; no aporta cierre semanal específico para esta cuenta.
- [U.S. OPM, Federal Holidays](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/): listas fechadas de festivos federales; no son un horario FX.
- [GOV.UK, UK bank holidays](https://www.gov.uk/bank-holidays): fechas históricas de Inglaterra y Gales, entre otros territorios; no acredita disponibilidad de cotizaciones.
- [Canada Revenue Agency, Public holidays](https://www.canada.ca/en/revenue-agency/services/tax/public-holidays.html): calendario publicado de 2026; no cubre por igual cada provincia.
- [Australian Fair Work Ombudsman, 2026 public holidays](https://www.fairwork.gov.au/employment-conditions/public-holidays/2026-public-holidays): calendario por estado/territorio de 2026, no horario de bróker.
- [NIST, Daylight Saving Time Rules](https://www.nist.gov/pml/time-and-frequency-division/popular-links/daylight-saving-time-dst): reglas civiles de EE. UU.; no identifica el huso del servidor.
- [EUR-Lex, calendario de horario de verano 2022–2026](https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX%3A52021XC0427%2801%29): fechas oficiales de transición de la UE; no identifica el huso del servidor.

No se encontró prueba pública y fechada que conecte una entidad legal concreta con la cuenta. `config/broker_calendar_trusted_sources.json` permanece sin cambios y con `trusted_source_hosts=[]`. No se creó ni propuso un manifiesto de calendario porque faltan la entidad y reglas oficiales de horario por instrumento, huso, timeframe y vigencia 2023–2026.

## Casos de revisión

### Huecos sin resolver

La tabla siguiente contiene los 36 episodios con 68 registros `UNRESOLVED_OPEN_SESSION_GAP`. Los tiempos son el rango observado del episodio; la relación exacta entre cada hueco, fila fuente y hash se obtiene mediante los `gap_ids` de `gap_episode_groups.json` y el inventario completo. No se halló una fuente pública que explique un fallo de feed o pruebe ausencia de cotizaciones en estos instantes.

| Episode ID | Intervalo UTC observado | Símbolos | TF | Registros | gap_id |
|---|---|---|---|---:|---|
| episode_7e353cb9c313b13288a5 | 2023-01-18T00:10:00+00:00 → 2023-01-18T00:20:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_b964d407e086522e92a13835<br>gap_bca08ed305481cb302c9d09b<br>gap_bd7090ab46c63437f6f2aa1b |
| episode_e84e4c2b328760f331fc | 2023-03-15T00:45:00+00:00 → 2023-03-15T00:50:00+00:00 | USDCAD | M5 | 1 | gap_e142aa4f2f38bc4dc0b84260 |
| episode_74197ec214a934210068 | 2023-03-15T13:20:00+00:00 → 2023-03-15T13:25:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_1a1539d12d4141715ee57f3a<br>gap_67347ab89665b3a93d1ca0f1<br>gap_cbdcecd911aad8326f1ccdf3 |
| episode_e2016dca8ce7627dab51 | 2023-04-03T01:00:00+00:00 → 2023-04-03T01:15:00+00:00 | AUDUSD, EURUSD, USDCAD | M5, M15 | 6 | gap_1440a1f757e562732a447a52<br>gap_4bf4eb69e75a969829dd2ee6<br>gap_7c77ce4372009c83f5482be0<br>gap_b101ebb7dfbc9eb303bc7ccf<br>gap_e35993bf0deae4331be19357<br>gap_fde3ede7ff833d7d2dd92397 |
| episode_88063fcd86c94f86c07d | 2023-04-06T00:05:00+00:00 → 2023-04-06T00:10:00+00:00 | USDCAD | M5 | 1 | gap_0ec3a3a2ea8a18682b2357e5 |
| episode_807e0595704cffa6afdc | 2023-04-25T00:05:00+00:00 → 2023-04-25T01:00:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_a54ff64f78470a8e16341f7d<br>gap_a65778181615c8bbe2d404b2<br>gap_e749a4966ffb099e7eacf34c |
| episode_0e2ce411dd178036e721 | 2023-04-25T00:15:00+00:00 → 2023-04-25T01:00:00+00:00 | AUDUSD, EURUSD, USDCAD | M15 | 3 | gap_25f81c537f816d75c75f28c9<br>gap_27dcfde22cc16ee3a6102807<br>gap_a2c4336e556c292d3e68101d |
| episode_7d5d05309d1376220776 | 2023-05-22T00:35:00+00:00 → 2023-05-22T00:40:00+00:00 | USDCAD | M5 | 1 | gap_0be3d5f1585966a8d2ac19d7 |
| episode_b6d8b3d748f2535785ce | 2023-05-26T00:40:00+00:00 → 2023-05-26T00:45:00+00:00 | AUDUSD | M5 | 1 | gap_cd04c28e34e8e4b66926d6de |
| episode_9ba56e69c7d3ac306f13 | 2023-05-26T13:15:00+00:00 → 2023-05-26T13:20:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_055262d8aa35b29cfb9ab67d<br>gap_bde845ff7b3cf1a12022a44b<br>gap_c0ea96e0f348809357686d3b |
| episode_58ef19cb01e1f947b92a | 2023-06-08T00:45:00+00:00 → 2023-06-08T00:50:00+00:00 | EURUSD | M5 | 1 | gap_0689e5d0d270e3da3308fd6c |
| episode_27386ca0cb41fb9696d4 | 2023-07-03T00:35:00+00:00 → 2023-07-03T00:40:00+00:00 | EURUSD | M5 | 1 | gap_51488950f98550713d6cc659 |
| episode_0ef8bbc74eb08b5bc438 | 2023-08-15T08:05:00+00:00 → 2023-08-15T08:40:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_05ef7765eb20326440eee9d2<br>gap_b092e181430beeec98c2ffbc<br>gap_da3094879f13b42a69a8c1e4 |
| episode_0e62940e16fbeafbc88b | 2023-08-15T08:15:00+00:00 → 2023-08-15T08:30:00+00:00 | AUDUSD, EURUSD, USDCAD | M15 | 3 | gap_7fabd78a63efc7d2b9262b49<br>gap_a96a7c8a00687331dcad5ff3<br>gap_f1189c52e83cb36c39780dca |
| episode_2e9481a68e60087e68c6 | 2023-08-24T20:05:00+00:00 → 2023-08-24T21:00:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_03e5bc7291e5501a99ade474<br>gap_54187642357adf60a3ddeecd<br>gap_da5b679383fe698acb5b2838 |
| episode_1471bf7b169c35239559 | 2023-08-24T20:15:00+00:00 → 2023-08-24T21:00:00+00:00 | AUDUSD, EURUSD, USDCAD | M15 | 3 | gap_35f0970f11b307e373d96714<br>gap_4eb5c7050b5a3535689c888b<br>gap_9b0ac0ca72c195948c2f6c6b |
| episode_41dc1e21fa887e669a85 | 2023-08-31T00:05:00+00:00 → 2023-08-31T00:10:00+00:00 | USDCAD | M5 | 1 | gap_f726652a6a47d50476cc6bdc |
| episode_b58e4d2d4a0d7eabaa74 | 2023-09-25T06:15:00+00:00 → 2023-09-25T06:20:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_78cbcd3b6d1b911d7047a912<br>gap_7f5f5df2b42c30e223da78ad<br>gap_9081571e2c0e1a7c7f3c5133 |
| episode_71cb15037bdd67f08fb1 | 2024-05-13T00:15:00+00:00 → 2024-05-13T00:20:00+00:00 | AUDUSD | M5 | 1 | gap_7fa3de02d0d9f9f1a2c2900c |
| episode_740de7df1bee362f0353 | 2024-07-17T00:50:00+00:00 → 2024-07-17T00:55:00+00:00 | AUDUSD | M5 | 1 | gap_bd33e1a425f8ed766e4cea86 |
| episode_a1806db418fe3b910a6a | 2024-08-16T00:20:00+00:00 → 2024-08-16T00:25:00+00:00 | AUDUSD | M5 | 1 | gap_ce747acfe7c72063a4e81c78 |
| episode_81a6b6832fb49383f69d | 2024-08-21T00:50:00+00:00 → 2024-08-21T00:55:00+00:00 | AUDUSD | M5 | 1 | gap_bbf1154f23f91307f7ebb653 |
| episode_cd28e2ca9be4b90470e7 | 2024-08-23T00:25:00+00:00 → 2024-08-23T00:30:00+00:00 | USDCAD | M5 | 1 | gap_19c448c39d714a18cd1d498a |
| episode_5e6f5fe1d4809741b8ee | 2024-08-29T00:40:00+00:00 → 2024-08-29T00:45:00+00:00 | EURUSD | M5 | 1 | gap_b0ce3f71d55c84b91033418e |
| episode_569af9cb1dfc0a515f79 | 2024-09-06T00:25:00+00:00 → 2024-09-06T00:30:00+00:00 | AUDUSD | M5 | 1 | gap_0d8118634757f7d85c775145 |
| episode_0bb62ea314a3a25eaa4f | 2024-09-17T00:30:00+00:00 → 2024-09-17T00:35:00+00:00 | AUDUSD | M5 | 1 | gap_1cdb54370384cfd529a1300a |
| episode_4f9e50077d6963dc5986 | 2024-10-23T00:20:00+00:00 → 2024-10-23T00:25:00+00:00 | AUDUSD | M5 | 1 | gap_e4572b77a7e2574524301027 |
| episode_f6dd46b936e684868302 | 2024-12-03T00:50:00+00:00 → 2024-12-03T00:55:00+00:00 | AUDUSD | M5 | 1 | gap_7953016684c0aa399fbe121a |
| episode_2e5b7cb254adc86fbe24 | 2024-12-04T00:35:00+00:00 → 2024-12-04T00:40:00+00:00 | USDCAD | M5 | 1 | gap_c783c8f620343cc815365055 |
| episode_650ace1d2e495203da86 | 2024-12-05T00:40:00+00:00 → 2024-12-05T00:45:00+00:00 | USDCAD | M5 | 1 | gap_7bf3e5db5304bbe500df7e49 |
| episode_ce4c79c16f5b914bd504 | 2024-12-23T00:15:00+00:00 → 2024-12-23T00:30:00+00:00 | USDCAD | M5, M15 | 2 | gap_5d83947bc26923044d320f61<br>gap_77bb832364ead6d37965db44 |
| episode_ef916c8f46a745745f1d | 2024-12-24T21:40:00+00:00 → 2024-12-24T21:45:00+00:00 | AUDUSD | M5 | 1 | gap_a4016f9d406d4255da7417e6 |
| episode_e846395df7d805c3b52e | 2024-12-24T23:50:00+00:00 → 2024-12-24T23:55:00+00:00 | AUDUSD | M5 | 1 | gap_d430d729d617bd447d73ef73 |
| episode_97eed99fe5aad335f66d | 2026-04-22T10:00:00+00:00 → 2026-04-22T10:05:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_61875a9071adf369c6b39638<br>gap_addeb3505cbd526aef6a6935<br>gap_cae7e7a7131e76c135d53d40 |
| episode_80f9760a53b0f477620b | 2026-09-24T00:35:00+00:00 → 2026-09-24T00:40:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_4a7f5ddc47e43a28fd8a2ad4<br>gap_da6a5342c0cfe9522f04bd23<br>gap_e6cba5dc8441835c98c18002 |
| episode_6da5444dcd76cbf669f4 | 2026-10-28T00:35:00+00:00 → 2026-10-28T00:50:00+00:00 | AUDUSD, EURUSD, USDCAD | M5 | 3 | gap_098b599008d40fb4ffe374c4<br>gap_3d835786c26001a19ffb5a70<br>gap_b209f040ae56279e247ef8d8 |

### Festivos y eventos de fin de año

Los 130 registros de festivo son candidatos basados en un solapamiento de fechas públicas. Incluyen casos intradía como Año Nuevo observado 2023, Labor Day 2024, Good Friday 2024, Good Friday y Canada Day 2026, así como ventanas de Navidad/Año Nuevo en las que aparecen los tres símbolos y cinco timeframes. La tabla de episodios con fechas y número de registros está en `gap_episode_groups.json`; los días públicos exactos y sus IDs de evidencia se conservan por fila en `gap_inventory.json`.

Las fuentes son calendarios civiles oficiales de EE. UU., Reino Unido, Canadá y Australia citados arriba. Ninguna contiene reglas del bróker, por lo que estos 130 siguen siendo `SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED`.

### Diferencias de borde viernes/reapertura

Se marcaron **5 registros en 4 ventanas** por compartir un borde con la ventana dominante del episodio y diferir en el otro hasta 15 minutos. El intervalo entero sigue sin verificarse; no se rellenó ni recortó:

| Episodio UTC | Gaps que requieren revisar el borde | Hallazgo |
|---|---|---|
| 2023-06-17 a 2023-06-19 00:05 | `gap_867b1fbd11688e5b2114eb15` | Reanudación 5 minutos más tarde que la ventana dominante. |
| 2024-12-25 a 2024-12-26 00:05 | `gap_6c222144113a4a0b3ec12b22` | Reanudación 5 minutos más tarde. |
| 2025-11-08 a 2025-11-10 00:05 | `gap_aeabfc68c576954380fe9641` | Reanudación 5 minutos más tarde. |
| 2026-04-03 23:45 a 2026-04-06 00:00 | `gap_42cc997533086610b275ef2a`, `gap_5ef7bd7a2506a62371477acd` | Dos registros empiezan hasta 15 minutos antes de la ventana dominante. |

Se preservaron esas diferencias como ventanas distintas dentro del episodio. No se asumió que la porción de borde estuviera cerrada por el bróker.

### DST y límites de velas

**241 registros en 17 episodios** caen dentro de dos días de una transición civil de EE. UU. o la UE. Es una etiqueta de proximidad, no una causa. El registro intradía USDCAD M5 del 2024-03-29 03:25–03:30 UTC coincide además con Good Friday y está cerca de la transición europea del 2024-03-31; su causa continúa sin probarse. No se atribuyeron huecos a DST.

No existe evidencia pública del huso de servidor ni de reglas H4/D1 para estos CSV. Por eso `VERIFIED_TIMEFRAME_BOUNDARY_EFFECT=0`, incluso cuando una separación semanal de timeframe alto coincide con una sesión que parece cerrada. No se infirieron ticks ausentes ni se fabricaron velas.

## Pruebas, reproducibilidad e invariantes

`python scripts/validate_phase2_2_2i.py` pasó **32 pruebas**: 12 nuevas de Fase 2.2.2i, 17 pruebas offline de sesiones/DST y 3 regresiones de auditoría/manifiesto. Compilación Python: PASS. El clasificador se reconstruyó dos veces y los hashes de las salidas coincidieron. El informe de validación contiene los comandos, versión del intérprete, estados y hashes.

Reproducción directa:

```bash
python scripts/classify_market_gaps_2023.py
python -m unittest tests.test_phase2_2_2i -v
python scripts/validate_phase2_2_2i.py
```

Invariantes registrados: `MT5_CALLS=0`, `ORDERS=0`, `LLM_PAID_CALLS=0`, `BACKTESTS=0`, `MARKET_CSV_BYTES_CHANGED=0`, `FROZEN_SCAN_CHANGED=0`. La investigación de fuentes fue lectura web pública; la validación offline no hizo llamadas de red.

## Información no sensible necesaria para elevar el nivel de evidencia

Para una futura revisión del calendario, basta con aportar el nombre de la entidad legal y jurisdicción del bróker (sin número de cuenta, servidor privado ni credenciales), además de enlaces a documentos oficiales históricos fechados de horarios, huso de servidor, excepciones y política de velas por instrumento/timeframe. Sin eso, los estados apoyados y no resueltos son el resultado correcto y el validador de sesiones debe continuar cerrado.
