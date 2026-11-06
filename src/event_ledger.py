"""Append-only, offline-friendly event ledger for prospective model research.

Only hashes and an allowlisted technical snapshot are persisted. LLM request and
response bodies, credentials, account identifiers and free-form reasoning are
never stored. The ledger records decisions; it never executes orders.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from src.phase2_1_evaluation import utc_timestamp


LEDGER_SCHEMA_VERSION = "1.0"
OPPORTUNITY_ID_VERSION = "agenttrading-opportunity-v1"
_CONTEXT: ContextVar[tuple[str, str] | None] = ContextVar("agenttrading_event_context", default=None)
_SAFE_SNAPSHOT_FIELDS = {
    "symbol", "analysis_time", "timestamp", "direction", "bias", "decision",
    "strategy", "market_regime", "setup_quality", "overall_score", "alignment", "technical_decision", "technical_action",
    "timeframe_scores", "technical_evaluation", "supporting_evidence", "conflicting_evidence", "evidence_used",
    "session", "indicators", "support_resistance", "regime", "version",
}
_SAFE_DECISION_FIELDS = {"decision", "action", "direction", "recommendation", "strategy", "market_regime", "confidence", "evidence_used", "evidence_ids", "thesis"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return sha256(blob.encode("utf-8")).hexdigest()


def default_ledger_path() -> Path:
    configured = os.environ.get("AGENTTRADING_EVENT_DB")
    return Path(configured) if configured else Path(__file__).resolve().parents[1] / "data" / "events" / "agenttrading_events.sqlite3"


def runtime_code_revision(root: str | Path | None = None) -> str | None:
    repository = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if head.returncode:
        return None
    code_paths = ["src", "config", "scripts", "tests", ":(glob)*.py"]
    status = subprocess.run(["git", "status", "--porcelain", "--", *code_paths], cwd=repository, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if status.returncode or not status.stdout.strip():
        return head.stdout.strip()
    diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--", *code_paths], cwd=repository, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=repository, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    hasher = sha256(status.stdout.encode("utf-8") + diff.stdout)
    for name in sorted(untracked.stdout.splitlines() if untracked.returncode == 0 else []):
        path = repository / name
        if path.is_file() and path.suffix.lower() == ".py" and (name.startswith(("src/", "scripts/", "tests/", "config/")) or "/" not in name):
            hasher.update(name.encode("utf-8"))
            hasher.update(path.read_bytes())
    digest = hasher.hexdigest()
    return f"{head.stdout.strip()}+working-tree:{digest}"


def runtime_config_sha256(root: str | Path | None = None) -> str | None:
    config_dir = (Path(root) if root is not None else Path(__file__).resolve().parents[1]) / "config"
    if not config_dir.is_dir():
        return None
    digest = sha256()
    for path in sorted(config_dir.rglob("*.py")):
        digest.update(path.relative_to(config_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def _open_db(path: str | Path) -> Iterator[sqlite3.Connection]:
    db = _connect(path)
    try:
        yield db
    finally:
        db.close()


def initialize_ledger(path: str | Path | None = None) -> Path:
    db_path = Path(path) if path is not None else default_ledger_path()
    with _open_db(db_path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS opportunities (
            event_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            opportunity_id_version TEXT NOT NULL,
            symbol TEXT NOT NULL,
            decision_time_utc TEXT NOT NULL,
            opportunity_ordinal INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            strategy_version TEXT,
            config_sha256 TEXT,
            code_revision TEXT,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_events (
            llm_event_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES opportunities(event_id),
            schema_name TEXT NOT NULL,
            requested_at_utc TEXT NOT NULL,
            responded_at_utc TEXT NOT NULL,
            provider TEXT NOT NULL,
            configured_model TEXT NOT NULL,
            reported_model TEXT,
            prompt_sha256 TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            allowed_evidence_json TEXT NOT NULL,
            decision_json TEXT,
            pydantic_status TEXT NOT NULL,
            rejection_code TEXT,
            latency_ms INTEGER,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            error_code TEXT,
            final_system_decision TEXT,
            order_executed INTEGER NOT NULL DEFAULT 0 CHECK(order_executed=0),
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_events (
            system_event_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES opportunities(event_id),
            created_at_utc TEXT NOT NULL,
            final_decision TEXT NOT NULL,
            decision_stage TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            rejection_code TEXT,
            order_executed INTEGER NOT NULL DEFAULT 0 CHECK(order_executed=0)
        );
        CREATE TABLE IF NOT EXISTS outcomes (
            outcome_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES opportunities(event_id),
            known_at_utc TEXT NOT NULL,
            outcome_schema_version TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS opportunities_no_update BEFORE UPDATE ON opportunities BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS opportunities_no_delete BEFORE DELETE ON opportunities BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS llm_events_no_update BEFORE UPDATE ON llm_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS llm_events_no_delete BEFORE DELETE ON llm_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS system_events_no_update BEFORE UPDATE ON system_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS system_events_no_delete BEFORE DELETE ON system_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        """)
    return db_path


def make_event_id(symbol: str, decision_time: Any, opportunity_ordinal: int = 0) -> tuple[str, str]:
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("EVENT_SYMBOL_MISSING")
    if isinstance(opportunity_ordinal, bool) or int(opportunity_ordinal) < 0:
        raise ValueError("EVENT_ORDINAL_INVALID")
    timestamp = utc_timestamp(decision_time, field="decision_time").isoformat().replace("+00:00", "Z")
    value = {"version": OPPORTUNITY_ID_VERSION, "symbol": normalized_symbol, "decision_time_utc": timestamp, "ordinal": int(opportunity_ordinal)}
    return canonical_hash(value), timestamp


