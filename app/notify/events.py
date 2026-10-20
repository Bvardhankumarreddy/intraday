"""Turn persisted records into WhatsApp messages (Phase 15 add-on).

Read-only over the paper databases; each event has a stable key, so it is sent
once even after restarts. Every message is labelled as a PAPER research
signal: the user decides and places any real order manually.

Events:
- SIGNAL: the AI-assisted arm's CANDIDATE recommendations (passed every
  deterministic check and the AI did not object). Entry, stop, target, whole
  shares, costs, net reward:risk and validity come from the deterministic
  engines.
- ENTRY / EXIT: paper fills and closed positions in either arm (A baseline,
  B AI-assisted). Exits include net P&L after all costs.
- DAILY SUMMARY: one message after the session.
"""

from __future__ import annotations

import json
from datetime import date, datetime

LABEL = "PAPER"


def _rows(store, sql, params=()) -> list[dict]:
    if store is None:
        return []
    try:
        return [dict(r) for r in store.all(sql, params)]
    except Exception:  # noqa: BLE001 - table absent: nothing recorded
        return []


def _hm(ts: str | None) -> str:
    return datetime.fromisoformat(ts).strftime("%H:%M") if ts else "?"


def signal_events(b_store, day: str) -> list[tuple[str, str]]:
    out = []
    for r in _rows(b_store, "SELECT recommendation_id, created_at, body FROM "
                            "agent_recommendations WHERE market_date = ? AND status = "
                            "'CANDIDATE' ORDER BY created_at", (day,)):
        b = json.loads(r["body"])
        out.append((f"sig:{r['recommendation_id']}",
                    f"{LABEL} SIGNAL {b['symbol']} at {_hm(r['created_at'])}: entry above Rs "
                    f"{b.get('entry_reference')}, stop Rs {b.get('stop')}, target Rs "
                    f"{b.get('target')}, qty {b.get('quantity')} (whole shares), est. costs Rs "
                    f"{b.get('estimated_costs_at_target')}, net reward:risk "
                    f"{b.get('net_reward_risk')}, valid till {_hm(b.get('valid_until'))}. "
                    "Checks passed; AI did not object. Your decision; place any real order "
                    "manually"))
    return out


def trade_events(stores: dict, day: str, fills: bool, exits: bool) -> list[tuple[str, str]]:
    out = []
    names = {"A": "baseline", "B": "AI-assisted"}
    for arm, store in stores.items():
        if fills:
            for o in _rows(store, "SELECT o.order_id, o.symbol, o.fill_quantity, o.fill_price, "
                                  "o.fill_time, s.stop, s.target FROM orders o JOIN signals s "
                                  "USING (signal_id) WHERE s.trading_date = ? AND o.side = "
                                  "'BUY' AND o.state = 'FILLED'", (day,)):
                out.append((f"fill:{arm}:{o['order_id']}",
                            f"{LABEL} ENTRY ({names.get(arm, arm)}) {o['symbol']}: bought "
                            f"{o['fill_quantity']} @ Rs {o['fill_price']} at "
                            f"{_hm(o['fill_time'])}; stop Rs {o['stop']}, target Rs "
                            f"{o['target']}"))
        if exits:
            for p in _rows(store, "SELECT position_id, symbol, quantity, exit_price, exit_time, "
                                  "exit_reason, net_pnl, total_costs FROM positions WHERE "
                                  "trading_date = ? AND state = 'CLOSED'", (day,)):
                out.append((f"exit:{arm}:{p['position_id']}",
                            f"{LABEL} EXIT ({names.get(arm, arm)}) {p['symbol']}: sold "
                            f"{p['quantity']} @ Rs {p['exit_price']} at {_hm(p['exit_time'])} "
                            f"({p['exit_reason']}); net P&L Rs {p['net_pnl']} after Rs "
                            f"{p['total_costs']} costs"))
    return out


def daily_summary(stores: dict, b_store, day: date) -> str:
    ds = day.isoformat()
    parts = [f"{LABEL} DAILY SUMMARY {ds}"]
    for arm, name in (("A", "baseline"), ("B", "AI-assisted")):
        st = stores.get(arm)
        pos = _rows(st, "SELECT net_pnl FROM positions WHERE trading_date = ? AND state = "
                        "'CLOSED'", (ds,))
        sig = _rows(st, "SELECT review_status FROM signals WHERE trading_date = ?", (ds,))
        pf = _rows(st, "SELECT cash FROM portfolio")
        net = sum(float(p["net_pnl"]) for p in pos if p["net_pnl"] is not None)
        parts.append(f"{name}: {len(sig)} signal(s), {len(pos)} closed trade(s), net Rs "
                     f"{net:.2f}, cash Rs {pf[0]['cash'] if pf else 'n/a'}")
    cands = _rows(b_store, "SELECT COUNT(*) AS n FROM agent_recommendations WHERE market_date "
                           "= ? AND status = 'CANDIDATE'", (ds,))
    parts.append(f"AI candidates: {cands[0]['n'] if cands else 0}")
    sess = _rows(stores.get("A"), "SELECT state FROM sessions WHERE trading_date = ?", (ds,))
    parts.append(f"session: {sess[0]['state'] if sess else 'did not run'}")
    parts.append("A no-trade day is a valid outcome")
    return "; ".join(parts)


class SessionEvents:
    """After-step hook for the integrated engine: send new signal, entry and exit
    messages. Never raises into the trading loop."""

    def __init__(self, settings, stores: dict, notifications):
        self.settings, self.stores, self.n = settings, stores, notifications
        self.errors: list[str] = []

    def after_step(self, now: datetime) -> None:
        cfg = self.settings.whatsapp
        if not cfg.enabled:
            return
        try:
            day = now.date().isoformat()
            events = []
            if cfg.send_signals:
                events += [(k, "signal", t) for k, t in signal_events(self.stores.get("B"), day)]
            events += [(k, k.split(":")[0], t) for k, t in
                       trade_events(self.stores, day, cfg.send_fills, cfg.send_exits)]
            for key, kind, text in events:
                self.n.deliver(key, kind, text, now)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{now.isoformat()}: {type(exc).__name__}: {exc}"[:300])
