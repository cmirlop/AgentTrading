from datetime import datetime, timezone

import MetaTrader5 as mt5


# ==========================================================
# UTILIDADES
# ==========================================================

def timestamp_to_iso(timestamp):

    if not timestamp:
        return None

    return datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).isoformat()


def position_type_name(
    position_type,
):

    if (
        position_type
        == mt5.POSITION_TYPE_BUY
    ):
        return "BUY"

    if (
        position_type
        == mt5.POSITION_TYPE_SELL
    ):
        return "SELL"

    return "UNKNOWN"


def order_type_name(
    order_type,
):

    mapping = {
        mt5.ORDER_TYPE_BUY:
            "BUY",

        mt5.ORDER_TYPE_SELL:
            "SELL",

        mt5.ORDER_TYPE_BUY_LIMIT:
            "BUY_LIMIT",

        mt5.ORDER_TYPE_SELL_LIMIT:
            "SELL_LIMIT",

        mt5.ORDER_TYPE_BUY_STOP:
            "BUY_STOP",

        mt5.ORDER_TYPE_SELL_STOP:
            "SELL_STOP",

        mt5.ORDER_TYPE_BUY_STOP_LIMIT:
            "BUY_STOP_LIMIT",

        mt5.ORDER_TYPE_SELL_STOP_LIMIT:
            "SELL_STOP_LIMIT",
    }

    return mapping.get(
        order_type,
        "UNKNOWN",
    )


# ==========================================================
# CUENTA
# ==========================================================

def get_account_state():

    account = (
        mt5.account_info()
    )

    terminal = (
        mt5.terminal_info()
    )

    if account is None:

        raise RuntimeError(
            "No se pudo obtener "
            "account_info()"
        )

    if terminal is None:

        raise RuntimeError(
            "No se pudo obtener "
            "terminal_info()"
        )

    return {
        # No guardamos número/login
        "server":
            account.server,

        "currency":
            account.currency,

        "balance":
            float(account.balance),

        "equity":
            float(account.equity),

        "profit":
            float(account.profit),

        "margin":
            float(account.margin),

        "margin_free":
            float(account.margin_free),

        "margin_level":
            (
                float(
                    account.margin_level
                )
                if account.margin_level
                else None
            ),

        "leverage":
            int(account.leverage),

        "margin_mode":
            int(account.margin_mode),

        "account_mode":
            (
                "HEDGING"
                if account.margin_mode == 2
                else "OTHER"
            ),

        "terminal_connected":
            bool(
                terminal.connected
            ),

        "terminal_trade_allowed":
            bool(
                terminal.trade_allowed
            ),

        "python_trading_disabled":
            bool(
                terminal.tradeapi_disabled
            ),
    }


# ==========================================================
# POSICIONES
# ==========================================================

def get_positions_state():

    positions = (
        mt5.positions_get()
    )

    if positions is None:

        raise RuntimeError(
            "Error positions_get(): "
            f"{mt5.last_error()}"
        )

    result = []

    for position in positions:

        result.append(
            {
                "ticket":
                    int(
                        position.ticket
                    ),

                "symbol":
                    position.symbol,

                "side":
                    position_type_name(
                        position.type
                    ),

                "volume":
                    float(
                        position.volume
                    ),

                "price_open":
                    float(
                        position.price_open
                    ),

                "price_current":
                    float(
                        position.price_current
                    ),

                "stop_loss":
                    float(
                        position.sl
                    ),

                "take_profit":
                    float(
                        position.tp
                    ),

                "profit":
                    float(
                        position.profit
                    ),

                "swap":
                    float(
                        position.swap
                    ),

                "magic":
                    int(
                        position.magic
                    ),

                "comment":
                    position.comment,

                "time_open":
                    timestamp_to_iso(
                        position.time
                    ),

                "has_stop_loss":
                    bool(
                        position.sl > 0
                    ),

                "has_take_profit":
                    bool(
                        position.tp > 0
                    ),
            }
        )

    return result


# ==========================================================
# ORDENES PENDIENTES
# ==========================================================

def get_orders_state():

    orders = (
        mt5.orders_get()
    )

    if orders is None:

        raise RuntimeError(
            "Error orders_get(): "
            f"{mt5.last_error()}"
        )

    result = []

    for order in orders:

        result.append(
            {
                "ticket":
                    int(
                        order.ticket
                    ),

                "symbol":
                    order.symbol,

                "type":
                    order_type_name(
                        order.type
                    ),

                "volume_initial":
                    float(
                        order.volume_initial
                    ),

                "volume_current":
                    float(
                        order.volume_current
                    ),

                "price_open":
                    float(
                        order.price_open
                    ),

                "stop_loss":
                    float(
                        order.sl
                    ),

                "take_profit":
                    float(
                        order.tp
                    ),

                "magic":
                    int(
                        order.magic
                    ),

                "comment":
                    order.comment,

                "time_setup":
                    timestamp_to_iso(
                        order.time_setup
                    ),
            }
        )

    return result