import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5

from src.trade_planner import (
    build_trade_plan,
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


def load_market_state(
    symbol,
):

    path = (
        STATE_DIR
        / f"{symbol}.json"
    )

    with open(
        path,
        "r",
        encoding="utf-8",
    ) as file:

        return json.load(
            file
        )


def main():

    parser = (
        argparse.ArgumentParser()
    )

    parser.add_argument(
        "symbol",
        choices=[
            "EURUSD",
            "USDCAD",
            "AUDUSD",
        ],
    )

    parser.add_argument(
        "direction",
        choices=[
            "BUY",
            "SELL",
        ],
    )

    parser.add_argument(
        "strategy",
        choices=[
            "TREND_CONTINUATION",
            "PULLBACK",
            "BREAKOUT",
        ],
    )

    args = parser.parse_args()

    print("=" * 80)
    print(
        "TRADE PLANNER - "
        "SYNTHETIC DRY RUN"
    )
    print("=" * 80)

    print()
    print(
        "ATENCION: esta prueba NO es "
        "una señal de trading."
    )

    print(
        "Solo valida matematicamente "
        "el planificador."
    )

    if not mt5.initialize():

        raise RuntimeError(
            f"MT5 error: "
            f"{mt5.last_error()}"
        )

    try:

        terminal = (
            mt5.terminal_info()
        )

        # Durante estas pruebas quiero que
        # Python siga expresamente bloqueado.

        if not terminal.tradeapi_disabled:

            raise RuntimeError(
                "SEGURIDAD: Python trading "
                "NO esta deshabilitado en MT5. "
                "Activa el bloqueo antes "
                "de continuar."
            )

        state = load_market_state(
            args.symbol
        )

        plan = build_trade_plan(
            symbol=args.symbol,
            direction=args.direction,
            strategy=args.strategy,
            market_state=state,
        )

        print()
        print(
            json.dumps(
                plan,
                indent=2,
                ensure_ascii=False,
            )
        )

    finally:

        mt5.shutdown()


if __name__ == "__main__":
    main()