"""WhatsApp notifications (Meta Cloud API) with a fake HTTP session: no network,
no real messages. Data are synthetic (tests/paper_helpers)."""

from __future__ import annotations

import ast
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import PROJECT_ROOT, Settings
from app.monitoring.alerts import MonitorStore
from app.notify.events import SessionEvents, daily_summary, signal_events, trade_events
from app.notify.whatsapp import (Notifications, WhatsAppAlertNotifier, WhatsAppSender,
                                 clean_param)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2025, 3, 12, 10, 0, tzinfo=IST)
TOKEN = "EAAGtesttoken0123456789abcdefNOTREAL"  # gate: allow-secret (fake)


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, data, timeout, headers):
        self.calls.append({"url": url, "body": json.loads(data), "headers": headers})
        r = self.responses.pop(0) if self.responses else Resp(200, {"messages": [{"id": "x"}]})
        if isinstance(r, Exception):
            raise r
        return r


OK = Resp(200, {"messages": [{"id": "wamid.1"}]})


def wa_settings(tmp_path=None, **over):
    base = {"enabled": True, "phone_number_id": "123456789012", "recipients": ["919800000001"]}
    kw = {"whatsapp": {**base, **over}, "whatsapp_token": TOKEN}
    if tmp_path is not None:
        kw["monitoring"] = {"db_path": str(tmp_path / "mon.db"),
                            "alert_outbox": str(tmp_path / "out.jsonl")}
    return Settings(**kw)


def test_template_request_shape():
    sess = FakeSession(OK)
    res = WhatsAppSender(wa_settings(), sess, sleep=lambda s: None).send("hello\nworld")
    assert res["status"] == "ACCEPTED"
    c = sess.calls[0]
    assert c["url"] == "https://graph.facebook.com/v23.0/123456789012/messages"
    assert c["headers"]["Authorization"] == f"Bearer {TOKEN}"
    t = c["body"]["template"]
    assert c["body"]["type"] == "template" and c["body"]["to"] == "919800000001"
    assert t["name"] == "nse_alert" and t["language"] == {"code": "en"}
    assert t["components"][0]["parameters"] == [{"type": "text", "text": "hello | world"}]


def test_param_cleaning_meets_meta_rules():
    s = clean_param("a\nb\tc" + " " * 9 + "d" + "x" * 2000)
    assert "\n" not in s and "\t" not in s and "    " not in s and len(s) <= 1000
    from app.utils.logging import register_secret

    register_secret("MyRegisteredSecretValue123")
    assert "MyRegisteredSecretValue123" not in clean_param("leak MyRegisteredSecretValue123")
    assert "abcdef1234567890" not in clean_param("api_key=abcdef1234567890")


def test_not_configured_sends_nothing():
    sess = FakeSession()
    res = WhatsAppSender(Settings(), sess).send("x")
    assert res["status"] == "SKIPPED" and sess.calls == []
    assert "NSE_WHATSAPP_TOKEN" in res["detail"]


def test_bounded_retries_only_for_transient_errors():
    sleeps = []
    sess = FakeSession(Resp(500, {}), TimeoutError("t"), OK)
    assert WhatsAppSender(wa_settings(), sess, sleeps.append).send("x")["status"] == "ACCEPTED"
    assert len(sess.calls) == 3 and len(sleeps) == 2
    bad = FakeSession(Resp(401, {"error": {"code": 190, "message": "token expired"}}), OK)
    res = WhatsAppSender(wa_settings(), bad, lambda s: None).send("x")
    assert res["status"] == "FAILED" and len(bad.calls) == 1  # never retried
    assert "190" in res["detail"][0]["error"]
    assert res["detail"][0]["to"] == "...0001"  # number masked in records


def test_dedupe_cap_and_no_token_in_ledger(tmp_path):
    s = wa_settings(tmp_path, max_messages_per_day=2)
    ms = MonitorStore(s.monitoring.db_path)
    sess = FakeSession()
    n = Notifications(s, ms, WhatsAppSender(s, sess, lambda x: None))
    assert n.deliver("k1", "test", "one", NOW)["status"] == "ACCEPTED"
    assert n.deliver("k1", "test", "one", NOW)["status"] == "DUPLICATE"
    assert n.deliver("k2", "test", "two", NOW)["status"] == "ACCEPTED"
    assert n.deliver("k3", "test", "three", NOW)["status"] == "SKIPPED"  # daily cap
    assert len(sess.calls) == 2
    dump = json.dumps(ms.all("SELECT * FROM notify_log"))
    assert TOKEN not in dump and "919800000001" not in dump


