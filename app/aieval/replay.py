"""Historical replay: deterministic baseline (A) vs AI-assisted (B), Phase 11.

Both arms run the UNCHANGED Phase 4 engine (``run_backtest``) on the same
dataset, sessions, frozen settings, fee schedule and candle-v1 fills, each
from the same initial capital. They differ in one place only:

- At every candle close the engine scans for new signals. A thin wrapper
  around the engine's ``run_scan`` sees each NEW strategy setup at its signal
  time. Arm A records it and changes nothing. Arm B also asks the Phase 10
  research agent, run at that same timestamp on the same historical data with
  point-in-time tools (completed candles only; report files withheld). If
  the AI does not say "consider" for the signal, it is removed before the
  risk engine sees it (a veto). The engine then sizes, costs and risk-checks
  what is left with arm B's own portfolio state.

The wrapper patches the engine module's ``run_scan`` name only for the
duration of one simulation (and restores it). No frozen decision-path file
is modified, so the frozen baseline's code hash is unchanged. Without a
decider the wrapper is a pure observer, and arm A equals a plain
``run_backtest`` exactly (tested).

Every signal gets a decision record for each arm in the immutable ledger:
point-in-time inputs, the engine's indicative risk result, the AI's output
and the final decision. Outcomes (closed trade, unfilled order, per-share
price path) are appended separately.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import app.backtesting.engine as engine_mod
from app.aieval import metrics as M
from app.aieval.ledger import DuplicateRun, Ledger, canonical, sha
from app.aieval.protocol import ai_versions, load_protocol, pin_mismatches
from app.aieval.quality import decision_quality, load_agent_records
from app.backtesting.dataset import DatasetError
from app.backtesting.engine import STRATEGY_VERSION, run_backtest
from app.costs.money import D
from app.data.models import DataError
from app.utils.fs import private_dir, write_preserving
from app.utils.market_calendar import MarketCalendar

SPLITS = ("development", "validation", "holdout")


class ReplayRefused(RuntimeError):
    pass


@dataclass
class AIDecision:
    take: bool
    reason: str
    ai_status: str | None = None
    recommendation: str | None = None  # consider | wait_for_confirmation | no_trade
    rec_status: str | None = None
    recommendation_id: str | None = None
    agent_run_id: str | None = None
    data_fresh: bool | None = None
    agent_risk_status: str | None = None
    unsupported_claims: int = 0

    def category(self) -> str:
        if self.recommendation_id is None:
            return "not_reviewed"
        if self.ai_status != "OK":
            return "ai_unavailable_or_invalid"
        if not self.data_fresh:
            return "stale_data"
        return self.recommendation or "not_reviewed"


class AgentDecider:
    """Runs the Phase 10 research agent at a signal's timestamp (point in time)
    and reads back its immutable recommendation for that signal."""

    def __init__(self, settings, calendar, provider, store_path: Path, llm, max_runs: int,
                 **agent_kwargs):
        from app.agent.workflow import ResearchAgent
        from app.paper.store import PaperStore

        self.store = PaperStore(store_path)
        # agent_kwargs: a registered version's prompt variant and tool allow-list (Phase 14).
        self.agent = ResearchAgent(settings, calendar, provider, self.store, llm,
                                   point_in_time=True, **agent_kwargs)
        self.max_runs, self.runs, self.failures = max_runs, 0, []

    def decide(self, cand, now: datetime) -> AIDecision:
        from app.agent.workflow import TERMINAL

        st, day = self.agent.agent_store, now.date().isoformat()
        latest = st.latest_run(day, "intraday", self.agent._slot("intraday", now))
        if latest is None or latest["status"] not in TERMINAL:
            if self.runs >= self.max_runs:
                return AIDecision(False, f"AI budget exhausted ({self.max_runs} runs); not "
                                         "reviewed, so not taken", "UNAVAILABLE")
            self.runs += 1
            try:
                self.agent.run("intraday", now)
            except Exception as exc:  # noqa: BLE001 - recorded; the signal is not taken
                self.failures.append(f"{now.isoformat()}: {type(exc).__name__}: {exc}"[:300])
                return AIDecision(False, f"agent run failed: {exc}"[:300], "UNAVAILABLE")
        sid = cand.setup.signal_id
        bodies = [json.loads(r["body"]) for r in st.recommendations(market_date=day)]
        match = [b for b in bodies if b.get("signal_id") == sid
                 and datetime.fromisoformat(b["created_at"]) <= now]
        if not match:
            run = st.latest_run(day, "intraday", self.agent._slot("intraday", now))
            why = (f"agent run {run['status']}: {run['reason']}" if run and run["reason"]
                   else f"not in the agent's shortlist (max_candidates "
                        f"{self.agent.cfg.max_candidates})")
            return AIDecision(False, f"not reviewed by the AI ({why}); not taken"[:400])
        b = match[-1]
        rec = (b.get("ai_assessment") or {}).get("recommendation")
        take = b["ai_status"] == "OK" and rec == "consider" and bool(b["data_fresh"])
        if take:
            reason = "AI assessment: consider; the engine's risk check decides"
        elif b["ai_status"] != "OK":
            reason = f"AI {b['ai_status']}: not taken"
        elif not b["data_fresh"]:
            reason = "agent data gate: stale data; not taken"
        else:
            reason = f"AI assessment: {rec} (veto)"
        return AIDecision(take, reason, b["ai_status"], rec, b["status"],
                          b["recommendation_id"], b["run_id"], bool(b["data_fresh"]),
                          b.get("risk_status"), len(b.get("unsupported_claims", [])))


@contextmanager
def intercept_scans(log: list, decider=None):
    """Observe (and for arm B, veto) new setups at their signal time."""
    original = engine_mod.run_scan

    def wrapped(provider, settings, calendar, as_of, *args, **kwargs):
        scan = original(provider, settings, calendar, as_of, *args, **kwargs)
        new = [c for c in scan.ranked if c.setup is not None and c.setup.signal_time == as_of]
        vetoed = set()
        for c in new:
            decision = decider.decide(c, as_of) if decider is not None else None
            log.append({"time": as_of, "cand": c, "ai": decision})
            if decision is not None and not decision.take:
                vetoed.add(c.setup.signal_id)
        if vetoed:
            scan = scan.model_copy(update={"ranked": [c for c in scan.ranked
                                                      if c.setup.signal_id not in vetoed]})
        return scan

    engine_mod.run_scan = wrapped
    try:
        yield
    finally:
        engine_mod.run_scan = original


def run_arm(dataset, settings, calendar, start, end, membership=None, decider=None,
            shadow=False):
    log: list[dict] = []
    with intercept_scans(log, decider):
        result = run_backtest(dataset, settings, calendar, start, end, membership, shadow)
    return result, log


# -- helpers ---------------------------------------------------------------------------


def split_sessions(dataset, settings, calendar, protocol) -> tuple[dict, int, str]:
    from app.robustness.walk_forward import warmup_length

    warm, why = warmup_length(dataset, settings, calendar)
    sessions = [d for d in dataset.sessions() if calendar.is_regular_session(d)]
    tradable = sessions[warm:]
    n = len(tradable)
    sp = protocol.splits
    k_dev, k_val = int(n * sp.development_fraction), int(n * sp.validation_fraction)
    return ({"development": tradable[:k_dev], "validation": tradable[k_dev:k_dev + k_val],
             "holdout": tradable[k_dev + k_val:]}, warm, why)


def trade_dict(t) -> dict:
    return {"signal_id": t.signal_id, "symbol": t.symbol, "session": t.session,
            "entry_time": t.entry_time.isoformat(), "exit_time": t.exit_time.isoformat(),
            "entry_price": t.entry_price, "exit_price": t.exit_price, "stop": t.stop,
            "target": t.target, "quantity": t.quantity, "gross": t.gross_pnl,
            "costs": t.total_costs, "net": t.net_pnl, "buy_turnover": t.buy_turnover,
            "exit_reason": t.exit_reason, "incomplete": t.incomplete, "ambiguous": t.ambiguous,
            "fees": {"brokerage": t.brokerage, "stt": t.stt, "stamp_duty": t.stamp_duty,
                     "exchange_transaction": t.exchange_transaction, "ipft": t.ipft,
                     "sebi": t.sebi_turnover_fee, "gst": t.gst, "spread": t.spread_cost,
                     "slippage": t.slippage_cost}}


def engine_outcomes(result) -> dict[str, tuple[str, object]]:
    out: dict[str, tuple[str, object]] = {t.signal_id: ("TRADE", t) for t in result.trades}
    for r in result.rejected:
        out.setdefault(r.signal_id, ("RISK_REJECTED" if r.stage == "risk" else "NOT_FILLED", r))
    return out


def _r_multiple(entry, exit_, stop):
    risk = entry - stop
    return round(float((exit_ - entry) / risk), 4) if risk > 0 else None


def indicative_risk(cand, settings, engine, initial: Decimal) -> dict:
    """Risk/cost result with a fresh-capital state: the engine's view of the
    signal on its own (the arm's actual check also sees its open positions)."""
    from app.risk.interfaces import TradeEvaluationRequest
    from app.risk.risk_limits import DailyRiskState

    r = engine.evaluate(TradeEvaluationRequest.from_candidate(cand, settings),
                        DailyRiskState.new_session(cand.setup.signal_time.date(),
                                                   account_capital=initial))
    at = r.at_target
    return {"status": r.status.value, "quantity": r.final_quantity,
            "estimated_costs_at_target": str(at.total_costs) if at else None,
            "net_reward_risk": str(r.net_reward_risk) if r.net_reward_risk is not None
            else None,
            "planned_net_loss_at_stop": str(r.planned_net_loss_at_stop)
            if r.planned_net_loss_at_stop is not None else None,
            "reasons": [f"{x.code}: {x.message}" for x in r.rejection_reasons][:5],
            "basis": "fresh-capital risk state at the decision time (indicative)"}


def _final(kind: str | None, ai: AIDecision | None) -> tuple[str, str]:
    if ai is not None and not ai.take:
        return "VETOED_BY_AI", ai.reason
    if kind == "TRADE":
        return "TAKEN", "risk engine approved; order placed"
    if kind == "NOT_FILLED":
        return "TAKEN", "risk engine approved; order placed but not filled"
    if kind == "RISK_REJECTED":
        return "REJECTED_BY_RISK", "risk/cost engine rejected the signal"
    return "NOT_EVALUATED", "engine skipped the signal (position or order already open)"


def risk_violations(trades: list[dict], settings) -> dict:
    per_day = Counter(t["session"] for t in trades)
    over_day = sum(1 for k in per_day.values() if k > settings.risk.max_trades_per_day)
    worst = 0
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["session"]].append((t["entry_time"], t["exit_time"]))
    for spans in by_day.values():
        for s, _ in spans:
            worst = max(worst, sum(1 for a, b in spans if a <= s < b))
    over_conc = int(worst > settings.risk.max_concurrent_positions)
    return {"sessions_over_max_trades_per_day": over_day,
            "max_concurrent_positions_seen": worst,
            "concurrency_limit_exceeded": over_conc,
            "total": over_day + over_conc}