def sanitize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("SNAPSHOT_INVALID")
    return {key: snapshot[key] for key in sorted(_SAFE_SNAPSHOT_FIELDS) if key in snapshot}


def record_opportunity(
    symbol: str,
    decision_time: Any,
    snapshot: dict[str, Any],
    *,
    opportunity_ordinal: int = 0,
    strategy_version: str | None = None,
    config_sha256: str | None = None,
    code_revision: str | None = None,
    path: str | Path | None = None,
) -> str:
    event_id, timestamp = make_event_id(symbol, decision_time, opportunity_ordinal)
    if config_sha256 is None:
        config_sha256 = runtime_config_sha256()
    if code_revision is None:
        code_revision = runtime_code_revision()
    clean = sanitize_snapshot(snapshot)
    clean["symbol"] = str(symbol).strip().upper()
    clean["analysis_time"] = timestamp
    snapshot_json = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    db_path = initialize_ledger(path)
    with _open_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload_sha256 FROM opportunities WHERE event_id=?", (event_id,)).fetchone()
        digest = sha256(snapshot_json.encode("utf-8")).hexdigest()
        if old is not None:
            if old[0] != digest:
                db.rollback()
                raise ValueError("EVENT_ID_PAYLOAD_CONFLICT")
            db.commit()
            return event_id
        db.execute("""INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            event_id, LEDGER_SCHEMA_VERSION, OPPORTUNITY_ID_VERSION, clean["symbol"], timestamp,
            int(opportunity_ordinal), snapshot_json, digest, strategy_version, config_sha256,
            code_revision, utc_now(),
        ))
        db.commit()
    return event_id


@contextmanager
def bind_event(event_id: str, path: str | Path | None = None) -> Iterator[None]:
    token = _CONTEXT.set((str(event_id), str(path or default_ledger_path())))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _safe_decision(result: Any) -> dict[str, Any]:
    dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    return {key: dumped[key] for key in sorted(_SAFE_DECISION_FIELDS) if key in dumped and isinstance(dumped[key], (str, int, float, bool, list))}


def record_llm_call(*, schema_name: str, provider: str, model: str, prompt: Any, payload: Any,
                    allowed_evidence: list[str], requested_at: str, result: Any = None,
                    error_code: str | None = None, rejection_code: str | None = None,
                    elapsed_seconds: float | None = None, metadata: dict[str, Any] | None = None) -> None:
    context = _CONTEXT.get()
    if context is None:
        return
    event_id, raw_path = context
    metadata = metadata or {}
    try:
        db_path = initialize_ledger(raw_path)
        with _open_db(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM opportunities WHERE event_id=?", (event_id,)).fetchone()
            if exists is None:
                db.rollback()
                return
            db.execute("""INSERT INTO llm_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""", (
                str(uuid4()), event_id, schema_name, requested_at, utc_now(), provider, model,
                metadata.get("provider_reported_model"), canonical_hash(prompt), canonical_hash(payload),
                json.dumps(sorted(set(allowed_evidence))), json.dumps(_safe_decision(result), sort_keys=True) if result is not None else None,
                "VALID" if result is not None and error_code is None else "INVALID", rejection_code,
                int(elapsed_seconds * 1000) if elapsed_seconds is not None else None,
                metadata.get("prompt_tokens"), metadata.get("completion_tokens"), error_code,
                metadata.get("final_system_decision"), utc_now(),
            ))
            db.commit()
    except Exception:
        # Observability must never change the result of a trading analysis call.
        return


def record_system_decision(event_id: str, final_decision: str, *, validation_status: str,
                           decision_stage: str = "FINAL_SYSTEM",
                           rejection_code: str | None = None, path: str | Path | None = None) -> None:
    db_path = initialize_ledger(path)
    with _open_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO system_events VALUES (?, ?, ?, ?, ?, ?, ?, 0)", (
            str(uuid4()), event_id, utc_now(), str(final_decision), str(decision_stage), str(validation_status), rejection_code,
        ))
        db.commit()


def append_outcome(event_id: str, known_at: Any, outcome: dict[str, Any], *, path: str | Path | None = None) -> str:
    timestamp = utc_timestamp(known_at, field="known_at").isoformat().replace("+00:00", "Z")
    safe_fields = (
        "result", "net_r", "net_pnl", "net_pnl_eur", "cost_scenario", "ambiguous",
        "policy_id", "release_time_utc", "portfolio_accepted", "expired", "filled",
    )
    safe = {key: outcome[key] for key in safe_fields if key in outcome}
    outcome_id = str(uuid4())
    db_path = initialize_ledger(path)
    with _open_db(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        opportunity = db.execute("SELECT decision_time_utc FROM opportunities WHERE event_id=?", (event_id,)).fetchone()
        if not opportunity:
            db.rollback()
            raise ValueError("OUTCOME_EVENT_NOT_FOUND")
        decision = utc_timestamp(opportunity[0], field="decision_time")
        outcome_time = utc_timestamp(timestamp, field="known_at")
        if outcome_time < decision:
            db.rollback()
            raise ValueError("OUTCOME_BEFORE_DECISION")
        db.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?)", (
            outcome_id, event_id, timestamp, LEDGER_SCHEMA_VERSION,
            json.dumps(safe, sort_keys=True, separators=(",", ":")), utc_now(),
        ))
        db.commit()
    return outcome_id
