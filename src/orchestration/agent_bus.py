from datetime import (
    datetime,
    timezone,
)


class AgentBus:

    def __init__(
        self,
        context,
    ):

        self.context = context

    def publish(
        self,
        agent,
        message_type,
        payload,
    ):

        message = {
            "sequence":
                len(
                    self.context.messages
                )
                + 1,

            "timestamp":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "agent":
                agent,

            "type":
                message_type,

            "symbol":
                self.context.symbol,

            "payload":
                payload,
        }

        self.context.messages.append(
            message
        )

        return message

    def latest(
        self,
        message_type,
    ):

        for message in reversed(
            self.context.messages
        ):

            if (
                message[
                    "type"
                ]
                == message_type
            ):

                return message

        return None

    def require(
        self,
        message_type,
    ):

        message = self.latest(
            message_type
        )

        if message is None:

            raise RuntimeError(
                f"Mensaje requerido "
                f"no encontrado: "
                f"{message_type}"
            )

        return message

    def messages_from(
        self,
        agent,
    ):

        return [
            message

            for message
            in self.context.messages

            if message[
                "agent"
            ]
            == agent
        ]