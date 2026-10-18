import json

import ollama

from config.agent_config import (
    OLLAMA_MODEL,
    OLLAMA_CONTEXT,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_TEMPERATURE,
)


def _extract_content(
    response,
):

    if isinstance(
        response,
        dict,
    ):

        return response[
            "message"
        ][
            "content"
        ]

    return (
        response
        .message
        .content
    )


def structured_chat(
    schema,
    system_prompt,
    payload,
):

    
    output_schema = schema.model_json_schema()

    if schema.__name__ == "LevelHypothesisComparison":

        allowed_ids = list(
            dict.fromkeys(
                payload.get("allowed_evidence_ids", [])
            )
        )

        if not allowed_ids:
            raise ValueError(
                "NO_ALLOWED_EVIDENCE_IDS"
            )

        for field_name in (
            "hold_evidence_used",
            "break_evidence_used",
        ):
            output_schema["properties"][field_name]["items"] = {
                "type": "string",
                "enum": allowed_ids,
            }

    response = ollama.chat(
        model=
            OLLAMA_MODEL,

        messages=[
            {
                "role":
                    "system",

                "content":
                    system_prompt,
            },
            {
                "role":
                    "user",

                "content":
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                    ),
            },
        ],

        format=output_schema,

        options={
            "temperature":
                OLLAMA_TEMPERATURE,

            "num_ctx":
                OLLAMA_CONTEXT,
        },

        #think=True,
        think=(
            schema.__name__
            == "LevelHypothesisComparison"
        ),

        keep_alive=
            OLLAMA_KEEP_ALIVE,
    )

    if isinstance(
        response,
        dict,
    ):

        prompt_eval_count = (
            response.get(
                "prompt_eval_count"
            )
        )

        eval_count = (
            response.get(
                "eval_count"
            )
        )

    else:

        prompt_eval_count = getattr(
            response,
            "prompt_eval_count",
            None,
        )

        eval_count = getattr(
            response,
            "eval_count",
            None,
        )


    print(
        "[LLM USAGE]",
        f"schema={schema.__name__}",
        f"prompt_eval_count={prompt_eval_count}",
        f"eval_count={eval_count}",
        f"num_ctx={OLLAMA_CONTEXT}",
    )

    content = (
        _extract_content(
            response
        )
    )
    if schema.__name__ == "LevelHypothesisComparison":
        print(
            "\n[LLM RAW JSON]",
            content,
        )


    if schema.__name__ == "LevelHypothesisComparison":
        if isinstance(response, dict):
            message = response.get("message") or {}
            thinking = message.get("thinking") or ""
            done_reason = response.get("done_reason")
            eval_duration = response.get("eval_duration")
        else:
            message = getattr(response, "message", None)
            thinking = getattr(message, "thinking", None) or ""
            done_reason = getattr(response, "done_reason", None)
            eval_duration = getattr(response, "eval_duration", None)

        print(
            "[LLM THINKING DIAG]",
            f"thinking_chars={len(thinking)}",
            f"done_reason={done_reason}",
            f"eval_seconds={(eval_duration or 0) / 1e9:.2f}",
        )

    return (
        schema
        .model_validate_json(
            content
        )
    )