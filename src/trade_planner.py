import MetaTrader5 as mt5

from config.operational_config import (
    MAX_TECHNICAL_AGE_SECONDS,
)

from config.risk_config import (
    LOT_SIZE,
    MAX_LOSS_PER_TRADE_EUR,
    MAX_SPREAD_POINTS_BY_SYMBOL,
)

from config.trade_planner_config import (
    MIN_RISK_REWARD,
    TARGET_RISK_REWARD,
    ATR_STOP_MULTIPLIER,
    ENTRY_BUFFER_ATR_MULTIPLIER,
    SWING_LOOKBACK_BARS,
    MIN_BUFFER_POINTS,
    MAX_TICK_AGE_SECONDS,
    MAX_LIVE_DRIFT_ATR,
)

from datetime import (
    datetime,
    timezone,
    timedelta,
)


# ==========================================================
# UTILIDADES
# ==========================================================

def round_price(
    symbol_info,
    price,
):

    return round(
        float(price),
        int(symbol_info.digits),
    )


def validate_volume(
    symbol_info,
):

    errors = []

    volume = LOT_SIZE

    if volume < symbol_info.volume_min:

        errors.append(
            "VOLUME_BELOW_MINIMUM"
        )

    if volume > symbol_info.volume_max:

        errors.append(
            "VOLUME_ABOVE_MAXIMUM"
        )

    step = float(
        symbol_info.volume_step
    )

    if step > 0:

        normalized = round(
            volume / step
        ) * step

        if abs(
            normalized - volume
        ) > 1e-9:

            errors.append(
                "INVALID_VOLUME_STEP"
            )

    return errors


def normalize_tick_time(
    tick_timestamp,
    now_utc,
    server_utc_offset_seconds,
):
    """
    Algunos terminales/brokers entregan tick.time como UTC.
    Otros pueden entregar un epoch desplazado con la hora
    del servidor.

    Detectamos ambos casos sin asumir uno de ellos.
    """

    raw_time = datetime.fromtimestamp(
        tick_timestamp,
        tz=timezone.utc,
    )

    raw_delta = (
        raw_time
        - now_utc
    ).total_seconds()

    # Caso normal:
    # tick ya expresado correctamente en UTC.
    if abs(raw_delta) <= 300:

        return (
            raw_time,
            "UTC",
        )

    # Caso observado en IC Markets:
    # timestamp desplazado exactamente por el
    # offset horario del servidor.
    if (
        server_utc_offset_seconds
        is not None
    ):

        offset_difference = (
            raw_delta
            - server_utc_offset_seconds
        )

        # Permitimos hasta 15 minutos porque
        # un tick realmente antiguo debe seguir
        # detectándose como antiguo después.
        if abs(
            offset_difference
        ) <= 900:

            normalized = (
                raw_time
                -
                timedelta(
                    seconds=
                        server_utc_offset_seconds
                )
            )

            return (
                normalized,
                "BROKER_SERVER_OFFSET",
            )

    # Si no reconocemos la convención temporal,
    # no maquillamos el dato.
    return (
        raw_time,
        "UNNORMALIZED",
    )


def parse_utc_datetime(
    value,
):

    if not value:
        return None

    value = value.replace(
        "Z",
        "+00:00",
    )

    dt = datetime.fromisoformat(
        value
    )

    if dt.tzinfo is None:

        dt = dt.replace(
            tzinfo=timezone.utc
        )

    return dt.astimezone(
        timezone.utc
    )

# ==========================================================
# PLAN
# ==========================================================

