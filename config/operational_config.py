# ==========================================================
# FRESHNESS
# ==========================================================

# El sistema analiza al cierre de M15.
# Nunca usaremos para abrir una operación
# un análisis técnico demasiado antiguo.

MAX_TECHNICAL_AGE_SECONDS = 20 * 60

# Estado de cuenta/posiciones:
MAX_RUNTIME_AGE_SECONDS = 2 * 60

# calendar.json debe haberse reconstruido recientemente.
MAX_CALENDAR_STATE_AGE_SECONDS = 2 * 60


# ==========================================================
# SESIONES
# ==========================================================

REQUIRE_ALLOWED_SESSION = True


# Londres - hora local de Londres
LONDON_OPEN_HOUR = 8
LONDON_OPEN_MINUTE = 0

LONDON_CLOSE_HOUR = 17
LONDON_CLOSE_MINUTE = 0


# Nueva York - hora local de Nueva York
NEW_YORK_OPEN_HOUR = 8
NEW_YORK_OPEN_MINUTE = 0

NEW_YORK_CLOSE_HOUR = 17
NEW_YORK_CLOSE_MINUTE = 0