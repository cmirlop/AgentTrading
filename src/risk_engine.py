"""Pure, offline-verifiable monetary sizing and portfolio exposure rules."""

from __future__ import annotations

from collections import deque
from math import floor, isfinite


def _get(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _nonnegative(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number >= 0 else None


def convert_currency(amount, source, target, rates):
    """Convert via a deterministic BFS over explicitly supplied FX mid-rates."""
    amount = float(amount)
    source = str(source or "").upper()
    target = str(target or "").upper()
    if not isfinite(amount) or not source or not target:
        raise ValueError("CURRENCY_CONVERSION_INPUT_INVALID")
    if source == target:
        return amount

    graph = {}
    for pair, raw_rate in (rates or {}).items():
        pair = str(pair).replace("/", "").upper()
        rate = _positive(raw_rate)
        if len(pair) != 6 or rate is None:
            continue
        base, quote = pair[:3], pair[3:]
        graph.setdefault(base, []).append((quote, rate))
        graph.setdefault(quote, []).append((base, 1.0 / rate))

    queue = deque([(source, 1.0)])
    visited = {source}
    while queue:
        currency, factor = queue.popleft()
        for next_currency, edge in graph.get(currency, []):
            if next_currency in visited:
                continue
            next_factor = factor * edge
            if next_currency == target:
                return amount * next_factor
            visited.add(next_currency)
            queue.append((next_currency, next_factor))
    raise ValueError("CURRENCY_CONVERSION_MISSING")


def _risk_per_lot(entry, stop_loss, symbol_metadata, account_currency, conversion_rates):
    distance = abs(float(entry) - float(stop_loss))
    if not isfinite(distance) or distance <= 0:
        raise ValueError("STOP_LOSS_INVALID")

    tick_size = _positive(_get(symbol_metadata, "trade_tick_size"))
    tick_value = _positive(_get(symbol_metadata, "trade_tick_value_loss"))
    tick_value_currency = str(
        _get(symbol_metadata, "trade_tick_value_loss_currency")
        or _get(symbol_metadata, "account_currency")
        or ""
    ).upper()
    if tick_size and tick_value and tick_value_currency == str(account_currency).upper():
        return distance / tick_size * tick_value, "MT5_TICK_VALUE_LOSS"

    contract_size = _positive(_get(symbol_metadata, "trade_contract_size"))
    quote_currency = str(
        _get(symbol_metadata, "currency_profit")
        or _get(symbol_metadata, "quote_currency")
        or ""
    ).upper()
    if contract_size and quote_currency:
        quote_risk = distance * contract_size
        account_risk = convert_currency(
            quote_risk,
            quote_currency,
            account_currency,
            conversion_rates,
        )
        return account_risk, "CONTRACT_SIZE_WITH_FX_CONVERSION"
    raise ValueError("TICK_VALUE_OR_CONTRACT_METADATA_MISSING")


def calculate_position_size(
    *,
    symbol,
    direction,
    entry,
    stop_loss,
    equity,
    balance,
    account_currency,
    symbol_metadata,
    conversion_rates,
    max_risk_percent,
    max_loss_absolute=5.0,
    max_loss_currency="EUR",
    risk_reference="equity",
):
    """Size downward to the broker step and reject if the minimum breaks risk."""
    if direction not in {"BUY", "SELL"}:
        return {"status": "REJECTED", "errors": ["DIRECTION_INVALID"]}
    try:
        entry = float(entry)
        stop_loss = float(stop_loss)
    except (TypeError, ValueError):
        return {"status": "REJECTED", "errors": ["ENTRY_OR_STOP_INVALID"]}
    if not isfinite(entry) or not isfinite(stop_loss):
        return {"status": "REJECTED", "errors": ["ENTRY_OR_STOP_INVALID"]}
    if (direction == "BUY" and stop_loss >= entry) or (direction == "SELL" and stop_loss <= entry):
        return {"status": "REJECTED", "errors": ["STOP_LOSS_INVALID"]}

    reference = equity if risk_reference == "equity" else balance if risk_reference == "balance" else None
    reference = _positive(reference)
    percent = _positive(max_risk_percent)
    if reference is None or percent is None:
        return {"status": "REJECTED", "errors": ["RISK_REFERENCE_INVALID"]}

    minimum = _positive(_get(symbol_metadata, "volume_min"))
    maximum = _positive(_get(symbol_metadata, "volume_max"))
    step = _positive(_get(symbol_metadata, "volume_step"))
    if minimum is None or maximum is None or step is None or minimum > maximum:
        return {"status": "REJECTED", "errors": ["SYMBOL_VOLUME_METADATA_MISSING"]}

    try:
        risk_per_lot, valuation_method = _risk_per_lot(
            entry, stop_loss, symbol_metadata, account_currency, conversion_rates
        )
        percent_budget = reference * percent / 100.0
        absolute_cap = convert_currency(
            float(max_loss_absolute), max_loss_currency, account_currency, conversion_rates
        )
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        reason = str(exc) if str(exc) in {
            "CURRENCY_CONVERSION_MISSING",
            "CURRENCY_CONVERSION_INPUT_INVALID",
            "STOP_LOSS_INVALID",
            "TICK_VALUE_OR_CONTRACT_METADATA_MISSING",
        } else "RISK_METADATA_INVALID"
        return {"status": "REJECTED", "errors": [reason]}

    if not isfinite(risk_per_lot) or risk_per_lot <= 0:
        return {"status": "REJECTED", "errors": ["RISK_PER_LOT_INVALID"]}
    budget = min(percent_budget, absolute_cap)
    raw_volume = budget / risk_per_lot
    if raw_volume + 1e-12 < minimum:
        return {
            "status": "REJECTED",
            "errors": ["VOLUME_BELOW_MINIMUM_RISK_BUDGET"],
            "risk_budget_account": round(budget, 8),
            "risk_at_minimum_account": round(risk_per_lot * minimum, 8),
            "raw_volume": raw_volume,
            "volume_min": minimum,
        }

    capped = min(raw_volume, maximum)
    volume = floor(capped / step + 1e-10) * step
    decimals = max(0, min(8, len(str(step).split(".")[-1].rstrip("0"))))
    volume = round(volume, decimals)
    if volume < minimum or volume > maximum:
        return {"status": "REJECTED", "errors": ["VOLUME_NORMALIZATION_FAILED"]}

    risk_amount = volume * risk_per_lot
    if risk_amount > budget + max(1e-8, budget * 1e-9):
        return {"status": "REJECTED", "errors": ["RISK_BUDGET_EXCEEDED"]}
    try:
        risk_eur = convert_currency(risk_amount, account_currency, "EUR", conversion_rates)
    except ValueError:
        risk_eur = None

    return {
        "status": "VALID",
        "errors": [],
        "symbol": symbol,
        "volume": volume,
        "risk_amount_account": round(risk_amount, 8),
        "risk_budget_account": round(budget, 8),
        "risk_amount_eur": round(risk_eur, 8) if risk_eur is not None else None,
        "risk_per_lot_account": round(risk_per_lot, 8),
        "valuation_method": valuation_method,
        "risk_reference": risk_reference,
        "risk_reference_amount": reference,
        "max_risk_percent": float(max_risk_percent),
        "volume_min": minimum,
        "volume_max": maximum,
        "volume_step": step,
    }


def _trade_side(trade):
    side = str(trade.get("side") or trade.get("type") or trade.get("direction") or "").upper()
    if side.startswith("BUY"):
        return "BUY"
    if side.startswith("SELL"):
        return "SELL"
    return None


def calculate_currency_exposure(trades, symbol_metadata, conversion_rates, account_currency):
    gross = {}
    net = {}
    for trade in trades:
        symbol = trade.get("symbol")
        metadata = symbol_metadata.get(symbol)
        if metadata is None:
            raise ValueError("SYMBOL_METADATA_MISSING")
        side = _trade_side(trade)
        if side is None:
            raise ValueError("POSITION_DIRECTION_MISSING")
        volume = _positive(trade.get("volume", trade.get("volume_current", trade.get("volume_initial"))))
        contract = _positive(_get(metadata, "trade_contract_size"))
        price = _positive(trade.get("price_current") or trade.get("price_open") or trade.get("entry"))
        base = str(_get(metadata, "currency_base") or "").upper()
        quote = str(_get(metadata, "currency_profit") or _get(metadata, "quote_currency") or "").upper()
        if volume is None or contract is None or price is None or len(base) != 3 or len(quote) != 3:
            raise ValueError("EXPOSURE_METADATA_INVALID")
        signed_base = volume * contract * (1.0 if side == "BUY" else -1.0)
        currency_amounts = {base: signed_base, quote: -signed_base * price}
        for currency, amount in currency_amounts.items():
            account_amount = convert_currency(abs(amount), currency, account_currency, conversion_rates)
            gross[currency] = gross.get(currency, 0.0) + account_amount
            net[currency] = net.get(currency, 0.0) + (account_amount if amount >= 0 else -account_amount)
    return {
        "gross_account": {key: round(value, 6) for key, value in sorted(gross.items())},
        "net_account": {key: round(value, 6) for key, value in sorted(net.items())},
    }


def evaluate_portfolio_limits(
    *,
    positions,
    pending_orders,
    proposed_plan,
    symbol_metadata,
    conversion_rates,
    account_currency,
    equity,
    max_portfolio_risk_percent,
    max_currency_exposure_equity_multiple,
):
    """Fail closed on unknown stops or currency conversion data."""
    errors = []
    existing = list(positions or []) + list(pending_orders or [])
    existing_risk = 0.0
    for trade in existing:
        risk = _nonnegative(trade.get("risk_to_sl_account"))
        if risk is None:
            errors.append("EXISTING_POSITION_RISK_UNAVAILABLE")
        else:
            existing_risk += risk

    proposed_risk = _positive(proposed_plan.get("estimated_loss_account"))
    if proposed_risk is None:
        errors.append("PROPOSED_RISK_UNAVAILABLE")
        proposed_risk = 0.0

    equity = _positive(equity)
    if equity is None:
        errors.append("ACCOUNT_EQUITY_UNAVAILABLE")

    risk_percent = _positive(max_portfolio_risk_percent)
    risk_limit = equity * risk_percent / 100.0 if equity is not None and risk_percent is not None else None
    projected_risk = existing_risk + float(proposed_risk)
    if risk_limit is None:
        errors.append("PORTFOLIO_RISK_LIMIT_INVALID")
    elif projected_risk > risk_limit + max(1e-8, risk_limit * 1e-9):
        errors.append("MAX_PORTFOLIO_RISK_EXCEEDED")

    exposure = None
    try:
        proposed = {
            "symbol": proposed_plan.get("symbol"),
            "side": proposed_plan.get("direction"),
            "volume": proposed_plan.get("volume"),
            "entry": proposed_plan.get("entry"),
        }
        exposure = calculate_currency_exposure(
            existing + [proposed], symbol_metadata, conversion_rates, account_currency
        )
    except (TypeError, ValueError, ZeroDivisionError):
        errors.append("CURRENCY_EXPOSURE_UNAVAILABLE")

    exposure_multiple = _positive(max_currency_exposure_equity_multiple)
    exposure_limit = equity * exposure_multiple if equity is not None and exposure_multiple is not None else None
    if exposure is not None:
        if exposure_limit is None:
            errors.append("CURRENCY_EXPOSURE_LIMIT_INVALID")
        else:
            for currency, amount in exposure["gross_account"].items():
                if amount > exposure_limit + max(1e-8, exposure_limit * 1e-9):
                    errors.append(f"CURRENCY_EXPOSURE_LIMIT_EXCEEDED_{currency}")

    return {
        "approved": not errors,
        "errors": list(dict.fromkeys(errors)),
        "existing_risk_account": round(existing_risk, 8),
        "proposed_risk_account": round(float(proposed_risk), 8),
        "projected_risk_account": round(projected_risk, 8),
        "portfolio_risk_limit_account": round(risk_limit, 8) if risk_limit is not None else None,
        "currency_exposure": exposure,
        "currency_exposure_limit_account": round(exposure_limit, 8) if exposure_limit is not None else None,
    }
