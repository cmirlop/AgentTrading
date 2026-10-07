from datetime import (
    datetime,
    timezone,
)

import MetaTrader5 as mt5

from config.risk_config import (
    LOT_SIZE,
    MAX_LOSS_PER_TRADE_EUR,
    MAX_SPREAD_POINTS_BY_SYMBOL,
)

from config.trade_planner_config import (
    MIN_RISK_REWARD,
)

from config.execution_config import (
    MAGIC_NUMBER,
    ORDER_COMMENT,
    PENDING_ORDER_EXPIRATION_MINUTES,
)

from config.calendar_config import (
    HIGH_IMPACT_BLOCK_BEFORE_MINUTES,
    CRITICAL_BLOCK_BEFORE_MINUTES,
)


def validate_dry_run(
    symbol,
    candidate,
    trade_plan,
    request,
    order_check_result,
    terminal_info,
):

    errors = []

    # ======================================================
    # CANDIDATE
    # ======================================================

    if not candidate.get(
        "allowed",
        False,
    ):

        errors.append(
            "CANDIDATE_NOT_ALLOWED"
        )

    if candidate.get(
        "status"
    ) not in {
        "CANDIDATE_LONG",
        "CANDIDATE_SHORT",
    }:

        errors.append(
            "INVALID_CANDIDATE_STATUS"
        )

    # ======================================================
    # PLAN
    # ======================================================

    if trade_plan.get(
        "status"
    ) != "VALID":

        errors.append(
            "TRADE_PLAN_NOT_VALID"
        )

    if trade_plan.get(
        "symbol"
    ) != symbol:

        errors.append(
            "PLAN_SYMBOL_MISMATCH"
        )

    # ======================================================
    # VOLUMEN
    # ======================================================

    volume = trade_plan.get(
        "volume"
    )

    if (
        volume is None
        or
        abs(
            float(volume)
            - LOT_SIZE
        ) > 1e-9
    ):

        errors.append(
            "INVALID_LOT_SIZE"
        )

    # ======================================================
    # ENTRY / SL / TP
    # ======================================================

    entry = trade_plan.get(
        "entry"
    )

    stop_loss = trade_plan.get(
        "stop_loss"
    )

    take_profit = trade_plan.get(
        "take_profit"
    )

    if (
        entry is None
        or stop_loss is None
        or take_profit is None
    ):

        errors.append(
            "ENTRY_SL_TP_MISSING"
        )

    else:

        direction = (
            trade_plan.get(
                "direction"
            )
        )

        if direction == "BUY":

            if not (
                stop_loss
                < entry
                < take_profit
            ):

                errors.append(
                    "INVALID_BUY_PRICE_STRUCTURE"
                )

        elif direction == "SELL":

            if not (
                take_profit
                < entry
                < stop_loss
            ):

                errors.append(
                    "INVALID_SELL_PRICE_STRUCTURE"
                )

        else:

            errors.append(
                "INVALID_DIRECTION"
            )

    # ======================================================
    # PERDIDA MAXIMA
    # ======================================================

    estimated_loss = (
        trade_plan.get(
            "estimated_loss_eur"
        )
    )

    if (
        estimated_loss is None
        or estimated_loss <= 0
    ):

        errors.append(
            "INVALID_ESTIMATED_LOSS"
        )

    elif (
        estimated_loss
        >
        MAX_LOSS_PER_TRADE_EUR
    ):

        errors.append(
            "MAX_LOSS_EXCEEDED"
        )

    # ======================================================
    # RISK / REWARD
    # ======================================================

    risk_reward = (
        trade_plan.get(
            "risk_reward"
        )
    )

    if (
        risk_reward is None
        or
        risk_reward
        < MIN_RISK_REWARD
    ):

        errors.append(
            "RISK_REWARD_TOO_LOW"
        )

    # ======================================================
    # SPREAD
    # ======================================================

    spread = (
        trade_plan.get(
            "spread_points"
        )
    )

    max_spread = (
        MAX_SPREAD_POINTS_BY_SYMBOL
        .get(symbol)
    )

    if max_spread is None:

        errors.append(
            "SPREAD_LIMIT_NOT_CONFIGURED"
        )

    elif (
        spread is None
        or spread > max_spread
    ):

        errors.append(
            "SPREAD_TOO_HIGH"
        )

    # ======================================================
    # MARGEN
    # ======================================================

    margin_required = (
        trade_plan.get(
            "margin_required_eur"
        )
    )

    free_margin = (
        trade_plan.get(
            "free_margin_eur"
        )
    )

    if (
        margin_required is None
        or free_margin is None
    ):

        errors.append(
            "MARGIN_INFORMATION_MISSING"
        )

    elif (
        margin_required
        >
        free_margin
    ):

        errors.append(
            "INSUFFICIENT_FREE_MARGIN"
        )

    # ======================================================
    # REQUEST MT5
    # ======================================================

    if request.get(
        "action"
    ) != mt5.TRADE_ACTION_PENDING:

        errors.append(
            "REQUEST_NOT_PENDING"
        )

    if request.get(
        "symbol"
    ) != symbol:

        errors.append(
            "REQUEST_SYMBOL_MISMATCH"
        )

    if request.get(
        "magic"
    ) != MAGIC_NUMBER:

        errors.append(
            "INVALID_MAGIC_NUMBER"
        )

    if request.get(
        "comment"
    ) != ORDER_COMMENT:

        errors.append(
            "INVALID_ORDER_COMMENT"
        )

    if (
        abs(
            float(
                request.get(
                    "volume",
                    0,
                )
            )
            - LOT_SIZE
        ) > 1e-9
    ):

        errors.append(
            "REQUEST_VOLUME_MISMATCH"
        )

    # ======================================================
    # EXPIRACION
    # ======================================================

    expiration = request.get(
        "expiration"
    )

    if not expiration:

        errors.append(
            "EXPIRATION_MISSING"
        )

    else:

        now_timestamp = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )

        seconds_until_expiry = (
            expiration
            - now_timestamp
        )

        maximum_seconds = (
            PENDING_ORDER_EXPIRATION_MINUTES
            * 60
            + 10
        )

        if seconds_until_expiry <= 0:

            errors.append(
                "ORDER_ALREADY_EXPIRED"
            )

        elif (
            seconds_until_expiry
            >
            maximum_seconds
        ):

            errors.append(
                "EXPIRATION_TOO_LONG"
            )

    # ======================================================
    # REDUNDANCIA MACRO
    # ======================================================

    macro = candidate.get(
        "macro"
    )

    if macro is None:

        errors.append(
            "MACRO_STATE_MISSING"
        )

    else:

        if macro.get(
            "new_trades_blocked",
            True,
        ):

            errors.append(
                "MACRO_BLOCK_ACTIVE"
            )

        overlap_horizon = (
            PENDING_ORDER_EXPIRATION_MINUTES
            +
            HIGH_IMPACT_BLOCK_BEFORE_MINUTES
        )

        for event in macro.get(
            "upcoming_high_events",
            [],
        ):

            minutes_until = (
                event.get(
                    "minutes_until_event"
                )
            )

            if minutes_until is None:
                continue

            if (
                0
                <= minutes_until
                <= overlap_horizon
            ):

                errors.append(
                    "HIGH_IMPACT_OVERLAPS_ORDER_LIFETIME"
                )

                break

        critical_overlap_horizon = (
            PENDING_ORDER_EXPIRATION_MINUTES
            +
            CRITICAL_BLOCK_BEFORE_MINUTES
        )

        for event in macro.get(
            "upcoming_critical_events",
            [],
        ):

            minutes_until = (
                event.get(
                    "minutes_until_event"
                )
            )

            if minutes_until is None:
                continue

            if (
                0
                <= minutes_until
                <= critical_overlap_horizon
            ):

                errors.append(
                    "CRITICAL_EVENT_OVERLAPS_ORDER_LIFETIME"
                )

                break

    # ======================================================
    # ORDER CHECK
    # ======================================================

    if order_check_result is None:

        errors.append(
            "ORDER_CHECK_FAILED"
        )

    elif (
        order_check_result.retcode
        != 0
    ):

        errors.append(
            f"ORDER_CHECK_RETCODE_"
            f"{order_check_result.retcode}"
        )

    # ======================================================
    # MODO SEGURO
    # ======================================================

    if terminal_info is None:

        errors.append(
            "TERMINAL_INFO_MISSING"
        )

    elif not terminal_info.tradeapi_disabled:

        errors.append(
            "PYTHON_TRADING_NOT_DISABLED"
        )

    errors = list(
        dict.fromkeys(
            errors
        )
    )

    approved = (
        len(errors) == 0
    )

    return {
        "status":
            (
                "DRY_RUN_APPROVED"
                if approved
                else "REJECTED"
            ),

        "approved":
            approved,

        "errors":
            errors,

        "risk": {
            "volume":
                volume,

            "estimated_loss_eur":
                estimated_loss,

            "max_loss_eur":
                MAX_LOSS_PER_TRADE_EUR,

            "risk_reward":
                risk_reward,

            "minimum_risk_reward":
                MIN_RISK_REWARD,

            "spread_points":
                spread,

            "max_spread_points":
                max_spread,

            "margin_required_eur":
                margin_required,

            "free_margin_eur":
                free_margin,
        },
    }