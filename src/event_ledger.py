"""Append-only, offline-friendly event ledger for prospective model research.

Only hashes and an allowlisted technical snapshot are persisted. LLM request and
response bodies, credentials, account identifiers and free-form reasoning are
never stored. The ledger records decisions; it never executes orders.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import subprocess
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from src.phase2_1_evaluation import utc_timestamp


LEDGER_SCHEMA_VERSION = "2.0"
OPPORTUNITY_ID_VERSION = "agenttrading-opportunity-v1"
_CONTEXT: ContextVar[tuple[str, str, str | None, str | None, str | None] | None] = ContextVar("agenttrading_event_context", default=None)
LOGGER = logging.getLogger("agenttrading.ledger")
_SAFE_SNAPSHOT_FIELDS = {
    "symbol", "analysis_time", "timestamp", "direction", "bias", "decision",
    "strategy", "market_regime", "setup_quality", "overall_score", "alignment", "technical_decision", "technical_action",
    "timeframe_scores", "technical_evaluation", "supporting_evidence", "conflicting_evidence", "evidence_used",
    "session", "indicators", "support_resistance", "regime", "version",
    "risk_context_sha256", "risk_context_summary", "strategy_version", "data_version",
}
_SAFE_RISK_SUMMARY_FIELDS = {
    "risk_policy_version", "max_open_positions", "active_slots", "pending_count",
    "open_risk_pct_equity", "pending_risk_pct_equity", "daily_loss_used_pct",
    "currency_exposure_pct_equity", "proposal_risk_pct_equity", "risk_approved",
    "rejection_codes", "reservation_count", "margin_headroom_pct",
}
_SAFE_PLAN_FIELDS = {
    "symbol", "status", "direction", "strategy", "order_type", "entry",
    "stop_loss", "take_profit", "price_reference", "signal_time_utc",
    "spread_quality", "market_data_quality",
}
_SAFE_SYMBOL_METADATA_FIELDS = {
    "currency_base", "currency_profit", "currency_margin", "quote_currency",
    "account_currency", "trade_contract_size", "trade_tick_size",
    "trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss",
    "trade_tick_value_loss_currency", "volume_min", "volume_max", "volume_step",
    "point", "digits",
}
_SAFE_TRADE_CONTEXT_FIELDS = {
    "candidate_status", "candidate_rejection_codes", "plan_status",
    "risk_approved", "risk_rejection_codes", "market_data_quality",
    "context_known_at_utc", "risk_context_known_at_utc",
    "market_data_time_utc", "plan_strategy_source",
}
_SAFE_DECISION_FIELDS = {"decision", "action", "direction", "recommendation", "strategy", "market_regime", "confidence", "evidence_used", "evidence_ids"}


@dataclass(frozen=True)
class LedgerWriteResult:
    status: str
    persisted: bool
    eligible: bool
    event_id: str | None = None
    error_code: str | None = None


class LedgerPersistenceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def research_log_required() -> bool:
    return os.environ.get("RESEARCH_LOG_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _sqlite_error_code(exc: BaseException) -> str:
    if isinstance(exc, LedgerPersistenceError):
        return exc.code
    text = str(exc).lower()
    if "database is locked" in text or "database is busy" in text:
        return "LEDGER_SQLITE_BUSY"
    if "malformed" in text or "not a database" in text:
        return "LEDGER_DATABASE_CORRUPT"
    if "no such table" in text or "no such column" in text:
        return "LEDGER_SCHEMA_ERROR"
    if isinstance(exc, ValueError) and str(exc).startswith(("OUTCOME_", "EVENT_", "SNAPSHOT_")):
        return "LEDGER_DATA_VALIDATION_ERROR"
    if isinstance(exc, (TypeError, ValueError, UnicodeError)):
        return "LEDGER_SERIALIZATION_ERROR"
    return "LEDGER_WRITE_FAILED"


def _write_failed(event_id: str | None, exc: BaseException) -> LedgerWriteResult:
    code = _sqlite_error_code(exc)
    LOGGER.error("research ledger persistence failed status=%s event_id=%s", code, event_id or "MISSING")
    result = LedgerWriteResult("WRITE_FAILED", False, False, event_id, code)
    if research_log_required():
        raise LedgerPersistenceError(code) from None
    return result


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
            agent_role TEXT NOT NULL DEFAULT 'UNREGISTERED_SCHEMA',
            agent_stage TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
            decision_policy_id TEXT,
            schema_version TEXT NOT NULL DEFAULT '2.0',
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
            agent_role TEXT,
            decision_policy_id TEXT,
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
        CREATE TABLE IF NOT EXISTS coverage_events (
            coverage_event_id TEXT PRIMARY KEY,
            event_id TEXT REFERENCES opportunities(event_id),
            event_type TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            agent_role TEXT,
            decision_policy_id TEXT,
            payload_sha256 TEXT,
            status_code TEXT,
            eligible INTEGER NOT NULL DEFAULT 0 CHECK(eligible IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS trade_contexts (
            context_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES opportunities(event_id),
            context_type TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            context_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(event_id, context_type)
        );
        CREATE TRIGGER IF NOT EXISTS opportunities_no_update BEFORE UPDATE ON opportunities BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS opportunities_no_delete BEFORE DELETE ON opportunities BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS llm_events_no_update BEFORE UPDATE ON llm_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS llm_events_no_delete BEFORE DELETE ON llm_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS system_events_no_update BEFORE UPDATE ON system_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS system_events_no_delete BEFORE DELETE ON system_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS coverage_events_no_update BEFORE UPDATE ON coverage_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS coverage_events_no_delete BEFORE DELETE ON coverage_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS trade_contexts_no_update BEFORE UPDATE ON trade_contexts BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        CREATE TRIGGER IF NOT EXISTS trade_contexts_no_delete BEFORE DELETE ON trade_contexts BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
        """)
        migrations = {
            "llm_events": {
                "agent_role": "TEXT NOT NULL DEFAULT 'UNREGISTERED_SCHEMA'",
                "agent_stage": "TEXT NOT NULL DEFAULT 'UNCLASSIFIED'",
                "decision_policy_id": "TEXT",
                "schema_version": "TEXT NOT NULL DEFAULT '2.0'",
            },
            "system_events": {
                "agent_role": "TEXT",
                "decision_policy_id": "TEXT",
            },
        }
        for table, definitions in migrations.items():
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for column, definition in definitions.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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
    clean = {key: snapshot[key] for key in sorted(_SAFE_SNAPSHOT_FIELDS - {"risk_context_summary"}) if key in snapshot}
    risk_summary = snapshot.get("risk_context_summary")
    if isinstance(risk_summary, dict):
        clean["risk_context_summary"] = {
            key: risk_summary[key] for key in sorted(_SAFE_RISK_SUMMARY_FIELDS) if key in risk_summary
        }
    if isinstance(snapshot.get("plan"), dict):
        clean["plan"] = {
            key: snapshot["plan"][key] for key in sorted(_SAFE_PLAN_FIELDS)
            if key in snapshot["plan"]
        }
    if isinstance(snapshot.get("risk_context"), dict):
        clean["risk_context"] = _sanitize_risk_context(snapshot["risk_context"])
    return clean