def build_trade_plan(
    symbol,
    direction,
    strategy,
    market_state,
    server_utc_offset_seconds=None,
):

    errors = []

    # ======================================================
    # INFORMACION MT5
    # ======================================================

    info = mt5.symbol_info(
        symbol
    )

    if info is None:

        return {
            "status": "REJECTED",
            "errors": [
                "SYMBOL_INFO_UNAVAILABLE"
            ],
        }

    if not info.visible:

        if not mt5.symbol_select(
            symbol,
            True,
        ):

            return {
                "status": "REJECTED",
                "errors": [
                    "SYMBOL_NOT_SELECTABLE"
                ],
            }

    tick = mt5.symbol_info_tick(
        symbol
    )

    if tick is None:

        return {
            "status": "REJECTED",
            "errors": [
                "TICK_UNAVAILABLE"
            ],
        }

    account = mt5.account_info()



    if account is None:

        return {
            "status": "REJECTED",
            "errors": [
                "ACCOUNT_INFO_UNAVAILABLE"
            ],
        }


    # ======================================================
    # FRESHNESS / CONSISTENCIA DEL MARKET STATE
    # ======================================================

    now_utc = datetime.now(
        timezone.utc
    )

    if (
        market_state.get("symbol")
        != symbol
    ):

        errors.append(
            "MARKET_STATE_SYMBOL_MISMATCH"
        )

    data_quality = (
        market_state.get(
            "data_quality"
        )
    )

    if isinstance(
        data_quality,
        dict,
    ):

        quality_status = (
            data_quality.get(
                "status"
            )
        )

    else:

        quality_status = (
            data_quality
        )

    if quality_status != "OK":

        errors.append(
            "MARKET_DATA_NOT_OK"
        )

    analysis_time = (
        parse_utc_datetime(
            market_state.get(
                "analysis_time"
            )
        )
    )

    state_age_seconds = None

    if analysis_time is None:

        errors.append(
            "MARKET_STATE_TIME_MISSING"
        )

    else:

        state_age_seconds = (
            now_utc
            - analysis_time
        ).total_seconds()

        if state_age_seconds < -60:

            errors.append(
                "MARKET_STATE_FROM_FUTURE"
            )

        elif (
            state_age_seconds
            >
            MAX_TECHNICAL_AGE_SECONDS
        ):

            errors.append(
                "MARKET_STATE_STALE"
            )

    # ======================================================
    # FRESHNESS DEL TICK
    # ======================================================

    tick_timestamp = (
        tick.time_msc / 1000
        if getattr(
            tick,
            "time_msc",
            0,
        )
        else tick.time
    )


    (
        tick_time,
        tick_time_mode,
    ) = normalize_tick_time(
        tick_timestamp=
            tick_timestamp,

        now_utc=
            now_utc,

        server_utc_offset_seconds=
            server_utc_offset_seconds,
    )

    

    tick_age_seconds = (
        now_utc
        - tick_time
    ).total_seconds()

    if tick_age_seconds < -5:

        errors.append(
            "TICK_FROM_FUTURE"
        )

    elif (
        tick_age_seconds
        > MAX_TICK_AGE_SECONDS
    ):

        errors.append(
            "TICK_STALE"
        )

    if errors:

        return {
            "symbol":
                symbol,

            "status":
                "REJECTED",

            "errors":
                errors,

            "diagnostics": {
                "market_state_age_seconds":
                    (
                        round(
                            state_age_seconds,
                            1,
                        )
                        if state_age_seconds
                        is not None
                        else None
                    ),

                "tick_age_seconds":
                    round(
                        tick_age_seconds,
                        1,
                    ),

                "tick_time_mode":
                    tick_time_mode,

                "server_utc_offset_seconds":
                    server_utc_offset_seconds,
            },

            "dry_run":
                True,
        }

    # ======================================================
    # VOLUMEN
    # ======================================================

    errors.extend(
        validate_volume(
            info
        )
    )

    # ======================================================
    # M15
    # ======================================================

    m15 = (
        market_state[
            "timeframes"
        ]["M15"]
    )

    indicators = (
        m15[
            "indicators"
        ]
    )

    candles = (
        m15[
            "recent_candles"
        ]
    )

    recent = candles[
        -SWING_LOOKBACK_BARS:
    ]

    if len(recent) < 3:

        errors.append(
            "NOT_ENOUGH_M15_CANDLES"
        )

    atr = indicators[
        "atr14"
    ]

    ema20 = indicators[
        "ema20"
    ]

    if atr is None or atr <= 0:

        errors.append(
            "ATR_UNAVAILABLE"
        )

    if ema20 is None:

        errors.append(
            "EMA20_UNAVAILABLE"
        )

    if errors:

        return {
            "symbol": symbol,
            "status": "REJECTED",
            "errors": errors,
        }


    # ======================================================
    # DESPLAZAMIENTO DESDE EL ESTADO ANALIZADO
    # ======================================================

    if not candles:

        return {
            "symbol": symbol,
            "status": "REJECTED",
            "errors": [
                "NO_M15_CANDLES"
            ],
            "dry_run": True,
        }

    last_m15_close = float(
        candles[-1]["close"]
    )

    live_mid = (
        float(tick.bid)
        +
        float(tick.ask)
    ) / 2

    live_drift_atr = (
        abs(
            live_mid
            -
            last_m15_close
        )
        / atr
    )

    if (
        live_drift_atr
        >
        MAX_LIVE_DRIFT_ATR
    ):

        errors.append(
            "MARKET_MOVED_TOO_FAR_SINCE_STATE"
        )

    if errors:

        return {
            "symbol":
                symbol,

            "status":
                "REJECTED",

            "errors":
                errors,

            "diagnostics": {
                "last_m15_close":
                    last_m15_close,

                "live_mid":
                    live_mid,

                "m15_atr":
                    atr,

                "live_drift_atr":
                    round(
                        live_drift_atr,
                        2,
                    ),
            },

            "dry_run":
                True,
        }

    # ======================================================
    # BROKER LIMITS
    # ======================================================

    point = float(
        info.point
    )

    min_stop_distance = (
        float(
            info.trade_stops_level
        )
        * point
    )

    freeze_distance = (
        float(
            info.trade_freeze_level
        )
        * point
    )

    buffer_distance = max(
        MIN_BUFFER_POINTS
        * point,

        atr
        * ENTRY_BUFFER_ATR_MULTIPLIER,
    )

    # ======================================================
    # SWINGS M15
    # ======================================================

    recent_high = max(
        candle["high"]
        for candle in recent
    )

    recent_low = min(
        candle["low"]
        for candle in recent
    )

    # ======================================================
    # TIPO Y ENTRADA
    # ======================================================

    order_type = None
    entry = None

    # ------------------------------------------------------
    # PULLBACK / CONTINUACION
    # ------------------------------------------------------

    if strategy in {
        "TREND_CONTINUATION",
        "PULLBACK",
    }:

        if direction == "BUY":

            order_type = (
                "BUY_LIMIT"
            )

            entry = float(
                ema20
            )

            # BUY LIMIT debe estar por debajo
            # del precio Ask actual.

            maximum_entry = (
                tick.ask
                - min_stop_distance
                - point
            )

            if entry >= maximum_entry:

                errors.append(
                    "BUY_LIMIT_ENTRY_NOT_VALID"
                )

        elif direction == "SELL":

            order_type = (
                "SELL_LIMIT"
            )

            entry = float(
                ema20
            )

            minimum_entry = (
                tick.bid
                + min_stop_distance
                + point
            )

            if entry <= minimum_entry:

                errors.append(
                    "SELL_LIMIT_ENTRY_NOT_VALID"
                )

    # ------------------------------------------------------
    # BREAKOUT
    # ------------------------------------------------------

    elif strategy == "BREAKOUT":

        if direction == "BUY":

            order_type = (
                "BUY_STOP"
            )

            entry = max(
                recent_high
                + buffer_distance,

                tick.ask
                + min_stop_distance
                + point,
            )

        elif direction == "SELL":

            order_type = (
                "SELL_STOP"
            )

            entry = min(
                recent_low
                - buffer_distance,

                tick.bid
                - min_stop_distance
                - point,
            )

    else:

        errors.append(
            "STRATEGY_NOT_SUPPORTED_BY_PLANNER"
        )

    if direction not in {
        "BUY",
        "SELL",
    }:

        errors.append(
            "INVALID_DIRECTION"
        )

    if entry is None:

        errors.append(
            "ENTRY_NOT_AVAILABLE"
        )

    if errors:

        return {
            "symbol": symbol,
            "status": "REJECTED",
            "errors": errors,
        }

    entry = round_price(
        info,
        entry,
    )

    # ======================================================
    # STOP LOSS
    # ======================================================

    atr_stop_distance = (
        atr
        * ATR_STOP_MULTIPLIER
    )

    if direction == "BUY":

        technical_stop = (
            recent_low
            - buffer_distance
        )

        atr_stop = (
            entry
            - atr_stop_distance
        )

        stop_loss = min(
            technical_stop,
            atr_stop,
        )

        # Garantizar distancia mínima del broker
        stop_loss = min(
            stop_loss,
            entry
            - min_stop_distance
            - point,
        )

    else:

        technical_stop = (
            recent_high
            + buffer_distance
        )

        atr_stop = (
            entry
            + atr_stop_distance
        )

        stop_loss = max(
            technical_stop,
            atr_stop,
        )

        stop_loss = max(
            stop_loss,
            entry
            + min_stop_distance
            + point,
        )

    stop_loss = round_price(
        info,
        stop_loss,
    )

    # ======================================================
    # TAKE PROFIT
    # ======================================================

    risk_price_distance = abs(
        entry - stop_loss
    )

    if risk_price_distance <= 0:

        errors.append(
            "INVALID_STOP_DISTANCE"
        )

        return {
            "symbol": symbol,
            "status": "REJECTED",
            "errors": errors,
        }

    if direction == "BUY":

        take_profit = (
            entry
            +
            risk_price_distance
            * TARGET_RISK_REWARD
        )

    else:

        take_profit = (
            entry
            -
            risk_price_distance
            * TARGET_RISK_REWARD
        )

    take_profit = round_price(
        info,
        take_profit,
    )

    # ======================================================
    # CALCULOS MT5 EN MONEDA DE CUENTA
    # ======================================================

    calc_action = (
        mt5.ORDER_TYPE_BUY
        if direction == "BUY"
        else mt5.ORDER_TYPE_SELL
    )

    loss_result = (
        mt5.order_calc_profit(
            calc_action,
            symbol,
            LOT_SIZE,
            entry,
            stop_loss,
        )
    )

    profit_result = (
        mt5.order_calc_profit(
            calc_action,
            symbol,
            LOT_SIZE,
            entry,
            take_profit,
        )
    )

    margin_required = (
        mt5.order_calc_margin(
            calc_action,
            symbol,
            LOT_SIZE,
            entry,
        )
    )

    # ======================================================
    # VALIDAR CALCULOS
    # ======================================================

    if loss_result is None:

        errors.append(
            "LOSS_CALC_FAILED"
        )

    if profit_result is None:

        errors.append(
            "PROFIT_CALC_FAILED"
        )

    if margin_required is None:

        errors.append(
            "MARGIN_CALC_FAILED"
        )

    if errors:

        return {
            "symbol": symbol,
            "status": "REJECTED",
            "errors": errors,
        }

    estimated_loss_eur = abs(
        float(loss_result)
    )

    estimated_profit_eur = abs(
        float(profit_result)
    )

    margin_required = float(
        margin_required
    )

    # ======================================================
    # REGLAS RIESGO
    # ======================================================

    if estimated_loss_eur <= 0:

        errors.append(
            "INVALID_ESTIMATED_LOSS"
        )

    if (
        estimated_loss_eur
        >
        MAX_LOSS_PER_TRADE_EUR
    ):

        errors.append(
            "MAX_LOSS_EXCEEDED"
        )

    if (
        margin_required
        >
        account.margin_free
    ):

        errors.append(
            "INSUFFICIENT_FREE_MARGIN"
        )

    risk_reward = None

    if estimated_loss_eur > 0:

        risk_reward = (
            estimated_profit_eur
            /
            estimated_loss_eur
        )

        if (
            risk_reward
            <
            MIN_RISK_REWARD
            
        ):

            errors.append(
                "RISK_REWARD_TOO_LOW"
            )

    # ======================================================
    # SPREAD ACTUAL
    # ======================================================

    spread_points = (
        (
            tick.ask
            -
            tick.bid
        )
        / point
    )

    max_spread_points = (
        MAX_SPREAD_POINTS_BY_SYMBOL.get(
            symbol
        )
    )

    if max_spread_points is None:

        errors.append(
            "SPREAD_LIMIT_NOT_CONFIGURED"
        )

    elif (
        spread_points
        >
        max_spread_points
    ):

        errors.append(
            "SPREAD_TOO_HIGH"
        )

    # ======================================================
    # RESULTADO
    # ======================================================

    status = (
        "VALID"
        if not errors
        else "REJECTED"
    )

    return {
        "symbol":
            symbol,

        "status":
            status,

        "errors":
            errors,

        "direction":
            direction,

        "strategy":
            strategy,

        "order_type":
            order_type,

        "volume":
            LOT_SIZE,

        "entry":
            entry,

        "stop_loss":
            stop_loss,

        "take_profit":
            take_profit,

        "estimated_loss_eur":
            round(
                estimated_loss_eur,
                2,
            ),

        "estimated_profit_eur":
            round(
                estimated_profit_eur,
                2,
            ),

        "risk_reward":
            (
                round(
                    risk_reward,
                    2,
                )
                if risk_reward
                is not None
                else None
            ),

        "margin_required_eur":
            round(
                margin_required,
                2,
            ),

        "free_margin_eur":
            round(
                float(
                    account.margin_free
                ),
                2,
            ),

        "spread_points":
            round(
                spread_points,
                1,
            ),

        "broker_constraints": {
            "digits":
                int(
                    info.digits
                ),

            "point":
                point,

            "trade_stops_level_points":
                int(
                    info.trade_stops_level
                ),

            "trade_freeze_level_points":
                int(
                    info.trade_freeze_level
                ),

            "minimum_stop_distance":
                min_stop_distance,

            "freeze_distance":
                freeze_distance,

            "volume_min":
                float(
                    info.volume_min
                ),

            "volume_max":
                float(
                    info.volume_max
                ),

            "volume_step":
                float(
                    info.volume_step
                ),
        },

        "market_reference": {
            "bid":
                float(
                    tick.bid
                ),

            "ask":
                float(
                    tick.ask
                ),

            "m15_atr":
                atr,

            "m15_ema20":
                ema20,

            "recent_high":
                recent_high,

            "recent_low":
                recent_low,
        },

        "dry_run":
            True,

        "max_spread_points":
            max_spread_points,

        "timing": {
            "market_state_age_seconds":
                (
                    round(
                        state_age_seconds,
                        1,
                    )
                    if state_age_seconds
                    is not None
                    else None
                ),

            "tick_age_seconds":
                round(
                    tick_age_seconds,
                    1,
                ),

            "tick_time_mode":
                tick_time_mode,

            "server_utc_offset_seconds":
                server_utc_offset_seconds,

            "live_drift_atr":
                round(
                    live_drift_atr,
                    2,
                ),
        },
    }