# -- evaluation -----------------------------------------------------------------------


def evaluate_replay(live_settings, data_dir: Path, split: str, ledger: Ledger, *,
                    llm=None, protocol_path: Path | None = None,
                    baseline_path: Path | None = None, now: datetime | None = None,
                    decider_factory=None) -> dict:
    from app.agent.llm import build_llm
    from app.robustness.baseline import load_frozen
    from app.robustness.data_audit import membership_fn
    from app.robustness.runner import MANIFEST, RunOptions, _missing_data, load_data

    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    cfg = live_settings.ai_eval
    protocol, _, psha = load_protocol(protocol_path or cfg.protocol_file)
    frozen = load_frozen(baseline_path or protocol.baseline_path())
    if frozen.code_drift:
        raise ReplayRefused("decision-path code changed since the baseline was frozen; arm A "
                            "would not be the frozen baseline. Freeze a new baseline.")
    hist = frozen.historical_settings()
    calendar = MarketCalendar(hist.market)
    now = (now or datetime.now(calendar.tz)).astimezone(calendar.tz)
    llm = llm or build_llm(live_settings)
    versions = ai_versions(live_settings, llm)
    drift = pin_mismatches(protocol, versions)
    meta = {"mode": "HISTORICAL_REPLAY", "split": split, "protocol_version":
            protocol.protocol_version, "protocol_sha256": psha,
            "baseline_name": frozen.name, "baseline_fingerprint": frozen.fingerprint,
            "baseline_code_hash": frozen.body["code_hash"], "ai_versions": versions,
            "ai_system_drift": drift, "data_dir": str(data_dir)}

    missing = _missing_data(data_dir)
    dataset = None
    if not missing:
        try:
            dataset, bench, rows, audit, limitations = load_data(
                RunOptions(data_dir=Path(data_dir)), hist, calendar,
                frozen.protocol.regimes.benchmark_symbol)
        except (DatasetError, DataError) as exc:
            missing = [f"dataset unusable: {exc}"]
    if not missing:
        splits, warm, warm_why = split_sessions(dataset, hist, calendar, protocol)
        sessions = splits[split]
        if not sessions:
            missing = [f"the {split} split has no sessions ({sum(map(len, splits.values()))} "
                       f"tradable sessions after a {warm}-session warm-up)"]
    if missing:
        return {"status": "PENDING_DATA", "meta": meta, "missing": missing,
                "conclusion": {"conclusion": "PENDING_DATA", "reason": "; ".join(missing)}}

    start, end = sessions[0], sessions[-1]
    meta.update(dataset_id=dataset.dataset_id, period=[start, end], warmup_sessions=warm,
                warmup_reason=warm_why,
                split_sizes={k: len(v) for k, v in splits.items()},
                data_provenance="fetch manifest present" if (Path(data_dir) / MANIFEST).exists()
                else "no fetch manifest: provenance unverified (synthetic or hand-made data "
                     "cannot be told apart from fetched data)")
    labels = ["HISTORICAL_REPLAY", "SECONDARY_EVIDENCE: the model may have seen text "
              "covering these dates in training (hindsight contamination cannot be excluded)"]
    labels.append("UNBIASED_HOLDOUT (single use)" if split == "holdout" else
                  f"{split.upper()}: not final evidence")
    if drift:
        labels.append("AI_SYSTEM_DRIFT: " + "; ".join(drift))
    run_key = canonical({k: meta[k] for k in ("mode", "split", "protocol_sha256",
                                              "baseline_fingerprint", "dataset_id",
                                              "period")}
                        | {"ai": {k: versions[k] for k in ("prompt_version", "model",
                                                           "tool_schema_sha256",
                                                           "agent_code_sha256")}})
    run_id = "AE-" + sha(run_key)[:12]
    if split == "holdout":
        if drift:
            raise ReplayRefused("holdout refused: the AI system differs from the protocol "
                                f"pins ({'; '.join(drift)}). A changed AI system needs a new "
                                "protocol version and a fresh period.")
        used = ledger.holdout_runs(psha, dataset.dataset_id)
        if used:
            raise ReplayRefused(f"holdout refused: already used by {used[0]['run_id']} for "
                                "this protocol and dataset. It is single use; a new protocol "
                                "version needs fresh data.")
    if ledger.run(run_id) is not None:
        raise DuplicateRun(f"this evaluation already exists as {run_id}; it is never "
                           "processed twice")

    membership = membership_fn(rows)
    initial = D(hist.capital.initial_capital)
    res_a, log_a = run_arm(dataset, hist, calendar, start, end, membership, None, shadow=True)
    slots = sorted({e["time"] for e in log_a})
    if len(slots) > cfg.max_agent_runs:
        raise ReplayRefused(f"arm B would need {len(slots)} research runs, above "
                            f"ai_eval.max_agent_runs={cfg.max_agent_runs}; refused before any "
                            "model call (raise the limit knowingly)")

    run_dir = private_dir(Path(cfg.output_dir) / run_id)
    ledger.start_run({"run_id": run_id, "run_key": run_key, "mode": "HISTORICAL_REPLAY",
                      "split": split, "period_start": start, "period_end": end,
                      "dataset_id": dataset.dataset_id,
                      "protocol_version": protocol.protocol_version, "protocol_sha256": psha,
                      "baseline_name": frozen.name,
                      "baseline_fingerprint": frozen.fingerprint,
                      "ai_versions": canonical(versions), "labels": canonical(labels),
                      "started_at": now.isoformat()})
    try:
        agent_settings = hist.model_copy(update={
            "llm": live_settings.llm, "llm_api_key": live_settings.llm_api_key,
            "agent": live_settings.agent.model_copy(update={
                "reports_dir": run_dir / "agent_reports",
                "lock_file": run_dir / "agent.lock"})})
        factory = decider_factory or (lambda: AgentDecider(
            agent_settings, calendar, dataset.provider(calendar), run_dir / "agent.db", llm,
            cfg.max_agent_runs))
        decider = factory()
        res_b, log_b = run_arm(dataset, hist, calendar, start, end, membership, decider)
        results = _results(meta, labels, protocol, frozen, hist, dataset, bench, audit,
                           limitations, sessions, initial, res_a, log_a, res_b, log_b,
                           decider, live_settings, ledger, run_id, now, run_dir)
    except Exception as exc:
        ledger.finish_run(run_id, "FAILED", now, f"{type(exc).__name__}: {exc}"[:500])
        raise
    from app.aieval.report import write_reports

    path, digest = write_reports(results, run_dir)
    ledger.finish_run(run_id, "COMPLETED", now, None, results["conclusion"]["conclusion"],
                      str(path), digest)
    return results


