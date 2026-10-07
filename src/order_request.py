from datetime import (
    datetime,
    timedelta,
    timezone,
)

import MetaTrader5 as mt5

from config.execution_config import (
    MAGIC_NUMBER,
    ORDER_COMMENT,
    PENDING_ORDER_EXPIRATION_MINUTES,
)


# ==========================================================
# ORDER TYPE
# ==========================================================

ORDER_TYPE_MAP = {
    "BUY_LIMIT":
        mt5.ORDER_TYPE_BUY_LIMIT,

    "SELL_LIMIT":
        mt5.ORDER_TYPE_SELL_LIMIT,

    "BUY_STOP":
        mt5.ORDER_TYPE_BUY_STOP,

    "SELL_STOP":
        mt5.ORDER_TYPE_SELL_STOP,
}


# ==========================================================
# BUILD REQUEST
# ==========================================================

def build_mt5_request(
    trade_plan,
):

    if (
        trade_plan.get("status")
        != "VALID"
    ):

        raise ValueError(
            "Solo se puede construir request "
            "para un TradePlan VALID"
        )

    order_type_name = (
        trade_plan[
            "order_type"
        ]
    )

    if (
        order_type_name
        not in ORDER_TYPE_MAP
    ):

        raise ValueError(
            "Order type no soportado: "
            f"{order_type_name}"
        )

    expiration = (
        datetime.now(
            timezone.utc
        )
        +
        timedelta(
            minutes=
                PENDING_ORDER_EXPIRATION_MINUTES
        )
    )

    # MT5 necesita timestamp Unix
    expiration_timestamp = int(
        expiration.timestamp()
    )

    request = {
        "action":
            mt5.TRADE_ACTION_PENDING,

        "symbol":
            trade_plan[
                "symbol"
            ],

        "volume":
            float(
                trade_plan[
                    "volume"
                ]
            ),

        "type":
            ORDER_TYPE_MAP[
                order_type_name
            ],

        "price":
            float(
                trade_plan[
                    "entry"
                ]
            ),

        "sl":
            float(
                trade_plan[
                    "stop_loss"
                ]
            ),

        "tp":
            float(
                trade_plan[
                    "take_profit"
                ]
            ),

        "magic":
            MAGIC_NUMBER,

        "comment":
            ORDER_COMMENT,

        "type_time":
            mt5.ORDER_TIME_SPECIFIED,

        "expiration":
            expiration_timestamp,

        # Para pending orders utilizaremos RETURN.
        "type_filling":
            mt5.ORDER_FILLING_RETURN,
    }

    return (
        request,
        expiration,
    )