import json
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

from config.market_config import (
    SYMBOLS,
)

from config.risk_config import (
    LOT_SIZE,
    MAX_LOSS_PER_TRADE_EUR,
    MAX_OPEN_POSITIONS,
    REQUIRE_STOP_LOSS,
    REQUIRE_TAKE_PROFIT,
    BREAK_EVEN_ENABLED,
)

from src.mt5_runtime import (
    get_account_state,
    get_positions_state,
    get_orders_state,
)


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

STATE_DIR = (
    BASE_DIR
    / "data"
    / "state"
)


def connect_mt5():

    if not mt5.initialize():

        raise RuntimeError(
            "No se pudo conectar "
            f"con MT5: {mt5.last_error()}"
        )


def build_runtime_state():

    account = (
        get_account_state()
    )

    positions = (
        get_positions_state()
    )

    orders = (
        get_orders_state()
    )

    # ======================================================
    # POSICIONES SOBRE NUESTROS SIMBOLOS
    # ======================================================

    tracked_positions = [
        position

        for position in positions

        if position[
            "symbol"
        ] in SYMBOLS
    ]

    tracked_orders = [
        order

        for order in orders

        if order[
            "symbol"
        ] in SYMBOLS
    ]

    positions_by_symbol = {}

    for symbol in SYMBOLS:

        positions_by_symbol[
            symbol
        ] = [
            position

            for position
            in tracked_positions

            if position[
                "symbol"
            ] == symbol
        ]

    # ======================================================
    # COMPROBACIONES RIESGO
    # ======================================================

    positions_without_sl = [
        position["ticket"]

        for position
        in tracked_positions

        if not position[
            "has_stop_loss"
        ]
    ]

    positions_without_tp = [
        position["ticket"]

        for position
        in tracked_positions

        if not position[
            "has_take_profit"
        ]
    ]

    open_count = len(
        tracked_positions
    )

    available_slots = max(
        0,
        MAX_OPEN_POSITIONS
        - open_count,
    )

    state = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "account":
            account,

        "positions":
            tracked_positions,

        "pending_orders":
            tracked_orders,

        "positions_by_symbol":
            positions_by_symbol,

        "risk_config": {
            "lot_size":
                LOT_SIZE,

            "max_loss_per_trade_eur":
                MAX_LOSS_PER_TRADE_EUR,

            "max_open_positions":
                MAX_OPEN_POSITIONS,

            "require_stop_loss":
                REQUIRE_STOP_LOSS,

            "require_take_profit":
                REQUIRE_TAKE_PROFIT,

            "break_even_enabled":
                BREAK_EVEN_ENABLED,
        },

        "risk_status": {
            "open_positions":
                open_count,

            "available_position_slots":
                available_slots,

            "positions_without_sl":
                positions_without_sl,

            "positions_without_tp":
                positions_without_tp,

            "can_open_more_positions":
                available_slots > 0,
        },
    }

    return state


def save_state(
    state,
):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = (
        STATE_DIR
        / "runtime.json"
    )

    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return path


def print_summary(
    state,
):

    account = state[
        "account"
    ]

    risk = state[
        "risk_status"
    ]

    print("=" * 75)
    print(
        "TRADING AGENT - "
        "RUNTIME STATE"
    )
    print("=" * 75)

    print()
    print("--- CUENTA ---")

    print(
        "Servidor:",
        account["server"],
    )

    print(
        "Moneda:",
        account["currency"],
    )

    print(
        "Balance:",
        account["balance"],
    )

    print(
        "Equity:",
        account["equity"],
    )

    print(
        "Profit:",
        account["profit"],
    )

    print(
        "Margen libre:",
        account["margin_free"],
    )

    print(
        "Modo:",
        account["account_mode"],
    )

    print()
    print("--- SEGURIDAD ---")

    print(
        "MT5 conectado:",
        account[
            "terminal_connected"
        ],
    )

    print(
        "Trading terminal permitido:",
        account[
            "terminal_trade_allowed"
        ],
    )

    print(
        "Python trading deshabilitado:",
        account[
            "python_trading_disabled"
        ],
    )

    print()
    print("--- RIESGO ---")

    print(
        "Posiciones abiertas:",
        risk[
            "open_positions"
        ],
    )

    print(
        "Slots disponibles:",
        risk[
            "available_position_slots"
        ],
    )

    print(
        "Puede abrir mas:",
        risk[
            "can_open_more_positions"
        ],
    )

    print(
        "Posiciones sin SL:",
        risk[
            "positions_without_sl"
        ],
    )

    print(
        "Posiciones sin TP:",
        risk[
            "positions_without_tp"
        ],
    )

    print()
    print("--- POSICIONES ---")

    if not state["positions"]:

        print(
            "No hay posiciones "
            "en simbolos monitorizados."
        )

    else:

        for position in (
            state["positions"]
        ):

            print(
                position[
                    "symbol"
                ],
                position[
                    "side"
                ],
                "lot=",
                position[
                    "volume"
                ],
                "PnL=",
                position[
                    "profit"
                ],
                "SL=",
                position[
                    "stop_loss"
                ],
                "TP=",
                position[
                    "take_profit"
                ],
            )

    print()
    print("--- ORDENES PENDIENTES ---")

    if not state[
        "pending_orders"
    ]:

        print(
            "No hay ordenes pendientes."
        )

    else:

        for order in (
            state[
                "pending_orders"
            ]
        ):

            print(
                order["symbol"],
                order["type"],
                "lot=",
                order[
                    "volume_initial"
                ],
                "precio=",
                order[
                    "price_open"
                ],
            )


def main():

    connect_mt5()

    try:

        state = (
            build_runtime_state()
        )

        path = (
            save_state(
                state
            )
        )

        print_summary(
            state
        )

        print()
        print(
            "Guardado:",
            path,
        )

    finally:

        mt5.shutdown()


if __name__ == "__main__":
    main()