def _results(meta, labels, protocol, frozen, hist, dataset, bench, audit, limitations,
             sessions, initial, res_a, log_a, res_b, log_b, decider, live, ledger, run_id,
             now, run_dir) -> dict:
    from app.risk.engine import RiskCostEngine
    from app.robustness import regimes

    out_a, out_b = engine_outcomes(res_a), engine_outcomes(res_b)
    ai_by_sid = {e["cand"].setup.signal_id: e["ai"] for e in log_b}
    shadow = {s.signal_id: s for s in res_a.shadow}
    risk_engine = RiskCostEngine(hist)
    prompt_model = {"prompt_version": meta["ai_versions"]["prompt_version"],
                    "model": meta["ai_versions"]["model"]}
    decided = {"A": Counter(), "B": Counter()}

    for e in log_a:
        c, setup = e["cand"], e["cand"].setup
        sid = setup.signal_id
        common = {
            "signal_id": sid, "symbol": c.symbol, "exchange": "NSE",
            "decision_time": e["time"].isoformat(), "market_date": e["time"].date().isoformat(),
            "mode": "HISTORICAL_REPLAY",
            "data_sources": [{"source": f"historical CSV dataset {dataset.dataset_id}",
                              "as_of": e["time"].isoformat(),
                              "completed_candles_only": True}],
            "versions": {"strategy_version": STRATEGY_VERSION,
                         "config_version": hist.version(),
                         "baseline_fingerprint": frozen.fingerprint,
                         "fee_schedule_id": res_a.fee_schedule_id,
                         "execution_model": res_a.execution_model_version},
            "screening": {"screen_status": c.screen.status.value,
                          "signal_status": c.signal_status.value, "score": c.score},
            "proposal": {"entry_condition": setup.entry_trigger,
                         "entry_reference": setup.entry_reference, "stop": setup.stop_reference,
                         "target": setup.target_reference,
                         "expiry": setup.valid_until.isoformat()},
            "indicative_risk": indicative_risk(c, hist, risk_engine, initial),
        }
        for system, outs, ai in (("BASELINE", out_a, None), ("AI_ASSISTED", out_b,
                                                               ai_by_sid.get(sid))):
            kind, rec = outs.get(sid, (None, None))
            final, why = _final(kind, ai)
            decided["A" if system == "BASELINE" else "B"][final] += 1
            body = {**common, "system": system, "final_decision": final, "reason": why,
                    "engine_result": {"kind": kind, "status": getattr(rec, "status", None),
                                      "reasons": getattr(rec, "reasons", None)},
                    "evaluation_status": "EVALUATED"}
            if system == "AI_ASSISTED":
                body["versions"] = {**body["versions"], **prompt_model}
                body["ai"] = ai.__dict__ if ai is not None else {
                    "reason": "no AI decision recorded"}
            did = ledger.add_decision(run_id, system, body)
            if kind == "TRADE":
                ledger.add_outcome(did, "TRADE_CLOSED", now, trade_dict(rec))
            elif kind == "NOT_FILLED":
                ledger.add_outcome(did, "ORDER_NOT_FILLED", now, {"reason": rec.reasons})
            elif sid in shadow:
                sh = shadow[sid]
                ledger.add_outcome(did, "PRICE_PATH_DIAGNOSTIC", now, {
                    "filled": sh.filled, "not_filled_reason": sh.not_filled_reason,
                    "gross_per_share": sh.gross_per_share, "r_multiple": sh.r_multiple,
                    "exit_reason": sh.exit_reason, "hypothetical": True})

    ta = [trade_dict(t) for t in res_a.trades]
    tb = [trade_dict(t) for t in res_b.trades]
    eq_a = [(p.time.isoformat(), p.equity) for p in res_a.equity]
    eq_b = [(p.time.isoformat(), p.equity) for p in res_b.equity]
    ma, mb = (M.arm_metrics(ta, sessions, initial, eq_a), M.arm_metrics(tb, sessions, initial,
                                                                         eq_b))
    for m, res, log, dec, trades in ((ma, res_a, log_a, decided["A"], ta),
                                     (mb, res_b, log_b, decided["B"], tb)):
        buys = [o for o in res.orders if o.side == "BUY"]
        m.update({
            "complete_sessions": len(sessions), "incomplete_sessions": 0, "failed_sessions": 0,
            "signals_evaluated": len(log), "decisions": dict(dec),
            "recommendations_taken": dec["TAKEN"],
            "rejected_candidates": dec["REJECTED_BY_RISK"] + dec["VETOED_BY_AI"],
            "no_trade_decisions": len(log) - dec["TAKEN"],
            "paper_orders": len(buys),
            "orders_filled": sum(1 for o in buys if o.status == "FILLED"),
            "orders_partially_filled": 0,
            "orders_unfilled": sum(1 for o in buys if o.status == "CANCELLED"),
            "risk_rejections_by_status": dict(Counter(
                r.status for r in res.rejected if r.stage == "risk")),
            "risk_rule_violations": risk_violations(trades, hist),
            "data_quality_failures": len((audit or {}).get("rejected_sessions", []) or []),
        })
    table_a, table_b = M.session_table(ta, sessions), M.session_table(tb, sessions)
    st = protocol.statistics
    boot = M.paired_bootstrap(table_a, table_b, sessions, st.bootstrap_resamples,
                              st.confidence, st.seed, st.block_length)

    recs, models, tools, runs = load_agent_records(decider.store) if hasattr(
        decider, "store") else ([], [], [], [])
    quality = decision_quality(recs, models, tools, runs)
    usd = quality["operational"]["cost_usd"]
    fx = live.ai_eval.usd_inr_rate
    model_inr = round(usd * fx, 4) if usd is not None and fx else None
    taken_a = {s for s, (k, _) in out_a.items() if k in ("TRADE", "NOT_FILLED")}
    discordant = sum(1 for e in log_a if (e["cand"].setup.signal_id in taken_a) !=
                     (out_b.get(e["cand"].setup.signal_id, (None,))[0] in ("TRADE",
                                                                         "NOT_FILLED")))
    executable_a = len(taken_a)
    structural = None
    if not executable_a:
        structural = ("no baseline signal passed the risk/cost engine at the configured "
                      f"capital (Rs {initial}); a veto-only AI cannot change any trade, so "
                      "trading value is zero by construction in this period")
    selection = M.random_veto_benchmark(ta, tb, st.bootstrap_resamples, st.seed, st.confidence)
    concl = M.assess(protocol, len(sessions), ma, mb, discordant, boot, model_inr, structural,
                     selection)

    proxy, proxy_label = regimes.proxy_series(dataset, bench)
    classes = regimes.classify_sessions(proxy, frozen.protocol.regimes)
    diag = _selection_diagnostic(log_a, ai_by_sid, out_a, shadow)
    per_session = [{"session": d, "A_trades": table_a[d][1], "A_net": M._f(table_a[d][0]),
                    "B_trades": table_b[d][1], "B_net": M._f(table_b[d][0]),
                    "signals": sum(1 for e in log_a if e["time"].date() == d),
                    "ai_vetoes": sum(1 for e in log_b if e["time"].date() == d
                                     and e["ai"] is not None and not e["ai"].take)}
                   for d in sessions]
    feas = Counter(indicative_risk(e["cand"], hist, risk_engine, initial)["status"]
                   for e in log_a)
    return {
        "status": "OK", "run_id": run_id, "meta": meta, "labels": labels,
        "protocol": protocol.model_dump(mode="json"),
        "systems": {
            "A": "deterministic baseline: frozen settings, unchanged Phase 4 engine, Phase 3 "
                 "risk/cost engine, candle-v1 fills",
            "B": "AI-assisted: identical engine and data; the AI agent may only veto a new "
                 "signal at its signal time (point-in-time tools); the risk engine re-checks "
                 "everything left with arm B's own portfolio",
            "ai_may_change": "which deterministic signals are taken (veto only)",
            "ai_may_not_change": "entry, stop, target, quantity, costs, risk limits, fills, "
                                 "data, or add a signal",
        },
        "coverage": {"sessions": len(sessions), "period": [sessions[0], sessions[-1]],
                     "symbols": len(dataset.symbols), "audit": audit,
                     "limitations": limitations, "regime_proxy": proxy_label},
        "arms": {"A": ma, "B": mb},
        "equity": {"A": [(t, M._f(e)) for t, e in
                         ((p.time.isoformat(), p.equity) for p in res_a.daily_equity)],
                   "B": [(t, M._f(e)) for t, e in
                         ((p.time.isoformat(), p.equity) for p in res_b.daily_equity)]},
        "incremental": _incremental(ma, mb, ta, tb, quality, model_inr, fx, len(sessions)),
        "statistics": boot, "discordant_signals": discordant,
        "random_veto_benchmark": selection, "selection_diagnostic": diag,
        "quality": quality,
        "capital": {"initial_capital": float(initial),
                    "indicative_risk_status_counts": dict(feas),
                    "executable_baseline_signals": executable_a,
                    "note": "quantities are whole shares; costs include brokerage, STT, "
                            "exchange charges, GST, SEBI fee, stamp duty, spread and slippage "
                            "(Phase 3 engine). Model/API cost is reported separately from "
                            "trading costs."},
        "per_session": per_session,
        "regimes": M.regime_table(classes, sessions, table_a, table_b),
        "agent": {"research_runs": getattr(decider, "runs", None),
                  "failures": getattr(decider, "failures", [])},
        "conclusion": concl,
    }


