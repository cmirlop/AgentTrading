import pprint

from src.historical_state import (
    prepare_symbol_history,
    build_historical_market_state,
)

from src.support_resistance import (
    detect_support_resistance,
)


SYMBOL = "USDCAD"

ANALYSIS_TIME = (
    "2026-04-08T18:00:00+00:00"
)


history = (
    prepare_symbol_history(
        SYMBOL
    )
)


# Esta llamada no es necesaria para el detector,
# pero verifica que el instante histórico existe.
state = (
    build_historical_market_state(
        symbol=
            SYMBOL,

        analysis_time=
            ANALYSIS_TIME,

        prepared_history=
            history,
    )
)


levels = (
    detect_support_resistance(
        symbol=
            SYMBOL,

        analysis_time=
            ANALYSIS_TIME,

        prepared_history=
            history,
    )
)


print(
    "\n"
    + "=" * 80
)

print(
    f"{SYMBOL} | {ANALYSIS_TIME}"
)

print(
    "=" * 80
)

print(
    "Precio:",
    levels.get(
        "current_price"
    ),
)

print(
    "ATR M15:",
    levels.get(
        "reference_atr_m15"
    ),
)


print(
    "\nSOPORTE MAS CERCANO"
)

pprint.pp(
    levels.get(
        "nearest_support"
    )
)


print(
    "\nRESISTENCIA MAS CERCANA"
)

pprint.pp(
    levels.get(
        "nearest_resistance"
    )
)


print(
    "\nZONAS ACTIVAS"
)

pprint.pp(
    levels.get(
        "active_zones"
    )
)


print(
    "\nTODOS LOS SOPORTES"
)

pprint.pp(
    levels.get(
        "supports"
    )
)


print(
    "\nTODAS LAS RESISTENCIAS"
)

pprint.pp(
    levels.get(
        "resistances"
    )
)