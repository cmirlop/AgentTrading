from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = re.compile(
    r"(account_number|login_id|account_balance|account_equity|broker_server|"
    r"trade_server|credential|api_key|password|access_token|position_ticket)",
    re.I,
)
ABSOLUTE_USER_PATH = re.compile(r"[A-Z]:\\\\Users\\\\[^\\\\]+\\\\", re.I)


def check() -> list[str]:
    errors = []
    report_dir = ROOT / "reports" / "phase2_2_2e"
    if report_dir.exists():
        for path in sorted(report_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                errors.append(f"PUBLIC_JSON_INVALID:{path.name}")
                continue
            text = json.dumps(value, ensure_ascii=False)
            if ABSOLUTE_USER_PATH.search(text):
                errors.append(f"PUBLIC_ABSOLUTE_USER_PATH:{path.name}")
            stack = [value]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    for key, child in current.items():
                        if SENSITIVE_KEYS.search(str(key)):
                            errors.append(f"PUBLIC_SENSITIVE_KEY:{path.name}")
                        stack.append(child)
                elif isinstance(current, list):
                    stack.extend(current)
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in ("reports/private/", "/data/market_history/backfill_runs/"):
        if required not in gitignore:
            errors.append(f"GITIGNORE_RULE_MISSING:{required}")
    checks = (
        "reports/private/phase2_2_2e/macro_cache/sentinel.feed",
        "data/market_history/backfill_runs/phase222b-local-20261115/chunks/sentinel.csv",
    )
    for relative in checks:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative], cwd=ROOT,
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append(f"PRIVATE_PATH_NOT_IGNORED:{relative.split('/')[0]}")
    return errors


if __name__ == "__main__":
    findings = check()
    if findings:
        print(json.dumps({"status": "FAIL", "findings": findings}, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps({
        "status": "PASS", "reports_scanned": len(list((ROOT / "reports" / "phase2_2_2e").glob("*.json"))),
        "private_cache_excluded": True, "backfill_runs_excluded": True,
    }, sort_keys=True))
