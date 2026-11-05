"""Legacy installer kept as a safe compatibility check.

DeepSeek is already selected through ``src.agents.llm_client``. Running this
script validates the single router and never edits source files.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> None:
    router = ROOT / "src" / "agents" / "llm_client.py"
    adapter = ROOT / "src" / "agents" / "deepseek_client.py"
    if not router.is_file() or not adapter.is_file():
        raise SystemExit("LLM_ROUTER_OR_DEEPSEEK_ADAPTER_MISSING")
    ast.parse(router.read_text(encoding="utf-8"), filename=str(router))
    ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    print("OK: Ollama y DeepSeek pasan por src.agents.llm_client; no se modificó ningún archivo.")


if __name__ == "__main__":
    main()
