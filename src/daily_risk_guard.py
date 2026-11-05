"""Persistent, deterministic daily-loss guard used by dry-run risk checks."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _amount(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and number < float("inf") else None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return None


def _save(path, state):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def release_proposal_reservation(state_path, proposal_id):
    """Remove a DRY RUN reservation when later deterministic validation rejects it."""
    state = _load(state_path)
    if state is None:
        return False
    proposal_id = str(proposal_id)
    ids = state.get("proposal_ids", [])
    risks = state.get("proposal_risks", {})
    exposures = state.get("proposal_exposures", {})
    if not isinstance(ids, list) or not isinstance(risks, dict) or not isinstance(exposures, dict):
        return False
    if proposal_id not in ids:
        return True
    state["proposal_ids"] = [item for item in ids if item != proposal_id]
    risks.pop(proposal_id, None)
    exposures.pop(proposal_id, None)
    state["proposal_risks"] = risks
    state["proposal_exposures"] = exposures
    try:
        _save(state_path, state)
    except OSError:
        return False
    return True


def evaluate_daily_risk(
    *,
    realized_net_pnl,
    open_risk,
    pending_risk,
    proposed_risk=0.0,
    equity,
    max_loss_percent,
    timezone_name="UTC",
    state_path,
    now=None,
    proposal_id=None,
    proposal_exposure=None,
    reserve_proposal=True,
):
    """Count realized losses plus full SL risk of open/pending exposure.

    Positive realized PnL does not offset open risk. Once a daily limit is
    reached the day remains locked after restart, even if positions later close.
    Proposal IDs are recorded atomically to prevent duplicate approvals.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        local_now = now.astimezone(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError):
        return {"approved": False, "errors": ["DAILY_TIMEZONE_INVALID"]}
    realized = None
    try:
        realized = float(realized_net_pnl)
        open_amount = _amount(open_risk)
        pending_amount = _amount(pending_risk)
        proposed_amount = _amount(proposed_risk)
        equity_amount = _amount(equity)
        percent = _amount(max_loss_percent)
    except (TypeError, ValueError):
        realized = None
    if (
        realized is None
        or not (realized == realized and abs(realized) < float("inf"))
        or open_amount is None
        or pending_amount is None
        or proposed_amount is None
        or equity_amount is None
        or equity_amount <= 0
        or percent is None
        or percent <= 0
    ):
        return {"approved": False, "errors": ["DAILY_RISK_DATA_UNAVAILABLE"]}

    day = local_now.date().isoformat()
    limit = equity_amount * percent / 100.0
    used = max(0.0, -realized) + open_amount + pending_amount
    projected = used + proposed_amount
    state = _load(state_path)
    if state is None:
        return {"approved": False, "errors": ["DAILY_RISK_STATE_UNREADABLE"]}
    if state.get("day") != day:
        state = {"day": day, "locked": False, "proposal_ids": [], "proposal_risks": {}, "proposal_exposures": {}}
    proposal_risks = state.get("proposal_risks", {})
    proposal_exposures = state.get("proposal_exposures", {})
    raw_ids = state.get("proposal_ids", [])
    if not isinstance(raw_ids, list) or not isinstance(proposal_risks, dict) or not isinstance(proposal_exposures, dict):
        return {"approved": False, "errors": ["DAILY_RISK_STATE_INVALID"]}
    proposal_ids = set(raw_ids)
    reserved_risk = sum(_amount(value) or 0.0 for value in proposal_risks.values())
    used += reserved_risk
    projected = used + proposed_amount
    errors = []
    if state.get("locked"):
        errors.append("DAILY_LOSS_LIMIT_LOCKED")
    proposal_id = str(proposal_id) if proposal_id else None
    if proposal_id and proposal_id in proposal_ids:
        errors.append("DUPLICATE_PROPOSAL")
    if used >= limit:
        state["locked"] = True
        state["locked_reason"] = "DAILY_LOSS_LIMIT_REACHED"
        errors.append("DAILY_LOSS_LIMIT_REACHED")
    elif projected >= limit:
        errors.append("DAILY_RISK_BUDGET_EXCEEDED")
    if proposal_id and reserve_proposal and not errors:
        proposal_ids.add(proposal_id)
        proposal_risks[proposal_id] = proposed_amount
        if isinstance(proposal_exposure, dict):
            proposal_exposures[proposal_id] = proposal_exposure
    state["proposal_ids"] = sorted(proposal_ids)
    state["proposal_risks"] = proposal_risks
    state["proposal_exposures"] = proposal_exposures
    state["day"] = day
    try:
        _save(state_path, state)
    except OSError:
        return {"approved": False, "errors": ["DAILY_RISK_STATE_WRITE_FAILED"]}
    return {
        "approved": not errors,
        "errors": list(dict.fromkeys(errors)),
        "day": day,
        "timezone": timezone_name,
        "realized_net_pnl": round(realized, 8),
        "open_risk": round(open_amount, 8),
        "pending_risk": round(pending_amount, 8),
        "daily_risk_used": round(used, 8),
        "projected_daily_risk": round(projected, 8),
        "reserved_proposal_risk": round(reserved_risk, 8),
        "reserved_proposals": [
            {**exposure, "risk_to_sl_account": proposal_risks.get(pid)}
            for pid, exposure in sorted(proposal_exposures.items())
            if isinstance(exposure, dict)
        ],
        "daily_loss_limit": round(limit, 8),
        "locked": bool(state.get("locked")),
    }
