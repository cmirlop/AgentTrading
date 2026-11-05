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
    RISK_REFERENCE,
)

from src.mt5_runtime import (
    get_account_state,
    get_positions_state,
    get_orders_state,
)
from src.mt5_deal_history import calculate_realized_net_pnl


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


def _symbol_metadata(symbols, account_currency):
    result = {}
    for symbol in sorted(set(symbols)):
        try:
            info = mt5.symbol_info(symbol)
        except Exception:
            info = None
        if info is None:
            continue
        result[symbol] = {
            "trade_tick_size": getattr(info, "trade_tick_size", None),
            "trade_tick_value_loss": getattr(info, "trade_tick_value_loss", None),
            "trade_tick_value_loss_currency": account_currency,
            "trade_contract_size": getattr(info, "trade_contract_size", None),
            "currency_base": getattr(info, "currency_base", None),
            "currency_profit": getattr(info, "currency_profit", None),
            "volume_min": getattr(info, "volume_min", None),
            "volume_max": getattr(info, "volume_max", None),
            "volume_step": getattr(info, "volume_step", None),
        }
    return result


def _risk_for_existing(item, pending=False):
    try:
        stop = float(item.get("stop_loss") or 0)
    except (TypeError, ValueError):
        return None
    if stop <= 0:
        return None
    symbol = item.get("symbol")
    volume = item.get("volume_current", item.get("volume")) if pending else item.get("volume")
    price = item.get("price_open") if pending else item.get("price_current")
    raw_type = str(item.get("type") or item.get("side") or "").upper()
    side = "BUY" if raw_type.startswith("BUY") else "SELL" if raw_type.startswith("SELL") else None
    if not symbol or not side or not volume or not price:
        return None
    action = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    try:
        value = mt5.order_calc_profit(action, symbol, float(volume), float(price), stop)
    except Exception:
        value = None
    if value is None:
        return None
    # If the stop is already beyond break-even, it does not add downside risk.
    return max(0.0, -float(value))


def _risk_snapshot(account, all_positions, all_orders):
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        deals = mt5.history_deals_get(midnight, now)
    except Exception:
        return {"available": False, "reason": "DAILY_DEAL_HISTORY_UNAVAILABLE"}
    if deals is None:
        return {"available": False, "reason": "DAILY_DEAL_HISTORY_UNAVAILABLE"}
    daily_result = calculate_realized_net_pnl(
        deals,
        mt5,
        window_start_utc=midnight,
        window_end_utc=now,
    )
    if not daily_result.get("available"):
        return {
            "available": False,
            "reason": daily_result.get("reason") or "DAILY_DEAL_DATA_INVALID",
            "daily_window_start_utc": midnight.isoformat(),
            "daily_window_end_utc": now.isoformat(),
        }

    symbols = {item["symbol"] for item in all_positions + all_orders}
    symbols.update(SYMBOLS)
    metadata = _symbol_metadata(symbols, account.get("currency"))
    rates = {}
    for symbol in sorted(symbols):
        if len(symbol) != 6:
            continue
        try:
            tick = mt5.symbol_info_tick(symbol)
        except Exception:
            tick = None
        if tick is None:
            continue
        bid, ask = float(getattr(tick, "bid", 0) or 0), float(getattr(tick, "ask", 0) or 0)
        if bid > 0 and ask >= bid:
            rates[symbol] = (bid + ask) / 2.0

    for item in all_positions:
        item["risk_to_sl_account"] = _risk_for_existing(item, pending=False)
    for item in all_orders:
        item["risk_to_sl_account"] = _risk_for_existing(item, pending=True)
    positions_risk = [item.get("risk_to_sl_account") for item in all_positions]
    pending_risk = [item.get("risk_to_sl_account") for item in all_orders]
    available = (
        set(metadata) >= symbols
        and all(value is not None for value in positions_risk + pending_risk)
        and bool(account.get("currency"))
        and float(account.get("equity") or 0) > 0
    )
    return {
        "available": available,
        "reason": None if available else "EXISTING_POSITION_OR_SYMBOL_RISK_METADATA_UNAVAILABLE",
        "realized_net_pnl": daily_result["realized_net_pnl"],
        "daily_deal_counts": {
            "trading": daily_result["trade_deal_count"],
            "commission": daily_result["commission_deal_count"],
            "excluded_non_trade": daily_result["excluded_non_trade_deal_count"],
            "total": daily_result["history_deal_count"],
        },
        "open_risk_account": round(sum(positions_risk), 8) if all(v is not None for v in positions_risk) else None,
        "pending_risk_account": round(sum(pending_risk), 8) if all(v is not None for v in pending_risk) else None,
        "positions": all_positions,
        "pending_orders": all_orders,
        "symbol_metadata": metadata,
        "conversion_rates": rates,
        "daily_window_start_utc": midnight.isoformat(),
        "daily_window_end_utc": now.isoformat(),
    }


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

    # Preserve the full account view for aggregate risk. Display/runtime
    # portfolio counts remain limited to configured symbols below.
    risk_snapshot = _risk_snapshot(account, positions, orders)

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

        "risk_snapshot":
            risk_snapshot,

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

            "risk_reference":
                RISK_REFERENCE,
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
