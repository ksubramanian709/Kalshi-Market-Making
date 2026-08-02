"""
Independent risk layer: hard limits checked BEFORE every order-management
decision, deliberately outside the strategy logic. The point is that a bug
or bad decision in strategy.py can't override this — if a limit is
breached, this cancels everything and refuses to place anything new,
regardless of what the strategy wants.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_position_per_market: int = 10
    max_total_notional_dollars: float = 200.0
    max_loss_dollars: float = 100.0


@dataclass
class RiskState:
    halted: bool = False
    halt_reason: str = ""


def check(
    limits: RiskLimits,
    state: RiskState,
    *,
    positions: dict[str, int],
    mark_to_market: float,
    starting_cash: float,
) -> bool:
    """
    Returns True if trading should be halted. Once halted, stays halted for
    the rest of the process — this is a circuit breaker, not something that
    should silently re-arm and resume on its own.
    """
    if state.halted:
        return True

    for ticker, pos in positions.items():
        if abs(pos) > limits.max_position_per_market:
            state.halted = True
            state.halt_reason = f"{ticker} position {pos} exceeds max_position_per_market={limits.max_position_per_market}"
            return True

    total_notional = sum(abs(p) for p in positions.values())
    if total_notional > limits.max_total_notional_dollars:
        state.halted = True
        state.halt_reason = (
            f"total notional {total_notional} exceeds max_total_notional_dollars={limits.max_total_notional_dollars}"
        )
        return True

    loss = starting_cash - mark_to_market
    if loss > limits.max_loss_dollars:
        state.halted = True
        state.halt_reason = f"loss {loss:.2f} exceeds max_loss_dollars={limits.max_loss_dollars}"
        return True

    return False
