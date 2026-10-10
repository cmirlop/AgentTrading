from src.technical_evaluator import (
    evaluate_technical_state,
)


class TechnicalAgent:

    name = "technical_agent"

    def run(
        self,
        context,
        bus,
    ):

        technical = (
            evaluate_technical_state(
                context.snapshot
            )
        )

        payload = {
            "bias":
                technical[
                    "bias"
                ],

            "overall_score":
                technical[
                    "overall_score"
                ],

            "timeframe_scores":
                technical[
                    "timeframe_scores"
                ],

            "alignment":
                technical[
                    "alignment"
                ],

            "setup_quality":
                technical[
                    "setup_quality"
                ],

            "supporting_evidence":
                technical.get(
                    "supporting_evidence",
                    [],
                ),

            "conflicting_evidence":
                technical.get(
                    "conflicting_evidence",
                    [],
                ),

            "source":
                "DETERMINISTIC_PYTHON",
        }

        return bus.publish(
            agent=
                self.name,

            message_type=
                "TECHNICAL_ASSESSMENT",

            payload=
                payload,
        )