def _incremental(ma, mb, ta, tb, quality, model_inr, fx, n_sessions) -> dict:
    def diff(k):
        a, b = ma.get(k), mb.get(k)
        return round(b - a, 2) if a is not None and b is not None else None

    usd = quality["operational"]["cost_usd"]
    net_diff = diff("net_pnl")
    return {
        "net_pnl_difference": net_diff,
        "expectancy_difference": diff("expectancy"),
        "max_drawdown_difference": diff("max_drawdown_inr"),
        "transaction_cost_difference": diff("total_costs"),
        "trade_count_difference": mb["closed_trades"] - ma["closed_trades"],
        "exposure_difference": diff("exposure_buy_turnover"),
        "risk_violation_difference": mb["risk_rule_violations"]["total"]
        - ma["risk_rule_violations"]["total"],
        "abstention_rate_A": M._f(Decimal(ma["no_trade_decisions"]) / ma["signals_evaluated"],
                                  4) if ma["signals_evaluated"] else None,
        "abstention_rate_B": M._f(Decimal(mb["no_trade_decisions"]) / mb["signals_evaluated"],
                                  4) if mb["signals_evaluated"] else None,
        "model_cost_usd": usd, "usd_inr_rate": fx, "model_cost_inr": model_inr,
        "model_tokens": {"input": quality["operational"]["input_tokens"],
                         "output": quality["operational"]["output_tokens"]},
        "net_incremental_after_model_cost_inr": round(net_diff - model_inr, 2)
        if net_diff is not None and model_inr is not None else None,
        "net_incremental_note": None if model_inr is not None else
        "not computed: model prices (llm.*_cost_per_mtok) or ai_eval.usd_inr_rate not "
        "configured; never guessed",
        "decomposition": M.decompose(ta, tb),
        "explanatory_value": "reported separately in 'quality'; it is not trading value",
    }


