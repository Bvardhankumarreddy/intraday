"""Run a scan from the command line.

Usage:
    python -m app.scan [--at 2025-03-12T10:30] [--config PATH] [--json] [--all]

``--at`` is the evaluation time in IST (default: now). Only data available at
that moment is used. Prints ranked candidates or NO TRADE. Never places orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from app.config import load_settings
from app.data.factory import build_provider
from app.scan.models import Candidate, ScanResult
from app.risk.engine import RiskCostEngine
from app.scan.pipeline import run_scan
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NSE opening-range breakout scan")
    parser.add_argument("--at", help="evaluation time, ISO format, IST if no offset")
    parser.add_argument("--config", help="path to settings YAML")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--all", action="store_true", help="show every instrument's status")
    parser.add_argument("--no-risk", action="store_true",
                        help="skip the risk/cost stage (Phase 2 technical view only)")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    configure_logging(settings.log_level)
    calendar = MarketCalendar(settings.market)
    as_of = datetime.fromisoformat(args.at) if args.at else datetime.now(calendar.tz)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=calendar.tz)

    provider = build_provider(settings, calendar, as_of=as_of)
    engine = None if args.no_risk else RiskCostEngine(settings)
    result = run_scan(provider, settings, calendar, as_of, risk_engine=engine)
    print(result.model_dump_json(indent=2) if args.json else render(result, args.all))
    return 0


def render(result: ScanResult, show_all: bool) -> str:
    out = [
        f"Scan at {result.evaluated_at:%Y-%m-%d %H:%M %Z}  |  data: {result.data_source} "
        f"[{result.data_mode.value.upper()}]  |  config {result.config_version}",
        "",
        f"DECISION: {result.decision.value.replace('_', ' ')}",
    ]
    for i, c in enumerate(result.ranked, 1):
        out += ["", *_setup_lines(i, c)]
    if show_all or not result.ranked:
        out += ["", "All instruments:"]
        for c in result.candidates:
            reason = c.rejection_reasons[0].message if c.rejection_reasons else ""
            out.append(f"  {c.symbol:<12} screen {c.screen.status.value:<17} "
                       f"signal {c.signal_status.value:<18} {reason}")
    out += ["", *("Note: " + n for n in result.notes)]
    return "\n".join(out)


def _setup_lines(rank: int, c: Candidate) -> list[str]:
    s = c.setup
    lines = [
        f"#{rank} {c.symbol} ({c.name})  score {c.score:.1f}/100  [{c.signal_status.value}]",
        f"   signal       : {s.entry_trigger}",
        f"   entry ref    : {s.entry_reference:.2f}   ({s.entry_basis})",
        f"   stop ref     : {s.stop_reference:.2f}   ({s.stop_rule})",
        f"   target ref   : {s.target_reference:.2f}   ({s.target_rule})",
        f"   risk/share   : {s.risk_per_share:.2f}   planned R:R {s.planned_reward_risk:.2f} "
        "before costs",
        f"   valid until  : {s.valid_until:%H:%M}   exit by {s.exit_by:%H:%M}",
        "   score        : " + ", ".join(f"{p.name} {p.points:g}/{p.weight:g}"
                                        for p in c.score_components),
    ]
    r = c.risk_report
    if r is None:
        return lines + ["   quantity/costs: NOT EVALUATED"]
    lines += [
        f"   REVIEW STATUS : {r.status.value}",
        f"   capital Rs {r.available_capital}  risk budget Rs {r.risk_budget} "
        f"({r.risk_pct}%)  qty by risk {r.risk_based_quantity} / by capital "
        f"{r.capital_based_quantity} -> final {r.final_quantity} "
        f"(limited by {r.binding_constraint})",
    ]
    if r.at_target and r.at_stop:
        for sc in (r.at_target, r.at_stop):
            lines.append(
                f"   at {sc.label:<6} {sc.exit_price}: gross {sc.gross_pnl:+}  costs "
                f"{sc.total_costs} (fees {sc.fees_total}, spread {sc.spread_cost}, "
                f"slippage {sc.slippage_cost})  NET {sc.net_pnl:+} ({sc.net_return_pct}%)")
        b, se = r.at_target.buy, r.at_target.sell
        lines += [
            f"   buy  fees: brokerage {b.brokerage} stamp {b.stamp_duty} exch {b.exchange_transaction}"
            f" ipft {b.ipft} sebi {b.sebi_turnover_fee} GST {b.gst} = {b.total}",
            f"   sell fees: brokerage {se.brokerage} STT {se.stt} exch {se.exchange_transaction}"
            f" ipft {se.ipft} sebi {se.sebi_turnover_fee} GST {se.gst} = {se.total}",
            f"   break-even exit {r.break_even_exit}  |  exit for Rs {r.desired_net_profit} net "
            f"{r.exit_for_desired_profit}  |  net R:R {r.net_reward_risk}",
        ]
    for reason in r.rejection_reasons:
        lines.append(f"   x {reason.code}: {reason.message}")
    if r.cost_model:
        cm = r.cost_model
        label = "ILLUSTRATIVE" if cm.illustrative else "verified " + str(cm.verified_on)
        lines.append(f"   cost model: {cm.schedule_id} ({label})")
    lines += [f"   estimate: {e}" for e in r.estimates]
    return lines


if __name__ == "__main__":
    sys.exit(main())
