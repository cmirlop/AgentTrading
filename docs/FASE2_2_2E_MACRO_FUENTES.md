# Fuentes macro gratuitas — Fase 2.2.2e

RSS/Atom informa de publicaciones ya emitidas; no demuestra previsiones disponibles antes de la publicación. ICS representa agenda futura con su hora/zona; no es un calendario de sesiones del bróker.

| Fuente oficial | Catálogo y feed | Divisa | Resultado |
|---|---|---|---|
| BCE | https://www.ecb.europa.eu/home/html/rss.en.html · https://www.ecb.europa.eu/rss/press.html | EUR | UNAVAILABLE |
| Federal Reserve | https://www.federalreserve.gov/feeds/feeds.htm · https://www.federalreserve.gov/feeds/press_all.xml | USD | FETCH_OK HTTP 200 |
| Bank of Canada | https://www.bankofcanada.ca/rss-feeds/ · https://www.bankofcanada.ca/content_type/press-releases/feed/ | CAD | BLOCKED |
| RBA | https://www.rba.gov.au/rss/ · https://www.rba.gov.au/rss/rss-cb-media-releases.xml | AUD | UNAVAILABLE |
| BLS ICS | https://www.bls.gov/schedule/news_release/bls.ics | USD | El cliente HTTP local no hizo GET; el adaptador web previo no pudo parsear text/calendar ni expuso estado HTTP. No hay verificación de respuesta/origen. |

En la pasada local se hicieron 4 solicitudes HTTP a feeds. Contando conservadoramente también cinco consultas externas de URLs oficiales durante el descubrimiento y una consulta al buscador, la fase consumió 10 de 10 solicitudes posibles. No queda presupuesto para otras peticiones. Por origen: máximo una y sin reintentos. TLS, timeout 10 s, máximo 2 MiB, redirecciones rechazadas, caché ETag/Last-Modified y User-Agent identificable. El catálogo del BCE indica que los lectores consultan periódicamente; no determina una cuota numérica. Las licencias, reglas robots y límites numéricos por origen quedan NOT_VERIFIED porque no aparecieron especificados en los catálogos revisados. No se infiere permiso ni cuota.

Solo el feed Fed confirmó HTTP 200 y produjo 20 eventos. ECB y RBA acabaron en URLError sin estado HTTP registrado. Bank of Canada devolvió un cuerpo que el primer parser rechazó como raíz XML no válida; ese pase no guardó el estado HTTP ni el cuerpo, y no se repitió. El parser ahora acepta RSS 1.0, pero el resultado anterior sigue BLOCKED y no se presenta como PASS.

403/429 no se reintentan. Feed vacío es PARTIAL y no se toma como ausencia confirmada de noticias. No se guardan titulares ni contenido de artículos en informes públicos. El caché bruto vive bajo reports/private/phase2_2_2e/macro_cache/. Solo un ICS con disponibilidad previa, hora exacta e importancia de investigación conocida aparece en el preview inactivo. Ningún macro feed modifica data/state/calendar.json o las sesiones del bróker.

Evidencia: reports/phase2_2_2e/macro_source_audit.json, calendar_preview.json y macro_replay_causality.json.
