"""Chronological offline portfolio simulation using the shared risk preflight.

This adapter is intentionally strict: it returns NO_EVALUABLE if risk inputs
or outcome scaling data are missing. It never connects to MT5 or a provider.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any

from config import risk_config
from src.event_ledger import canonical_hash
from src.risk_engine import calculate_position_size, convert_currency
from src.risk_preflight import evaluate_plan_risk, release_plan_risk_reservation
from src.technical_reference import POLICY_SHA256 as TECHNICAL_REFERENCE_POLICY_SHA256
from src.research_policy import (
    CANONICAL_FILTER_ROLE, CANONICAL_MODEL, CANONICAL_PROVIDER, POLICY_VERSION,
    canonical_prompt_sha256, deepseek_filter_outcome, normalize_technical_action,
)


POLICIES = ("TECHNICAL_ONLY", "TECHNICAL_PLUS_DEEPSEEK", "ALWAYS_HOLD")
SUPPORTED_COST_SCENARIOS = {"BASE", "CONSERVATIVE", "STRESS"}


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMESTAMP_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def _technical_direction(action: Any) -> str | None:
    normalized = normalize_technical_action(action)
    if normalized == "LONG_CANDIDATE":
        return "BUY"
    if normalized == "SHORT_CANDIDATE":
        return "SELL"
    return None


def _select_candidate(event: dict[str, Any], policy: str) -> tuple[bool, str | None]:
    direction = _technical_direction(event.get("technical_decision"))
    if policy == "ALWAYS_HOLD":
        return False, "POLICY_ALWAYS_HOLD"
    if direction is None:
        return False, "NO_TECHNICAL_CANDIDATE"
    if policy == "TECHNICAL_ONLY":
        return True, None
    if policy != "TECHNICAL_PLUS_DEEPSEEK":
        raise ValueError("POLICY_UNKNOWN")
    if event.get("llm_role") != CANONICAL_FILTER_ROLE:
        return False, "LLM_ROLE_NOT_CANONICAL"
    if (event.get("provider") != CANONICAL_PROVIDER
            or event.get("model") != CANONICAL_MODEL
            or event.get("decision_policy_id") != POLICY_VERSION
            or event.get("prompt_sha256") != canonical_prompt_sha256()):
        return False, "LLM_PROVENANCE_UNVERIFIED"
    if event.get("llm_validation") != "VALID" or not event.get("llm_covered"):
        return False, str(event.get("llm_coverage_status") or "LLM_COVERAGE_MISSING")
    approved, reason = deepseek_filter_outcome(
        event.get("technical_decision"), event.get("llm_model_output"), role=str(event.get("llm_role")),
    )
    return (approved, None if approved else reason)


def _scale_outcome(outcome: dict[str, Any] | None, *, volume: float, scenario: str,
                   decision_time: datetime, expected_plan_sha256: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(outcome, dict):
        return None, "OUTCOME_MISSING"
    if str(outcome.get("cost_scenario", "")).upper() != scenario:
        return None, "COST_SCENARIO_MISMATCH"
    if not expected_plan_sha256 or outcome.get("plan_sha256") != expected_plan_sha256:
        return None, "OUTCOME_PLAN_HASH_MISSING_OR_MISMATCH"
    base_volume = outcome.get("base_volume")
    try:
        base_volume = float(base_volume)
        if base_volume <= 0:
            raise ValueError
        factor = volume / base_volume
    except (TypeError, ValueError):
        return None, "OUTCOME_BASE_VOLUME_MISSING"
    values = {}
    for name in ("net_pnl_eur", "gross_pnl_eur", "costs_eur", "net_r"):
        raw = outcome.get(name)
        if raw is None:
            values[name] = None
            continue
        try:
            values[name] = float(raw) * factor if name != "net_r" else float(raw)
        except (TypeError, ValueError):
            return None, "OUTCOME_VALUE_INVALID"
    values["result"] = str(outcome.get("result", "UNRESOLVED")).upper()
    values["ambiguous"] = bool(outcome.get("ambiguous")) or values["result"].startswith("AMBIGUOUS")
    values["filled"] = outcome.get("filled")
    values["fill_time_utc"] = outcome.get("fill_time_utc")
    values["release_time_utc"] = outcome.get("economic_close_time_utc") or outcome.get("release_time_utc")
    values["known_at_utc"] = outcome.get("outcome_observed_at_utc") or outcome.get("known_at_utc")
    values["persisted_at_utc"] = outcome.get("persisted_at_utc") or outcome.get("known_at_utc")
    values["accepted_at_utc"] = outcome.get("accepted_at_utc")
    values["placed_at_utc"] = outcome.get("placed_at_utc")
    values["expires_at_utc"] = outcome.get("expires_at_utc")
    values["outcome_status"] = str(outcome.get("outcome_status", "RESOLVED" if values["net_pnl_eur"] is not None else "UNRESOLVED")).upper()
    try:
        fill_time = _utc(values["fill_time_utc"]) if values["fill_time_utc"] else None
        release_time = _utc(values["release_time_utc"]) if values["release_time_utc"] else None
        expiry = _utc(values["expires_at_utc"]) if values["expires_at_utc"] else None
        known_at = _utc(values["known_at_utc"]) if values["known_at_utc"] else None
        persisted_at = _utc(values["persisted_at_utc"]) if values["persisted_at_utc"] else None
        accepted_at = _utc(values["accepted_at_utc"]) if values["accepted_at_utc"] else decision_time
        placed_at = _utc(values["placed_at_utc"]) if values["placed_at_utc"] else accepted_at
    except (TypeError, ValueError):
        return None, "OUTCOME_TIMESTAMP_INVALID"
    if fill_time is not None and fill_time < decision_time:
        return None, "OUTCOME_FILL_PRECEDES_DECISION"
    if accepted_at < decision_time:
        return None, "OUTCOME_ACCEPTANCE_PRECEDES_DECISION"
    if placed_at < accepted_at:
        return None, "OUTCOME_PLACEMENT_PRECEDES_ACCEPTANCE"
    if fill_time is not None and fill_time < placed_at:
        return None, "OUTCOME_FILL_PRECEDES_PLACEMENT"
    if release_time is not None and values.get("filled") is not False and (fill_time is None or release_time < fill_time):
        return None, "OUTCOME_RELEASE_PRECEDES_FILL"
    if expiry is not None and expiry < decision_time:
        return None, "OUTCOME_EXPIRY_PRECEDES_DECISION"
    if values.get("outcome_status") == "RESOLVED" and (release_time is None or known_at is None):
        return None, "OUTCOME_RESOLUTION_TIMESTAMP_MISSING"
    if known_at is not None and (known_at < decision_time or (release_time is not None and known_at < release_time)):
        return None, "OUTCOME_OBSERVED_BEFORE_RESOLUTION"
    if persisted_at is not None and known_at is not None and persisted_at < known_at:
        return None, "OUTCOME_PERSISTED_BEFORE_OBSERVATION"
    if all(values.get(key) is not None for key in ("net_pnl_eur", "gross_pnl_eur", "costs_eur")):
        if abs(values["net_pnl_eur"] - (values["gross_pnl_eur"] - values["costs_eur"])) > 0.0100001:
            return None, "OUTCOME_COST_RECONCILIATION_FAILED"
    return values, None


class _PolicyRun:
    def __init__(self, name: str, initial_equity: float, account_currency: str, state_path: Path, scenario: str):
        self.name = name
        self.initial_equity = float(initial_equity)
        self.account_currency = account_currency.upper()
        self.state_path = state_path
        self.scenario = scenario
        self.positions: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.cumulative_pnl_eur = 0.0
        self.daily_pnl_eur: dict[str, float] = {}
        self.metrics: dict[str, Any] = {
            "opportunities": 0, "eligible": 0, "llm_approved": 0,
            "llm_rejected_or_uncovered": 0, "risk_accepted": 0,
            "risk_rejected": 0, "filled": 0, "expired": 0,
            "closed": 0, "ambiguous": 0, "open_or_unresolved": 0,
            "no_evaluable": 0, "gross_pnl_eur": 0.0,
            "costs_eur": 0.0, "net_pnl_eur": 0.0, "net_r_sum": 0.0,
            "max_drawdown_eur": 0.0, "rejection_reasons": {},
            "positive_net_pnl_eur": 0.0, "negative_net_pnl_eur": 0.0,
        }
        self.equity_curve = [self.initial_equity]
        self.unresolved_event_ids: set[str] = set()

    def _reject(self, reason: str) -> None:
        reasons = self.metrics["rejection_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1

    def _advance(self, now: datetime) -> None:
        # Expiry/fill times are future labels, used only when their timestamp
        # enters the simulation clock. Decisions never inspect the future state.
        still_pending = []
        for order in self.pending:
            outcome = order["outcome"]
            fill_time = _utc(outcome["fill_time_utc"]) if outcome.get("fill_time_utc") else None
            expiry_value = outcome.get("expires_at_utc") or (outcome.get("release_time_utc") if outcome.get("filled") is False else None)
            expiry = _utc(expiry_value) if expiry_value else None
            ambiguous = bool(outcome.get("ambiguous")) or outcome.get("outcome_status") in {"AMBIGUOUS", "UNRESOLVED", "OPEN", "PENDING"}
            if outcome.get("filled") is True and fill_time is not None and fill_time <= now:
                position = dict(order)
                position["filled_at"] = fill_time
                self.positions.append(position)
                self.metrics["filled"] += 1
            elif ambiguous:
                still_pending.append(order)
            elif outcome.get("filled") is False and expiry is not None and expiry <= now:
                self.metrics["expired"] += 1
            elif expiry is not None and expiry <= now and outcome.get("filled") is not True:
                self.metrics["expired"] += 1
            else:
                still_pending.append(order)
        self.pending = still_pending

        still_open = []
        realized_by_observation: dict[datetime, float] = {}
        for position in self.positions:
            outcome = position["outcome"]
            release = _utc(outcome["release_time_utc"]) if outcome.get("release_time_utc") else None
            known = _utc(outcome["known_at_utc"]) if outcome.get("known_at_utc") else None
            # Slot/exposure release requires the economic close to be observable.
            # Database persistence time is intentionally excluded from this clock.
            resolved_at = max(release, known) if release is not None and known is not None else None
            if resolved_at is None or resolved_at > now:
                still_open.append(position)
                continue
            if outcome.get("ambiguous") or outcome.get("outcome_status") in {"AMBIGUOUS", "OPEN", "UNRESOLVED", "PENDING"}:
                still_open.append(position)
                self.unresolved_event_ids.add(position["event_id"])
                continue
            net = outcome.get("net_pnl_eur")
            gross = outcome.get("gross_pnl_eur")
            costs = outcome.get("costs_eur")
            if net is None or gross is None or costs is None:
                still_open.append(position)
                self.unresolved_event_ids.add(position["event_id"])
                continue
            self.metrics["closed"] += 1
            self.metrics["net_pnl_eur"] += net
            self.metrics["gross_pnl_eur"] += gross
            self.metrics["costs_eur"] += costs
            self.metrics["net_r_sum"] += outcome.get("net_r") or 0.0
            if net > 0:
                self.metrics["positive_net_pnl_eur"] += net
            elif net < 0:
                self.metrics["negative_net_pnl_eur"] += abs(net)
            pnl_day = known.date().isoformat()
            self.daily_pnl_eur[pnl_day] = self.daily_pnl_eur.get(pnl_day, 0.0) + net
            realized_by_observation[known] = realized_by_observation.get(known, 0.0) + net
        for observed_at, net in sorted(realized_by_observation.items()):
            self.cumulative_pnl_eur += net
            self.equity_curve.append(self.initial_equity + self.cumulative_pnl_eur)
        self.positions = still_open

    def _risk_snapshot(self, event: dict[str, Any], now: datetime) -> tuple[dict[str, Any] | None, str | None]:
        context = event.get("risk_context")
        if not isinstance(context, dict):
            return None, "RISK_CONTEXT_MISSING"
        currency = str(context.get("account_currency") or self.account_currency).upper()
        metadata = context.get("symbol_metadata")
        rates = context.get("conversion_rates")
        if not isinstance(metadata, dict) or not isinstance(rates, dict):
            return None, "RISK_MARKET_METADATA_MISSING"
        try:
            equity = convert_currency(self.initial_equity + self.cumulative_pnl_eur, "EUR", currency, rates)
            daily_realized = convert_currency(self.daily_pnl_eur.get(now.date().isoformat(), 0.0), "EUR", currency, rates)
        except (TypeError, ValueError):
            return None, "EQUITY_CONVERSION_MISSING"
        if equity <= 0:
            return None, "EQUITY_VALUE_INVALID"
        current_day = now.date().isoformat()
        open_risk = sum(float(item["risk_to_sl_account"]) for item in self.positions)
        pending_risk = sum(float(item["risk_to_sl_account"]) for item in self.pending)
        return {
            "account": {"currency": currency, "equity": equity},
            "risk_snapshot": {
                "available": True,
                "realized_net_pnl": daily_realized,
                "open_risk_account": open_risk,
                "pending_risk_account": pending_risk,
                "positions": [dict(item) for item in self.positions],
                "pending_orders": [dict(item) for item in self.pending],
                "symbol_metadata": metadata,
                "conversion_rates": rates,
            },
        }, None

    def consider(self, event: dict[str, Any]) -> dict[str, Any]:
        self.metrics["opportunities"] += 1
        now = _utc(event["decision_time_utc"])
        self._advance(now)
        if _technical_direction(event.get("technical_decision")):
            if event.get("plan_strategy_source") != "TECHNICAL_ONLY_REFERENCE":
                self.metrics["no_evaluable"] += 1
                self._reject("COUNTERFACTUAL_PLAN_NOT_INDEPENDENT_OF_LLM")
                return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE",
                        "reason": "COUNTERFACTUAL_PLAN_NOT_INDEPENDENT_OF_LLM"}
            if event.get("technical_plan_status") != "TECHNICAL_PLAN_VALID" or not isinstance(event.get("plan"), dict):
                self.metrics["no_evaluable"] += 1
                self._reject("TECHNICAL_PLAN_NOT_VALID")
                return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE",
                        "reason": "TECHNICAL_PLAN_NOT_VALID"}
            if (event.get("technical_plan_version") != "TECHNICAL_REFERENCE_V1"
                    or event.get("technical_plan_policy_sha256") != TECHNICAL_REFERENCE_POLICY_SHA256
                    or event["plan"].get("symbol") != str(event.get("symbol", "")).upper()
                    or canonical_hash(event["plan"]) != event.get("plan_sha256")):
                self.metrics["no_evaluable"] += 1
                self._reject("COMMON_TECHNICAL_PLAN_HASH_OR_SOURCE_INVALID")
                return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE",
                        "reason": "COMMON_TECHNICAL_PLAN_HASH_OR_SOURCE_INVALID"}
        eligible, rejection = _select_candidate(event, self.name)
        if not eligible:
            if self.name == "TECHNICAL_PLUS_DEEPSEEK" and _technical_direction(event.get("technical_decision")):
                self.metrics["llm_rejected_or_uncovered"] += 1
            self._reject(rejection or "POLICY_REJECTED")
            return {"event_id": event.get("event_id"), "status": "POLICY_REJECTED", "reason": rejection}
        self.metrics["eligible"] += 1
        if self.name == "TECHNICAL_PLUS_DEEPSEEK":
            self.metrics["llm_approved"] += 1
        plan_input = event.get("plan")
        if not isinstance(plan_input, dict):
            self.metrics["no_evaluable"] += 1
            self._reject("TRADE_PLAN_MISSING")
            return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE", "reason": "TRADE_PLAN_MISSING"}
        state, state_error = self._risk_snapshot(event, now)
        if state_error:
            self.metrics["no_evaluable"] += 1
            self._reject(state_error)
            return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE", "reason": state_error}
        direction = _technical_direction(event.get("technical_decision"))
        if str(plan_input.get("direction", "")).upper() != direction:
            self.metrics["no_evaluable"] += 1
            self._reject("PLAN_DIRECTION_MISMATCH")
            return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE", "reason": "PLAN_DIRECTION_MISMATCH"}
        account = state["account"]
        try:
            sizing = calculate_position_size(
                symbol=event["symbol"], direction=direction,
                entry=plan_input.get("entry"), stop_loss=plan_input.get("stop_loss"),
                equity=account["equity"], balance=account["equity"],
                account_currency=account["currency"],
                symbol_metadata=state["risk_snapshot"]["symbol_metadata"].get(event["symbol"]),
                conversion_rates=state["risk_snapshot"]["conversion_rates"],
                max_risk_percent=risk_config.MAX_RISK_PER_TRADE_PERCENT,
                max_loss_absolute=risk_config.MAX_LOSS_PER_TRADE_EUR,
                max_loss_currency="EUR", risk_reference=risk_config.RISK_REFERENCE,
            )
        except Exception:
            sizing = {"status": "REJECTED", "errors": ["RISK_SIZING_INPUT_INVALID"]}
        if sizing.get("status") != "VALID":
            missing_metadata = {
                "SYMBOL_VOLUME_METADATA_MISSING", "TICK_VALUE_OR_CONTRACT_METADATA_MISSING",
                "CURRENCY_CONVERSION_MISSING", "RISK_METADATA_INVALID",
            }
            if set(sizing.get("errors", [])) & missing_metadata:
                self.metrics["no_evaluable"] += 1
                for reason in sizing.get("errors", ["RISK_METADATA_UNAVAILABLE"]):
                    self._reject(str(reason))
                return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE", "reasons": sizing.get("errors", [])}
            self.metrics["risk_rejected"] += 1
            for reason in sizing.get("errors", ["RISK_SIZING_REJECTED"]):
                self._reject(str(reason))
            return {"event_id": event.get("event_id"), "status": "RISK_REJECTED", "reasons": sizing.get("errors", [])}
        plan = {
            "symbol": event["symbol"], "direction": direction,
            "order_type": plan_input.get("order_type", "MARKET"),
            "entry": float(plan_input["entry"]), "stop_loss": float(plan_input["stop_loss"]),
            "take_profit": float(plan_input["take_profit"]), "volume": sizing["volume"],
            "estimated_loss_account": sizing["risk_amount_account"],
        }
        risk = evaluate_plan_risk(plan, state, state_path=self.state_path, now=now)
        if not risk.get("approved"):
            self.metrics["risk_rejected"] += 1
            for reason in risk.get("errors", ["PREFLIGHT_REJECTED"]):
                self._reject(str(reason))
            return {"event_id": event.get("event_id"), "status": "RISK_REJECTED", "reasons": risk.get("errors", [])}
        self.metrics["risk_accepted"] += 1
        outcome, outcome_error = _scale_outcome(
            event.get("outcome"), volume=sizing["volume"], scenario=self.scenario, decision_time=now,
            expected_plan_sha256=event["plan_sha256"],
        )
        if outcome_error and outcome_error != "OUTCOME_MISSING":
            self.metrics["no_evaluable"] += 1
            self._reject(outcome_error)
            release_plan_risk_reservation(plan, reservation_token=risk.get("reservation_token"), state_path=self.state_path, now=now)
            self.metrics["risk_accepted"] -= 1
            return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE", "reason": outcome_error}
        if outcome is None:
            outcome = {"filled": None, "outcome_status": "UNRESOLVED", "result": "UNRESOLVED", "net_pnl_eur": None, "gross_pnl_eur": None, "costs_eur": None}
        reservation_token = risk.get("reservation_token")
        released = release_plan_risk_reservation(plan, reservation_token=reservation_token, state_path=self.state_path, now=now)
        if not released:
            self.metrics["no_evaluable"] += 1
            self._reject("SIMULATED_RESERVATION_RELEASE_FAILED")
            return {"event_id": event.get("event_id"), "status": "NO_EVALUABLE", "reason": "SIMULATED_RESERVATION_RELEASE_FAILED"}
        order = {
            "event_id": str(event.get("event_id")), "symbol": event["symbol"], "side": direction,
            "direction": direction, "volume": sizing["volume"], "entry": plan["entry"],
            "price_open": plan["entry"], "price_current": plan["entry"],
            "risk_to_sl_account": sizing["risk_amount_account"], "plan": plan,
            "outcome": outcome,
        }
        filled = outcome.get("filled") is True and outcome.get("fill_time_utc") and _utc(outcome["fill_time_utc"]) <= now
        if filled:
            order["filled_at"] = _utc(outcome["fill_time_utc"])
            self.positions.append(order)
            self.metrics["filled"] += 1
        elif outcome.get("filled") is False and outcome.get("expires_at_utc") and _utc(outcome["expires_at_utc"]) <= now:
            self.metrics["expired"] += 1
        else:
            self.pending.append(order)
        self._advance(now)
        return {"event_id": event.get("event_id"), "status": "SIMULATED_ACCEPTED", "volume": sizing["volume"], "risk_amount_account": sizing["risk_amount_account"]}

    def finalize(self) -> dict[str, Any]:
        self.metrics["open_or_unresolved"] = len(self.positions) + len(self.pending)
        self.metrics["ambiguous"] = sum(bool(item["outcome"].get("ambiguous")) for item in self.positions + self.pending)
        self.metrics["max_drawdown_eur"] = 0.0
        peak = self.equity_curve[0]
        for equity in self.equity_curve:
            peak = max(peak, equity)
            self.metrics["max_drawdown_eur"] = max(self.metrics["max_drawdown_eur"], peak - equity)
        for key in ("net_pnl_eur", "gross_pnl_eur", "costs_eur", "net_r_sum", "max_drawdown_eur",
                    "positive_net_pnl_eur", "negative_net_pnl_eur"):
            self.metrics[key] = round(float(self.metrics[key]), 8)
        self.metrics["expectancy_net_eur_per_closed_trade"] = round(self.metrics["net_pnl_eur"] / self.metrics["closed"], 8) if self.metrics["closed"] else None
        self.metrics["expectancy_net_eur_per_opportunity"] = round(
            self.metrics["net_pnl_eur"] / self.metrics["opportunities"], 8,
        ) if self.metrics["opportunities"] else None
        self.metrics["profit_factor_net"] = (
            round(self.metrics["positive_net_pnl_eur"] / self.metrics["negative_net_pnl_eur"], 8)
            if self.metrics["negative_net_pnl_eur"] > 0 else None
        )
        return self.metrics


def simulate_paired_policies(events: list[dict[str, Any]], *, cost_scenario: str = "CONSERVATIVE",
                             initial_equity_eur: float = 10_000.0, state_dir: str | Path | None = None) -> dict[str, Any]:
    """Run independent policy portfolios in identical deterministic order."""
    scenario = str(cost_scenario).upper()
    if scenario not in SUPPORTED_COST_SCENARIOS:
        raise ValueError("COST_SCENARIO_INVALID")
    if initial_equity_eur <= 0:
        raise ValueError("INITIAL_EQUITY_INVALID")
    ordered = sorted(events, key=lambda event: (
        _utc(event["decision_time_utc"]),
        -float(event.get("technical_setup_quality") or 0),
        -abs(float(event.get("technical_overall_score") or 0)),
        str(event.get("symbol", "")), str(event.get("event_id", "")),
    ))
    ids = [str(event.get("event_id")) for event in ordered]
    duplicate_ids = sorted({event_id for event_id in ids if ids.count(event_id) > 1})
    if duplicate_ids:
        return {
            "status": "NO_EVALUABLE",
            "economic_inference_allowed": False,
            "cost_scenario": scenario,
            "initial_equity_eur": float(initial_equity_eur),
            "sequence": [], "policies": {}, "event_results": {},
            "incomplete_events": [{"event_id": event_id, "reason": "DUPLICATE_EVENT_ID"} for event_id in duplicate_ids],
            "real_orders_sent": 0, "external_api_calls": 0,
        }
    plan_parity = all(
        not _technical_direction(event.get("technical_decision"))
        or (event.get("plan_strategy_source") == "TECHNICAL_ONLY_REFERENCE"
            and event.get("technical_plan_status") == "TECHNICAL_PLAN_VALID"
            and event.get("technical_plan_version") == "TECHNICAL_REFERENCE_V1"
            and event.get("technical_plan_policy_sha256") == TECHNICAL_REFERENCE_POLICY_SHA256
            and isinstance(event.get("plan"), dict)
                and event["plan"].get("symbol") == str(event.get("symbol", "")).upper()
            and canonical_hash(event["plan"]) == event.get("plan_sha256"))
        for event in ordered
    )
    if state_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="agenttrading-phase22-risk-")
        root = Path(temporary.name)
    else:
        temporary = None
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
    try:
        policy_runs = {
            name: _PolicyRun(name, initial_equity_eur, "EUR", root / f"{name.lower()}_daily_state.json", scenario)
            for name in POLICIES
        }
        event_results = {name: [] for name in POLICIES}
        for event in ordered:
            for name, run in policy_runs.items():
                event_results[name].append(run.consider(event))
        scoring_times = [
            _utc(value)
            for event in ordered
            for value in (
                (event.get("outcome") or {}).get("release_time_utc"),
                (event.get("outcome") or {}).get("known_at_utc"),
                (event.get("outcome") or {}).get("economic_close_time_utc"),
                (event.get("outcome") or {}).get("outcome_observed_at_utc"),
                (event.get("outcome") or {}).get("expires_at_utc"),
            ) if value
        ]
        if scoring_times:
            end_time = max(scoring_times)
            for run in policy_runs.values():
                run._advance(end_time)
        outputs = {name: run.finalize() for name, run in policy_runs.items()}
        incomplete = []
        for event in ordered:
            if _technical_direction(event.get("technical_decision")) and not event.get("risk_context"):
                incomplete.append({"event_id": event.get("event_id"), "reason": "RISK_CONTEXT_MISSING"})
            if _technical_direction(event.get("technical_decision")) and not event.get("plan"):
                incomplete.append({"event_id": event.get("event_id"), "reason": "TRADE_PLAN_MISSING"})
            if (_technical_direction(event.get("technical_decision"))
                    and event.get("technical_plan_status") != "TECHNICAL_PLAN_VALID"):
                incomplete.append({"event_id": event.get("event_id"), "reason": "TECHNICAL_PLAN_NOT_VALID"})
            if (_technical_direction(event.get("technical_decision")) and event.get("plan")
                    and canonical_hash(event["plan"]) != event.get("plan_sha256")):
                incomplete.append({"event_id": event.get("event_id"), "reason": "COMMON_PLAN_HASH_MISMATCH"})
            if (_technical_direction(event.get("technical_decision"))
                    and event.get("plan_strategy_source") != "TECHNICAL_ONLY_REFERENCE"):
                incomplete.append({"event_id": event.get("event_id"), "reason": "COUNTERFACTUAL_PLAN_NOT_INDEPENDENT_OF_LLM"})
            if _technical_direction(event.get("technical_decision")) and not event.get("llm_coverage_status"):
                incomplete.append({"event_id": event.get("event_id"), "reason": "LLM_COVERAGE_STATE_MISSING"})
            outcome = event.get("outcome")
            if _technical_direction(event.get("technical_decision")) and (not isinstance(outcome, dict) or not outcome.get("release_time_utc") or not outcome.get("known_at_utc")):
                incomplete.append({"event_id": event.get("event_id"), "reason": "OUTCOME_UNRESOLVED_OR_RELEASE_TIME_MISSING"})
        seen_incomplete = {(row.get("event_id"), row.get("reason")) for row in incomplete}
        for policy_name, rows in event_results.items():
            for row in rows:
                if row.get("status") == "NO_EVALUABLE" and (row.get("event_id"), row.get("reason")) not in seen_incomplete:
                    reason = row.get("reason") or ";".join(row.get("reasons", [])) or "RISK_OR_OUTCOME_CONTEXT_INCOMPLETE"
                    incomplete.append({"event_id": row.get("event_id"), "reason": reason, "policy": policy_name})
                    seen_incomplete.add((row.get("event_id"), reason))
        status = "NO_EVALUABLE" if not ordered or incomplete else "OFFLINE_RISK_PARITY_SIMULATION"
        technical = outputs["TECHNICAL_ONLY"]
        deepseek = outputs["TECHNICAL_PLUS_DEEPSEEK"]
        paired_delta = None
        if status == "OFFLINE_RISK_PARITY_SIMULATION":
            paired_delta = {
                "opportunities": len(ordered),
                "net_pnl_eur_difference": round(deepseek["net_pnl_eur"] - technical["net_pnl_eur"], 8),
                "net_expectancy_eur_per_opportunity_difference": round(
                    (deepseek["expectancy_net_eur_per_opportunity"] or 0.0)
                    - (technical["expectancy_net_eur_per_opportunity"] or 0.0), 8,
                ),
                "net_r_sum_difference": round(deepseek["net_r_sum"] - technical["net_r_sum"], 8),
                "technical_only_net_pnl_eur": technical["net_pnl_eur"],
                "technical_plus_deepseek_net_pnl_eur": deepseek["net_pnl_eur"],
                "descriptive_only": True,
                "confidence_interval": None,
            }
        return {
            "status": status,
            "economic_inference_allowed": False,
            "cost_scenario": scenario,
            "initial_equity_eur": float(initial_equity_eur),
            "risk_config": {
                "max_open_positions": risk_config.MAX_OPEN_POSITIONS,
                "max_risk_per_trade_percent": risk_config.MAX_RISK_PER_TRADE_PERCENT,
                "max_loss_per_trade_eur": risk_config.MAX_LOSS_PER_TRADE_EUR,
                "max_portfolio_risk_percent": risk_config.MAX_PORTFOLIO_RISK_PERCENT,
                "max_daily_loss_percent": risk_config.MAX_DAILY_LOSS_PERCENT,
                "max_currency_exposure_equity_multiple": risk_config.MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE,
                "reservation_ttl_minutes": risk_config.DAILY_RISK_RESERVATION_TTL_MINUTES,
                "daily_loss_timezone": risk_config.DAILY_LOSS_TIMEZONE,
                "source": "config.risk_config + src.risk_engine + src.risk_preflight",
            },
            "sequence": [{"event_id": item.get("event_id"), "symbol": item.get("symbol"), "decision_time_utc": item.get("decision_time_utc")} for item in ordered],
            "common_plan_parity": {
                "all_technical_candidates_share_one_plan_record": plan_parity,
                "plan_sha256_by_event": {str(event.get("event_id")): event.get("plan_sha256") for event in ordered
                                          if _technical_direction(event.get("technical_decision"))},
            },
            "policies": outputs,
            "paired_descriptive_delta": paired_delta,
            "event_results": event_results,
            "incomplete_events": incomplete,
            "limitations": [
                "This deterministic simulator uses the production risk-sizing and portfolio preflight functions with synthetic or verified supplied snapshots.",
                "MT5 margin checks and broker order validation are not reproducible offline and remain PENDING.",
                "NO_EVALUABLE inputs are retained in the population and block confirmatory inference.",
                "All fills and outcomes are hypothetical; no order is sent.",
            ],
            "real_orders_sent": 0,
            "external_api_calls": 0,
        }
    finally:
        if temporary is not None:
            temporary.cleanup()
