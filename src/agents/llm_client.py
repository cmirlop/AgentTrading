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

        think=False,

        keep_alive=
            OLLAMA_KEEP_ALIVE,
    )

    content = (
        _extract_content(
            response
        )
    )

    return (
        schema
        .model_validate_json(
            content
        )
    )