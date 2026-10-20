"""Paper-order and paper-position lifecycles.

Orders (simulated only - never sent to a broker):

    CREATED --submit--> PENDING --fill--> FILLED
       |                   |----> EXPIRED    (entry window passed unfilled)
       |                   |----> REJECTED   (fill conditions failed at fill time)
       |                   '----> CANCELLED  (session ended / operator)
       '----> REJECTED | CANCELLED

FILLED, EXPIRED, REJECTED and CANCELLED are terminal. PARTIALLY_FILLED is
deliberately not implemented: without reliable historical depth, partial
fills would be invented, and order sizes here are a few shares.

Positions:

    OPEN --exit fill--> CLOSED
    OPEN --data missing/stale--> FLAGGED --data back--> OPEN
    FLAGGED --exit fill (from real data)--> CLOSED

A FLAGGED position is never closed with an invented price.

Order IDs are ``PAPER-`` + a hash of (signal ID, side): deterministic, so the
same signal can never create a second order, and never confusable with
broker order IDs.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum


class InvalidTransition(ValueError):
    pass


class OrderState(StrEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


ORDER_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.CREATED: {OrderState.PENDING, OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.PENDING: {OrderState.FILLED, OrderState.EXPIRED, OrderState.REJECTED,
                         OrderState.CANCELLED},
    OrderState.FILLED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
    OrderState.CANCELLED: set(),
}


class PositionState(StrEnum):
    OPEN = "OPEN"
    FLAGGED = "FLAGGED"
    CLOSED = "CLOSED"


POSITION_TRANSITIONS: dict[PositionState, set[PositionState]] = {
    PositionState.OPEN: {PositionState.CLOSED, PositionState.FLAGGED},
    PositionState.FLAGGED: {PositionState.OPEN, PositionState.CLOSED},
    PositionState.CLOSED: set(),
}


class SessionState(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    # Session ended with unresolved state (e.g. a FLAGGED position).
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


def check_order_transition(old: OrderState, new: OrderState) -> None:
    if new not in ORDER_TRANSITIONS[old]:
        raise InvalidTransition(f"order {old.value} -> {new.value} is not allowed")


def check_position_transition(old: PositionState, new: PositionState) -> None:
    if new not in POSITION_TRANSITIONS[old]:
        raise InvalidTransition(f"position {old.value} -> {new.value} is not allowed")


def order_id(signal_id: str, side: str) -> str:
    return "PAPER-" + hashlib.sha256(f"{signal_id}|{side}".encode()).hexdigest()[:16]


def position_id(signal_id: str) -> str:
    return "PPOS-" + hashlib.sha256(signal_id.encode()).hexdigest()[:16]