def _sanitize_risk_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep only public market metadata needed for simulated risk sizing."""
    result: dict[str, Any] = {}
    currency = context.get("account_currency")
    if isinstance(currency, str) and len(currency) <= 8:
        result["account_currency"] = currency.upper()
    metadata = context.get("symbol_metadata")
    if isinstance(metadata, dict):
        clean_metadata = {}
        for symbol, values in metadata.items():
            if not isinstance(symbol, str) or not isinstance(values, dict):
                continue
            clean_metadata[symbol.upper()] = {
                key: values[key] for key in sorted(_SAFE_SYMBOL_METADATA_FIELDS)
                if key in values and isinstance(values[key], (str, int, float, bool, type(None)))
            }
        result["symbol_metadata"] = clean_metadata
    rates = context.get("conversion_rates")
    if isinstance(rates, dict):
        result["conversion_rates"] = {
            str(pair).upper(): value for pair, value in rates.items()
            if isinstance(pair, str) and isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    quality = context.get("market_data_quality")
    if isinstance(quality, dict):
        result["market_data_quality"] = {
            str(key): str(value)[:80] for key, value in quality.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
    return result


def sanitize_trade_context(context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise ValueError("TRADE_CONTEXT_INVALID")
    clean: dict[str, Any] = {}
    for key in ("candidate_status", "plan_status", "plan_strategy_source"):
        if isinstance(context.get(key), str):
            clean[key] = context[key][:100]
    for key in ("candidate_rejection_codes", "risk_rejection_codes"):
        value = context.get(key)
        if isinstance(value, (list, tuple)):
            clean[key] = [str(item)[:120] for item in value[:50]]
    if isinstance(context.get("risk_approved"), bool):
        clean["risk_approved"] = context["risk_approved"]
    quality = context.get("market_data_quality")
    if isinstance(quality, dict):
        clean["market_data_quality"] = {
            str(key)[:80]: str(value)[:80] for key, value in quality.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
    for key in ("context_known_at_utc", "risk_context_known_at_utc", "market_data_time_utc"):
        if context.get(key) is not None:
            clean[key] = utc_timestamp(context[key], field=key).isoformat().replace("+00:00", "Z")
    if isinstance(context.get("plan"), dict):
        plan = context["plan"]
        clean["plan"] = {key: plan[key] for key in sorted(_SAFE_PLAN_FIELDS) if key in plan}
    else:
        clean["plan"] = None
    if isinstance(context.get("risk_context"), dict):
        clean["risk_context"] = _sanitize_risk_context(context["risk_context"])
    return clean


def _insert_coverage(db: sqlite3.Connection, *, event_id: str | None, event_type: str,
                     agent_role: str | None = None, decision_policy_id: str | None = None,
                     payload_sha256: str | None = None, status_code: str | None = None,
                     eligible: bool = False) -> str:
    coverage_id = str(uuid4())
    db.execute(
        "INSERT INTO coverage_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (coverage_id, event_id, event_type, utc_now(), agent_role, decision_policy_id,
         payload_sha256, status_code, int(bool(eligible))),
    )
    return coverage_id


def record_opportunity(
    symbol: str,
    decision_time: Any,
    snapshot: dict[str, Any],
    *,
    opportunity_ordinal: int = 0,
    strategy_version: str | None = None,
    config_sha256: str | None = None,
    code_revision: str | None = None,
    eligible: bool | None = True,
    exclusion_reason: str | None = None,
    query_planned: bool = False,
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
        _insert_coverage(db, event_id=event_id, event_type="OPPORTUNITY_DETECTED", payload_sha256=digest)
        if eligible is True:
            _insert_coverage(db, event_id=event_id, event_type="OPPORTUNITY_ELIGIBLE", status_code="ELIGIBLE", eligible=True)
        elif eligible is False:
            _insert_coverage(db, event_id=event_id, event_type="OPPORTUNITY_EXCLUDED", status_code=(exclusion_reason or "EXCLUDED")[:120])
        if query_planned:
            from src.research_policy import CANONICAL_FILTER_ROLE, POLICY_VERSION
            _insert_coverage(db, event_id=event_id, event_type="LLM_QUERY_PLANNED", agent_role=CANONICAL_FILTER_ROLE,
                             decision_policy_id=POLICY_VERSION, status_code="PLANNED")
        db.commit()
    return event_id


def record_trade_context(event_id: str, context: dict[str, Any], *, context_type: str = "TRADE_PLAN_RISK_INPUT",
                         path: str | Path | None = None) -> LedgerWriteResult:
    """Append a sanitized, versioned plan/risk context; never stores account equity or positions."""
    db_path = Path(path) if path is not None else default_ledger_path()
    try:
        clean = sanitize_trade_context(context)
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        digest = sha256(encoded.encode("utf-8")).hexdigest()
        context_id = canonical_hash({"event_id": str(event_id), "context_type": str(context_type)})
        with _open_db(initialize_ledger(db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM opportunities WHERE event_id=?", (event_id,)).fetchone() is None:
                db.rollback()
                return LedgerWriteResult("MISSING_EVENT", False, False, event_id, "LEDGER_EVENT_CONTEXT_MISSING")
            prior = db.execute("SELECT payload_sha256 FROM trade_contexts WHERE event_id=? AND context_type=?",
                               (event_id, context_type)).fetchone()
            if prior:
                if prior[0] != digest:
                    db.rollback()
                    raise ValueError("TRADE_CONTEXT_PAYLOAD_CONFLICT")
                db.commit()
                return LedgerWriteResult("DUPLICATE_IDENTICAL", True, True, event_id)
            db.execute("INSERT INTO trade_contexts VALUES (?, ?, ?, ?, ?, ?, ?)", (
                context_id, event_id, str(context_type), LEDGER_SCHEMA_VERSION, encoded, digest, utc_now(),
            ))
            _insert_coverage(db, event_id=event_id, event_type="TRADE_CONTEXT_PERSISTED",
                             status_code=str(context_type), eligible=True)
            db.commit()
        return LedgerWriteResult("SAVED", True, True, event_id)
    except Exception as exc:
        return _write_failed(event_id, exc)


@contextmanager
def bind_event(event_id: str, path: str | Path | None = None, *, agent_role: str | None = None,
               agent_stage: str | None = None, decision_policy_id: str | None = None) -> Iterator[None]:
    token = _CONTEXT.set((str(event_id), str(path or default_ledger_path()), agent_role, agent_stage, decision_policy_id))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def record_coverage_event(event_type: str, *, event_id: str | None = None, agent_role: str | None = None,
                          decision_policy_id: str | None = None, payload: Any = None,
                          status_code: str | None = None, eligible: bool = False,
                          path: str | Path | None = None) -> LedgerWriteResult:
    db_path = Path(path) if path is not None else default_ledger_path()
    try:
        initialize_ledger(db_path)
        with _open_db(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if event_id and db.execute("SELECT 1 FROM opportunities WHERE event_id=?", (event_id,)).fetchone() is None:
                db.rollback()
                return LedgerWriteResult("MISSING_EVENT", False, False, event_id, "LEDGER_EVENT_CONTEXT_MISSING")
            _insert_coverage(db, event_id=event_id, event_type=str(event_type), agent_role=agent_role,
                             decision_policy_id=decision_policy_id,
                             payload_sha256=canonical_hash(payload) if payload is not None else None,
                             status_code=status_code, eligible=eligible)
            db.commit()
        return LedgerWriteResult("SAVED", True, bool(eligible), event_id)
    except Exception as exc:
        return _write_failed(event_id, exc)


def record_llm_query_started(*, role: str, policy_id: str | None, payload: Any = None) -> LedgerWriteResult:
    context = _CONTEXT.get()
    if context is None:
        record_coverage_event("LLM_QUERY_PLANNED", agent_role=role, decision_policy_id=policy_id,
                              payload=payload, status_code="MISSING_CONTEXT")
        result = record_coverage_event("LLM_QUERY_SENT", agent_role=role, decision_policy_id=policy_id,
                                       payload=payload, status_code="MISSING_CONTEXT")
        if research_log_required():
            raise LedgerPersistenceError("LEDGER_EVENT_CONTEXT_MISSING") from None
        return LedgerWriteResult("MISSING_CONTEXT", result.persisted, False, None, "LEDGER_EVENT_CONTEXT_MISSING")
    event_id, raw_path, bound_role, _stage, bound_policy = context
    if not bound_role:
        bound_role = role
    if research_log_required() and bound_role in {None, "UNREGISTERED_SCHEMA"}:
        raise LedgerPersistenceError("LEDGER_AGENT_ROLE_MISSING") from None
    effective_policy = bound_policy or policy_id
    planned = record_coverage_event("LLM_QUERY_PLANNED", event_id=event_id, agent_role=bound_role,
                                    decision_policy_id=effective_policy, payload=payload,
                                    status_code="PLANNED", path=raw_path)
    if research_log_required() and not planned.persisted:
        raise LedgerPersistenceError(planned.error_code or "LEDGER_QUERY_PLAN_NOT_PERSISTED") from None
    result = record_coverage_event("LLM_QUERY_SENT", event_id=event_id, agent_role=bound_role,
                                   decision_policy_id=effective_policy, payload=payload,
                                   status_code="SENT", path=raw_path)
    if research_log_required() and not result.persisted:
        raise LedgerPersistenceError(result.error_code or "LEDGER_QUERY_START_NOT_PERSISTED") from None
    return result


def _safe_decision(result: Any) -> dict[str, Any]:
    dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    return {key: dumped[key] for key in sorted(_SAFE_DECISION_FIELDS) if key in dumped and isinstance(dumped[key], (str, int, float, bool, list))}


def record_llm_call(*, schema_name: str, provider: str, model: str, prompt: Any, payload: Any,
                    allowed_evidence: list[str], requested_at: str, result: Any = None,
                    error_code: str | None = None, rejection_code: str | None = None,
                    elapsed_seconds: float | None = None, metadata: dict[str, Any] | None = None,
                    agent_role: str | None = None, agent_stage: str | None = None,
                    decision_policy_id: str | None = None) -> LedgerWriteResult:
    context = _CONTEXT.get()
    if context is None:
        return _write_failed(None, LedgerPersistenceError("LEDGER_EVENT_CONTEXT_MISSING"))
    event_id, raw_path, bound_role, bound_stage, bound_policy = context
    metadata = metadata or {}
    try:
        from src.research_policy import schema_role
        schema_agent_role, schema_stage, _is_filter = schema_role(type("SchemaRef", (), {"__module__": metadata.get("schema_module", ""), "__name__": schema_name}))
        role = agent_role or bound_role or metadata.get("agent_role") or schema_agent_role
        stage = agent_stage or bound_stage or metadata.get("agent_stage") or schema_stage
        policy = decision_policy_id or bound_policy or metadata.get("decision_policy_id")
        db_path = initialize_ledger(raw_path)
        with _open_db(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT 1 FROM opportunities WHERE event_id=?", (event_id,)).fetchone()
            if exists is None:
                db.rollback()
                return LedgerWriteResult("MISSING_EVENT", False, False, event_id, "LEDGER_EVENT_CONTEXT_MISSING")
            payload_hash = canonical_hash(payload)
            response_status = "VALID" if result is not None and error_code is None else "INVALID"
            db.execute("""INSERT INTO llm_events (
                llm_event_id,event_id,schema_name,requested_at_utc,responded_at_utc,provider,configured_model,
                reported_model,prompt_sha256,payload_sha256,allowed_evidence_json,decision_json,pydantic_status,
                rejection_code,latency_ms,prompt_tokens,completion_tokens,error_code,final_system_decision,
                agent_role,agent_stage,decision_policy_id,schema_version,order_executed,created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""", (
                str(uuid4()), event_id, schema_name, requested_at, utc_now(), provider, model,
                metadata.get("provider_reported_model"), canonical_hash(prompt), canonical_hash(payload),
                json.dumps(sorted(set(allowed_evidence))), json.dumps(_safe_decision(result), sort_keys=True) if result is not None else None,
                response_status, rejection_code or error_code,
                int(elapsed_seconds * 1000) if elapsed_seconds is not None else None,
                metadata.get("prompt_tokens"), metadata.get("completion_tokens"), error_code,
                metadata.get("final_system_decision"), role, stage, policy, LEDGER_SCHEMA_VERSION, utc_now(),
            ))
            _insert_coverage(db, event_id=event_id, event_type="LLM_RESPONSE_RECEIVED" if result is not None else "LLM_ERROR_RECEIVED",
                             agent_role=role, decision_policy_id=policy, payload_sha256=payload_hash,
                             status_code=response_status if result is not None else (error_code or "LLM_ERROR"),
                             eligible=response_status == "VALID" and role != "UNREGISTERED_SCHEMA")
            _insert_coverage(db, event_id=event_id, event_type="LLM_RESPONSE_PERSISTED", agent_role=role,
                             decision_policy_id=policy, payload_sha256=payload_hash,
                             status_code=response_status,
                             eligible=response_status == "VALID" and role != "UNREGISTERED_SCHEMA")
            db.commit()
        return LedgerWriteResult("SAVED", True, response_status == "VALID" and role != "UNREGISTERED_SCHEMA", event_id)
    except Exception as exc:
        return _write_failed(event_id, exc)


def record_system_decision(event_id: str, final_decision: str, *, validation_status: str,
                           decision_stage: str = "FINAL_SYSTEM",
                           rejection_code: str | None = None, agent_role: str | None = None,
                           decision_policy_id: str | None = None,
                           path: str | Path | None = None) -> LedgerWriteResult:
    db_path = Path(path) if path is not None else default_ledger_path()
    try:
        initialize_ledger(db_path)
        with _open_db(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM opportunities WHERE event_id=?", (event_id,)).fetchone() is None:
                db.rollback()
                return LedgerWriteResult("MISSING_EVENT", False, False, event_id, "LEDGER_EVENT_CONTEXT_MISSING")
            db.execute("""INSERT INTO system_events (
                system_event_id,event_id,created_at_utc,final_decision,decision_stage,
                validation_status,rejection_code,agent_role,decision_policy_id,order_executed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""", (
                str(uuid4()), event_id, utc_now(), str(final_decision), str(decision_stage),
                str(validation_status), rejection_code, agent_role, decision_policy_id,
            ))
            _insert_coverage(db, event_id=event_id, event_type="SYSTEM_DECISION_PERSISTED",
                             agent_role=agent_role, decision_policy_id=decision_policy_id,
                             status_code=str(validation_status), eligible=str(validation_status).upper() == "VALID")
            db.commit()
        return LedgerWriteResult("SAVED", True, str(validation_status).upper() == "VALID", event_id)
    except Exception as exc:
        return _write_failed(event_id, exc)


def append_outcome(event_id: str, known_at: Any, outcome: dict[str, Any], *, path: str | Path | None = None) -> str:
    timestamp = utc_timestamp(known_at, field="known_at").isoformat().replace("+00:00", "Z")
    safe_fields = (
        "result", "outcome_status", "net_r", "net_pnl", "net_pnl_eur", "gross_pnl_eur", "costs_eur",
        "cost_scenario", "ambiguous", "policy_id", "release_time_utc", "known_at_utc",
        "portfolio_accepted", "expired", "filled", "fill_time_utc", "expires_at_utc",
        "base_volume", "risk_context_sha256", "plan_sha256",
    )
    safe = {key: outcome[key] for key in safe_fields if key in outcome}
    safe["known_at_utc"] = timestamp
    outcome_id = canonical_hash({"event_id": event_id, "known_at_utc": timestamp,
                                 "policy_id": safe.get("policy_id"), "cost_scenario": safe.get("cost_scenario")})
    db_path = initialize_ledger(path)
    try:
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
            encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"))
            existing = db.execute("SELECT outcome_json FROM outcomes WHERE outcome_id=?", (outcome_id,)).fetchone()
            if existing:
                if existing[0] != encoded:
                    db.rollback()
                    raise ValueError("OUTCOME_ID_CONFLICT")
                db.commit()
                return outcome_id
            db.execute("INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?)", (
                outcome_id, event_id, timestamp, LEDGER_SCHEMA_VERSION, encoded, utc_now(),
            ))
            _insert_coverage(db, event_id=event_id, event_type="OUTCOME_PERSISTED",
                             status_code=safe.get("outcome_status", safe.get("result", "OUTCOME")), eligible=True)
            db.commit()
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        LOGGER.error("research outcome persistence failed status=%s event_id=%s", _sqlite_error_code(exc), event_id)
        if research_log_required():
            raise LedgerPersistenceError(_sqlite_error_code(exc)) from None
        raise
    return outcome_id


def reconcile_ledger(path: str | Path | None = None) -> dict[str, Any]:
    """Reconcile detected population through responses, decisions and outcomes."""
    db_path = Path(path) if path is not None else default_ledger_path()
    try:
        initialize_ledger(db_path)
        with _open_db(db_path) as db:
            counts = {row[0]: int(row[1]) for row in db.execute("SELECT event_type,COUNT(*) FROM coverage_events GROUP BY event_type")}
            opportunities = int(db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
            llm_rows = int(db.execute("SELECT COUNT(*) FROM llm_events").fetchone()[0])
            valid_responses = int(db.execute("SELECT COUNT(*) FROM llm_events WHERE pydantic_status='VALID'").fetchone()[0])
            persisted_decisions = int(db.execute("SELECT COUNT(*) FROM system_events").fetchone()[0])
            outcomes = int(db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
            outcome_events = int(db.execute("SELECT COUNT(DISTINCT event_id) FROM outcomes").fetchone()[0])
            trade_contexts = int(db.execute("SELECT COUNT(*) FROM trade_contexts").fetchone()[0])
            eligible_without_trade_context = int(db.execute("""
                SELECT COUNT(*) FROM coverage_events e
                WHERE e.event_type='OPPORTUNITY_ELIGIBLE' AND NOT EXISTS (
                    SELECT 1 FROM trade_contexts t WHERE t.event_id=e.event_id
                      AND t.context_type='TRADE_PLAN_RISK_INPUT'
                )
            """).fetchone()[0])
            planned_queries = int(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_type='LLM_QUERY_PLANNED'").fetchone()[0])
            sent_queries = int(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_type='LLM_QUERY_SENT'").fetchone()[0])
            missing_request_response = int(db.execute("""
                SELECT COUNT(*) FROM coverage_events c
                WHERE c.event_type='LLM_QUERY_SENT' AND NOT EXISTS (
                    SELECT 1 FROM llm_events l WHERE l.event_id=c.event_id AND l.agent_role=c.agent_role
                      AND l.payload_sha256=c.payload_sha256
                )
            """).fetchone()[0])
            response_without_decision = int(db.execute("""
                SELECT COUNT(DISTINCT l.event_id) FROM llm_events l
                WHERE l.pydantic_status='VALID' AND l.agent_role=? AND NOT EXISTS (
                    SELECT 1 FROM system_events s WHERE s.event_id=l.event_id AND s.decision_stage='MARKET_ANALYSIS'
                )
            """, ("MARKET_ANALYST_STRATEGY_FAMILY",)).fetchone()[0])
            orphan_coverage = int(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_id IS NULL").fetchone()[0])
        discrepancies = []
        if counts.get("OPPORTUNITY_DETECTED", 0) != opportunities:
            discrepancies.append("OPPORTUNITY_DETECTION_COUNT_MISMATCH")
        if counts.get("LLM_RESPONSE_PERSISTED", 0) != llm_rows:
            discrepancies.append("LLM_PERSISTENCE_COUNT_MISMATCH")
        if missing_request_response:
            discrepancies.append("SENT_QUERY_WITHOUT_PERSISTED_RESPONSE")
        if response_without_decision:
            discrepancies.append("VALID_FILTER_RESPONSE_WITHOUT_SYSTEM_DECISION")
        if orphan_coverage:
            discrepancies.append("ORPHAN_COVERAGE_EVENTS")
        if eligible_without_trade_context:
            discrepancies.append("ELIGIBLE_OPPORTUNITY_WITHOUT_TRADE_CONTEXT")
        if planned_queries < sent_queries:
            discrepancies.append("SENT_QUERY_WITHOUT_PLANNED_COVERAGE")
        return {
            "status": "PASS" if not discrepancies else "FAIL",
            "counts": {
                "opportunities_detected": opportunities,
                "opportunities_eligible": counts.get("OPPORTUNITY_ELIGIBLE", 0),
                "opportunities_excluded": counts.get("OPPORTUNITY_EXCLUDED", 0),
                "queries_planned": counts.get("LLM_QUERY_PLANNED", 0),
                "queries_sent": counts.get("LLM_QUERY_SENT", 0),
                "responses_received": counts.get("LLM_RESPONSE_RECEIVED", 0),
                "provider_errors": counts.get("LLM_ERROR_RECEIVED", 0),
                "responses_persisted": llm_rows,
                "responses_valid": valid_responses,
                "system_decisions_persisted": persisted_decisions,
                "outcomes_persisted": outcomes,
                "outcomes_pending": max(0, opportunities - outcome_events),
                "trade_contexts_persisted": trade_contexts,
                "eligible_without_trade_context": eligible_without_trade_context,
                "events_without_traceability": orphan_coverage,
                "sent_queries_without_response": missing_request_response,
                "valid_filter_responses_without_system_decision": response_without_decision,
            },
            "discrepancies": discrepancies,
            "confirmatory_eligible": not bool(discrepancies) and orphan_coverage == 0 and outcome_events == opportunities,
            "provider_calls_or_orders_created_by_reconciliation": 0,
        }
    except Exception as exc:
        code = _sqlite_error_code(exc)
        LOGGER.error("research ledger reconciliation failed status=%s", code)
        return {"status": "FAIL", "counts": {}, "discrepancies": [code], "confirmatory_eligible": False,
                "provider_calls_or_orders_created_by_reconciliation": 0}


def export_anonymized_ledger(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return analysis rows with event IDs rehashed and no account/credential fields."""
    db_path = Path(path) if path is not None else default_ledger_path()
    with _open_db(initialize_ledger(db_path)) as db:
        rows = db.execute("""
            SELECT o.event_id,o.symbol,o.decision_time_utc,o.snapshot_json,
                   l.agent_role,l.provider,l.configured_model,l.prompt_sha256,l.payload_sha256,
                   l.pydantic_status,l.error_code,l.decision_json,
                   s.final_decision,s.validation_status
            FROM opportunities o
            LEFT JOIN llm_events l ON l.event_id=o.event_id
            LEFT JOIN system_events s ON s.event_id=o.event_id AND s.decision_stage='MARKET_ANALYSIS'
            ORDER BY o.decision_time_utc,o.symbol,o.event_id
        """).fetchall()
    exported = []
    for row in rows:
        snapshot = json.loads(row[3] or "{}")
        exported.append({
            "event_id_sha256": sha256(str(row[0]).encode("utf-8")).hexdigest(),
            "symbol": row[1], "decision_time_utc": row[2],
            "technical_decision": snapshot.get("technical_decision"),
            "agent_role": row[4], "provider": row[5], "model": row[6],
            "prompt_sha256": row[7], "payload_sha256": row[8],
            "validation_status": row[9], "error_code": row[10],
            "llm_decision_json": row[11], "system_decision": row[12],
            "system_validation_status": row[13],
        })
    return exported