def test_failures_never_raise(tmp_path):
    s = wa_settings(tmp_path)

    class Boom:
        def configured(self):
            raise RuntimeError("boom")

    res = Notifications(s, MonitorStore(s.monitoring.db_path), Boom()).deliver("k", "t", "x",
                                                                               NOW)
    assert res["status"] == "ERROR"


def test_signals_entries_exits_from_a_real_integrated_session(tmp_path, calendar):
    from tests.test_integrated import make, run_day

    eng = make(calendar, tmp_path)
    run_day(eng)
    day = "2025-03-12"
    sig = signal_events(eng.ai.store, day)
    assert len(sig) == 1 and "SIGNAL SYNTH" in sig[0][1] and "qty 28" in sig[0][1]
    assert "Your decision" in sig[0][1] and "PAPER" in sig[0][1]
    trades = trade_events({"A": eng.baseline.store, "B": eng.ai.store}, day, True, True)
    kinds = sorted(k.split(":")[0] + k.split(":")[1] for k, _ in trades)
    assert kinds == ["exitA", "exitB", "fillA", "fillB"]
    assert any("net P&L Rs 15.69" in t for _, t in trades)
    summary = daily_summary({"A": eng.baseline.store, "B": eng.ai.store}, eng.ai.store,
                            date(2025, 3, 12))
    assert "baseline: 1 signal(s), 1 closed trade(s), net Rs 15.69" in summary
    s = wa_settings(tmp_path)
    sess = FakeSession()
    hook = SessionEvents(s, {"A": eng.baseline.store, "B": eng.ai.store},
                         Notifications(s, MonitorStore(s.monitoring.db_path),
                                       WhatsAppSender(s, sess, lambda x: None)))
    hook.after_step(datetime(2025, 3, 12, 15, 30, tzinfo=IST))
    hook.after_step(datetime(2025, 3, 12, 15, 35, tzinfo=IST))  # nothing new: no resend
    assert len(sess.calls) == 5 and hook.errors == []


def test_alert_notifier_filters_severity_and_reports_recovery(tmp_path):
    s = wa_settings(tmp_path, min_alert_severity="CRITICAL")
    sess = FakeSession()
    notifier = WhatsAppAlertNotifier(Notifications(s, MonitorStore(s.monitoring.db_path),
                                                   WhatsAppSender(s, sess, lambda x: None)))
    base = {"alert_id": "AL-1", "component": "market-data", "rule": "stale data",
            "session": "2025-03-12", "steps": ["check the feed"], "ts": NOW.isoformat()}
    notifier.send({**base, "event": "RAISED", "severity": "WARNING"})
    assert sess.calls == []
    notifier.send({**base, "event": "RAISED", "severity": "CRITICAL"})
    notifier.send({**base, "event": "RECOVERED", "severity": "CRITICAL",
                   "ts": datetime(2025, 3, 12, 10, 30, tzinfo=IST).isoformat()})
    texts = [c["body"]["template"]["components"][0]["parameters"][0]["text"]
             for c in sess.calls]
    assert texts[0].startswith("SYSTEM CRITICAL") and "check the feed" in texts[0]
    assert texts[1].startswith("SYSTEM RESOLVED")


def test_alert_center_adds_whatsapp_only_when_configured(tmp_path):
    from app.monitoring.alerts import AlertCenter

    ok = wa_settings(tmp_path)
    names = [n.name for n in AlertCenter(MonitorStore(ok.monitoring.db_path), ok).notifiers]
    assert "whatsapp" in names
    off = Settings(monitoring={"db_path": str(tmp_path / "m2.db"),
                               "alert_outbox": str(tmp_path / "o2")})
    assert "whatsapp" not in [n.name for n in AlertCenter(MonitorStore(off.monitoring.db_path),
                                                          off).notifiers]


def test_whatsapp_settings_never_change_the_config_version():
    assert Settings().version() == wa_settings().version()


def test_recipient_validation():
    with pytest.raises(ValueError):
        Settings(whatsapp={"recipients": ["+91 98000"]})


def test_engine_hook_failure_does_not_break_trading(tmp_path, calendar):
    from tests.test_integrated import make, run_day

    eng = make(calendar, tmp_path)

    class Broken:
        def after_step(self, now):
            raise RuntimeError("hook down")

    eng.hooks.append(Broken())
    run_day(eng)
    assert [p["net_pnl"] for p in eng.baseline.store.all("SELECT net_pnl FROM positions")] == \
        ["15.69"]


def test_notify_has_no_order_path():
    for p in (PROJECT_ROOT / "app" / "notify").glob("*.py"):
        src = p.read_text()
        imps = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)
                and n.module}
        assert not {i for i in imps if "groww" in i or i == "app.paper.engine"}, p.name
        for word in ("place_order", "modify_order", "cancel_order", "PaperEngine"):
            assert word not in src, (p.name, word)
