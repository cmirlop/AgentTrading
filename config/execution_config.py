# ==========================================================
# IDENTIFICACION DE OPERACIONES DEL SISTEMA
# ==========================================================

MAGIC_NUMBER = 510001

ORDER_COMMENT = "AgentTrading_V1"


# ==========================================================
# ORDENES PENDIENTES
# ==========================================================

# No queremos ordenes olvidadas durante horas.
#
# En V1 una oportunidad M15 expira tras 60 minutos.

PENDING_ORDER_EXPIRATION_MINUTES = 60