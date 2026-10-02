import MetaTrader5 as mt5


def main():

    print("=" * 60)
    print("TRADING AGENT - COMPROBACION MT5")
    print("=" * 60)

    # ----------------------------------------------------------
    # CONEXION
    # ----------------------------------------------------------

    if not mt5.initialize():
        print("ERROR: No se pudo conectar con MetaTrader 5")
        print("MT5 error:", mt5.last_error())
        return

    print("\n[OK] Conexion con MetaTrader 5 establecida")

    # ----------------------------------------------------------
    # VERSION DEL TERMINAL
    # ----------------------------------------------------------

    version = mt5.version()

    print("\n--- VERSION MT5 ---")
    print(version)

    # ----------------------------------------------------------
    # INFORMACION DEL TERMINAL
    # ----------------------------------------------------------

    terminal = mt5.terminal_info()

    if terminal is None:
        print("\nERROR obteniendo informacion del terminal")
    else:
        print("\n--- TERMINAL ---")
        print("Conectado:", terminal.connected)
        print("Trading permitido:", terminal.trade_allowed)
        print("Ruta:", terminal.path)

    # ----------------------------------------------------------
    # INFORMACION DE CUENTA
    # ----------------------------------------------------------

    account = mt5.account_info()

    if account is None:

        print("\nERROR obteniendo informacion de cuenta")
        print(mt5.last_error())

    else:

        print("\n--- CUENTA ---")

        # NO imprimimos login/número de cuenta deliberadamente

        print("Servidor:", account.server)
        print("Moneda:", account.currency)
        print("Balance:", account.balance)
        print("Equity:", account.equity)
        print("Margen:", account.margin)
        print("Margen libre:", account.margin_free)
        print("Apalancamiento:", account.leverage)

        print("Modo de cuenta:", account.margin_mode)

    # ----------------------------------------------------------
    # SIMBOLOS
    # ----------------------------------------------------------

    symbols = [
        "EURUSD",
        "USDCAD",
        "AUDUSD"
    ]

    print("\n--- SIMBOLOS ---")

    for symbol in symbols:

        info = mt5.symbol_info(symbol)

        if info is None:

            print(f"[ERROR] {symbol} no encontrado")
            continue

        if not info.visible:
            mt5.symbol_select(symbol, True)

        tick = mt5.symbol_info_tick(symbol)

        print(f"\n{symbol}")

        print("  Visible:", info.visible)
        print("  Digitos:", info.digits)
        print("  Point:", info.point)
        print("  Volumen minimo:", info.volume_min)
        print("  Volumen maximo:", info.volume_max)
        print("  Paso volumen:", info.volume_step)

        if tick:

            print("  Bid:", tick.bid)
            print("  Ask:", tick.ask)

            spread_points = (
                (tick.ask - tick.bid) / info.point
                if info.point
                else 0
            )

            print("  Spread puntos:", round(spread_points, 2))

    # ----------------------------------------------------------
    # POSICIONES ABIERTAS
    # ----------------------------------------------------------

    positions = mt5.positions_get()

    print("\n--- POSICIONES ABIERTAS ---")

    if positions is None:

        print("Error obteniendo posiciones:")
        print(mt5.last_error())

    elif len(positions) == 0:

        print("No hay posiciones abiertas.")

    else:

        print("Numero de posiciones:", len(positions))

        for p in positions:

            print(
                p.symbol,
                "volumen:",
                p.volume,
                "precio:",
                p.price_open,
                "SL:",
                p.sl,
                "TP:",
                p.tp,
                "beneficio:",
                p.profit
            )

    # ----------------------------------------------------------
    # DESCONEXION
    # ----------------------------------------------------------

    mt5.shutdown()

    print("\n[OK] Conexion cerrada correctamente")
    print("=" * 60)


if __name__ == "__main__":
    main()