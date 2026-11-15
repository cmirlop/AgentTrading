"""Run only the bounded, offline Phase 2.2.2d validation suites.

This runner never imports or connects to MetaTrader, Ollama, or an LLM SDK.
Each suite prints a progress marker and its output is streamed as it runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from queue import Empty, Queue
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "phase2_2_2d" / "offline_validation_execution.json"
SUITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "phase2_2_2_abcd_offline",
        ("-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_2_2*.py"),
    ),
    (
        "phase1_historical_simulator",
        ("-m", "unittest", "discover", "-s", "tests", "-p", "test_phase1_backtest.py"),
    ),
    (
        "phase1_mt5_readonly_safety",
        ("-m", "unittest", "tests.test_mt5_real_readonly", "tests.test_phase1_mt5_safety"),
    ),
    ("no_lookahead", ("test_no_lookahead.py",)),
)


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args), cwd=ROOT, check=True, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _run(name: str, argv: tuple[str, ...]) -> dict[str, Any]:
    command = [sys.executable, *argv]
    started = datetime.now(timezone.utc)
    print(f"[phase2_2_2d] START {name}", flush=True)
    started_clock = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        return {
            "name": name,
            "command": ["python", *argv],
            "status": "FAIL",
            "exit_code": 127,
            "error_type": type(exc).__name__,
            "tests_run": 0,
            "tests_failed": None,
            "elapsed_seconds": round(time.monotonic() - started_clock, 3),
        }

    output_lines: list[str] = []
    assert process.stdout is not None
    lines: Queue[str | None] = Queue()

    def collect_output() -> None:
        assert process.stdout is not None
        for output_line in process.stdout:
            lines.put(output_line)
        lines.put(None)

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()
    stream_closed = False
    while not stream_closed or process.poll() is None:
        try:
            line = lines.get(timeout=15)
            if line is None:
                stream_closed = True
            else:
                print(line, end="", flush=True)
                output_lines.append(line.rstrip("\r\n"))
        except Empty:
            elapsed = round(time.monotonic() - started_clock)
            print(f"[phase2_2_2d] PROGRESS {name}: running after {elapsed}s", flush=True)
    reader.join(timeout=1)
    exit_code = process.wait()
    combined = "\n".join(output_lines)
    ran = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s", combined)
    custom_checks = re.search(r"Checks realizados:\s*(\d+)", combined, re.IGNORECASE)
    failures = re.search(r"FAILED\s*\(([^)]*)\)", combined)
    skipped = re.search(r"skipped=(\d+)", combined)
    tests_run = int(ran.group(1)) if ran else 0
    tests_failed = 0 if exit_code == 0 else None
    if failures:
        failure_count = re.search(r"failures=(\d+)", failures.group(1))
        error_count = re.search(r"errors=(\d+)", failures.group(1))
        tests_failed = sum(int(item.group(1)) for item in (failure_count, error_count) if item)
    elif exit_code != 0:
        tests_failed = 1
    check_output_ok = name != "no_lookahead" or (
        custom_checks is not None and "[OK] sin look-ahead" in combined
    )
    status = "PASS" if exit_code == 0 and (ran or check_output_ok) else "FAIL"
    print(f"[phase2_2_2d] END {name}: {status} (exit {exit_code})", flush=True)
    return {
        "name": name,
        "command": ["python", *argv],
        "status": status,
        "exit_code": exit_code,
        "tests_run": tests_run,
        "checks_run": int(custom_checks.group(1)) if custom_checks else 0,
        "tests_failed": tests_failed,
        "tests_skipped": int(skipped.group(1)) if skipped else 0,
        "elapsed_seconds": round(time.monotonic() - started_clock, 3),
        "started_utc": started.isoformat().replace("+00:00", "Z"),
    }


def main() -> int:
    results = []
    for name, argv in SUITES:
        result = _run(name, argv)
        results.append(result)
        if result["status"] != "PASS":
            break
    all_pass = len(results) == len(SUITES) and all(item["status"] == "PASS" for item in results)
    report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "interpreter": "project_virtual_environment" if ".venv" in sys.executable else "unverified_interpreter",
        "python_version": sys.version.split()[0],
        "repository": {"branch": _git("branch", "--show-current"), "head": _git("rev-parse", "HEAD")},
        "status": "PASS" if all_pass else "FAIL",
        "tests_run": sum(item["tests_run"] for item in results),
        "checks_run": sum(item["checks_run"] for item in results),
        "tests_failed": sum(item["tests_failed"] or 0 for item in results),
        "tests_skipped": sum(item.get("tests_skipped", 0) for item in results),
        "suites": results,
        "scope": {
            "mt5_connections": 0,
            "mt5_history_reads": 0,
            "orders_sent": 0,
            "llm_calls": 0,
            "historical_backtests": 0,
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_name(f".{REPORT.name}.tmp")
    payload = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
    temporary.replace(REPORT)
    print(f"[phase2_2_2d] REPORT {REPORT.relative_to(ROOT).as_posix()}", flush=True)
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
