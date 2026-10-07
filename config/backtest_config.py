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