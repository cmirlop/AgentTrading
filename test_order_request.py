import argparse
import json

import MetaTrader5 as mt5

from test_trade_planner import (
    load_market_state,
)

from src.trade_planner import (
    build_trade_plan,
)

from src.order_request import (
    build_mt5_request,
)

from pathlib import Path

from src.calendar_reader import (
    load_calendar_csv,
)


def json_safe_request(
    request,
):

    result = {}

    for key, value in (
        request.items()
    ):

        if isinstance(
            value,
            (str, int, float, bool)
        ):

            result[key] = value

        else:

            result[key] = str(
                value
            )

    return result


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
        "MT5 ORDER REQUEST - "
        "SAFE PREFLIGHT"
    )
    print("=" * 80)

    if not mt5.initialize():

        raise RuntimeError(
            f"MT5 error: "
            f"{mt5.last_error()}"
        )

    try:

        terminal = (
            mt5.terminal_info()
        )


        calendar_path = (
            Path(
                terminal.commondata_path
            )
            / "Files"
            / "AgentTrading"
            / "economic_calendar.csv"
        )

        calendar_df = (
            load_calendar_csv(
                calendar_path
            )
        )

        latest_calendar_row = (
            calendar_df.loc[
                calendar_df[
                    "generated_utc"
                ].idxmax()
            ]
        )

        server_utc_offset_seconds = int(
            latest_calendar_row[
                "server_utc_offset_seconds"
            ]
        )

        print(
            "Broker UTC offset:",
            server_utc_offset_seconds,
            "segundos",
        )

        print()
        print(
            "Python trading deshabilitado:",
            terminal.tradeapi_disabled,
        )

        state = load_market_state(
            args.symbol
        )

        # ==================================================
        # TRADE PLAN
        # ==================================================

        plan = build_trade_plan(
            symbol=args.symbol,
            direction=args.direction,
            strategy=args.strategy,
            market_state=state,
            server_utc_offset_seconds=
                server_utc_offset_seconds,
        )

        print()
        print("--- TRADE PLAN ---")

        print(
            json.dumps(
                plan,
                indent=2,
                ensure_ascii=False,
            )
        )

        if (
            plan.get("status")
            != "VALID"
        ):

            print()
            print(
                "[REJECTED] "
                "No se construye request."
            )

            return

        # ==================================================
        # REQUEST
        # ==================================================

        request, expiration = (
            build_mt5_request(
                plan
            )
        )

        print()
        print("--- MT5 REQUEST ---")

        print(
            json.dumps(
                json_safe_request(
                    request
                ),
                indent=2,
                ensure_ascii=False,
            )
        )

        print(
            "Expiration UTC:",
            expiration.isoformat(),
        )

        # ==================================================
        # ORDER CHECK
        # ==================================================

        print()
        print("--- ORDER CHECK ---")

        result = mt5.order_check(
            request
        )

        if result is None:

            print(
                "order_check() -> None"
            )

            print(
                "MT5 last_error:",
                mt5.last_error(),
            )

            return

        print(
            "retcode:",
            result.retcode,
        )

        print(
            "comment:",
            result.comment,
        )

        print(
            "balance:",
            result.balance,
        )

        print(
            "equity:",
            result.equity,
        )

        print(
            "margin:",
            result.margin,
        )

        print(
            "margin_free:",
            result.margin_free,
        )

        # ==================================================
        # INTERPRETACION
        # ==================================================

        print()
        print("--- RESULTADO ---")

        if result.retcode == 0:

            print(
                "[OK] MT5 considera "
                "la solicitud valida."
            )

            print(
                "NO se ha enviado ninguna orden."
            )

        elif result.retcode == 10027:

            print(
                "[SAFE BLOCK] "
                "MT5 ha bloqueado order_check "
                "porque el trading automatico "
                "esta deshabilitado."
            )

            print(
                "Esto es ESPERADO durante "
                "la fase actual."
            )

        else:

            print(
                "[CHECK FAILED] "
                "MT5 rechazo la solicitud."
            )

            print(
                "Investigar retcode antes "
                "de avanzar."
            )

        print()
        print(
            "IMPORTANTE: "
            "este script NO contiene "
            "mt5.order_send()."
        )

    finally:

        mt5.shutdown()


if __name__ == "__main__":
    main()