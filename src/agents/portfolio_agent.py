from datetime import (
    datetime,
    timezone,
)

from config.operational_config import (
    MAX_RUNTIME_AGE_SECONDS,
)

from config.risk_config import (
    MAX_OPEN_POSITIONS,
)


def parse_utc(
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


class PortfolioAgent:

    name = "portfolio_agent"

    def run(
        self,
        context,
        bus,
    ):

        runtime = (
            context.runtime_state
        )

        if not runtime:

            return bus.publish(
                agent=self.name,
                message_type=
                    "PORTFOLIO_ASSESSMENT",
                payload={
                    "approved":
                        False,

                    "errors":
                        [
                            "RUNTIME_STATE_MISSING"
                        ],
                },
            )

        generated_at = parse_utc(
            runtime.get(
                "generated_at"
            )
        )

        if generated_at is None:

            age_seconds = None
            fresh = False

        else:

            age_seconds = (
                datetime.now(
                    timezone.utc
                )
                - generated_at
            ).total_seconds()

            fresh = (
                0
                <= age_seconds
                <= MAX_RUNTIME_AGE_SECONDS
            )

        positions = (
            runtime.get(
                "monitored_positions",
                runtime.get(
                    "positions",
                    [],
                ),
            )
            or []
        )

        orders = (
            runtime.get(
                "monitored_orders",
                runtime.get(
                    "orders",
                    [],
                ),
            )
            or []
        )

        symbol_positions = [
            item

            for item in positions

            if item.get(
                "symbol"
            )
            == context.symbol
        ]

        symbol_orders = [
            item

            for item in orders

            if item.get(
                "symbol"
            )
            == context.symbol
        ]

        # Una pendiente también reserva capacidad.
        reservations = (
            len(positions)
            +
            len(orders)
        )

        free_slots = max(
            0,
            MAX_OPEN_POSITIONS
            - reservations,
        )

        errors = []

        if not fresh:

            errors.append(
                "RUNTIME_STATE_STALE"
            )

        if free_slots <= 0:

            errors.append(
                "NO_GLOBAL_SLOT_AVAILABLE"
            )

        if symbol_positions:

            errors.append(
                "POSITION_ALREADY_OPEN_FOR_SYMBOL"
            )

        if symbol_orders:

            errors.append(
                "PENDING_ORDER_ALREADY_EXISTS_FOR_SYMBOL"
            )

        return bus.publish(
            agent=self.name,

            message_type=
                "PORTFOLIO_ASSESSMENT",

            payload={
                "approved":
                    len(errors) == 0,

                "fresh":
                    fresh,

                "age_seconds":
                    (
                        round(
                            age_seconds,
                            1,
                        )
                        if age_seconds
                        is not None
                        else None
                    ),

                "open_positions":
                    len(
                        positions
                    ),

                "pending_orders":
                    len(
                        orders
                    ),

                "reservations":
                    reservations,

                "max_reservations":
                    MAX_OPEN_POSITIONS,

                "free_slots":
                    free_slots,

                "symbol_has_position":
                    bool(
                        symbol_positions
                    ),

                "symbol_has_pending_order":
                    bool(
                        symbol_orders
                    ),

                "errors":
                    errors,

                "source":
                    "DETERMINISTIC_PYTHON",
            },
        )