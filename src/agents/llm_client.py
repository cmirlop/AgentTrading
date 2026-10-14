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

        format=
            schema.model_json_schema(),

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

    return (
        schema
        .model_validate_json(
            content
        )
    )