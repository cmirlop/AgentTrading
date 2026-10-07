# ==========================================================
# CONFIGURACION DE RIESGO
# ==========================================================

LOT_SIZE = 0.01

MAX_LOSS_PER_TRADE_EUR = 5.0

MAX_OPEN_POSITIONS = 2

REQUIRE_STOP_LOSS = True

REQUIRE_TAKE_PROFIT = True

BREAK_EVEN_ENABLED = True


# Todavia por definir mediante backtesting
MAX_DAILY_LOSS_EUR = None

MAX_SPREAD_POINTS = None

MIN_RISK_REWARD = None

# ==========================================================
# SPREAD MAXIMO PROVISIONAL
# ==========================================================

MAX_SPREAD_POINTS_BY_SYMBOL = {
    "EURUSD": 20.0,
    "USDCAD": 25.0,
    "AUDUSD": 20.0,
}