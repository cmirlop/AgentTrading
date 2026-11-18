# Fase 2.2.2g — Histórico MT5 desde 2023

**Estado:** COMPLETE_VS_MT5_2023_PLUS

Rango solicitado: `2023-01-01T00:00:00+00:00` hasta el corte UTC inmutable `2026-11-17T20:13:49.261352+00:00`. La fecha de apertura determina la inclusión. Las velas en formación se excluyen; D1 usa la siguiente apertura observada o una gracia conservadora de 26 horas, no un cierre fijo de 86.400 s.

Cuenta REAL, terminal conectado y `tradeapi_disabled=True`: **PASS**. El proceso envió 0 órdenes y ejecutó 0 operaciones. El dataset congelado permanece intacto: **True** (`0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3`).

Totales comprobados: 672,861 filas pre-2023 archivadas en los backups privados; 429,448 filas 2023+ añadidas respecto al estado inicial de esta fase; 1,252,070 filas finales en los 15 CSV operativos.

Fase 2.2.2f: 506,242 filas al inicio y 1,495,483 al cierre previo a esta fase; crecimiento neto verificado 989,241. Ese crecimiento previo no se cuenta como filas nuevas de fase 2.2.2g.

Consultas MT5: 51 en total, con límite de 150 por tanda y sin límite global fijo. Cobertura/filas observadas no certifican sesiones oficiales del bróker. Los intervalos sin confirmar incluyen períodos sin ticks y no se rellenan ni se etiquetan como errores de sesión automáticamente.

