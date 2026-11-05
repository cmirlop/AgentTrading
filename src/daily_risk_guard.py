"""Persistent, process-safe daily-loss guard for dry-run risk checks.

The state lock covers the complete caller transaction.  On Windows this uses
the standard-library ``msvcrt`` byte-range lock; on POSIX it uses ``flock``.
Reservations are durable, expire after a configured dry-run window, and retain
their lifecycle history for audit.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STATE_VERSION = 2
DEFAULT_RESERVATION_TTL_MINUTES = 15
MAX_LIFECYCLE_EVENTS = 1000


class DailyRiskStateError(RuntimeError):
    """Fail-closed state or locking error with a stable public code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _amount(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _utc(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_state(day=None):
    return {
        "version": STATE_VERSION,
        "day": day,
        "locked": False,
        "locked_reason": None,
        "reservations": {},
        "events": [],
    }


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            return None
        return value
    except FileNotFoundError:
        return _new_state()
    except (OSError, json.JSONDecodeError):
        return None


def _append_event(state, event):
    events = state.setdefault("events", [])
    if not isinstance(events, list):
        raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
    events.append(event)
    del events[:-MAX_LIFECYCLE_EVENTS]


def _validate_v2_state(state):
    if (
        state.get("version") != STATE_VERSION
        or not isinstance(state.get("locked"), bool)
        or not isinstance(state.get("reservations"), dict)
        or not isinstance(state.get("events"), list)
        or (state.get("day") is not None and not isinstance(state.get("day"), str))
    ):
        raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
    if state.get("locked") and not isinstance(state.get("locked_reason"), str):
        raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
    if state.get("day") is not None:
        try:
            datetime.strptime(state["day"], "%Y-%m-%d")
        except ValueError:
            raise DailyRiskStateError("DAILY_RISK_STATE_INVALID") from None
    for proposal_id, reservation in state["reservations"].items():
        if not isinstance(proposal_id, str) or not isinstance(reservation, dict):
            raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
        if reservation.get("status") not in {"ACTIVE", "RELEASED", "EXPIRED"}:
            raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
        if reservation.get("status") == "ACTIVE":
            if (
                _amount(reservation.get("risk")) is None
                or not isinstance(reservation.get("token"), str)
                or not isinstance(reservation.get("expires_at_utc"), str)
            ):
                raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
            try:
                expiry = datetime.fromisoformat(reservation["expires_at_utc"])
                if expiry.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise DailyRiskStateError("DAILY_RISK_STATE_INVALID") from None


def _upgrade_legacy_state(state, now, ttl_minutes):
    """Migrate the prior id/risk dictionaries once; never extend expiry later."""
    required = {"day", "locked", "proposal_ids", "proposal_risks", "proposal_exposures"}
    if not required.issubset(state) or not isinstance(state.get("locked"), bool):
        raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
    ids = state.get("proposal_ids", [])
    risks = state.get("proposal_risks", {})
    exposures = state.get("proposal_exposures", {})
    if (
        not isinstance(ids, list)
        or not isinstance(risks, dict)
        or not isinstance(exposures, dict)
        or any(str(item) not in risks for item in ids)
    ):
        raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
    upgraded = _new_state(state.get("day"))
    upgraded["locked"] = bool(state.get("locked", False))
    upgraded["locked_reason"] = state.get("locked_reason")
    created = _utc(now)
    expires = created + timedelta(minutes=ttl_minutes)
    for raw_id in ids:
        proposal_id = str(raw_id)
        risk = _amount(risks[raw_id] if raw_id in risks else risks.get(proposal_id))
        if risk is None:
            raise DailyRiskStateError("DAILY_RISK_STATE_INVALID")
        upgraded["reservations"][proposal_id] = {
            "status": "ACTIVE",
            "risk": risk,
            "exposure": exposures.get(raw_id, exposures.get(proposal_id, {})),
            "token": uuid.uuid4().hex,
            "created_at_utc": created.isoformat(),
            "expires_at_utc": expires.isoformat(),
        }
        _append_event(upgraded, {
            "type": "MIGRATED_ACTIVE_RESERVATION",
            "proposal_id": proposal_id,
            "at_utc": created.isoformat(),
        })
    return upgraded


def _prepare_state(state, now, ttl_minutes):
    if state is None:
        raise DailyRiskStateError("DAILY_RISK_STATE_UNREADABLE")
    if state.get("version") != STATE_VERSION:
        state = _upgrade_legacy_state(state, now, ttl_minutes)
    _validate_v2_state(state)
    return state


def _save(path, state):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


@contextmanager
def _interprocess_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(lock_path, "a+b")
    except OSError:
        raise DailyRiskStateError("DAILY_RISK_STATE_LOCK_FAILED") from None
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                raise DailyRiskStateError("DAILY_RISK_STATE_LOCK_FAILED") from None
            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                raise DailyRiskStateError("DAILY_RISK_STATE_LOCK_FAILED") from None
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            try:
                unlock()
            except OSError:
                raise DailyRiskStateError("DAILY_RISK_STATE_LOCK_FAILED") from None
    finally:
        handle.close()


def _active_reservations(state):
    return {
        proposal_id: reservation
        for proposal_id, reservation in state["reservations"].items()
        if reservation["status"] == "ACTIVE"
    }


class DailyRiskTransaction:
    """Mutable daily guard state, used only while its interprocess lock is held."""

    def __init__(self, state_path, state, *, now=None, reservation_ttl_minutes=DEFAULT_RESERVATION_TTL_MINUTES):
        self.state_path = Path(state_path)
        self.now = _utc(now)
        self.ttl_minutes = float(reservation_ttl_minutes)
        if not math.isfinite(self.ttl_minutes) or self.ttl_minutes <= 0:
            raise DailyRiskStateError("DAILY_RISK_RESERVATION_TTL_INVALID")
        self.state = _prepare_state(state, self.now, self.ttl_minutes)
        self.dirty = state is None or state.get("version") != STATE_VERSION

    def _roll_day(self, day):
        if self.state.get("day") == day:
            return
        old_day = self.state.get("day")
        prior_events = list(self.state.get("events", []))[-MAX_LIFECYCLE_EVENTS:]
        for proposal_id, reservation in _active_reservations(self.state).items():
            prior_events.append({
                "type": "RESERVATION_EXPIRED",
                "proposal_id": proposal_id,
                "reason": "UTC_DAY_RESET",
                "at_utc": self.now.isoformat(),
            })
        self.state = _new_state(day)
        self.state["events"] = prior_events[-MAX_LIFECYCLE_EVENTS:]
        self.dirty = True
        if old_day is not None:
            _append_event(self.state, {
                "type": "UTC_DAY_RESET",
                "previous_day": old_day,
                "day": day,
                "at_utc": self.now.isoformat(),
            })

    def _expire(self):
        active = _active_reservations(self.state)
        for proposal_id, reservation in active.items():
            expiry = datetime.fromisoformat(reservation["expires_at_utc"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry.astimezone(timezone.utc) <= self.now:
                reservation["status"] = "EXPIRED"
                reservation["released_at_utc"] = self.now.isoformat()
                _append_event(self.state, {
                    "type": "RESERVATION_EXPIRED",
                    "proposal_id": proposal_id,
                    "at_utc": self.now.isoformat(),
                })
                self.dirty = True

    def evaluate(
        self,
        *,
        realized_net_pnl,
        open_risk,
        pending_risk,
        proposed_risk=0.0,
        equity,
        max_loss_percent,
        timezone_name="UTC",
        proposal_id=None,
        proposal_exposure=None,
        reserve_proposal=True,
    ):
        try:
            local_now = self.now.astimezone(ZoneInfo(timezone_name))
        except (ZoneInfoNotFoundError, ValueError):
            return {"approved": False, "errors": ["DAILY_TIMEZONE_INVALID"]}

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
            or not math.isfinite(realized)
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
        self._roll_day(day)
        self._expire()
        limit = equity_amount * percent / 100.0
        used = max(0.0, -realized) + open_amount + pending_amount
        reservations = _active_reservations(self.state)
        reserved_risk_values = [_amount(item.get("risk")) for item in reservations.values()]
        if any(value is None for value in reserved_risk_values):
            return {"approved": False, "errors": ["DAILY_RISK_STATE_INVALID"]}
        reserved_risk = sum(reserved_risk_values)
        used += reserved_risk
        projected = used + proposed_amount
        errors = []
        proposal_id = str(proposal_id) if proposal_id else None
        if self.state.get("locked"):
            errors.append("DAILY_LOSS_LIMIT_LOCKED")
        if proposal_id and proposal_id in reservations:
            errors.append("DUPLICATE_PROPOSAL")
        if used >= limit:
            if not self.state.get("locked"):
                self.state["locked"] = True
                self.state["locked_reason"] = "DAILY_LOSS_LIMIT_REACHED"
                _append_event(self.state, {
                    "type": "DAILY_LOSS_LIMIT_LOCKED",
                    "day": day,
                    "at_utc": self.now.isoformat(),
                })
                self.dirty = True
            errors.append("DAILY_LOSS_LIMIT_REACHED")
        elif projected >= limit:
            errors.append("DAILY_RISK_BUDGET_EXCEEDED")

        reservation_token = None
        if proposal_id and reserve_proposal and not errors:
            reservation_token = uuid.uuid4().hex
            expires = self.now + timedelta(minutes=self.ttl_minutes)
            exposure = proposal_exposure if isinstance(proposal_exposure, dict) else {}
            reservation = {
                "status": "ACTIVE",
                "risk": proposed_amount,
                "exposure": exposure,
                "token": reservation_token,
                "created_at_utc": self.now.isoformat(),
                "expires_at_utc": expires.isoformat(),
            }
            self.state["reservations"][proposal_id] = reservation
            _append_event(self.state, {
                "type": "RESERVATION_CREATED",
                "proposal_id": proposal_id,
                "risk": proposed_amount,
                "exposure": exposure,
                "at_utc": self.now.isoformat(),
                "expires_at_utc": expires.isoformat(),
            })
            self.dirty = True
            reservations[proposal_id] = reservation

        self.state["day"] = day
        self.dirty = True
        active_risk = sum(_amount(item["risk"]) or 0.0 for item in reservations.values())
        reserved_exposures = []
        for item in reservations.values():
            exposure = item.get("exposure")
            if isinstance(exposure, dict):
                reserved_exposures.append({
                    **exposure,
                    "risk_to_sl_account": item["risk"],
                    "reservation_token": item["token"],
                    "expires_at_utc": item["expires_at_utc"],
                })
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
            "reserved_proposal_risk": round(active_risk, 8),
            "reserved_proposals": reserved_exposures,
            "daily_loss_limit": round(limit, 8),
            "locked": bool(self.state.get("locked")),
            "reservation_token": reservation_token,
        }

    def release(self, proposal_id, reservation_token):
        if not proposal_id or not reservation_token:
            return False
        proposal_id = str(proposal_id)
        reservation = self.state["reservations"].get(proposal_id)
        if (
            not isinstance(reservation, dict)
            or reservation.get("status") != "ACTIVE"
            or reservation.get("token") != reservation_token
        ):
            return False
        reservation["status"] = "RELEASED"
        reservation["released_at_utc"] = self.now.isoformat()
        _append_event(self.state, {
            "type": "RESERVATION_RELEASED",
            "proposal_id": proposal_id,
            "at_utc": self.now.isoformat(),
        })
        self.dirty = True
        return True

    def commit(self):
        if self.dirty:
            try:
                _save(self.state_path, self.state)
            except (OSError, TypeError, ValueError):
                raise DailyRiskStateError("DAILY_RISK_STATE_WRITE_FAILED") from None


@contextmanager
def daily_risk_transaction(
    state_path, *, now=None, reservation_ttl_minutes=DEFAULT_RESERVATION_TTL_MINUTES
):
    """Lock, read, evaluate and atomically persist the daily-risk state."""
    path = Path(state_path)
    with _interprocess_lock(path):
        state = _load(path)
        transaction = DailyRiskTransaction(
            path,
            state,
            now=now,
            reservation_ttl_minutes=reservation_ttl_minutes,
        )
        try:
            yield transaction
        finally:
            transaction.commit()


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
    reservation_ttl_minutes=DEFAULT_RESERVATION_TTL_MINUTES,
):
    """Public single-check API, backed by the same process-safe transaction."""
    try:
        with daily_risk_transaction(
            state_path, now=now, reservation_ttl_minutes=reservation_ttl_minutes
        ) as transaction:
            return transaction.evaluate(
                realized_net_pnl=realized_net_pnl,
                open_risk=open_risk,
                pending_risk=pending_risk,
                proposed_risk=proposed_risk,
                equity=equity,
                max_loss_percent=max_loss_percent,
                timezone_name=timezone_name,
                proposal_id=proposal_id,
                proposal_exposure=proposal_exposure,
                reserve_proposal=reserve_proposal,
            )
    except DailyRiskStateError as exc:
        return {"approved": False, "errors": [exc.code]}


def release_proposal_reservation(state_path, proposal_id, reservation_token, *, now=None):
    """Release only the exact reservation owner-token returned on approval."""
    try:
        with daily_risk_transaction(state_path, now=now) as transaction:
            return transaction.release(proposal_id, reservation_token)
    except DailyRiskStateError:
        return False
