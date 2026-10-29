"""Proveedor DeepSeek Flash para structured_chat().

La clave se obtiene exclusivamente de DEEPSEEK_API_KEY. No imprime
prompts, datos de mercado ni el razonamiento interno del proveedor.
"""

from __future__ import annotations

import json
import os
from time import perf_counter

from openai import OpenAI


_ALLOWED_EFFORTS = {"low", "high", "max"}


def deepseek_structured_chat(schema, system_prompt, payload):
    """Genera un objeto Pydantic con DeepSeek; falla si la salida no es válida."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Falta DEEPSEEK_API_KEY. Configúrala en esta sesión de PowerShell."
        )

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
    effort = os.environ.get("DEEPSEEK_REASONING_EFFORT", "low").strip().lower()
    if effort not in _ALLOWED_EFFORTS:
        raise ValueError("DEEPSEEK_REASONING_EFFORT debe ser low, high o max")

    is_comparison = schema.__name__ == "LevelHypothesisComparison"
    max_output = int(
        os.environ.get(
            "DEEPSEEK_MAX_TOKENS",
            "8192" if is_comparison else "2048",
        )
    )
    if not 1 <= max_output <= 65536:
        raise ValueError("DEEPSEEK_MAX_TOKENS fuera del intervalo 1..65536")

    # Ollama acepta JSON Schema como 'format'. Chat Completions de DeepSeek
    # utiliza json_object, de modo que transmitimos el esquema como instrucción
    # y volvemos a validarlo con Pydantic al recibir la respuesta.
    output_schema = schema.model_json_schema()
    allowed_ids = None
    if is_comparison:
        allowed_ids = payload.get("allowed_evidence_ids", [])
        if not isinstance(allowed_ids, list) or not allowed_ids:
            raise ValueError("NO_ALLOWED_EVIDENCE_IDS")
        if not all(isinstance(v, str) for v in allowed_ids):
            raise ValueError("INVALID_ALLOWED_EVIDENCE_IDS")
        allowed_ids = list(dict.fromkeys(allowed_ids))
        for field_name in ("hold_evidence_used", "break_evidence_used"):
            output_schema["properties"][field_name]["items"] = {
                "type": "string",
                "enum": allowed_ids,
            }
        output_schema["required"] = list(dict.fromkeys(
            output_schema.get("required", [])
            + ["hold_evidence_used", "break_evidence_used"]
        ))

    instructions = (
        system_prompt.rstrip()
        + "\n\nReturn ONLY a valid JSON object. No Markdown. "
        + "Follow this JSON Schema exactly, including the required fields:\n"
        + json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
    )

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=180.0,
        max_retries=0,  # Evitar reintentos no solicitados con coste adicional.
    )
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": max_output,
    }
    if is_comparison:
        kwargs["reasoning_effort"] = effort
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    else:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = 0.0

    started = perf_counter()
    response = client.chat.completions.create(**kwargs)
    duration = perf_counter() - started

    if not response.choices:
        raise RuntimeError("DEEPSEEK_EMPTY_CHOICES")
    choice = response.choices[0]
    finish_reason = choice.finish_reason
    if finish_reason != "stop":
        raise RuntimeError(f"DEEPSEEK_INCOMPLETE: finish_reason={finish_reason}")
    content = choice.message.content or ""
    if not content.strip():
        raise RuntimeError("DEEPSEEK_EMPTY_JSON")

    # Antes de Pydantic, verificar las restricciones que json_object no impone.
    if is_comparison:
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("DEEPSEEK_EXPECTED_JSON_OBJECT")
        required = {
            "hold_score", "break_score", "confidence",
            "hold_evidence_used", "break_evidence_used",
        }
        if required - parsed.keys():
            raise ValueError("DEEPSEEK_MISSING_FIELDS: " + ", ".join(sorted(required - parsed.keys())))
        for field in ("hold_evidence_used", "break_evidence_used"):
            values = parsed[field]
            if not isinstance(values, list) or any(
                not isinstance(item, str) or item not in allowed_ids
                for item in values
            ):
                raise ValueError(f"DEEPSEEK_INVALID_EVIDENCE: {field}")

    result = schema.model_validate_json(content)
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(details, "reasoning_tokens", None)
    print(
        "[LLM USAGE]",
        f"provider=deepseek model={model}",
        f"schema={schema.__name__}",
        f"prompt_tokens={prompt_tokens}",
        f"completion_tokens={completion_tokens}",
        f"reasoning_tokens={reasoning_tokens}",
        f"finish_reason={finish_reason}",
        f"elapsed_seconds={duration:.2f}",
    )
    if is_comparison:
        print("\n[LLM RAW JSON]", content)
    return result
