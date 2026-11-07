"""Frozen decision taxonomy, LLM role registry and offline budget gate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any


POLICY_VERSION = "deepseek-strategy-family-v1"
CANONICAL_FILTER_ROLE = "MARKET_ANALYST_STRATEGY_FAMILY"
CANONICAL_PROVIDER = "deepseek"
CANONICAL_MODEL = "deepseek-flash"

MARKET_ANALYST_FILTER_PROMPT = """
Eres especialista en patrones y estrategias
de trading Forex.

Python YA ha calculado:

- dirección técnica
- score de cada timeframe
- alineación temporal
- setup_quality
- evidencias alcistas/bajistas

NO debes recalcular esos valores.

Tu única función es clasificar qué estrategia
describe mejor la situación.

Estrategias permitidas:

TREND_CONTINUATION
PULLBACK
BREAKOUT
REVERSAL
RANGE_FADE
NONE

La temporalidad principal es M15.

NO inventes noticias.
NO inventes gaps.
NO inventes indicadores.

evidence_used SOLO puede contener identificadores
exactos presentes en available_evidence_ids.

Si no hay una configuración clara:
strategy = NONE.

Devuelve exclusivamente el JSON requerido.
"""


def canonical_prompt_sha256() -> str:
    encoded = json.dumps(MARKET_ANALYST_FILTER_PROMPT, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()

# Roles are module-qualified to avoid collisions between the two
# StrategyAssessment Pydantic classes in the agent package.
SCHEMA_ROLES = {
    "src.agents.market_analyst.StrategyAssessment": (CANONICAL_FILTER_ROLE, "STRATEGY_FAMILY_ASSESSMENT", True),
    "src.agents.market_analyst.MarketAnalysis": ("MARKET_ANALYSIS_SYSTEM_OUTPUT", "SYSTEM_DERIVED", False),
    "src.agents.regime_agent.RegimeAssessment": ("REGIME_DIAGNOSTIC", "ANALYSIS", False),
    "src.agents.strategy_agent.StrategyAssessment": ("STRATEGY_PROPOSAL", "ANALYSIS", False),
    "src.agents.critic_agent.CriticAssessment": ("CRITIC_VETO", "VALIDATION", False),
    "src.agents.level_thesis_agent.LevelHypothesisComparison": ("LEVEL_HYPOTHESIS_COMPARISON", "LEVEL_ANALYSIS", False),
    "src.agents.level_thesis_agent.LevelThesisAssessment": ("LEVEL_THESIS", "LEVEL_ANALYSIS", False),
    "src.agents.level_thesis_agent.SupportLevelThesisAssessment": ("SUPPORT_LEVEL_THESIS", "LEVEL_ANALYSIS", False),
    "src.agents.level_thesis_agent.ResistanceLevelThesisAssessment": ("RESISTANCE_LEVEL_THESIS", "LEVEL_ANALYSIS", False),
}


def schema_role(schema: type) -> tuple[str, str, bool]:
    return SCHEMA_ROLES.get(
        f"{schema.__module__}.{schema.__name__}",
        ("UNREGISTERED_SCHEMA", "UNCLASSIFIED", False),
    )


TECHNICAL_ACTIONS = {
    "WATCH_LONG": "LONG_CANDIDATE",
    "WATCH_SHORT": "SHORT_CANDIDATE",
    "BUY": "LONG_CANDIDATE",
    "SELL": "SHORT_CANDIDATE",
    "NO_TRADE": "NO_CANDIDATE",
}

LLM_ACTIONS = {
    # These are explicit approvals of a technical watch. They are not relabeled
    # as market BUY/SELL orders.
    "WATCH_LONG": "APPROVE_LONG_CANDIDATE",
    "WATCH_SHORT": "APPROVE_SHORT_CANDIDATE",
    "BUY": "APPROVE_LONG_CANDIDATE",
    "SELL": "APPROVE_SHORT_CANDIDATE",
    "NO_TRADE": "REJECT_CANDIDATE",
    "HOLD": "REJECT_CANDIDATE",
    "WAIT": "REJECT_CANDIDATE",
    "NONE": "REJECT_CANDIDATE",
}


def normalize_technical_action(value: Any) -> str:
    return TECHNICAL_ACTIONS.get(str(value or "").strip().upper(), "INVALID_TECHNICAL_ACTION")


def normalize_llm_action(value: Any) -> str:
    return LLM_ACTIONS.get(str(value or "").strip().upper(), "INVALID_LLM_ACTION")


def llm_approves_technical_direction(technical_action: Any, llm_action: Any) -> tuple[bool, str]:
    technical = normalize_technical_action(technical_action)
    llm = normalize_llm_action(llm_action)
    if technical == "INVALID_TECHNICAL_ACTION":
        return False, "TECHNICAL_ACTION_INVALID"
    if technical == "NO_CANDIDATE":
        return False, "NO_TECHNICAL_CANDIDATE"
    if llm == "INVALID_LLM_ACTION":
        return False, "LLM_ACTION_INVALID"
    if llm == "REJECT_CANDIDATE":
        return False, "LLM_REJECTED"
    expected = "APPROVE_LONG_CANDIDATE" if technical == "LONG_CANDIDATE" else "APPROVE_SHORT_CANDIDATE"
    if llm != expected:
        return False, "DIRECTION_DISAGREEMENT"
    return True, "LLM_APPROVED_MATCHING_CANDIDATE"


APPROVED_STRATEGY_FAMILIES = {
    "TREND_CONTINUATION", "PULLBACK", "BREAKOUT", "REVERSAL", "RANGE_FADE",
}


def deepseek_filter_outcome(technical_action: Any, model_output: Any, *, role: str) -> tuple[bool, str]:
    """Apply the preregistered strategy-family gate without changing side."""
    if role != CANONICAL_FILTER_ROLE:
        return False, "LLM_ROLE_NOT_CANONICAL"
    technical = normalize_technical_action(technical_action)
    if technical not in {"LONG_CANDIDATE", "SHORT_CANDIDATE"}:
        return False, "NO_TECHNICAL_CANDIDATE"
    strategy = str(model_output or "").strip().upper()
    if strategy == "NONE":
        return False, "MODEL_DECLINED_STRATEGY"
    if strategy not in APPROVED_STRATEGY_FAMILIES:
        return False, "MODEL_STRATEGY_INVALID"
    return True, "MODEL_APPROVED_TECHNICAL_DIRECTION_UNCHANGED"


@dataclass(frozen=True)
class BudgetLimits:
    max_requests: int
    max_tokens_per_request: int
    max_total_tokens: int
    max_estimated_cost: Decimal
    authorized_total: Decimal


class BudgetGate:
    """Deterministic mockable budget guard; never calls a provider."""

    def __init__(self, limits: BudgetLimits):
        if limits.max_requests < 0 or limits.max_tokens_per_request < 0 or limits.max_total_tokens < 0:
            raise ValueError("BUDGET_LIMIT_INVALID")
        if limits.max_estimated_cost < 0 or limits.authorized_total < 0:
            raise ValueError("BUDGET_LIMIT_INVALID")
        self.limits = limits
        self.requests = 0
        self.tokens = 0
        self.estimated_cost = Decimal("0")
        self.disabled = False
        self.events: list[dict[str, Any]] = []
        self.pending_token_reservations: dict[str, int] = {}

    def authorize(self, *, input_tokens: int, max_output_tokens: int,
                  price_input_per_million: Decimal | None = None,
                  price_output_per_million: Decimal | None = None,
                  prices_verified: bool = False) -> dict[str, Any]:
        if self.disabled:
            return {"authorized": False, "status": "DISABLED", "reason": "BUDGET_DISABLED"}
        if input_tokens < 0 or max_output_tokens < 0 or max_output_tokens > self.limits.max_tokens_per_request:
            return {"authorized": False, "status": "BLOCKED", "reason": "REQUEST_TOKEN_LIMIT"}
        request_tokens = input_tokens + max_output_tokens
        if self.requests + 1 > self.limits.max_requests:
            self.disabled = True
            reason = "MAX_REQUESTS_REACHED"
        elif self.tokens + request_tokens > self.limits.max_total_tokens:
            self.disabled = True
            reason = "MAX_TOTAL_TOKENS_REACHED"
        elif not prices_verified or price_input_per_million is None or price_output_per_million is None:
            reason = "VERIFIED_API_PRICES_REQUIRED"
        else:
            try:
                estimated = (Decimal(input_tokens) * Decimal(price_input_per_million) + Decimal(max_output_tokens) * Decimal(price_output_per_million)) / Decimal(1_000_000)
            except (InvalidOperation, TypeError, ValueError):
                return {"authorized": False, "status": "BLOCKED", "reason": "PRICE_INPUT_INVALID"}
            cap = min(self.limits.max_estimated_cost, self.limits.authorized_total)
            if self.estimated_cost + estimated > cap:
                self.disabled = True
                reason = "AUTHORIZED_COST_LIMIT_REACHED"
            else:
                self.requests += 1
                self.tokens += request_tokens
                self.estimated_cost += estimated
                request_id = f"MOCK-{self.requests:06d}"
                self.pending_token_reservations[request_id] = request_tokens
                result = {"authorized": True, "status": "MOCK_AUTHORIZED", "request_id": request_id, "request_tokens_reserved": request_tokens, "estimated_cost": str(estimated), "real_provider_call": False}
                self.events.append(result.copy())
                return result
        result = {"authorized": False, "status": "BLOCKED", "reason": reason, "real_provider_call": False}
        self.events.append(result.copy())
        return result

    def record_usage(self, *, request_id: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        """Reconcile mock usage against one request's reserved token allowance."""
        if input_tokens < 0 or output_tokens < 0:
            return {"status": "INVALID_USAGE", "disabled": self.disabled}
        used = input_tokens + output_tokens
        reserved = self.pending_token_reservations.pop(str(request_id), None)
        if reserved is None:
            return {"status": "UNKNOWN_REQUEST", "disabled": self.disabled}
        self.tokens += used - reserved
        if self.tokens > self.limits.max_total_tokens:
            self.disabled = True
        result = {"status": "RECORDED_MOCK_USAGE", "tokens": used, "tokens_reserved": reserved, "disabled": self.disabled, "real_provider_call": False}
        self.events.append(result.copy())
        return result