def _selection_diagnostic(log_a, ai_by_sid, out_a, shadow) -> dict:
    """Per-share outcome of every baseline signal, grouped by the AI's view.
    Gross, per share, no capital, no costs: a diagnostic of selection skill on
    matched signals, never part of P&L or the conclusion."""
    groups: dict[str, list[float]] = defaultdict(list)
    counts: Counter = Counter()
    for e in log_a:
        sid = e["cand"].setup.signal_id
        ai = ai_by_sid.get(sid)
        cat = ai.category() if ai is not None else "not_reviewed"
        counts[cat] += 1
        kind, rec = out_a.get(sid, (None, None))
        r = None
        if kind == "TRADE":
            r = _r_multiple(rec.entry_price, rec.exit_price, rec.stop)
        elif sid in shadow and shadow[sid].filled and shadow[sid].r_multiple is not None:
            r = float(shadow[sid].r_multiple)
        if r is not None:
            groups[cat].append(r)
    rows = []
    for cat in sorted(counts):
        vals = groups.get(cat, [])
        rows.append({"ai_view": cat, "signals": counts[cat], "with_price_path": len(vals),
                     "mean_r_multiple": round(sum(vals) / len(vals), 4) if vals else None,
                     "share_positive": round(sum(v > 0 for v in vals) / len(vals), 4)
                     if vals else None,
                     "interpretable": len(vals) >= 10})
    return {"groups": rows,
            "note": "DIAGNOSTIC ONLY: gross R-multiple per share of each baseline signal "
                    "(executed or risk-rejected shadow), grouped by the AI's assessment. No "
                    "capital scaling and no costs; never part of P&L or the conclusion. Groups "
                    "under 10 are not interpretable."}
