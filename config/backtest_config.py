BACKTEST_EVALUATION_DAYS = 183

# Días adicionales de historia requeridos antes
# del comienzo del periodo evaluado.
#
# Cada timeframe necesita una cantidad diferente.
# D1 necesita mucho más por EMA200.

BACKTEST_WARMUP_DAYS = {
    "M5": 7,
    "M15": 14,
    "H1": 30,
    "H4": 90,
    "D1": 450,
}

# Cantidad mínima que queremos tener disponible
# antes de confiar en indicadores largos.
MIN_WARMUP_BARS = 250

# Número de timestamps que usaremos para el
# test automático anti-lookahead.
NO_LOOKAHEAD_TEST_SAMPLES = 25


# ==========================================================
# CANDIDATE SCAN
# ==========================================================

QUALITY_THRESHOLDS = [
    50,
    55,
    60,
    65,
    70,
    75,
    80,
    85,
    90,
]

# El umbral que actualmente usa el sistema live.
CURRENT_LIVE_THRESHOLD = 65

# ==========================================================
# STATEFUL BACKTEST
# ==========================================================

STATEFUL_THRESHOLDS = [
    65,
    75,
    85,
    90,
]

STATEFUL_ENTRY_MODES = [
    "LIMIT",
    "STOP",
]

# Si entrada y salida pueden haber ocurrido dentro
# de la misma vela M5 y no conocemos el orden intrabar,
# NO inventamos el resultado.
#
# Se registra como AMBIGUOUS.
AMBIGUOUS_POLICY = "EXCLUDE"

# Historia total que queremos conservar para
# validación histórica ampliada.
BACKTEST_HISTORY_DAYS = 730

# Los últimos 183 días ya han sido utilizados
# para exploración y NO se considerarán
# out-of-sample.
EXPLORATION_DAYS = 183

# ==========================================================
# PERIODOS CONGELADOS
# ==========================================================

HISTORICAL_VALIDATION_START = (
    "2025-06-10T00:00:00+00:00"
)

HISTORICAL_VALIDATION_END = (
    "2026-04-07T18:00:00+00:00"
)

EXPLORATION_START = (
    "2026-04-07T18:15:00+00:00"
)