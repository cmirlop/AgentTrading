OLLAMA_MODEL = "qwen3:4b"

OLLAMA_CONTEXT = 4096

OLLAMA_KEEP_ALIVE = "30m"

OLLAMA_TEMPERATURE = 0.0


# Prefiltro provisional.
# NO significa que quality=65 haya quedado validado
# como estrategia rentable.

TECHNICAL_PREFILTER_QUALITY = 65


PLANNER_SUPPORTED_STRATEGIES = {
    "TREND_CONTINUATION",
    "PULLBACK",
    "BREAKOUT",
}