"""Versioned source contract shared by verified scan and backtest adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from config.market_config import SYMBOLS, TIMEFRAMES
from src.history_data_integrity import CANONICAL_HASH_POLICY
from src.phase2_2_2d_sessions import validate_session_manifest
from src.verified_history_scan import (
    load_verified_history_frames,
    scan_symbol_from_verified_frames,
    validate_history_source_manifest,
)


FRAME_COLUMNS = ("time", "bar_close", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")


def _timestamp(value: datetime | str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise ValueError("DATASET_TIMESTAMP_MUST_BE_AWARE")
    return parsed.tz_convert("UTC")


def _frame_sha256(frame: pd.DataFrame) -> str:
    missing = set(FRAME_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError("DATASET_REQUIRED_FRAME_COLUMNS_MISSING")
    ordered = frame.loc[:, FRAME_COLUMNS].copy()
    ordered["time"] = pd.to_datetime(ordered["time"], utc=True).map(lambda x: x.isoformat())
    ordered["bar_close"] = pd.to_datetime(ordered["bar_close"], utc=True).map(lambda x: x.isoformat())
    rows = ordered.astype(object).where(pd.notna(ordered), None).to_dict(orient="records")
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass
class VerifiedResearchDataset:
    frames_by_symbol: dict[str, dict[str, pd.DataFrame]]
    session_manifest: dict[str, Any]
    public_manifest: dict[str, Any]
    source_lineage: dict[str, Any]
    _synthetic_test_only: bool = False

    @classmethod
    def load_from_sources(
        cls,
        root: Path,
        source_manifest: dict[str, Any],
        session_manifest: dict[str, Any],
        *,
        warmup_start_utc: datetime | str,
        evaluation_start_utc: datetime | str,
        evaluation_end_utc: datetime | str,
        observation_end_utc: datetime | str,
        as_of_utc: datetime | str,
        strategy_version: str,
        spread_policy: str = "HISTORICAL_BID_ASK_EMBEDDED",
        cost_scenario: str = "BASE",
    ) -> "VerifiedResearchDataset":
        validate_history_source_manifest(source_manifest)
        periods = {
            "warmup_start_utc": _timestamp(warmup_start_utc),
            "evaluation_start_utc": _timestamp(evaluation_start_utc),
            "evaluation_end_utc": _timestamp(evaluation_end_utc),
            "observation_end_utc": _timestamp(observation_end_utc),
            "as_of_utc": _timestamp(as_of_utc),
        }
        if not (
            periods["warmup_start_utc"] < periods["evaluation_start_utc"]
            < periods["evaluation_end_utc"] <= periods["observation_end_utc"]
            <= periods["as_of_utc"]
        ):
            raise ValueError("DATASET_PERIODS_INVALID")
        frames_by_symbol, evidence = load_verified_history_frames(
            root, source_manifest, session_manifest,
            evaluation_start_utc=periods["evaluation_start_utc"].isoformat(),
            evaluation_end_utc=periods["evaluation_end_utc"].isoformat(),
            as_of_utc=periods["as_of_utc"].isoformat(),
        )
        return cls._build(
            frames_by_symbol, session_manifest, source_manifest, evidence, periods,
            strategy_version=strategy_version, spread_policy=spread_policy,
            cost_scenario=cost_scenario, synthetic=False,
        )

    @classmethod
    def synthetic_test_dataset(
        cls,
        frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
        session_manifest: dict[str, Any],
        *,
        warmup_start_utc: datetime | str,
        evaluation_start_utc: datetime | str,
        evaluation_end_utc: datetime | str,
        observation_end_utc: datetime | str,
        as_of_utc: datetime | str,
    ) -> "VerifiedResearchDataset":
        """Test helper only: this can never produce real-data eligibility."""
        periods = {
            "warmup_start_utc": _timestamp(warmup_start_utc),
            "evaluation_start_utc": _timestamp(evaluation_start_utc),
            "evaluation_end_utc": _timestamp(evaluation_end_utc),
            "observation_end_utc": _timestamp(observation_end_utc),
            "as_of_utc": _timestamp(as_of_utc),
        }
        if not (
            periods["warmup_start_utc"] < periods["evaluation_start_utc"]
            < periods["evaluation_end_utc"] <= periods["observation_end_utc"]
            <= periods["as_of_utc"]
        ):
            raise ValueError("DATASET_PERIODS_INVALID")
        source_stub = {
            "schema_version": 1, "source_manifest_sha256": "SYNTHETIC_TEST_ONLY",
            "run_id": "synthetic-test", "source_count": 0,
        }
        evidence = {"closed_rows_accepted": 0, "provenance": {}}
        return cls._build(
            frames_by_symbol, session_manifest, source_stub, evidence, periods,
            strategy_version="synthetic-test-only", spread_policy="SYNTHETIC",
            cost_scenario="BASE", synthetic=True,
        )

    @classmethod
    def _build(
        cls,
        frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
        session_manifest: dict[str, Any],
        source_manifest: dict[str, Any],
        source_evidence: dict[str, Any],
        periods: dict[str, pd.Timestamp],
        *,
        strategy_version: str,
        spread_policy: str,
        cost_scenario: str,
        synthetic: bool,
    ) -> "VerifiedResearchDataset":
        if not isinstance(frames_by_symbol, dict):
            raise ValueError("DATASET_FRAMES_INVALID")
        if set(frames_by_symbol) != set(SYMBOLS):
            raise ValueError("DATASET_SYMBOL_COVERAGE_MISMATCH")
        clean_frames: dict[str, dict[str, pd.DataFrame]] = {}
        frame_hashes: dict[str, dict[str, str]] = {}
        counts: dict[str, dict[str, int]] = {}
        excluded_partial: dict[str, dict[str, int]] = {}
        phase_counts: dict[str, dict[str, dict[str, int]]] = {}
        for symbol in SYMBOLS:
            if set(frames_by_symbol[symbol]) != set(TIMEFRAMES):
                raise ValueError("DATASET_TIMEFRAME_COVERAGE_MISMATCH")
            clean_frames[symbol] = {}
            frame_hashes[symbol] = {}
            counts[symbol] = {}
            excluded_partial[symbol] = {}
            phase_counts[symbol] = {}
            for timeframe in TIMEFRAMES:
                frame = frames_by_symbol[symbol][timeframe].copy()
                if set(FRAME_COLUMNS) - set(frame.columns):
                    raise ValueError(f"DATASET_FRAME_COLUMNS_MISSING:{symbol}:{timeframe}")
                frame["time"] = pd.to_datetime(frame["time"], utc=True, errors="raise")
                frame["bar_close"] = pd.to_datetime(frame["bar_close"], utc=True, errors="raise")
                if frame["time"].duplicated().any() or not frame["time"].is_monotonic_increasing:
                    raise ValueError(f"DATASET_TIME_ORDER_OR_DUPLICATE:{symbol}:{timeframe}")
                if frame["bar_close"].isna().any() or not frame["bar_close"].is_monotonic_increasing:
                    raise ValueError(f"DATASET_BAR_CLOSE_INVALID:{symbol}:{timeframe}")
                # Preserve pre-evaluation warmup, evaluation and observation rows;
                # remove any bar not known closed at the as-of instant.
                eligible = frame["bar_close"] <= periods["as_of_utc"]
                excluded_partial[symbol][timeframe] = int((~eligible).sum())
                in_window = (
                    eligible
                    & (frame["bar_close"] >= periods["warmup_start_utc"])
                )
                frame = frame.loc[in_window].reset_index(drop=True)
                if frame.empty:
                    raise ValueError(f"DATASET_NO_CLOSED_BARS:{symbol}:{timeframe}")
                clean_frames[symbol][timeframe] = frame
                frame_hashes[symbol][timeframe] = _frame_sha256(frame)
                counts[symbol][timeframe] = len(frame)
                closes = frame["bar_close"]
                phase_counts[symbol][timeframe] = {
                    "warmup": int((closes < periods["evaluation_start_utc"]).sum()),
                    "evaluation": int(closes.between(
                        periods["evaluation_start_utc"], periods["evaluation_end_utc"],
                        inclusive="both",
                    ).sum()),
                    "observation": int((
                        (closes > periods["evaluation_end_utc"])
                        & (closes <= periods["observation_end_utc"])
                    ).sum()),
                }

        session_hash = hashlib.sha256(json.dumps(session_manifest, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        public = {
            "schema_version": 1, "contract": "VerifiedResearchDataset",
            "dataset_id": source_manifest.get("run_id", "synthetic-test"),
            "source_manifest_sha256": source_manifest.get("source_manifest_sha256"),
            "source_canonicalization_policy": CANONICAL_HASH_POLICY if not synthetic else "SYNTHETIC_TEST_ONLY",
            "source_count": source_manifest.get("source_count", 0),
            "source_lineage_sha256": source_evidence.get("lineage_sha256"),
            "session_rule_version_id": session_manifest.get("rule_version_id"),
            "session_document_sha256": (
                session_manifest.get("evidence", {}).get("document_sha256") if not synthetic else None
            ),
            "session_manifest_sha256": session_hash,
            "frame_sha256_by_symbol_timeframe": frame_hashes,
            "bar_count_by_symbol_timeframe": counts,
            "period_bar_count_by_symbol_timeframe": phase_counts,
            "partial_bars_excluded_by_symbol_timeframe": excluded_partial,
            "warmup_start_utc": periods["warmup_start_utc"].isoformat(),
            "evaluation_start_utc": periods["evaluation_start_utc"].isoformat(),
            "evaluation_end_utc": periods["evaluation_end_utc"].isoformat(),
            "observation_end_utc": periods["observation_end_utc"].isoformat(),
            "as_of_utc": periods["as_of_utc"].isoformat(),
            "spread_policy": spread_policy, "cost_scenario": cost_scenario,
            "strategy_version": strategy_version,
            "raw_storage": "LOCAL_PRIVATE_ONLY",
            "eligibility_status": (
                "UNVERIFIED_HYPOTHETICAL_SESSION_SCENARIO" if synthetic
                else "VERIFIED_RESEARCH_DATASET"
            ),
            "economics_eligible": not synthetic,
        }
        public["dataset_fingerprint"] = _canonical_sha256(public)
        return cls(clean_frames, session_manifest, public, source_evidence, synthetic)

    def assert_unchanged(self) -> None:
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                actual = _frame_sha256(self.frames_by_symbol[symbol][timeframe])
                expected = self.public_manifest["frame_sha256_by_symbol_timeframe"][symbol][timeframe]
                if actual != expected:
                    raise ValueError("DATASET_FRAME_MUTATED_AFTER_VERIFICATION")

    def scan_symbol(self, symbol: str) -> dict[str, Any]:
        self.assert_unchanged()
        if symbol not in SYMBOLS:
            raise ValueError("DATASET_SYMBOL_UNSUPPORTED")
        from src.verified_history_scan import scan_symbol_from_verified_frames
        rows, lineage, counts = scan_symbol_from_verified_frames(
            symbol,
            self.frames_by_symbol[symbol],
            self.session_manifest,
            evaluation_start_utc=self.public_manifest["evaluation_start_utc"],
            evaluation_end_utc=self.public_manifest["evaluation_end_utc"],
            source_lineage=self.source_lineage.get("provenance"),
        )
        return {
            "symbol": symbol, "rows": rows, "lineage": lineage, "counts": counts,
            "dataset_fingerprint": self.public_manifest["dataset_fingerprint"],
            "evaluation_start_utc": self.public_manifest["evaluation_start_utc"],
            "evaluation_end_utc": self.public_manifest["evaluation_end_utc"],
        }

    def prepare_backtest_history(self, symbol: str) -> dict[str, pd.DataFrame]:
        self.assert_unchanged()
        if symbol not in SYMBOLS:
            raise ValueError("DATASET_SYMBOL_UNSUPPORTED")
        from src.historical_state import prepare_symbol_history_from_frames
        return prepare_symbol_history_from_frames(self.frames_by_symbol[symbol])

    def assert_scan_parity(self, scan_bundle: dict[str, Any], *, symbol: str) -> None:
        self.assert_unchanged()
        if (not isinstance(scan_bundle, dict)
                or scan_bundle.get("dataset_fingerprint") != self.public_manifest["dataset_fingerprint"]
                or scan_bundle.get("symbol") != symbol
                or scan_bundle.get("evaluation_start_utc") != self.public_manifest["evaluation_start_utc"]
                or scan_bundle.get("evaluation_end_utc") != self.public_manifest["evaluation_end_utc"]):
            raise ValueError("BACKTEST_SOURCE_MISMATCH")
        rows = scan_bundle.get("rows")
        if rows is None or not hasattr(rows, "columns"):
            raise ValueError("BACKTEST_SCAN_CONTRACT_INVALID")
        if not rows.empty:
            times = pd.to_datetime(rows["analysis_time"], utc=True)
            if (times < _timestamp(self.public_manifest["evaluation_start_utc"])).any() or (
                times > _timestamp(self.public_manifest["evaluation_end_utc"])
            ).any():
                raise ValueError("BACKTEST_SCAN_PERIOD_MISMATCH")


def validate_scan_dataset_fingerprint(scan_fingerprint: str, backtest_fingerprint: str) -> None:
    if not scan_fingerprint or scan_fingerprint != backtest_fingerprint:
        raise ValueError("BACKTEST_SOURCE_MISMATCH")
