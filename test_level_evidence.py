import pprint

from src.historical_state import (
    prepare_symbol_history,
)

from src.support_resistance import (
    detect_support_resistance,
)

from src.level_evidence import (
    build_level_evidence,
)


SYMBOL = "USDCAD"

TIME = (
    "2026-04-08T18:00:00+00:00"
)


history = (
    prepare_symbol_history(
        SYMBOL
    )
)


levels = (
    detect_support_resistance(
        symbol=
            SYMBOL,

        analysis_time=
            TIME,

        prepared_history=
            history,
    )
)


evidence = (
    build_level_evidence(
        levels
    )
)


pprint.pp(
    evidence
)