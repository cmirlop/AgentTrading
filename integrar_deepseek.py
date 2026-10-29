"""Instala la ruta opcional DeepSeek sin sobrescribir cambios de llm_client.py.

Ejecutar una sola vez desde la raíz de AgentTrading:
    python integrar_deepseek.py
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import sys

ROOT = Path.cwd()
AGENTS = ROOT / "src" / "agents"
LLM_CLIENT = AGENTS / "llm_client.py"
DEEPSEEK_TARGET = AGENTS / "deepseek_client.py"
DEEPSEEK_SOURCE = Path(__file__).resolve().with_name("deepseek_client.py")
MARKER = "# DEEPSEEK_PROVIDER_SWITCH_V1"


def main():
    if not LLM_CLIENT.exists() or not DEEPSEEK_SOURCE.exists():
        raise SystemExit(
            "Ejecuta desde la raíz de AgentTrading y deja deepseek_client.py "
            "junto al instalador."
        )

    original = LLM_CLIENT.read_text(encoding="utf-8")
    newline = "\r\n" if b"\r\n" in LLM_CLIENT.read_bytes() else "\n"

    if MARKER not in original:
        pattern = re.compile(r"(?m)^def structured_chat\([\s\S]*?^\):[ \t]*\n")
        normalized = original.replace("\r\n", "\n")
        match = pattern.search(normalized)
        if not match:
            raise SystemExit(
                "No se ha encontrado la cabecera de structured_chat(). "
                "No se ha modificado ningún archivo."
            )
        injection = '''    # DEEPSEEK_PROVIDER_SWITCH_V1
    import os
    _provider = os.environ.get("LLM_PROVIDER", "ollama").strip().lower()
    if _provider == "deepseek":
        from src.agents.deepseek_client import deepseek_structured_chat
        return deepseek_structured_chat(schema, system_prompt, payload)
    if _provider != "ollama":
        raise ValueError("LLM_PROVIDER debe ser ollama o deepseek")

'''
        modified = normalized[:match.end()] + injection + normalized[match.end():]
        ast.parse(modified, filename=str(LLM_CLIENT))
    else:
        modified = original.replace("\r\n", "\n")

    client_code = DEEPSEEK_SOURCE.read_text(encoding="utf-8")
    ast.parse(client_code, filename=str(DEEPSEEK_SOURCE))

    if DEEPSEEK_TARGET.exists() and DEEPSEEK_TARGET.read_text(encoding="utf-8") != client_code:
        raise SystemExit(
            "Ya existe src/agents/deepseek_client.py con cambios diferentes. "
            "No se ha modificado ningún archivo."
        )

    if MARKER not in original:
        backup = AGENTS / "llm_client.before_deepseek.bak"
        if backup.exists():
            raise SystemExit("El backup ya existe; no sobrescribo archivos. Revisa la instalación.")
        shutil.copy2(LLM_CLIENT, backup)
        with LLM_CLIENT.open("w", encoding="utf-8", newline="") as out:
            out.write(modified.replace("\n", newline))
        print(f"Backup del cliente original: {backup}")
    else:
        print("El selector DeepSeek ya estaba instalado; no se ha duplicado.")

    if not DEEPSEEK_TARGET.exists():
        shutil.copy2(DEEPSEEK_SOURCE, DEEPSEEK_TARGET)
    print("OK: instalado DeepSeek como opción. Ollama sigue siendo el predeterminado.")
    print("Selecciona DEEPSEEK_API_KEY y LLM_PROVIDER=deepseek para activarlo.")


if __name__ == "__main__":
    main()