| Símbolo | TF | Primera 2023+ UTC | Último cierre UTC | Filas antes (todo) | Filas antes (2023+) | Filas finales (2023+) | Añadidas 2023+ | Pre-2023 archivadas | Cobertura proveedor | Intervalos sin confirmar | Estado | SHA-256 |
|---|---|---|---|---:|---:|---:|---:|---:|---|---:|---|---|
| EURUSD | M5 | 2023-01-02T00:00:00+00:00 | 2026-11-17T20:05:00+00:00 | 148704 | 148704 | 289633 | 140929 | 0 | 2023-01-02T00:00:00+00:00 → 2026-11-17T20:05:00+00:00 (289633 barras) | 223 | AUDIT_WINDOWS_COMPLETE | `cbad63f9f40df0f1a318c01f9ca839b1945fee9fd51d219251fdb24a6aab577a` |
| EURUSD | M15 | 2023-01-02T00:00:00+00:00 | 2026-11-17T19:45:00+00:00 | 148704 | 96376 | 96552 | 176 | 52328 | 2023-01-02T00:00:00+00:00 → 2026-11-17T19:45:00+00:00 (96552 barras) | 210 | AUDIT_WINDOWS_COMPLETE | `13a3999ddb9ad8e781dd132987e1aa5904bb6e58b9b154e395f9fbf0dad21137` |
| EURUSD | H1 | 2023-01-02T00:00:00+00:00 | 2026-11-17T19:00:00+00:00 | 148704 | 24096 | 24140 | 44 | 124608 | 2023-01-02T00:00:00+00:00 → 2026-11-17T19:00:00+00:00 (24140 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `3f9347f1f7622c164111f5dc54397add8a2b59eb3fb977cd2b8e0c8bdd79983e` |
| EURUSD | H4 | 2023-01-02T00:00:00+00:00 | 2026-11-17T16:00:00+00:00 | 50556 | 6024 | 6035 | 11 | 44532 | 2023-01-02T00:00:00+00:00 → 2026-11-17T16:00:00+00:00 (6035 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `70059664589ba0059950c2f128c95bc448933e6f95e463800c02d948feeb880f` |
| EURUSD | D1 | 2023-01-02T00:00:00+00:00 | 2026-11-16T00:00:00+00:00 | 14402 | 1004 | 1005 | 1 | 13398 | 2023-01-02T00:00:00+00:00 → 2026-11-16T00:00:00+00:00 (1005 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `7dea45785f4544081bc41c9920636e2d7255c17b8444cfd359fc9ffa4a523f48` |
| USDCAD | M5 | 2023-01-02T00:05:00+00:00 | 2026-11-17T20:05:00+00:00 | 141718 | 141718 | 289623 | 147905 | 0 | 2023-01-02T00:05:00+00:00 → 2026-11-17T20:05:00+00:00 (289623 barras) | 229 | AUDIT_WINDOWS_COMPLETE | `6183abe4d8824bc2a8b422f29ea1f48c87f0d9dcfe045c7cd54476061a5dd90f` |
| USDCAD | M15 | 2023-01-02T00:00:00+00:00 | 2026-11-17T19:45:00+00:00 | 142716 | 96375 | 96551 | 176 | 46341 | 2023-01-02T00:00:00+00:00 → 2026-11-17T19:45:00+00:00 (96551 barras) | 211 | AUDIT_WINDOWS_COMPLETE | `afa841b20435beacf894d7fe995a43479ddbb3edda685bb03f482f9375dec726` |
| USDCAD | H1 | 2023-01-02T00:00:00+00:00 | 2026-11-17T19:00:00+00:00 | 142716 | 24096 | 24140 | 44 | 118620 | 2023-01-02T00:00:00+00:00 → 2026-11-17T19:00:00+00:00 (24140 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `ad0a5fe2a6a54bb534d0892bf822e448547c079f7c317f0b5247844300faaf8c` |
| USDCAD | H4 | 2023-01-02T00:00:00+00:00 | 2026-11-17T16:00:00+00:00 | 44858 | 6024 | 6035 | 11 | 38834 | 2023-01-02T00:00:00+00:00 → 2026-11-17T16:00:00+00:00 (6035 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `d82bbbc90187b305187012647a6563b0c640208b2bfd1b1bf2791589fc2133f6` |
| USDCAD | D1 | 2023-01-02T00:00:00+00:00 | 2026-11-16T00:00:00+00:00 | 8719 | 1004 | 1005 | 1 | 7715 | 2023-01-02T00:00:00+00:00 → 2026-11-16T00:00:00+00:00 (1005 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `c0b8090fddfa504cacae158013883da89733a031ff5ca9f0a073e387fa226e53` |
| AUDUSD | M5 | 2023-01-02T00:05:00+00:00 | 2026-11-17T20:05:00+00:00 | 149702 | 149702 | 289620 | 139918 | 0 | 2023-01-02T00:05:00+00:00 → 2026-11-17T20:05:00+00:00 (289620 barras) | 230 | AUDIT_WINDOWS_COMPLETE | `3585aeac0c60941e47bb51b78b984c68b798c92e397afad57903d415ca6ec267` |
| AUDUSD | M15 | 2023-01-02T00:00:00+00:00 | 2026-11-17T19:45:00+00:00 | 149702 | 96375 | 96551 | 176 | 53327 | 2023-01-02T00:00:00+00:00 → 2026-11-17T19:45:00+00:00 (96551 barras) | 210 | AUDIT_WINDOWS_COMPLETE | `02084214d28d72f1a23b588f677e58e8d3e3c5e0c73a850cb7ce9ecc6eba5fd3` |
| AUDUSD | H1 | 2023-01-02T00:00:00+00:00 | 2026-11-17T19:00:00+00:00 | 150700 | 24096 | 24140 | 44 | 126604 | 2023-01-02T00:00:00+00:00 → 2026-11-17T19:00:00+00:00 (24140 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `365970e1e1e6a654ed9b67a697e3450181f199908e3811a6a75474519f6d2681` |
| AUDUSD | H4 | 2023-01-02T00:00:00+00:00 | 2026-11-17T16:00:00+00:00 | 44863 | 6024 | 6035 | 11 | 38839 | 2023-01-02T00:00:00+00:00 → 2026-11-17T16:00:00+00:00 (6035 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `681a41e11b3caf789236693013f6817f076506aa8065a9a5f6eda069de3b1508` |
| AUDUSD | D1 | 2023-01-02T00:00:00+00:00 | 2026-11-16T00:00:00+00:00 | 8719 | 1004 | 1005 | 1 | 7715 | 2023-01-02T00:00:00+00:00 → 2026-11-16T00:00:00+00:00 (1005 barras) | 206 | AUDIT_WINDOWS_COMPLETE | `8966b62d950365c7a94954b3ae3279a6a67d6c7de0247115b83868d5516afdee` |

Idempotencia segundo pase: `True`; nuevas filas en ese pase: `0`.
Evidencia privada (backups originales, manifiesto, ventanas y chunks): `reports/private/phase2_2_2g/20261117T201330Z`.

## Comandos para retomar o verificar

```powershell
python scripts/sync_mt5_history_2023_readonly.py --audit --run-id <nuevo-id>
python scripts/sync_mt5_history_2023_readonly.py --resume --run-id <mismo-id>
python scripts/sync_mt5_history_2023_readonly.py --apply --run-id <mismo-id>
python scripts/sync_mt5_history_2023_readonly.py --verify --run-id <mismo-id>
```

`--resume` conserva el `as_of_utc` del manifiesto. Para un corte posterior, iniciar un run nuevo. No ejecutar ni modificar símbolos, órdenes, posiciones, estrategia o terminal.
