import time
from typing import Literal

from ollama import chat
from pydantic import BaseModel


MODEL = "qwen3:4b"


class OllamaTestResponse(BaseModel):
    status: Literal["OK", "ERROR"]
    model_role: str
    understands_market_data: bool
    message: str


def main():

    print("=" * 70)
    print("TRADING AGENT - TEST OLLAMA")
    print("=" * 70)

    system_prompt = """
Eres un componente de un sistema automatizado de análisis financiero.

Por ahora NO debes tomar decisiones de trading.
Esta prueba únicamente comprueba que puedes recibir datos
estructurados y devolver JSON válido.

Devuelve siempre la respuesta utilizando exactamente
el esquema solicitado.
"""

    user_prompt = """
Datos de prueba:

{
    "symbol": "EURUSD",
    "timeframe": "M15",
    "close": 1.12815,
    "rsi14": 67.16,
    "atr14": 0.001166
}

Confirma que puedes interpretar estos datos estructurados.

status debe ser "OK" si puedes procesarlos.
understands_market_data debe ser true.

No recomiendes comprar ni vender.
"""

    start = time.perf_counter()

    response = chat(
        model=MODEL,

        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],

        format=OllamaTestResponse.model_json_schema(),

        options={
            "temperature": 0,
            "num_ctx": 4096,
        },

        think=False,
    )

    elapsed = time.perf_counter() - start

    result = OllamaTestResponse.model_validate_json(
        response.message.content
    )

    print()
    print("--- RESPUESTA VALIDADA ---")
    print(result.model_dump_json(indent=2))

    print()
    print("--- RENDIMIENTO ---")
    print(f"Tiempo total: {elapsed:.2f} segundos")

    eval_count = getattr(
        response,
        "eval_count",
        None,
    )

    eval_duration = getattr(
        response,
        "eval_duration",
        None,
    )

    if eval_count:
        print(
            "Tokens generados:",
            eval_count,
        )

    if eval_count and eval_duration:

        seconds = (
            eval_duration
            / 1_000_000_000
        )

        if seconds > 0:

            print(
                "Velocidad:",
                round(
                    eval_count / seconds,
                    2,
                ),
                "tokens/s",
            )

    print()
    print("[OK] Python -> Ollama funciona")


if __name__ == "__main__":
    main()