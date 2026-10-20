"""Chronological walk-forward plan for the frozen baseline.

Sessions (regular NSE sessions with accepted data, in date order) are split
as::

    | warm-up | fold 0: train | validation | test |
              |  test len  | fold 1: train | validation | test |
              ...

- **Warm-up** sessions are history only (relative-volume and liquidity
  baselines) and are never traded or counted. Length:
  ``relative_volume_lookback_sessions``, raised to
  ``liquidity_lookback_sessions`` unless daily candles cover that lookback
  before the first intraday session.
- Window lengths come from the predeclared rule in the protocol
  (``walk_forward``). Folds roll forward by the test length, so test windows
  never overlap and their union is the **aggregated out-of-sample** period.
- Nothing is shuffled. Each window is simulated separately from the initial
  capital, with earlier sessions visible only as indicator history. The
  frozen baseline is the only configuration in the primary evaluation.
  Train and validation windows exist to judge stability (and for the
  sensitivity grid), never to choose parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.backtesting.dataset import HistoricalDataset
from app.backtesting.engine import BacktestResult, EquityPoint
from app.config import Settings
from app.robustness.protocol import WalkForwardRule
from app.scan.snapshot import lookback_start
from app.utils.market_calendar import MarketCalendar

PARTITIONS = ("train", "validation", "test")


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    fold: int
    partition: str  # train | validation | test
    sessions: tuple[date, ...]

    @property
    def start(self) -> date:
        return self.sessions[0]

    @property
    def end(self) -> date:
        return self.sessions[-1]


@dataclass(frozen=True)
class Fold:
    index: int
    train: Window
    validation: Window
    test: Window

    def windows(self) -> tuple[Window, Window, Window]:
        return self.train, self.validation, self.test


@dataclass(frozen=True)
class Plan:
    status: str  # OK | INSUFFICIENT_DATA
    reason: str
    warmup: tuple[date, ...]
    warmup_reason: str
    tradable: tuple[date, ...]
    train_sessions: int
    validation_sessions: int
    test_sessions: int
    rule: str
    folds: tuple[Fold, ...] = ()
    unused_tail: tuple[date, ...] = ()
    skipped_non_regular: tuple[date, ...] = field(default=())

    @property
    def oos_sessions(self) -> list[date]:
        return [d for f in self.folds for d in f.test.sessions]

    @property
    def evaluated_range(self) -> tuple[date, date] | None:
        if not self.folds:
            return None
        return self.folds[0].train.start, self.folds[-1].test.end

    def summary(self) -> dict:
        return {
            "status": self.status, "reason": self.reason,
            "warmup_sessions": len(self.warmup), "warmup_reason": self.warmup_reason,
            "warmup_range": [self.warmup[0], self.warmup[-1]] if self.warmup else None,
            "tradable_sessions": len(self.tradable),
            "train_sessions": self.train_sessions,
            "validation_sessions": self.validation_sessions,
            "test_sessions": self.test_sessions, "rule": self.rule,
            "folds": [{"fold": f.index, **{w.partition: [w.start, w.end, len(w.sessions)]
                                           for w in f.windows()}} for f in self.folds],
            "oos_sessions": len(self.oos_sessions),
            "unused_tail_sessions": len(self.unused_tail),
            "skipped_non_regular_sessions": list(self.skipped_non_regular),
        }


def warmup_length(dataset: HistoricalDataset, settings: Settings,
                  calendar: MarketCalendar) -> tuple[int, str]:
    rv = settings.strategy.relative_volume_lookback_sessions
    liq = settings.screening.liquidity_lookback_sessions
    sessions = dataset.sessions()
    if not sessions:
        return rv, "no sessions"
    need_from = lookback_start(calendar, sessions[0], liq)
    daily_ok = all(
        d.daily_source == "1d file"
        and int((d.daily["timestamp"].dt.date.between(need_from, sessions[0], inclusive="left"))
                .sum()) >= liq
        for d in dataset.symbols.values())
    if daily_ok:
        return rv, (f"{rv} sessions for the relative-volume baseline (daily candles cover the "
                    f"{liq}-session liquidity lookback)")
    n = max(rv, liq)
    return n, (f"{n} sessions: relative-volume lookback {rv}, liquidity lookback {liq} "
               "(no daily file covering it)")


def make_plan(dataset: HistoricalDataset, settings: Settings, calendar: MarketCalendar,
              rule: WalkForwardRule, train: int | None = None, validation: int | None = None,
              test: int | None = None) -> Plan:
    all_days = dataset.sessions()
    regular = [d for d in all_days if calendar.is_regular_session(d)]
    skipped = tuple(d for d in all_days if not calendar.is_regular_session(d))
    w, why = warmup_length(dataset, settings, calendar)
    warmup, tradable = tuple(regular[:w]), tuple(regular[w:])
    s = len(tradable)
    t = test or rule.test_sessions or max(rule.min_test_sessions, s // 8)
    v = validation or rule.validation_sessions or t
    tr = train or rule.train_sessions or 2 * t
    text = (f"test = {'given' if (test or rule.test_sessions) else f'max({rule.min_test_sessions}, floor({s}/8))'}"
            f" = {t}; validation = {v}; train = {tr}; roll forward by {t}")
    if tr + v + t > s:
        return Plan("INSUFFICIENT_DATA",
                    f"{s} tradable sessions after a {w}-session warm-up; one fold needs "
                    f"{tr + v + t} ({tr} train + {v} validation + {t} test)",
                    warmup, why, tradable, tr, v, t, text, skipped_non_regular=skipped)
    folds, k = [], 0
    while k * t + tr + v + t <= s:
        o = k * t
        folds.append(Fold(k, Window(k, "train", tradable[o:o + tr]),
                          Window(k, "validation", tradable[o + tr:o + tr + v]),
                          Window(k, "test", tradable[o + tr + v:o + tr + v + t])))
        k += 1
    used_to = (k - 1) * t + tr + v + t
    plan = Plan("OK", f"{len(folds)} fold(s)", warmup, why, tradable, tr, v, t, text,
                tuple(folds), tradable[used_to:], skipped)
    validate_plan(plan)
    return plan


def validate_plan(plan: Plan) -> None:
    """Chronological order, no overlap inside a fold, test windows disjoint
    and in order, and nothing traded inside the warm-up."""
    last_test_end = None
    for f in plan.folds:
        a, b, c = f.windows()
        if not (a.end < b.start and b.end < c.start):
            raise PlanError(f"fold {f.index}: windows overlap or are out of order")
        if plan.warmup and a.start <= plan.warmup[-1]:
            raise PlanError(f"fold {f.index}: training starts inside the warm-up")
        if last_test_end is not None and c.start <= last_test_end:
            raise PlanError(f"fold {f.index}: test window overlaps an earlier one")
        last_test_end = c.end


def chain_results(results: list[BacktestResult], initial: Decimal) -> BacktestResult:
    """Aggregate separately simulated windows into one out-of-sample record.
    Each window started from ``initial``; equity is chained by adding the
    earlier windows' P&L so drawdown runs across the whole OOS period."""
    if not results:
        raise ValueError("no results to chain")
    offset = Decimal(0)
    first = results[0]
    out = BacktestResult(
        start=first.start, end=results[-1].end, initial_capital=initial, final_cash=initial,
        dataset_id=first.dataset_id, config_version=first.config_version,
        strategy_version=first.strategy_version,
        execution_model_version=first.execution_model_version,
        fee_schedule_id=first.fee_schedule_id, sessions=[],
    )
    out.assumptions = list(first.assumptions)
    for r in results:
        shift = offset - (r.initial_capital - initial)

        def moved(p: EquityPoint) -> EquityPoint:
            return EquityPoint(p.time, p.cash + shift, p.position_value, p.equity + shift)

        out.sessions += r.sessions
        out.trades += r.trades
        out.orders += r.orders
        out.fills += r.fills
        out.rejected += r.rejected
        out.shadow += r.shadow
        out.equity += [moved(p) for p in r.equity]
        out.daily_equity += [moved(p) for p in r.daily_equity]
        offset += r.final_cash - r.initial_capital
    out.final_cash = initial + offset
    return out


def window_label(w: Window) -> str:
    return f"fold{w.fold}-{w.partition}"
