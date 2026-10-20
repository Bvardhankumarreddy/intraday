"""Broker fee schedules: typed, sourced, dated and validated.

Each schedule is a YAML file in ``config/fees/``. Every rate carries its
source, effective date, the side it applies to, its rounding rule and whether
GST applies. Nothing defaults to zero: a missing rate is a validation error.

Rates are written as quoted strings in YAML (``"0.025"``) so they become
exact ``Decimal`` values, never binary floats.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import PROJECT_ROOT, Settings

FEES_DIR = PROJECT_ROOT / "config" / "fees"
SEGMENT = "nse_equity_intraday"


class FeeScheduleError(ValueError):
    """The fee schedule is missing, invalid, unsupported or not in effect."""


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"
    BOTH = "both"


class ChargeKind(StrEnum):
    STT = "stt"
    STAMP_DUTY = "stamp_duty"
    EXCHANGE_TRANSACTION = "exchange_transaction"
    IPFT = "ipft"
    SEBI_TURNOVER_FEE = "sebi_turnover_fee"
    OTHER = "other"


REQUIRED_KINDS = {
    ChargeKind.STT,
    ChargeKind.STAMP_DUTY,
    ChargeKind.EXCHANGE_TRANSACTION,
    ChargeKind.SEBI_TURNOVER_FEE,
}


class Rounding(StrEnum):
    PAISA = "paisa"  # round half up to 0.01
    RUPEE = "rupee"  # round half up to 1


class Source(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    retrieved_on: date
    note: str = ""


def _decimal_from_string(value: object) -> object:
    if isinstance(value, float):
        raise ValueError(f"write rate {value!r} as a quoted string to keep it exact")
    return value


class Dated(BaseModel):
    model_config = ConfigDict(frozen=True)

    effective_from: date
    effective_to: date | None = None
    source: Source

    @model_validator(mode="after")
    def _check_dates(self):
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to is before effective_from")
        return self

    def in_effect(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)


class BrokerageRule(Dated):
    """brokerage = max(min(rate x value, cap), min(min_per_order, min_pct x value))

    The second term is the minimum. Brokers differ: ``min_pct_of_value`` is
    optional, and so are the cap and the minimum.
    """

    rate_pct: Decimal = Field(ge=0)
    cap_per_order: Decimal | None = Field(None, ge=0)
    min_per_order: Decimal | None = Field(None, ge=0)
    min_pct_of_value: Decimal | None = Field(None, ge=0)
    rounding: Rounding = Rounding.PAISA

    _strings = field_validator(
        "rate_pct", "cap_per_order", "min_per_order", "min_pct_of_value", mode="before"
    )(_decimal_from_string)


class StatutoryCharge(Dated):
    kind: ChargeKind
    name: str
    rate_pct: Decimal = Field(ge=0)  # % of the order's turnover
    side: Side
    gst_applicable: bool
    rounding: Rounding = Rounding.PAISA

    _strings = field_validator("rate_pct", mode="before")(_decimal_from_string)


class GstRule(Dated):
    rate_pct: Decimal = Field(ge=0)

    _strings = field_validator("rate_pct", mode="before")(_decimal_from_string)


class FeeSchedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    schedule_id: str
    broker: str
    segment: str
    illustrative: bool
    version: str
    verified_on: date
    notes: list[str] = []
    brokerage: BrokerageRule
    gst: GstRule
    charges: list[StatutoryCharge]

    @model_validator(mode="after")
    def _check(self) -> FeeSchedule:
        if self.segment != SEGMENT:
            raise ValueError(f"segment must be {SEGMENT!r}, got {self.segment!r}")
        kinds = [c.kind for c in self.charges if c.kind is not ChargeKind.OTHER]
        missing = REQUIRED_KINDS - set(kinds)
        if missing:
            raise ValueError(f"missing required charges: {sorted(k.value for k in missing)}")
        dupes = {k for k in kinds if kinds.count(k) > 1}
        if dupes:
            # Duplicate kinds are allowed only when their date ranges don't overlap.
            for kind in dupes:
                periods = sorted(
                    (c.effective_from, c.effective_to or date.max)
                    for c in self.charges if c.kind is kind
                )
                for (_, end), (start, _) in zip(periods, periods[1:]):
                    if start <= end:
                        raise ValueError(f"overlapping effective periods for {kind.value}")
        return self

    def rates_on(self, day: date, allow_retroactive: bool = False) -> ResolvedRates:
        """Rates in effect on ``day``.

        Raises ``FeeScheduleError`` if the brokerage, GST or a required charge
        has no rate in effect - never silently skipped. With
        ``allow_retroactive``, a rate whose verified period starts *after*
        ``day`` is used anyway and a warning is recorded (for research on
        past dates when historical tariffs are unknown).
        """
        warnings: list[str] = []
        problems: list[str] = []

        def pick(items: list, label: str):
            active = [i for i in items if i.in_effect(day)]
            if active:
                return active[0]
            future = sorted((i for i in items if i.effective_from > day),
                            key=lambda i: i.effective_from)
            if allow_retroactive and future:
                warnings.append(f"{label}: rate effective from {future[0].effective_from} "
                                f"applied retroactively to {day}")
                return future[0]
            problems.append(label)
            return None

        brokerage = pick([self.brokerage], "brokerage")
        gst = pick([self.gst], "gst")
        charges = []
        kinds = {c.kind for c in self.charges}
        for kind in sorted(kinds, key=lambda k: k.value):
            group = [c for c in self.charges if c.kind is kind]
            if kind is ChargeKind.OTHER:
                charges += [c for c in group if c.in_effect(day)]
                continue
            chosen = pick(group, kind.value)
            if chosen is not None:
                charges.append(chosen)
        if problems:
            raise FeeScheduleError(
                f"{self.schedule_id}: no rate in effect on {day} for {sorted(problems)}"
            )
        return ResolvedRates(self, day, brokerage, gst, charges, warnings)


class ResolvedRates:
    """The concrete rates used for one trading date."""

    def __init__(self, schedule: FeeSchedule, day: date, brokerage: BrokerageRule,
                 gst: GstRule, charges: list[StatutoryCharge], warnings: list[str]):
        self.schedule = schedule
        self.day = day
        self.brokerage = brokerage
        self.gst = gst
        self.charges = charges
        self.warnings = warnings


def schedule_path(broker: str) -> Path:
    return FEES_DIR / f"{broker.lower()}_{SEGMENT}.yaml"


def load_fee_schedule(settings: Settings) -> FeeSchedule:
    path = settings.broker.fee_schedule_file or schedule_path(settings.broker.name)
    if not path.exists():
        available = sorted(p.name.split("_")[0] for p in FEES_DIR.glob(f"*_{SEGMENT}.yaml"))
        raise FeeScheduleError(
            f"unsupported broker {settings.broker.name!r}: no fee schedule at {path}. "
            f"Available: {available}"
        )
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    try:
        schedule = FeeSchedule.model_validate(raw)
    except ValueError as exc:
        raise FeeScheduleError(f"invalid fee schedule {path}: {exc}") from exc
    if schedule.broker.lower() != settings.broker.name.lower():
        raise FeeScheduleError(
            f"{path} is for broker {schedule.broker!r}, not {settings.broker.name!r}"
        )
    return schedule
