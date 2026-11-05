"""Backtest transaction-cost cases; non-BASE values are explicit hypotheses.

BASE uses the historical bid/ask spread column and adds no commission or
slippage because verified broker-specific history is not available here.
CONSERVATIVE and STRESS values are scenario assumptions, not broker quotes.
"""

COST_SCENARIOS = {
    "BASE": {
        "historical_spread_multiplier": 1.0,
        "commission_eur_per_lot_round_turn": 0.0,
        "slippage_points_per_side": 0.0,
        "assumptions_unverified": ["commission=0", "slippage=0"],
    },
    "CONSERVATIVE": {
        "historical_spread_multiplier": 1.25,
        "commission_eur_per_lot_round_turn": 7.0,
        "slippage_points_per_side": 1.0,
        "assumptions_unverified": ["spread_multiplier=1.25", "commission=7 EUR/lot", "slippage=1 point/side"],
    },
    "STRESS": {
        "historical_spread_multiplier": 2.0,
        "commission_eur_per_lot_round_turn": 10.0,
        "slippage_points_per_side": 3.0,
        "assumptions_unverified": ["spread_multiplier=2.0", "commission=10 EUR/lot", "slippage=3 points/side"],
    },
}
