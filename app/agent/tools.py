"""Read-only research tools for the AI agent (Phase 10).

Rules enforced here, for every tool:
- **Strict input.** Pydantic models with ``extra="forbid"`` and bounds
  (symbols must be in the configured universe; row and day counts capped by
  ``agent.*``).
- **Budget.** At most ``agent.max_tool_calls`` executions per run;
  exceeding it raises ``ToolBudgetExceeded``.
- **Read-only access.** Tools read only through existing project modules:
  calendar, the market-data provider interface (completed candles only, as
  of the evaluation time), ``run_scan``, ``RiskCostEngine``,
  ``round_trip``, ``PaperStore`` queries, and fixed report locations.
  There is no shell, no arbitrary file path, no network call beyond the
  configured market-data provider, and no broker action. The market-data
  provider is the data-only interface; the Groww adapter cannot reach order
  endpoints.
- **Provenance.** Every output carries its source, data mode and as-of time.
  Every call is recorded (arguments, output or error, timing).
- **No fabrication.** A failed or unavailable tool returns an error or
  ``UNAVAILABLE`` record, never a substitute value. ``get_news`` returns
  UNAVAILABLE because no approved, source-attributed news provider is
  configured.
- **Untrusted text.** Text inside tool outputs is data, never instructions.
  Instruction-like content is detected and flagged (``untrusted_flags``).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.data.base_provider import MarketDataProvider
from app.data.models import DataError, parse_interval
from app.utils.market_calendar import MarketCalendar

INJECTION = re.compile(
    r"(?i)(ignore (all |any |the )?(previous|prior|above) (instructions|rules)|disregard .{0,30}"
    r"(instructions|constraints)|you are now|system prompt|place (an? )?(buy|sell )?order|"
    r"execute (a )?trade|cancel (the |all )?orders?|modify (the )?order|override (the )?risk|"
    r"disable .{0,20}(risk|limit))")


class ToolError(RuntimeError):
    pass


class ToolBudgetExceeded(ToolError):
    pass


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_In):
    pass


class SymbolArg(_In):
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9&_.-]+$")


class CandlesArg(SymbolArg):
    interval: Literal["5m", "1d"] = "5m"
    rows: int = Field(30, ge=1, le=400)


class ScreeningArg(_In):
    limit: int = Field(20, ge=1, le=50)


class CostArg(_In):
    price: float = Field(gt=1, le=100000)
    quantity: int = Field(ge=1, le=10000)
    exit_price: float | None = Field(None, gt=1, le=100000)


class HistoryArg(_In):
    symbol: str | None = Field(None, max_length=32, pattern=r"^[A-Z0-9&_.-]+$")
    days: int = Field(5, ge=1, le=30)


def jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=lambda o: str(o) if isinstance(
        o, (Decimal, datetime, date, Path)) else getattr(o, "value", str(o))))


def find_injection(obj: Any) -> list[str]:
    hits = []
    if isinstance(obj, str):
        m = INJECTION.search(obj)
        if m:
            hits.append(m.group(0)[:80])
    elif isinstance(obj, dict):
        for v in obj.values():
            hits += find_injection(v)
    elif isinstance(obj, list):
        for v in obj:
            hits += find_injection(v)
    return hits


@dataclass
class ToolCall:
    id: str
    tool: str
    args: dict
    ok: bool
    output: dict | None
    error: str | None
    started_at: str
    duration_ms: int
    requested_by: str  # "workflow" or "model"
    untrusted_flags: list[str] = field(default_factory=list)


class ToolContext:
    """Everything the tools may read, fixed for one research run."""

    def __init__(self, settings: Settings, calendar: MarketCalendar,
                 provider: MarketDataProvider, store, now: datetime, agent_store=None,
                 point_in_time: bool = False):
        self.settings, self.calendar, self.provider = settings, calendar, provider
        self.store, self.now, self.agent_store = store, now.astimezone(calendar.tz), agent_store
        # Historical replay (Phase 11): ``now`` is in the past, so anything that is
        # not timestamped as of ``now`` (report files on disk) is withheld.
        self.point_in_time = point_in_time
        self.universe = {i.symbol for i in provider.list_instruments()}
        self._scan = None

    def scan(self):
        if self._scan is None:
            from app.scan.pipeline import run_scan

            self._scan = run_scan(self.provider, self.settings, self.calendar, self.now)
        return self._scan

    def candidate(self, symbol: str):
        return next((c for c in self.scan().candidates if c.symbol == symbol), None)

    def check_symbol(self, symbol: str) -> None:
        if symbol not in self.universe:
            raise ToolError(f"symbol {symbol!r} is not in the configured universe")


def _prov(ctx: ToolContext, source: str, **extra) -> dict:
    return {"source": source, "as_of": ctx.now.isoformat(),
            "data_mode": getattr(ctx.provider.mode, "value", str(ctx.provider.mode)), **extra}


# -- tools ---------------------------------------------------------------------------------


def t_market_status(ctx: ToolContext, a: NoArgs) -> dict:
    cal, now = ctx.calendar, ctx.now
    day = now.date()
    out = {"now": now, "market_date": day, "is_trading_day": cal.is_trading_day(day),
           "is_regular_session": cal.is_regular_session(day),
           "session_status": cal.session_status(now).value,
           "holiday_name": cal.holidays.get(day), "holiday_list_covers_year":
               cal.covers_year(day.year)}
    if cal.is_trading_day(day):
        o, c = cal.session_bounds(day)
        out.update(session_open=o, session_close=c)
    return {"result": out, "provenance": _prov(ctx, "NSE holiday calendar (config)")}


def t_data_health(ctx: ToolContext, a: NoArgs) -> dict:
    s, cal, now = ctx.settings, ctx.calendar, ctx.now
    day = now.date()
    iv = parse_interval(s.strategy.candle_interval)
    rows, fresh = [], 0
    if cal.is_trading_day(day):
        open_dt, close_dt = cal.session_bounds(day)
        effective = min(now, close_dt)
        expected = open_dt + ((effective - open_dt) // iv.timedelta) * iv.timedelta \
            if effective >= open_dt + iv.timedelta else None
        tol = timedelta(seconds=s.data.max_staleness_seconds)
        for sym in sorted(ctx.universe)[:50]:
            try:
                frame = ctx.provider.get_candles(sym, iv.label, day, day).completed(now)
                last = (frame["timestamp"].iloc[-1] + iv.timedelta) if not frame.empty else None
                if expected is None:
                    state = "SESSION_NOT_STARTED"
                elif last is None or last < expected - tol:
                    state = "STALE"
                else:
                    state = "OK"
                rows.append({"symbol": sym, "state": state, "last_candle_end": last,
                             "expected_end": expected})
            except DataError as exc:
                rows.append({"symbol": sym, "state": "ERROR", "error": str(exc)[:200]})
            fresh += rows[-1]["state"] == "OK"
    health = ctx.provider.health() if hasattr(ctx.provider, "health") else None
    beat = ctx.store.get_meta("heartbeat") if ctx.store is not None else None
    return {"result": {"symbols": rows, "fresh_symbols": fresh, "total_symbols": len(rows),
                       "provider_health": health, "paper_engine_heartbeat": beat},
            "provenance": _prov(ctx, f"market-data provider {ctx.provider.name}")}


def _setup(c) -> dict | None:
    if c.setup is None:
        return None
    st = c.setup
    return {"signal_id": st.signal_id, "signal_time": st.signal_time, "valid_until":
            st.valid_until, "entry_reference": st.entry_reference, "stop": st.stop_reference,
            "target": st.target_reference, "entry_trigger": st.entry_trigger,
            "planned_reward_risk": st.planned_reward_risk}


def t_screening(ctx: ToolContext, a: ScreeningArg) -> dict:
    scan = ctx.scan()
    order = {c.symbol: i for i, c in enumerate(scan.ranked)}
    cands = sorted(scan.candidates, key=lambda c: (c.symbol not in order,
                                                   order.get(c.symbol, 0), c.symbol))
    rows = [{"symbol": c.symbol, "screen_status": c.screen.status.value,
             "signal_status": c.signal_status.value, "score": c.score,
             "last_price": c.screen.last_price, "last_price_time": c.screen.last_price_time,
             "avg_traded_value": c.screen.avg_traded_value,
             "reasons": [r.message for r in c.rejection_reasons[:3]],
             "setup": _setup(c)} for c in cands[:a.limit]]
    return {"result": {"decision": scan.decision.value, "candidates": rows,
                       "ranked_symbols": [c.symbol for c in scan.ranked],
                       "config_version": scan.config_version, "notes": scan.notes},
            "provenance": _prov(ctx, f"run_scan ({ctx.provider.name})")}


def t_candles(ctx: ToolContext, a: CandlesArg) -> dict:
    ctx.check_symbol(a.symbol)
    rows = min(a.rows, ctx.settings.agent.max_candle_rows)
    day = ctx.now.date()
    if a.interval == "5m":
        start = day - timedelta(days=1 if rows <= 75 else 7)
    else:
        start = day - timedelta(days=int(rows * 1.6) + 7)
    series = ctx.provider.get_candles(a.symbol, a.interval, start, day)
    frame = series.completed(ctx.now).tail(rows)  # completed candles only: no look-ahead
    data = [[r.timestamp, r.open, r.high, r.low, r.close, r.volume]
            for r in frame.itertuples(index=False)]
    return {"result": {"symbol": a.symbol, "interval": a.interval,
                       "columns": ["start", "open", "high", "low", "close", "volume"],
                       "rows": data, "warnings": series.warnings[:5]},
            "provenance": _prov(ctx, series.source, retrieved_at=series.retrieved_at,
                                completed_candles_only=True)}


def t_indicators(ctx: ToolContext, a: SymbolArg) -> dict:
    ctx.check_symbol(a.symbol)
    c = ctx.candidate(a.symbol)
    if c is None:
        raise ToolError(f"{a.symbol} was not part of the scan")
    if c.screen.status.value != "PASS":
        return {"result": {"symbol": a.symbol, "status": "NOT_COMPUTED",
                           "reason": f"screen status {c.screen.status.value}: indicators are "
                                     "only computed for screen-eligible stocks"},
                "provenance": _prov(ctx, "strategy evaluate (not run)")}
    return {"result": {"symbol": a.symbol, "signal_status": c.signal_status.value,
                       "indicators": c.indicators.model_dump(mode="json"),
                       "qualification": [r.message for r in c.qualification_reasons[:8]],
                       "rejections": [r.message for r in c.rejection_reasons[:8]]},
            "provenance": _prov(ctx, "opening_range_breakout.evaluate on completed candles")}


def _risk_state(ctx: ToolContext):
    from app.risk.risk_limits import DailyRiskState

    day = ctx.now.date()
    if ctx.store is not None and ctx.store.session(day) is not None:
        from app.paper.engine import PaperEngine

        return PaperEngine(ctx.provider, ctx.settings, ctx.calendar, ctx.store)._risk_state(day)
    try:
        cash = Decimal(ctx.store.portfolio()["cash"]) if ctx.store is not None else None
    except Exception:  # noqa: BLE001 - no paper portfolio yet
        cash = None
    from app.costs.money import D

    return DailyRiskState.new_session(day, account_capital=cash if cash is not None
                                      else D(ctx.settings.capital.initial_capital))


def t_risk(ctx: ToolContext, a: SymbolArg) -> dict:
    from app.risk.engine import RiskCostEngine
    from app.risk.interfaces import TradeEvaluationRequest

    ctx.check_symbol(a.symbol)
    c = ctx.candidate(a.symbol)
    if c is None or c.setup is None:
        return {"result": {"symbol": a.symbol, "status": "NO_SETUP",
                           "reason": "no qualified strategy setup to size or cost"},
                "provenance": _prov(ctx, "RiskCostEngine (not run)")}
    engine = RiskCostEngine(ctx.settings)
    r = engine.evaluate(TradeEvaluationRequest.from_candidate(c, ctx.settings), _risk_state(ctx))
    at_t = r.at_target
    return {"result": {
        "symbol": a.symbol, "signal_id": c.setup.signal_id, "status": r.status.value,
        "final_quantity": r.final_quantity, "binding_constraint": r.binding_constraint,
        "available_capital": r.available_capital, "risk_budget": r.risk_budget,
        "position_value": r.position_value,
        "planned_net_loss_at_stop": r.planned_net_loss_at_stop,
        "net_reward_risk": r.net_reward_risk, "break_even_exit": r.break_even_exit,
        "costs_at_target": None if at_t is None else {
            "total_costs": at_t.total_costs, "spread_cost": at_t.spread_cost,
            "slippage_cost": at_t.slippage_cost, "net_pnl": at_t.net_pnl},
        "spread_pct_used": r.spread_pct_used, "spread_source": r.spread_source,
        "slippage_pct_per_side": r.slippage_pct_per_side,
        "fee_schedule": r.cost_model.schedule_id if r.cost_model else None,
        "rejection_reasons": [f"{x.code}: {x.message}" for x in r.rejection_reasons]},
        "provenance": _prov(ctx, "Phase 3 RiskCostEngine + configured broker tariff")}


def t_costs(ctx: ToolContext, a: CostArg) -> dict:
    from app.costs.calculator import round_trip
    from app.costs.money import D
    from app.costs.schedule import load_fee_schedule

    sched = load_fee_schedule(ctx.settings)
    rates = sched.rates_on(ctx.now.date(),
                           ctx.settings.cost_review.apply_current_rates_to_past_dates)
    exit_p = a.exit_price or a.price
    rt = round_trip(rates, D(a.price), D(exit_p), a.quantity, ctx.settings.execution, None)
    return {"result": {"hypothetical": True, "price": a.price, "exit_price": exit_p,
                       "quantity": a.quantity, "gross_pnl": rt.gross_pnl,
                       "fees_total": rt.fees_total, "spread_cost": rt.execution.spread_total,
                       "slippage_cost": rt.execution.slippage_total,
                       "total_costs": rt.total_costs, "net_pnl": rt.net_pnl},
            "provenance": _prov(ctx, f"fee schedule {sched.schedule_id} (verified "
                                     f"{sched.verified_on})")}


def t_portfolio(ctx: ToolContext, a: NoArgs) -> dict:
    s = ctx.settings
    out: dict = {"configured_capital": s.capital.initial_capital,
                 "limits": {"risk_pct": s.risk.risk_pct, "max_daily_loss": s.risk.max_daily_loss,
                            "max_trades_per_day": s.risk.max_trades_per_day,
                            "max_concurrent_positions": s.risk.max_concurrent_positions},
                 "paper_portfolio": None}
    if ctx.store is not None:
        try:
            pf = ctx.store.portfolio()
            sess = ctx.store.session(ctx.now.date())
            out["paper_portfolio"] = {
                "cash": pf["cash"], "reserved": pf["reserved"],
                "available": str(Decimal(pf["cash"]) - Decimal(pf["reserved"])),
                "open_positions": len([p for p in ctx.store.positions()
                                       if p["state"] != "CLOSED"]),
                "session_locked": bool(sess["locked"]) if sess else None,
                "lock_reason": sess["lock_reason"] if sess else None}
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            out["paper_portfolio"] = f"unavailable: {exc}"
    return {"result": out, "provenance": _prov(ctx, "paper database (simulated portfolio)")}


def t_reports(ctx: ToolContext, a: NoArgs) -> dict:
    s = ctx.settings
    out: dict = {}
    if ctx.store is not None:
        dates = ctx.store.report_dates()
        if ctx.point_in_time:  # only reports of sessions that ended before the decision
            dates = [d for d in dates if d < ctx.now.date().isoformat()]
        if dates:
            body = ctx.store.report(date.fromisoformat(dates[-1]))
            out["latest_paper_report"] = {"trading_date": dates[-1],
                                          "totals": body.get("totals")}
        try:
            ev = ctx.store.one("SELECT evaluation_id, name, start_date FROM evaluations WHERE "
                               "active = 1")
            out["active_evaluation"] = dict(ev) if ev else None
        except Exception:  # noqa: BLE001
            out["active_evaluation"] = None
    rob = sorted(Path(s.robustness.output_dir).glob("*/*/summary.json"),
                 key=lambda p: p.stat().st_mtime)
    if ctx.point_in_time:
        out["robustness"] = ("withheld in historical replay: report files are not "
                             "timestamped as of the decision time")
    elif rob:
        body = json.loads(rob[-1].read_text())
        out["robustness"] = {"status": body.get("status"),
                             "verdict": body.get("assessment", {}).get("verdict")}
    else:
        out["robustness"] = "no robustness report"
    return {"result": out, "provenance": _prov(ctx, "paper database and report files")}


def t_news(ctx: ToolContext, a: SymbolArg) -> dict:
    ctx.check_symbol(a.symbol)
    return {"result": {"symbol": a.symbol, "status": "UNAVAILABLE",
                       "reason": "no approved, source-attributed news or corporate-event "
                                 "provider is configured; no news is known to this system"},
            "provenance": _prov(ctx, "none")}


def t_history(ctx: ToolContext, a: HistoryArg) -> dict:
    if a.symbol:
        ctx.check_symbol(a.symbol)
    if ctx.agent_store is None:
        return {"result": {"recommendations": []}, "provenance": _prov(ctx, "agent store")}
    days = min(a.days, ctx.settings.agent.max_history_days)
    since = (ctx.now.date() - timedelta(days=days)).isoformat()
    recs = ctx.agent_store.recent(since, ctx.now.isoformat(), a.symbol)
    return {"result": {"recommendations": recs[:30]},
            "provenance": _prov(ctx, "agent recommendation store (append-only)")}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_In]
    fn: Any


TOOLS = {t.name: t for t in [
    ToolSpec("get_market_status", "Exchange calendar and session state now.", NoArgs,
             t_market_status),
    ToolSpec("get_data_health", "Freshness of completed candles per universe symbol and "
                                "provider health.", NoArgs, t_data_health),
    ToolSpec("get_screening_results", "Existing screening/strategy scan: screen status, signal "
                                      "status, score, reasons, strategy setup.", ScreeningArg,
             t_screening),
    ToolSpec("get_candles", "Completed OHLCV candles (5m or 1d) up to the evaluation time.",
             CandlesArg, t_candles),
    ToolSpec("get_indicators", "Opening range, VWAP, EMA, relative volume and rule results "
                               "from the existing strategy.", SymbolArg, t_indicators),
    ToolSpec("check_risk_and_costs", "Existing Phase 3 risk/cost engine on the symbol's "
                                     "strategy setup: whole-share quantity, costs, net "
                                     "reward:risk, approval or rejection.", SymbolArg, t_risk),
    ToolSpec("estimate_costs", "Hypothetical round-trip costs from the configured broker "
                               "tariff.", CostArg, t_costs),
    ToolSpec("get_portfolio_risk", "Simulated paper portfolio and configured risk limits "
                                   "(read-only).", NoArgs, t_portfolio),
    ToolSpec("get_reports_summary", "Latest paper report totals, evaluation and robustness "
                                    "status.", NoArgs, t_reports),
    ToolSpec("get_news", "Source-attributed news/events (currently UNAVAILABLE: no approved "
                         "source).", SymbolArg, t_news),
    ToolSpec("get_past_recommendations", "Earlier persisted recommendations (append-only).",
             HistoryArg, t_history),
]}


class ToolRegistry:
    def __init__(self, ctx: ToolContext, max_calls: int, recorder=None,
                 model_tools: list[str] | None = None):
        self.ctx, self.max_calls, self.recorder = ctx, max_calls, recorder
        self.calls: list[ToolCall] = []
        unknown = set(model_tools or ()) - set(TOOLS)
        if unknown:
            raise ToolError(f"unknown tools in the allow-list: {sorted(unknown)}")
        # Tools the MODEL may request (None = all approved tools). The workflow's
        # own deterministic calls are unaffected.
        self.model_tools = set(model_tools) if model_tools is not None else set(TOOLS)

    def definitions(self) -> list[dict]:
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.args.model_json_schema()} for t in TOOLS.values()
                if t.name in self.model_tools]

    def execute(self, name: str, args: dict | None, requested_by: str = "workflow") -> ToolCall:
        if len(self.calls) >= self.max_calls:
            raise ToolBudgetExceeded(f"tool-call limit {self.max_calls} reached")
        cid = f"T{len(self.calls) + 1}"
        started = time.monotonic()
        stamp = datetime.now(self.ctx.calendar.tz).isoformat()
        spec = TOOLS.get(name)
        output, error = None, None
        try:
            if spec is None:
                raise ToolError(f"unknown tool {name!r}; only approved tools are available")
            if requested_by == "model" and name not in self.model_tools:
                raise ToolError(f"tool {name!r} is not enabled for this agent version")
            parsed = spec.args.model_validate(args or {})
            output = jsonable(spec.fn(self.ctx, parsed))
            if "provenance" not in output or "result" not in output:
                raise ToolError("tool output missing result/provenance")
        except ValidationError as exc:
            error = f"invalid arguments: {exc.errors(include_url=False)[:3]}"
        except (ToolError, DataError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - recorded, never hidden
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
        call = ToolCall(cid, name, jsonable(args or {}), error is None, output, error, stamp,
                        int((time.monotonic() - started) * 1000), requested_by,
                        find_injection(output) if output else [])
        self.calls.append(call)
        if self.recorder is not None:
            self.recorder(call)
        return call

    def by_id(self, cid: str) -> ToolCall | None:
        return next((c for c in self.calls if c.id == cid), None)
