# ==========================================================
# DIVISAS RELEVANTES POR INSTRUMENTO
# ==========================================================

SYMBOL_CURRENCIES = {
    "EURUSD": [
        "EUR",
        "USD",
    ],

    "USDCAD": [
        "USD",
        "CAD",
    ],

    "AUDUSD": [
        "AUD",
        "USD",
    ],
}


# ==========================================================
# SEGURIDAD DEL FEED
# ==========================================================

# El bridge MQL5 actualiza cada 60 segundos.
# Si pasan mas de 3 minutos sin actualización,
# consideramos el calendario no fiable.

MAX_CALENDAR_AGE_SECONDS = 180


# ==========================================================
# BLOQUEO DE NOTICIAS HIGH
# ==========================================================

HIGH_IMPACT_BLOCK_BEFORE_MINUTES = 30

HIGH_IMPACT_BLOCK_AFTER_MINUTES = 15


# ==========================================================
# VENTANA DE INFORMACION
# ==========================================================

# Eventos que guardaremos en el estado para
# que otros módulos puedan consultarlos.

EVENT_LOOKAHEAD_HOURS = 24

EVENT_LOOKBACK_HOURS = 2


# No bloqueamos todavía eventos MODERATE,
# pero sí los incluiremos como advertencia.

MODERATE_WARNING_MINUTES = 60

# ==========================================================
# EVENTOS CRITICOS
# ==========================================================
#
# Estos eventos bloquean nuevas operaciones aunque MT5
# los clasifique como MODERATE.
#
# Añadiremos códigos nuevos únicamente cuando aparezcan
# realmente en nuestro feed y los validemos.

CRITICAL_EVENT_CODES = {
    "fomc-minutes",
}


# Usamos la misma ventana conservadora de los eventos HIGH.

CRITICAL_BLOCK_BEFORE_MINUTES = 30
CRITICAL_BLOCK_AFTER_MINUTES = 15