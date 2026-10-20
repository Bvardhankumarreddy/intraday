"""Phase 6: evaluation sessions, data quality, signal audit, reconciliation,
metrics and reports. Deterministic fixtures only - no live market needed."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.config import MarketConfig
from app.data.models import DataMode, DataUnavailableError
from app.evaluation.metrics import cumulative_metrics
from app.evaluation.quality import assess_batch
from app.evaluation.reconcile import reconcile
from app.evaluation.recorder import EvalStatus, EvaluationRecorder, SessionKind
from app.evaluation.report import daily_report, write_cumulative, write_daily
from app.paper.scheduler import SessionScheduler, replay_session
from app.paper.store import PaperStore
from app.utils.market_calendar import MarketCalendar
from tests.backtest_helpers import pad
from tests.helpers import bars, orb_today
from tests.paper_helpers import GAP_DOWN, NEUTRAL, TARGET, build

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)
DAY2 = date(2025, 3, 13)


def t(hhmm: str, day: date = DAY, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, sec, tzinfo=IST)


def setup_eval(calendar, tmp_path, after=None, mode=DataMode.LIVE, start=DAY, **kw):
    over = {"evaluation": {"output_dir": str(tmp_path / "eval")}}
    for k, v in kw.pop("over", {}).items():
        over[k] = {**over.get(k, {}), **v}
    engine, provider, store = build(calendar, tmp_path, after, **kw, **over)
    provider.mode = mode
    recorder = EvaluationRecorder(store, engine.settings, calendar)
    engine.recorder = recorder
    eid = recorder.create_evaluation("test", start, t("08:00", start))
    return engine, provider, store, recorder, eid


def status(store, day=DAY, kind="LIVE"):
    return store.one("SELECT * FROM eval_sessions WHERE trading_date = ? AND kind = ?",
                     (day.isoformat(), kind))


# -- sessions ---------------------------------------------------------------------


def test_normal_session_without_signals(calendar, tmp_path):
    flat = pad([(70.0, 70.1, 69.9, 70.0, 1000)])
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, days={DAY: flat})
    replay_session(engine, DAY)
    s = status(store)
    assert s["status"] == "COMPLETED" and s["primary_eval"] == 1 and s["kind"] == "LIVE"
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["sessions"]["completed"] == 1 and m["sessions"]["with_trades"] == 0
    assert m["funnel"]["signals"] == 0 and m["trades"]["eligible"] == 0
    assert m["trades"]["win_rate_pct"] is None and m["trades"]["profit_factor"] is None
    decisions = {r["decision"] for r in store.all("SELECT decision FROM signal_audit")}
    assert decisions <= {"NO_SETUP_YET", "NO_VALID_SETUP"}


def test_session_with_completed_trade(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    assert status(store)["status"] == "COMPLETED"
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["funnel"] == {"signals": 1, "signals_rejected": 0, "orders_created": 1,
                           "orders_filled": 1, "orders_unfilled": 0, "closed_trades": 1,
                           "trades_excluded_reconciliation": 0}
    tr = m["trades"]
    assert (tr["net_pnl"], tr["gross_pnl"], tr["total_costs"]) == ("15.69", "32.20", "16.51")
    assert tr["win_rate_pct"] == "100.00" and tr["exits"] == {"TARGET": 1}
    assert tr["avg_holding_minutes"] == "5.00"
    assert m["evidence"].startswith("INSUFFICIENT SAMPLE")


def test_signal_audit_records_inputs_and_decision(calendar, tmp_path):
    engine, _, store, _, _ = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    row = store.one("SELECT * FROM signal_audit WHERE decision = 'ORDER_CREATED'")
    assert row["ts"] == t("09:40", sec=1).isoformat() and row["symbol"] == "SYNTH"
    assert (row["opening_range_high"], row["opening_range_low"]) == ("70.5", "69.5")
    # cumulative relative volume: (4 x 1000 + 5000) / (5 x 1000)
    assert (row["relative_volume"], row["relative_volume_today"],
            row["relative_volume_baseline"]) == ("1.8", "9000.0", "5000.0")
    assert (row["entry_reference"], row["stop"], row["target"]) == ("71.0", "70.4", "72.2")
    assert row["quantity"] == 28 and row["risk_status"] == "QUALIFIED_FOR_REVIEW"
    assert row["est_round_trip_costs"] is not None and row["net_reward_risk"] is not None
    later = store.all("SELECT decision FROM signal_audit WHERE signal_id = ? AND ts > ?",
                      (row["signal_id"], row["ts"]))
    assert all(r["decision"] in ("ALREADY_PROCESSED", "SETUP_NO_LONGER_VALID") for r in later)


def test_replay_is_never_primary(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET],
                                          mode=DataMode.HISTORICAL)
    replay_session(engine, DAY)
    s = status(store, kind="REPLAY")
    assert s["status"] == "COMPLETED" and s["primary_eval"] == 0
    assert cumulative_metrics(store, eid, engine.settings)["funnel"]["closed_trades"] == 0
    assert cumulative_metrics(store, eid, engine.settings, primary_only=False)["funnel"][
        "closed_trades"] == 1


def test_config_change_recorded_and_excluded(calendar, tmp_path):
    engine, provider, store, recorder, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    from app.paper.engine import PaperEngine
    from tests.paper_helpers import paper_settings

    changed = paper_settings(tmp_path, risk={"max_trades_per_day": 4},
                             evaluation={"output_dir": str(tmp_path / "eval")})
    engine2 = PaperEngine(provider, changed, calendar, store,
                          recorder=EvaluationRecorder(store, changed, calendar))
    replay_session(engine2, DAY)
    assert status(store)["primary_eval"] == 0
    change = store.one("SELECT * FROM config_changes")
    assert "risk.max_trades_per_day" in json.loads(change["changed_keys"])


# -- schedule, holidays, interruptions ----------------------------------------------


def test_schedule_skips_holidays_weekends_and_special_sessions(tmp_path):
    cfg = MarketConfig(special_sessions=[{"date": "2025-03-16", "open": "18:00",
                                          "close": "19:00", "name": "Special"}])
    cal = MarketCalendar(cfg, holidays={date(2025, 3, 14): "Holiday"})
    engine, _, store, recorder, _ = setup_eval(cal, tmp_path, [NEUTRAL], start=date(2025, 3, 10))
    recorder.sync_schedule(date(2025, 3, 17), t("20:00", date(2025, 3, 17)))
    days = [r["trading_date"] for r in store.all("SELECT trading_date FROM eval_sessions")]
    assert days == ["2025-03-10", "2025-03-11", "2025-03-12", "2025-03-13", "2025-03-17"]


def test_missed_session_is_incomplete_not_a_quiet_zero(calendar, tmp_path):
    engine, _, store, recorder, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    recorder.sync_schedule(DAY2, t("16:00", DAY2))  # app never ran on either day
    for d in (DAY, DAY2):
        s = status(store, d)
        assert s["status"] == "INCOMPLETE" and "did not run" in s["status_reason"]
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["sessions"]["incomplete"] == 2 and m["sessions"]["completed"] == 0
    report = daily_report(store, DAY2, engine.settings, eid)
    assert "no paper session ran on this date" in \
        report["8_missing_information_and_limitations"]["missing"]


def test_interrupted_session_marked_incomplete(calendar, tmp_path):
    engine, _, store, recorder, _ = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    engine.open_session(DAY, t("09:00"))
    engine.begin_evaluation_session(DAY, t("09:00"))
    engine.step(t("09:20", sec=1))  # ... then the process dies
    assert status(store)["status"] == "RUNNING"
    recorder.sync_schedule(DAY2, t("09:00", DAY2))
    s = status(store)
    assert s["status"] == "INCOMPLETE" and "interrupted" in s["status_reason"]


def test_restart_same_day_resumes_without_double_counting(calendar, tmp_path):
    db = tmp_path / "r.db"
    engine, provider, store, recorder, _ = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET],
                                                      db=db)
    engine.open_session(DAY, t("09:00"))
    engine.begin_evaluation_session(DAY, t("09:00"))
    for when in [t("09:20", sec=1), t("09:25", sec=1), t("09:40", sec=1)]:
        engine.step(when)
    engine.step(t("09:40", sec=1))  # repeated step: idempotent
    store.close()

    from app.paper.engine import PaperEngine

    store2 = PaperStore(db)
    rec2 = EvaluationRecorder(store2, engine.settings, calendar)
    engine2 = PaperEngine(provider, engine.settings, calendar, store2, recorder=rec2)

    class Clock:
        now = t("09:43")

        def __call__(self):
            return self.now

        def sleep(self, s):
            self.now += timedelta(seconds=max(s, 0))

    clock = Clock()
    SessionScheduler(engine2, clock=clock, sleep=clock.sleep).run()
    s = store2.one("SELECT * FROM eval_sessions WHERE kind = 'LIVE'")
    # 3 steps before the crash (the repeated 09:40 step is not counted twice),
    # 09:30 and 09:35 missed, then 09:45 .. 15:30 after the restart: 73 of 75 = 97%
    assert (s["steps_executed"], s["steps_expected"]) == (73, 75)
    assert s["status"] == "COMPLETED"  # 97% >= the 90% monitoring threshold
    assert len(store2.orders(DAY)) == 2
    assert store2.one("SELECT COUNT(*) n FROM signals")["n"] == 1
    assert any(e["kind"] == "RECOVERY" for e in store2.events(DAY))


# -- data quality -----------------------------------------------------------------------


def raw(rows, start="09:15"):
    return bars(DAY, rows, start=start)


def test_assess_batch_counts(calendar):
    from app.data.models import parse_interval

    iv = parse_interval("5m")
    rows = [(70, 70.5, 69.5, 70, 100)] * 6
    frame = raw(rows)
    # Appended rows: 09:25 again (after 09:40) and 09:15 again (after 09:25): both are
    # duplicates AND both arrive out of order.
    frame = pd.concat([frame, frame.iloc[[2]]], ignore_index=True)
    frame.loc[len(frame)] = [frame["timestamp"].iloc[0], 70, 70.5, 69.5, 70, 100]
    frame.loc[1, "high"] = 60                                            # invalid OHLC
    frame.loc[3, "volume"] = -5                                          # invalid volume
    q = assess_batch("X", frame, DAY, t("09:50"), calendar, iv, 120)
    assert q.expected == 7  # 09:15 .. 09:45 completed by 09:50
    assert (q.duplicates, q.out_of_order, q.invalid_ohlc, q.invalid_volume) == (2, 2, 1, 1)
    assert q.received == 4 and q.missing == 3  # 09:20 bad, 09:30 bad, 09:45 absent
    assert q.stale and not q.ok


def test_assess_batch_clean_and_stale(calendar):
    from app.data.models import parse_interval

    iv = parse_interval("5m")
    good = assess_batch("X", raw([(70, 70.5, 69.5, 70, 100)] * 7), DAY, t("09:50"),
                        calendar, iv, 120)
    assert good.ok and (good.expected, good.received, good.missing) == (7, 7, 0)
    late = assess_batch("X", raw([(70, 70.5, 69.5, 70, 100)] * 3), DAY, t("09:50"),
                        calendar, iv, 120)
    assert late.stale and late.missing == 4
    none = assess_batch("X", None, DAY, t("09:50"), calendar, iv, 120, error="timeout")
    assert none.error == "timeout" and none.received == 0 and none.stale


def test_dq_counts_never_use_future_rows(calendar, tmp_path):
    rows = orb_today(after=[NEUTRAL, TARGET])
    rows = pad(rows)
    engine, provider, store, _, _ = setup_eval(calendar, tmp_path, days={DAY: rows})
    full = provider.candles[("SYNTH", "5m")]
    dup = full[full["timestamp"] == pd.Timestamp(t("14:00"))]
    provider.candles[("SYNTH", "5m")] = pd.concat([full, dup], ignore_index=True) \
        .sort_values("timestamp", kind="stable").reset_index(drop=True)
    provider.prevalidated = True  # serve the raw (afternoon duplicate) batch
    engine.open_session(DAY, t("09:00"))
    engine.begin_evaluation_session(DAY, t("09:00"))
    engine.step(t("10:00", sec=1))
    q = store.one("SELECT * FROM dq_batches WHERE ts = ?", (t("10:00", sec=1).isoformat(),))
    assert q["duplicates"] == 0 and q["expected"] == 9  # 14:00 duplicate not yet visible


def test_stale_data_blocks_signals_and_is_recorded(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(
        calendar, tmp_path, days={DAY: pad(orb_today()[:3], n=3)},
        over={"backtest": {"max_missing_candles_per_session": 75}})
    replay_session(engine, DAY)
    blocked = store.all("SELECT * FROM eval_steps WHERE signals_blocked = 1")
    assert blocked and "no symbol has fresh" in blocked[0]["blocked_reason"]
    s = status(store)
    assert s["status"] == "INCOMPLETE" and "data coverage" in s["status_reason"]
    assert cumulative_metrics(store, eid, engine.settings)["data_quality"]["stale"] > 0


class Flaky:
    """Wraps a provider; candle calls fail between two times (an outage)."""

    def __init__(self, inner, down_from, down_to):
        self.inner, self.down_from, self.down_to = inner, down_from, down_to
        self.now = None
        self.name, self.mode, self.calendar = inner.name, inner.mode, inner.calendar
        self.last_raw = inner.last_raw
        self.state = "CONNECTED"

    def list_instruments(self):
        return self.inner.list_instruments()

    def get_instrument(self, s):
        return self.inner.get_instrument(s)

    def get_candles(self, *a):
        if self.now is not None and self.down_from <= self.now < self.down_to:
            self.state = "DISCONNECTED"
            raise DataUnavailableError("provider timeout")
        self.state = "CONNECTED"
        return self.inner.get_candles(*a)

    def get_quote(self, s):
        return self.inner.get_quote(s)

    def health(self):
        return {"state": self.state, "retries": 0}


def test_provider_outage_and_recovery(calendar, tmp_path):
    engine, provider, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    flaky = Flaky(provider, t("10:00"), t("10:30"))
    engine.provider = flaky
    replay_session(engine, DAY, before_step=lambda when: setattr(flaky, "now", when))
    down = store.all("SELECT * FROM eval_steps WHERE provider_state = 'DISCONNECTED'")
    assert len(down) == 6  # 10:00:01 .. 10:25:01
    assert all(r["signals_blocked"] for r in down)
    errors = store.all("SELECT * FROM dq_batches WHERE error IS NOT NULL")
    assert errors and "timeout" in errors[0]["error"]
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["monitoring"]["steps_provider_down"] == 6
    assert m["funnel"]["closed_trades"] == 1  # trade before the outage still completed


# -- orders, capital, risk -----------------------------------------------------------------


def test_rejected_and_unfilled_orders_not_in_win_rate(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [(70.3, 70.4, 70.2, 70.3, 1000)])
    replay_session(engine, DAY)
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["funnel"]["orders_created"] == 1 and m["funnel"]["orders_unfilled"] == 1
    assert m["funnel"]["closed_trades"] == 0 and m["trades"]["win_rate_pct"] is None


def test_partial_fills_not_supported_by_design():
    from app.paper.models import OrderState

    assert "PARTIALLY_FILLED" not in OrderState.__members__


def test_500_rupees_whole_shares_rejected_by_costs(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(
        calendar, tmp_path, [NEUTRAL, TARGET],
        over={"capital": {"initial_capital": 500},
              "risk": {"risk_pct": 0.5, "include_costs_in_risk": True}})
    replay_session(engine, DAY)
    row = store.one("SELECT * FROM signal_audit WHERE decision = 'SIGNAL_REJECTED_RISK'")
    assert row["risk_status"] == "REJECTED_COSTS" and row["quantity"] == 0
    assert "costs_exceed_risk_budget" in row["risk_reasons"]
    assert cumulative_metrics(store, eid, engine.settings)["rejections"] == {"REJECTED_COSTS": 1}
    assert Decimal(store.portfolio()["cash"]) == 500


def second_symbol():
    return pad([(70.0, 70.1, 69.9, 70.0, 1000)] * 15 + [(70.0, 70.65, 70.0, 70.6, 10_000)])


def test_daily_loss_limit_rejection_audited(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, GAP_DOWN],
                                          extra={"SECOND": {DAY: second_symbol()}},
                                          over={"risk": {"max_daily_loss": 40}})
    replay_session(engine, DAY)
    row = store.one("SELECT * FROM signal_audit WHERE symbol = 'SECOND' AND "
                    "decision = 'SIGNAL_REJECTED_RISK'")
    assert "daily_loss_lock" in row["risk_reasons"]
    assert any("daily loss lock" in e["message"] for e in store.events(DAY))
    assert cumulative_metrics(store, eid, engine.settings)["trades"]["losses"] == 1


def test_open_position_at_session_end_makes_session_incomplete(calendar, tmp_path):
    short = pad(orb_today(after=[NEUTRAL]), n=30)
    engine, _, store, _, eid = setup_eval(
        calendar, tmp_path, days={DAY: short},
        over={"backtest": {"max_missing_candles_per_session": 60}})
    replay_session(engine, DAY)
    s = status(store)
    assert s["status"] == "INCOMPLETE" and "unresolved paper positions" in s["status_reason"]
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["funnel"]["closed_trades"] == 0  # open/flagged positions are not trades


# -- reconciliation ------------------------------------------------------------------------


def test_clean_session_reconciles(calendar, tmp_path):
    engine, provider, store, _, _ = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    assert reconcile(store, DAY, engine.settings, engine.risk.schedule, provider) == []


def test_tampered_costs_detected_not_repaired(calendar, tmp_path):
    engine, provider, store, recorder, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    pid = store.positions()[0]["position_id"]
    costs = json.loads(store.positions()[0]["costs"])
    costs["sell"]["stt"] = "9.99"
    with store.transaction():
        store.update_position(pid, total_costs=Decimal("1.00"), costs=json.dumps(costs))
    issues = reconcile(store, DAY, engine.settings, engine.risk.schedule, provider)
    codes = {i.code for i in issues}
    assert {"costs_mismatch", "net_mismatch", "fee_mismatch_sell_stt",
            "report_costs_mismatch"} <= codes
    assert store.positions()[0]["total_costs"] == "1.00"  # never silently repaired
    recorder.record_issues(DAY, t("16:00"), issues)
    m = cumulative_metrics(store, eid, engine.settings)
    assert m["funnel"]["trades_excluded_reconciliation"] == 1 and m["trades"]["eligible"] == 0


def test_cash_ledger_mismatch_detected(calendar, tmp_path):
    engine, provider, store, _, _ = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    with store.transaction():
        store.set_cash(Decimal("1"), Decimal(0))
    assert "cash_mismatch" in {i.code for i in reconcile(store, DAY, engine.settings,
                                                         engine.risk.schedule)}


# -- fee model -------------------------------------------------------------------------------


def test_outdated_or_missing_fee_model_flagged(calendar, tmp_path):
    engine, _, _, recorder, _ = setup_eval(calendar, tmp_path, [NEUTRAL])
    sched = engine.risk.schedule
    assert recorder.fee_model_flag(sched, sched.verified_on + timedelta(days=30)) is None
    flag = recorder.fee_model_flag(sched, sched.verified_on + timedelta(days=200))
    assert "re-verify" in flag and "200 days" in flag
    assert recorder.fee_model_flag(None, DAY) == "fee schedule unavailable"


def test_missing_fee_schedule_fails_session(calendar, tmp_path):
    engine, _, store, _, _ = setup_eval(calendar, tmp_path, [NEUTRAL],
                                        over={"broker": {"name": "nosuchbroker"}})

    class Clock:
        now = t("09:00")

        def __call__(self):
            return self.now

        def sleep(self, s):
            self.now += timedelta(seconds=s)

    clock = Clock()
    assert SessionScheduler(engine, clock=clock, sleep=clock.sleep).run() is None
    s = status(store)
    assert s["status"] == "FAILED" and "fee schedule unavailable" in s["status_reason"]


# -- reports ----------------------------------------------------------------------------------


def test_daily_report_sections_and_reproducibility(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    path = tmp_path / "eval" / eid / "daily" / "2025-03-12.json"
    first = path.read_bytes()
    body = json.loads(first)
    assert [k for k in body if k[0].isdigit()] == [
        "1_session_summary", "2_market_data_health", "3_signal_decisions",
        "4_orders_and_fills", "5_pnl_and_costs", "6_risk_control_events",
        "7_errors_and_recovery", "8_missing_information_and_limitations"]
    assert body["label"].startswith("PAPER TRADING - SIMULATED")
    assert body["5_pnl_and_costs"]["totals"]["net_pnl"] == "15.69"
    write_daily(store, DAY, engine.settings, eid)
    assert path.read_bytes() == first


def test_cumulative_report_and_exports(calendar, tmp_path):
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    path = write_cumulative(store, engine.settings, eid)
    body = json.loads(path.read_text())
    assert body["primary"]["trades"]["net_pnl"] == "15.69"
    for f in ("sessions_primary.csv", "sessions_all.csv", "trades.csv", "signal_audit.csv",
              "data_quality.csv"):
        assert (path.parent / f).exists()
    first = path.read_bytes()
    write_cumulative(store, engine.settings, eid)
    assert path.read_bytes() == first


def test_secrets_never_in_reports(calendar, tmp_path, monkeypatch):
    monkeypatch.setenv("NSE_BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    engine, _, store, _, eid = setup_eval(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    path = write_cumulative(store, engine.settings, eid)
    for f in path.parent.rglob("*"):
        if f.is_file():
            assert "JBSWY3DPEHPK3PXP" not in f.read_text()
    frozen = store.one("SELECT frozen_settings FROM evaluations")["frozen_settings"]
    assert "JBSWY3DPEHPK3PXP" not in frozen and "totp" not in frozen.lower()


def test_session_kind_mapping():
    from app.evaluation.recorder import KIND_FOR_MODE

    assert KIND_FOR_MODE[DataMode.LIVE] is SessionKind.LIVE
    assert KIND_FOR_MODE[DataMode.HISTORICAL] is SessionKind.REPLAY
    assert KIND_FOR_MODE[DataMode.SIMULATED] is SessionKind.SIMULATED
    assert {s.value for s in EvalStatus} == {"SCHEDULED", "RUNNING", "COMPLETED",
                                             "INCOMPLETE", "FAILED"}


def test_evaluation_code_has_no_order_endpoints():
    import pathlib

    import app.evaluation as pkg
    for path in pathlib.Path(pkg.__file__).parent.rglob("*.py"):
        text = path.read_text().lower()
        for word in ("place_order", "modify_order", "cancel_order", "import growwapi"):
            assert word not in text, f"{word} in {path}"
