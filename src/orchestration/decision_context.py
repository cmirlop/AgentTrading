from dataclasses import (
    dataclass,
    field,
)

from datetime import (
    datetime,
    timezone,
)


@dataclass
class DecisionContext:

    symbol: str

    market_state: dict

    snapshot: dict

    analysis_time: str

    created_at: str = field(
        default_factory=lambda:
            datetime.now(
                timezone.utc
            ).isoformat()
    )

    messages: list = field(
        default_factory=list
    )

    architecture_version: str = (
        "MULTI_AGENT_V1"
    )

    runtime_state: dict | None = None

    calendar_state: dict | None = None

    def to_dict(
        self,
    ):

        return {
            "architecture_version":
                self.architecture_version,

            "symbol":
                self.symbol,

            "analysis_time":
                self.analysis_time,

            "created_at":
                self.created_at,

            # Guardamos el snapshot compacto,
            # no todo el MarketState bruto.
            "snapshot":
                self.snapshot,

            "messages":
                self.messages,
        }