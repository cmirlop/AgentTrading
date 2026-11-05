"""Shared fail-closed portfolio and daily-loss checks for dry-run plans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from config.risk_config import (
    DAILY_LOSS_TIMEZONE,
    DAILY_RISK_RESERVATION_TTL_MINUTES,
    DAILY_RISK_STATE_PATH,
    MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE,
    MAX_DAILY_LOSS_PERCENT,
    MAX_PORTFOLIO_RISK_PERCENT,
    MAX_OPEN_POSITIONS,
)
from src.daily_risk_guard import DailyRiskStateError, daily_risk_transaction
from src.risk_engine import evaluate_portfolio_limits


def _proposal_id(plan):
    material = {
        key: plan.get(key)
        for key in ("symbol", "direction", "order_type", "entry", "stop_loss", "take_profit", "volume")
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reject(errors, daily=None, portfolio=None):
    return {
        "approved": False,
        "errors": list(dict.fromkeys(errors)),
        "daily": daily or {"approved": False, "errors": list(dict.fromkeys(errors))},
        "portfolio": portfolio or {"approved": False, "errors": list(dict.fromkeys(errors))},
        "reservation_id": None,
        "reservation_token": None,
        "source": "DETERMINISTIC_PYTHON_DRY_RUN",
    }


def evaluate_plan_risk(plan, runtime_state, *, state_path=None, now=None):
    """Check and reserve one proposal in a single cross-process transaction."""
    if not isinstance(runtime_state, dict):
        return _reject(["RUNTIME_STATE_MISSING"])
    snapshot = runtime_state.get("risk_snapshot")
    account = runtime_state.get("account") or {}
    if not isinstance(snapshot, dict) or not snapshot.get("available"):
        return _reject(["RISK_SNAPSHOT_UNAVAILABLE"])
    currency = account.get("currency")
    equity = account.get("equity")
    positions = snapshot.get("positions")
    pending = snapshot.get("pending_orders")
    metadata = snapshot.get("symbol_metadata")
    rates = snapshot.get("conversion_rates")
    if any(value is None for value in (currency, equity, positions, pending, metadata, rates)):
        return _reject(["RISK_SNAPSHOT_INCOMPLETE"])
    if not all(isinstance(value, list) for value in (positions, pending)):
        return _reject(["RISK_SNAPSHOT_INCOMPLETE"])
    if not all(isinstance(value, dict) for value in (metadata, rates)):
        return _reject(["RISK_SNAPSHOT_INCOMPLETE"])

    state_path = state_path or Path(__file__).resolve().parents[1] / DAILY_RISK_STATE_PATH
    proposal_id = _proposal_id(plan)
    symbol = plan.get("symbol")
    proposal_exposure = {
        "symbol": symbol,
        "side": plan.get("direction"),
        "volume": plan.get("volume"),
        "entry": plan.get("entry"),
    }
    try:
        with daily_risk_transaction(
            state_path,
            now=now,
            reservation_ttl_minutes=DAILY_RISK_RESERVATION_TTL_MINUTES,
        ) as transaction:
            daily = transaction.evaluate(
                realized_net_pnl=snapshot.get("realized_net_pnl"),
                open_risk=snapshot.get("open_risk_account"),
                pending_risk=snapshot.get("pending_risk_account"),
                proposed_risk=plan.get("estimated_loss_account"),
                equity=equity,
                max_loss_percent=MAX_DAILY_LOSS_PERCENT,
                timezone_name=DAILY_LOSS_TIMEZONE,
                proposal_id=proposal_id,
                proposal_exposure=proposal_exposure,
                reserve_proposal=False,
            )
            errors = list(daily.get("errors", []))
            reservations = daily.get("reserved_proposals", [])
            known_reserved_risk = sum(float(item.get("risk_to_sl_account") or 0) for item in reservations)
            if float(daily.get("reserved_proposal_risk") or 0) > known_reserved_risk + 1e-8:
                errors.append("PROPOSAL_RESERVATION_EXPOSURE_UNAVAILABLE")
            if any(item.get("symbol") == symbol for item in positions):
                errors.append("POSITION_ALREADY_OPEN_FOR_SYMBOL")
            if any(item.get("symbol") == symbol for item in pending):
                errors.append("PENDING_ORDER_ALREADY_EXISTS_FOR_SYMBOL")
            if any(item.get("symbol") == symbol for item in reservations):
                errors.append("DUPLICATE_PROPOSAL_FOR_SYMBOL")
            if len(positions) + len(pending) + len(reservations) >= MAX_OPEN_POSITIONS:
                errors.append("NO_GLOBAL_SLOT_AVAILABLE")

            portfolio = evaluate_portfolio_limits(
                positions=list(positions) + reservations,
                pending_orders=pending,
                proposed_plan=plan,
                symbol_metadata=metadata,
                conversion_rates=rates,
                account_currency=currency,
                equity=equity,
                max_portfolio_risk_percent=MAX_PORTFOLIO_RISK_PERCENT,
                max_currency_exposure_equity_multiple=MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE,
            )
            errors.extend(portfolio.get("errors", []))
            if errors:
                return _reject(errors, daily=daily, portfolio=portfolio)

            reserve = transaction.evaluate(
                realized_net_pnl=snapshot.get("realized_net_pnl"),
                open_risk=snapshot.get("open_risk_account"),
                pending_risk=snapshot.get("pending_risk_account"),
                proposed_risk=plan.get("estimated_loss_account"),
                equity=equity,
                max_loss_percent=MAX_DAILY_LOSS_PERCENT,
                timezone_name=DAILY_LOSS_TIMEZONE,
                proposal_id=proposal_id,
                proposal_exposure=proposal_exposure,
                reserve_proposal=True,
            )
            errors.extend(reserve.get("errors", []))
            return {
                "approved": not errors,
                "errors": list(dict.fromkeys(errors)),
                "daily": reserve,
                "portfolio": portfolio,
                "reservation_id": proposal_id if not errors else None,
                "reservation_token": reserve.get("reservation_token") if not errors else None,
                "reservation_expires_at_utc": next(
                    (item.get("expires_at_utc") for item in reserve.get("reserved_proposals", [])
                     if item.get("reservation_token") == reserve.get("reservation_token")),
                    None,
                ),
                "source": "DETERMINISTIC_PYTHON_DRY_RUN",
            }
    except DailyRiskStateError as exc:
        return _reject([exc.code])
    except (OSError, ValueError, TypeError):
        return _reject(["DAILY_RISK_TRANSACTION_FAILED"])


def release_plan_risk_reservation(plan, *, reservation_token=None, state_path=None, now=None):
    """Release only with the opaque owner token returned by this approval."""
    if not reservation_token:
        return False
    try:
        from src.daily_risk_guard import release_proposal_reservation

        return release_proposal_reservation(
            state_path or Path(__file__).resolve().parents[1] / DAILY_RISK_STATE_PATH,
            _proposal_id(plan),
            reservation_token,
            now=now,
        )
    except (OSError, ValueError, TypeError):
        return False
