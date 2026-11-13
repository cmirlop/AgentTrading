import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import validate_phase1 as validator


class ValidationRunnerTests(unittest.TestCase):
    def test_omitted_tests_report_non_applicability_without_claiming_pass(self):
        superseded = validator._omitted_test_result("test_ollama.py", {"ollama": "PASS"})
        self.assertEqual(superseded["status"], "SUPERSEDED_BY_OLLAMA_LIVE_PASS")
        self.assertFalse(superseded["command_executed"])
        for name in ("test_order_request.py", "test_trade_planner.py"):
            result = validator._omitted_test_result(name, {"mt5": "PASS"})
            self.assertEqual(result["status"], "NOT_RUN_READ_ONLY_NO_DEMO")
            self.assertFalse(result["command_executed"])
            self.assertNotEqual(result["status"], "PASS")

    def test_missing_live_provider_evidence_stays_pending(self):
        result = validator._omitted_test_result("test_ollama.py", {"ollama": "PENDING"})
        self.assertEqual(result["status"], "PENDING")

    def test_command_trace_does_not_claim_reused_backtests_or_deepseek_were_run_now(self):
        trace = validator._command_provenance(
            ["python", "scripts/validate_phase1.py"],
            [["python", "unit-test"]],
            ["python", "-c", "imports"],
            {
                "evidence_reused": True,
                "result_origin": "REUSED_VERIFIED_HISTORICAL_BACKTEST_EVIDENCE",
                "scenario_runs": {"portfolio_base_1": {"command": ["python", "backtest"]}},
                "evidence_reuse": {
                    "source_validation_commit": "abc123",
                    "source_generated_at_utc": "2026-11-13T10:00:00+00:00",
                    "fingerprint_sha256": "f" * 64,
                },
            },
            {
                "deepseek": {
                    "status": "PASS",
                    "evidence_reused": True,
                    "result_origin": "REUSED_VERIFIED_EXTERNAL_EVIDENCE",
                    "report": "reports/phase1_llm_live_deepseek.json",
                    "age_seconds": 60,
                    "origin_validation_commit": "def456",
                    "source_fingerprint_sha256": "a" * 64,
                    "command_executed": ["python", "deepseek", "--authorize-deepseek"],
                },
            },
        )
        self.assertNotIn(["python", "backtest"], trace["commands_executed"])
        self.assertNotIn(["python", "deepseek", "--authorize-deepseek"], trace["commands_executed"])
        self.assertTrue(trace["commands_reused"]["historical_backtests"]["reused"])
        self.assertEqual(trace["commands_reused"]["historical_backtests"]["scenario_names"], ["portfolio_base_1"])
        self.assertEqual(trace["commands_reused"]["external_integrations"][0]["provider"], "deepseek")
        self.assertFalse(trace["commands_reused"]["external_integrations"][0]["command_executed_this_run"])

    def test_streams_progress_and_redacts_secrets_and_account_identifiers(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = validator._run(
                [sys.executable, "-c", "print('token=private-value'); print('account_id=99887766')"],
                timeout=8,
                idle_timeout=3,
                phase="privacy_runner_test",
            )
        text = output.getvalue()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["execution_state"], "COMPLETED")
        self.assertIn("START", text)
        self.assertIn("PROGRESS", text)
        self.assertIn("END status=PASS", text)
        for secret in ("private-value", "99887766"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, result["stdout_tail"])
        self.assertGreaterEqual(result["output_line_count"], 2)

    def test_hang_is_distinguished_and_owned_child_tree_is_terminated(self):
        if os.name != "nt":
            self.skipTest("Windows process-tree termination is platform-specific.")
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "child.pid"
            child_code = "import time; time.sleep(20)"
            root_code = (
                "import subprocess,sys,time,pathlib; "
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid)); "
                "print('child-started',flush=True); time.sleep(20)"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = validator._run(
                    [sys.executable, "-c", root_code],
                    timeout=10,
                    idle_timeout=0.6,
                    phase="owned_process_tree_test",
                )
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["execution_state"], "HUNG")
            self.assertTrue(result["process_tree_terminated"])
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, child_pid)
            self.assertFalse(handle, "The owned descendant must be terminated with the root process.")

    def test_timeout_without_output_is_separate_from_idle_hang(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = validator._run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                timeout=0.4,
                idle_timeout=None,
                phase="deadline_timeout_test",
            )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["execution_state"], "TIMEOUT")
        self.assertTrue(result["process_tree_terminated"])

    def test_large_child_output_has_a_bounded_report_tail(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = validator._run(
                [sys.executable, "-c", "[print('line-%d' % i) for i in range(1000)]"],
                timeout=8,
                idle_timeout=3,
                phase="bounded_output_test",
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["output_line_count"], 1000)
        self.assertLessEqual(len(result["stdout_tail"]), 1200)

    def test_console_progress_replaces_characters_outside_windows_code_page(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252")
        with patch.object(validator.sys, "stdout", stream):
            validator._progress_print("progreso con reemplazo \ufffd y acento á")
        self.assertIn(b"progreso con reemplazo ? y acento", buffer.getvalue())

    def test_keyboard_interrupt_writes_pending_report_and_stops_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "reports" / "phase1_validation.json"
            with patch.object(validator, "ROOT", root), patch.object(validator, "REPORT_PATH", report_path), patch.object(
                validator.queue.Queue, "get", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    validator._run(
                        [sys.executable, "-c", "import time; time.sleep(10)"],
                        timeout=8,
                        phase="interrupt_state_test",
                    )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["overall_status"], "INCOMPLETE_PENDING_MANUAL")
            self.assertEqual(report["execution_state"], "INTERRUPTED")
            self.assertEqual(report["interrupted_phase"], "interrupt_state_test")
            self.assertNotEqual(report["overall_status"], "PHASE1_COMPLETE")


if __name__ == "__main__":
    unittest.main()
