# ==========================================================
# TRADE PLANNER
# ==========================================================

# Inicialmente exigimos beneficio potencial
# de al menos 2 veces el riesgo.
# Más adelante lo optimizaremos en backtesting.

MIN_RISK_REWARD = 2.0


# Stop técnico:
# como mínimo 1.2 ATR desde la entrada.

ATR_STOP_MULTIPLIER = 1.20


# Pequeño buffer alrededor de máximos/mínimos.

ENTRY_BUFFER_ATR_MULTIPLIER = 0.10


# Velas M15 utilizadas para swing high / swing low.

SWING_LOOKBACK_BARS = 6


# Buffer mínimo absoluto en puntos.

MIN_BUFFER_POINTS = 2


TARGET_RISK_REWARD = 2.10


# ==========================================================
# FRESHNESS / LIVE DRIFT
# ==========================================================

# Un tick más antiguo que esto no se utiliza
# para diseñar una nueva operación.
MAX_TICK_AGE_SECONDS = 30

# Si desde el MarketState hasta el precio live
# el mercado se ha desplazado demasiado,
# el plan queda invalidado.
MAX_LIVE_DRIFT_ATR = 1.50