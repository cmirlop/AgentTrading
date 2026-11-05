# ==========================================================
# CONFIGURACION DE RIESGO
# ==========================================================

LOT_SIZE = 0.01

MAX_LOSS_PER_TRADE_EUR = 5.0

MAX_OPEN_POSITIONS = 2

REQUIRE_STOP_LOSS = True

REQUIRE_TAKE_PROFIT = True

# Risk budgets. These defaults are conservative operational limits, not
# strategy-calibrated values. The legacy EUR 5 per-trade cap remains active.
RISK_REFERENCE = "equity"  # "equity" or "balance"
MAX_RISK_PER_TRADE_PERCENT = 0.5
MAX_DAILY_LOSS_PERCENT = 2.0
MAX_PORTFOLIO_RISK_PERCENT = 2.0
MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE = 1.0
DAILY_LOSS_TIMEZONE = "UTC"
DAILY_RISK_STATE_PATH = "data/state/daily_risk_guard.json"

# No position manager currently moves stops to break-even. Keep the advertised
# feature disabled until a simulated manager is implemented and tested.
BREAK_EVEN_ENABLED = False

# Legacy absolute cap. It is converted to account currency using available FX
# metadata; missing conversion rates fail closed.
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
