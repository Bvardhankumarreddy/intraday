"""Strategy Robustness Report: writes ``<output_dir>/<baseline>/<run_id>/``.

Files: report.md, report.html (inline SVG charts), summary.json, the CSV
tables listed in ``FILES``, charts/*.svg, data_quality.json, and a copy of
the frozen baseline. No wall-clock time is written, so the same inputs give
byte-identical output.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

import pandas as pd

from app.robustness import charts
from app.robustness.runner import run_id

DISCLAIMER = ("SIMULATED RESEARCH on historical candles. Not actual trading results and not a "
              "forecast. Past simulated performance does not guarantee future results. No "
              "orders are placed by this tool.")
COST_COLS = ["brokerage", "stt", "stamp_duty", "exchange_transaction", "ipft",
             "sebi_turnover_fee", "gst", "spread_cost", "slippage_cost", "total_costs"]
KNOWN_OPEN_ISSUES = [
    "Phase 4's `walk_forward()` helper (app/backtesting/analysis.py) picks the best training "
    "configuration per fold. Phase 7 does not use it: the primary evaluation runs only the "
    "frozen baseline.",
    "Groww 5-minute data has no 15:20 candle on any day observed; its volume is folded into "
    "15:25. Sessions keep one missing candle (within the 3-candle tolerance).",
    "Groww's intraday volume is cumulative and is differenced by the adapter. If Groww "
    "changes this, the fetch rejects the batch rather than produce negative volume.",
    "Fee rates are verified only from 2026-10-19; earlier sessions use them retroactively "
    "(labelled).",
]


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n")


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (float, Decimal)):
        return f"{float(v):,.2f}"
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


class Doc:
    """Collects blocks and renders Markdown and HTML from the same content."""

    def __init__(self, title: str):
        self.title = title
        self.blocks: list[tuple] = []

    def h(self, text: str):
        self.blocks.append(("h", text))

    def p(self, text: str):
        self.blocks.append(("p", text))

    def ul(self, items: list[str]):
        if items:
            self.blocks.append(("ul", items))

    def table(self, headers: list[str], rows: list[list]):
        self.blocks.append(("table", headers, [[_fmt(c) for c in r] for r in rows]))

    def chart(self, name: str, svg: str):
        self.blocks.append(("chart", name, svg))

    def markdown(self) -> str:
        out = [f"# {self.title}\n", f"> **{DISCLAIMER}**\n"]
        for b in self.blocks:
            if b[0] == "h":
                out.append(f"\n## {b[1]}\n")
            elif b[0] == "p":
                out.append(b[1] + "\n")
            elif b[0] == "ul":
                out.append("".join(f"- {i}\n" for i in b[1]))
            elif b[0] == "table":
                _, headers, rows = b
                if not rows:
                    out.append("_No rows._\n")
                    continue
                out.append("| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n"
                           + "".join("| " + " | ".join(c.replace("|", "/") for c in r) + " |\n"
                                     for r in rows))
            elif b[0] == "chart":
                out.append(f"![{b[1]}](charts/{b[1]}.svg)\n")
        return "\n".join(out)

    def html(self) -> str:
        css = ("body{font-family:system-ui,sans-serif;max-width:1000px;margin:24px auto;"
               "padding:0 16px;color:#222;background:#fff}table{border-collapse:collapse;"
               "margin:8px 0 16px;font-size:13px;display:block;overflow-x:auto}td,th{border:1px "
               "solid #ccc;padding:4px 8px;text-align:left}th{background:#f2f4f7}.warn{"
               "background:#fff6e0;border:1px solid #e0b84c;padding:8px 12px}svg{max-width:100%;"
               "height:auto}")
        out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta "
               f"name='viewport' content='width=device-width,initial-scale=1'><title>"
               f"{escape(self.title)}</title><style>{css}</style></head><body>",
               f"<h1>{escape(self.title)}</h1><p class='warn'><b>{escape(DISCLAIMER)}</b></p>"]
        for b in self.blocks:
            if b[0] == "h":
                out.append(f"<h2>{escape(b[1])}</h2>")
            elif b[0] == "p":
                out.append(f"<p>{escape(b[1])}</p>")
            elif b[0] == "ul":
                out.append("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in b[1]) + "</ul>")
            elif b[0] == "table":
                _, headers, rows = b
                if not rows:
                    out.append("<p><i>No rows.</i></p>")
                    continue
                out.append("<table><tr>" + "".join(f"<th>{escape(h)}</th>" for h in headers)
                           + "</tr>" + "".join("<tr>" + "".join(f"<td>{escape(c)}</td>"
                                                                for c in r) + "</tr>"
                                               for r in rows) + "</table>")
            elif b[0] == "chart":
                out.append(f"<figure>{b[2]}</figure>")
        out.append("</body></html>\n")
        return "".join(out)


# -- sections -------------------------------------------------------------------


def _baseline_section(doc: Doc, results: dict, frozen_body: dict) -> None:
    m = results["meta"]
    s = frozen_body["settings"]
    st, sc, rk, ex, bt = (s["strategy"], s["screening"], s["risk"], s["execution"],
                          s["backtest"])
    doc.h("3. Frozen baseline configuration")
    doc.table(["Item", "Value"], [
        ["Baseline", f"{m['baseline_name']} ({m['baseline_file']})"],
        ["Frozen at", m["baseline_created_at"]],
        ["Strategy version", m["strategy_version"]],
        ["Strategy fingerprint (config)", m["strategy_fingerprint"]],
        ["Strategy-code hash (frozen)", m["code_hash_frozen"]],
        ["Strategy-code hash (current)", m["code_hash_current"] + (
            "  ** DRIFT: code changed since freezing **" if m["code_drift"] else " (unchanged)")],
        ["Execution model", m["execution_model_version"]],
        ["Protocol", f"{m['protocol_version']} sha256 {m['protocol_sha256'][:16]}..."],
        ["Data version (dataset ID)", m.get("dataset_id")],
    ])
    doc.table(["Parameter", "Frozen value"], [
        ["Candle interval", st["candle_interval"]],
        ["Opening range", f"{st['opening_range_minutes']} min from 09:15"],
        ["Entry", "first 5-min close above the opening-range high after a close at/below it, "
                  f"by {st['last_signal_time']}"],
        ["Relative-volume threshold", f"{st['relative_volume_threshold']} "
                                      f"({st['relative_volume_mode']}, "
                                      f"{st['relative_volume_lookback_sessions']} sessions, "
                                      f"min {st['relative_volume_min_sessions']})"],
        ["VWAP filter", "close > session VWAP (always on)"],
        ["EMA filter", f"{'on' if st['use_ema_filter'] else 'off'} (EMA{st['ema_period']})"],
        ["Max extension above range", f"{st['max_extension_pct']}%"],
        ["Stop", f"{st['stop_reference']}; risk {st['min_risk_pct']}-{st['max_risk_pct']}% "
                 "of entry"],
        ["Target / min reward:risk", f"entry + {st['min_reward_risk']} x risk"],
        ["Time exit", st["eod_exit_time"]],
        ["Price band", f"Rs {sc['min_price']}-{sc['max_price']}"],
        ["Liquidity", f"avg traded value >= Rs {sc['min_avg_traded_value']:,.0f} over "
                      f"{sc['liquidity_lookback_sessions']} sessions"],
        ["Spread filter", f"<= {sc['max_spread_pct']}% (live only; see adaptations)"],
        ["Capital", f"Rs {s['capital']['initial_capital']}"],
        ["Position sizing", f"{rk['risk_pct']}% of capital at risk per trade, whole shares, "
                            f"costs {'included' if rk['include_costs_in_risk'] else 'excluded'}"],
        ["Daily limits", f"max loss Rs {rk['max_daily_loss']}, max {rk['max_trades_per_day']} "
                         f"trades, {rk['max_concurrent_positions']} concurrent, aggregate risk "
                         f"Rs {rk['max_aggregate_risk']}"],
        ["Cost review", f"min net reward:risk {s['cost_review']['min_net_reward_risk']}"],
        ["Spread / slippage assumption", f"{ex['assumed_spread_pct']}% spread, "
                                         f"{ex['slippage_pct_per_side']}% slippage per fill"],
        ["Execution", f"next-candle-open entry, max gap {bt['max_entry_gap_pct']}%, target "
                      f"trade-through {bt['target_trade_through_pct']}%"],
    ])
    fee = m["fee_schedule"]
    doc.p(f"Fee model: {fee['schedule_id']} ({fee['broker']}, version {fee['version']}, "
          f"verified {fee['verified_on']}, {'ILLUSTRATIVE' if fee['illustrative'] else 'broker tariff'}).")
    doc.table(["Charge", "Effective from", "Source", "Retrieved"],
              [[x["item"], x["effective_from"], f"{x['name']} - {x['url']}", x["retrieved_on"]]
               for x in fee["sources"]])
    doc.p("Historical-data adaptations (applied at run time, not part of the fingerprint):")
    doc.ul([f"{k} = {v['value']}: {v['reason']}" for k, v in m["historical_adaptations"].items()])


def _quality_section(doc: Doc, results: dict) -> None:
    a = results.get("audit")
    doc.h("2. Data coverage and data-quality findings")
    if not a:
        doc.p("No dataset was loaded.")
        doc.ul(results.get("missing", []))
        return
    man = a.get("fetch_manifest") or {}
    doc.table(["Item", "Value"], [
        ["Source", a["source"]], ["Fetched", man.get("fetched_at", "unknown (no manifest)")],
        ["Provider", man.get("provider", "unknown")],
        ["Symbols (strategy universe)", a["symbols"]],
        ["Sessions with data", f"{a['sessions_with_data']} ({a['first_session']} .. "
                               f"{a['last_session']})"],
        ["Excluded symbol-sessions", len(a["excluded_sessions"])],
        ["Exclusion reasons", "; ".join(f"{k}: {v}" for k, v in
                                        a["excluded_reason_counts"].items()) or "none"],
        ["Missing symbol-sessions", a["missing_session_count"]],
        ["Rejected symbols", "; ".join(f"{k}: {v}" for k, v in a["rejected_symbols"].items())
         or "none"],
        ["Suspected corporate actions", len(a["corporate_action_suspects"])],
        ["Point-in-time universe", "yes" if a["point_in_time_universe"] else
         "NO (survivorship bias possible)"],
        ["Benchmark symbol in data", a["benchmark_symbol_present"] or "no"],
        ["Special sessions in data", ", ".join(map(str, a["special_sessions_in_data"]))
         or "none"],
        ["Holiday list years", ", ".join(map(str, a["holiday_years_covered"])) or "NONE"],
    ])
    if a["corporate_action_actions"]:
        doc.table(["Symbol", "First suspect session", "Sessions dropped", "Policy"],
                  [[x["symbol"], x["first_suspect_session"], x["sessions_dropped"], x["policy"]]
                   for x in a["corporate_action_actions"]])
    per = a["per_symbol"]
    doc.table(["Symbol", "Sessions", "Accepted", "Excluded", "Missing candles", "Zero-volume",
               "Daily source"],
              [[k, v["sessions_total"], v["sessions_accepted"], v["sessions_excluded"],
                v["missing_candles"], v["zero_volume_candles"], v["daily_source"]]
               for k, v in sorted(per.items())])


def _periods_section(doc: Doc, plan: dict | None) -> None:
    doc.h("4. Training, validation and out-of-sample periods")
    if not plan:
        doc.p("PENDING: no plan (no usable data).")
        return
    doc.p(f"Warm-up (history only, never traded): {plan['warmup_sessions']} sessions - "
          f"{plan['warmup_reason']}.")
    doc.p(f"Window rule: {plan['rule']}. Tradable sessions: {plan['tradable_sessions']}; "
          f"out-of-sample sessions: {plan['oos_sessions']}; sessions after the last full "
          f"fold (unused): {plan['unused_tail_sessions']}.")
    if plan["status"] != "OK":
        doc.p(f"INSUFFICIENT DATA: {plan['reason']}")
        return
    doc.table(["Fold", "Train", "Validation", "Test (out of sample)"],
              [[f["fold"]] + [f"{f[p][0]} .. {f[p][1]} ({f[p][2]})"
                              for p in ("train", "validation", "test")] for f in plan["folds"]])


def _sensitivity_section(doc: Doc, sens: dict, charts_out: dict) -> None:
    doc.h("8. Parameter-sensitivity analysis")
    if sens.get("status") != "OK":
        doc.p(f"Not run: {sens.get('reason')}.")
        return
    doc.p(f"Configurations listed: {sens['configurations_listed']}; tested: "
          f"{sens['configurations_tested']}; untestable: {sens['configurations_untestable']}; "
          f"failed: {sens['configurations_failed']}. Each was run once, continuously over "
          "the evaluated range, and its trades were split by window. No configuration is "
          "selected or deployed from this table.")
    doc.table(["Configuration", "Status", "OOS trades", "OOS net exp.",
               "Mean train exp.", "Mean val. exp.", "Mean test exp.", "t (all)"],
              [[r["configuration"] + (" [FROZEN]" if r["is_frozen_baseline"] else ""),
                r["status"] + (f" ({r['note']})" if r.get("note") else ""),
                r.get("oos_trades"), r.get("oos_net_expectancy"),
                r.get("mean_fold_train_expectancy"), r.get("mean_fold_validation_expectancy"),
                r.get("mean_fold_test_expectancy"), r.get("t_stat_all_trades")]
               for r in sens["summary"]])
    if sens["instability"]:
        doc.p("Unstable regions (adjacent values):")
        doc.ul([f"{f['parameter']}: {f['from']} -> {f['to']}: {f['reasons']}"
                for f in sens["instability"]])
    else:
        doc.p("No adjacent one-at-a-time values met the instability rule (this is not "
              "evidence of robustness when trade counts are small).")
    sb = sens["selection_bias"]
    doc.p(f"Selection bias: best t-statistic {_fmt(sb['best_t_stat'])} "
          f"({sb['best_configuration_by_t']}); expected maximum by chance among "
          f"{sens['configurations_tested']} configurations with no edge: "
          f"{_fmt(sb['expected_max_t_under_no_edge'])}. {sb['note']}")
    doc.p("Train-vs-test rank correlation by fold: " + (", ".join(
        f"fold {k}: {_fmt(v)}" for k, v in sorted(sens["train_test_rank_correlation_by_fold"]
                                                    .items())) or "n/a")
          + ". " + sens["rank_correlation_note"])
    hm = sens.get("heatmap")
    if hm:
        vals = {(c["x"], c["y"]): c["oos_net_expectancy"] for c in hm["cells"]}
        cnts = {(c["x"], c["y"]): c["oos_trades"] for c in hm["cells"]}
        charts_out["sensitivity_heatmap"] = charts.heatmap(
            "Out-of-sample net expectancy (Rs/trade) by parameter pair", hm["x"], hm["y"],
            hm["x_values"], hm["y_values"], vals, cnts)
        doc.chart("sensitivity_heatmap", charts_out["sensitivity_heatmap"])


def build_report(results: dict, frozen_body: dict) -> tuple[Doc, dict[str, str]]:
    doc = Doc("Strategy Robustness Report - ORB long (Phase 7)")
    ch: dict[str, str] = {}
    verdict = results["assessment"]
    ok = results["status"] == "OK"

    # 1 ---------------------------------------------------------------------------
    doc.h("1. Executive summary")
    doc.p(f"Verdict: {verdict['verdict']} - {verdict['statement']}")
    doc.ul(verdict["reasons"])
    if ok:
        o, b, d = results["oos"], results["bootstrap"], results["diagnostics"]
        doc.ul([
            f"Out-of-sample: {o['eligible_sessions']} sessions, {o['trades']} closed trades, "
            f"net P&L Rs {_fmt(o['total_net_pnl'])} after Rs {_fmt(o['total_costs'])} costs "
            f"(gross Rs {_fmt(o['total_gross_pnl'])}) on Rs {_fmt(o['initial_capital'])} "
            "starting capital per test window.",
            f"Net expectancy per trade: {_fmt(o['expectancy_per_trade'])}; "
            f"{int(b['confidence'] * 100)}% CI: "
            + (f"[{_fmt(b['ci_low'])}, {_fmt(b['ci_high'])}]" if b["status"] == "OK"
               else f"not computable ({b['reason']})") + ".",
            f"Technical signals out of sample: {d['signals_total']} ({d['executed']} executed, "
            f"{d['risk_rejected']} rejected by the risk/cost review: "
            f"{d['risk_rejected_by_status'] or 'none'}).",
            f"Sample reading: {results['sample_size'].get('reading') or results['sample_size'].get('note')}",
        ])
    else:
        doc.p("No empirical result was calculated. What is missing:")
        doc.ul(results["missing"])
        doc.p("Data needed to complete the evaluation:")
        doc.ul(results["data_needed"])

    _quality_section(doc, results)
    _baseline_section(doc, results, frozen_body)
    _periods_section(doc, results.get("plan"))

    # 5 ---------------------------------------------------------------------------
    doc.h("5. Results by walk-forward fold")
    if ok:
        doc.p("Every window was simulated separately from the initial capital with the frozen "
              "configuration. All folds are shown; none is omitted.")
        doc.table(["Fold", "Window", "Range", "Sessions", "Signals", "Trades", "Gross",
                   "Costs", "Net", "Net exp.", "Win %", "Profit factor", "Max DD"],
                  [[r["fold"], r["partition"], f"{r['start']}..{r['end']}", r["sessions"],
                    r["signals"], r["trades"], r["gross_pnl"], r["total_costs"], r["net_pnl"],
                    r["net_expectancy"], r["win_rate_pct"],
                    r["profit_factor"] if r["profit_factor"] is not None
                    else (r["profit_factor_note"] or "n/a"), r["max_drawdown_inr"]]
                   for r in results["folds"]])
        tests = [r for r in results["folds"] if r["partition"] == "test"]
        ch["by_fold"] = charts.bars("Out-of-sample net expectancy by fold (Rs/trade)",
                                    [f"fold {r['fold']}" for r in tests],
                                    [r["net_expectancy"] for r in tests],
                                    [f"n={r['trades']}" for r in tests])
        doc.chart("by_fold", ch["by_fold"])
        eq = results["oos_result"].daily_equity
        labels = [p.time.date().isoformat() for p in eq]
        values = [float(p.equity) for p in eq]
        ch["equity"] = charts.line("Out-of-sample equity, end of session (Rs; windows chained)",
                                   labels, values, zero_line=False)
        peak, dds = float(results["oos_result"].initial_capital), []
        for v in values:
            peak = max(peak, v)
            dds.append(v - peak)
        ch["drawdown"] = charts.line("Out-of-sample drawdown, end of session (Rs)", labels, dds)
        doc.chart("equity", ch["equity"])
        doc.chart("drawdown", ch["drawdown"])
    else:
        doc.p("PENDING: not calculated.")

    # 6 ---------------------------------------------------------------------------
    doc.h("6. Gross versus net performance")
    if ok:
        gn, o = results["gross_net"], results["oos"]
        doc.table(["Measure", "Out of sample"], [
            ["Trades", gn["trades"]], ["Gross P&L", gn["gross_pnl"]],
            ["Total costs", gn["total_costs"]], ["Net P&L", gn["net_pnl"]],
            ["Gross expectancy / trade", gn["gross_expectancy"]],
            ["Cost / trade", gn["cost_per_trade"]],
            ["Net expectancy / trade", gn["net_expectancy"]],
            ["Costs as % of gross profits", o["cost_pct_of_gross_profit"]],
            ["Costs as % of traded value", o["cost_pct_of_traded_value"]]])
        trades = results["oos_result"].table("trades")
        if not trades.empty:
            doc.table(["Cost component", "Total (Rs)"],
                      [[c, sum((Decimal(str(v)) for v in trades[c]), Decimal(0))]
                       for c in COST_COLS])
        d = results["diagnostics"]
        gb = d["gross_r_bootstrap"]
        doc.p(f"{d['label']}: {d['signals_total']} technically qualified signals; "
              f"{d['with_outcome']} had a simulated outcome. Mean gross R "
              f"{_fmt(d['mean_gross_r'])} (median {_fmt(d['median_gross_r'])}, "
              f"{_fmt((d['share_positive_gross'] or 0) * 100)}% positive). "
              + (f"{int(gb['confidence'] * 100)}% CI of mean gross R: "
                 f"[{_fmt(gb['ci_low'])}, {_fmt(gb['ci_high'])}]. " if gb["status"] == "OK"
                 else f"No interval: {gb['reason']}. ")
              + f"Risk-rejected signals at the largest affordable size would have netted Rs "
                f"{_fmt(d['shadow_affordable_net_total'])} over "
                f"{d['shadow_affordable_trades']} trades ({d['shadow_affordable_note']}).")
    else:
        doc.p("PENDING: not calculated.")

    # 7 ---------------------------------------------------------------------------
    doc.h("7. Transaction-cost sensitivity")
    if ok:
        sc = results["cost_scenarios"]
        doc.table(["Scenario", "Slippage x", "Spread x", "Target trade-through %", "Trades",
                   "Gross", "Costs", "Net", "Net exp.", "Rejected"],
                  [[k, v["slippage_multiplier"], v["spread_multiplier"],
                    v["target_trade_through_pct"], v["trades"], v["gross_pnl"],
                    v["total_costs"], v["net_pnl"], v["net_expectancy"], v["rejected_signals"]]
                   for k, v in sc.items()])
        ch["costs"] = charts.bars("Net expectancy by cost scenario (Rs/trade)", list(sc),
                                  [v["net_expectancy"] for v in sc.values()],
                                  [f"n={v['trades']}" for v in sc.values()])
        doc.chart("costs", ch["costs"])
        be = results["break_even"]
        doc.p("Break-even: " + (
            f"an extra Rs {_fmt(be['extra_cost_per_trade_inr'])} per trade "
            f"({be['extra_cost_pct_of_traded_value']}% of traded value) would bring net "
            f"expectancy to zero ({be['assumption']})." if be["status"] == "OK"
            else be.get("note", "not meaningful")))
        doc.p("Spread and slippage are assumptions: no historical bid/ask data exists, so "
              "every net figure depends on them. They cannot be verified from candles.")
    else:
        doc.p("PENDING: not calculated.")

    if ok:
        _sensitivity_section(doc, results["sensitivity"], ch)
    else:
        doc.h("8. Parameter-sensitivity analysis")
        doc.p("PENDING: not calculated.")

    # 9 ---------------------------------------------------------------------------
    doc.h("9. Market-regime and stock-level analysis")
    if ok:
        rg = results["regimes"]
        doc.p(f"Market proxy: {rg['proxy']}. Rules were fixed in the protocol before results "
              "(see docs/robustness.md). Descriptive only: groups marked 'no' under "
              "Interpretable have too few trades to say anything.")
        doc.table(["Dimension", "Group", "Sessions", "Trades", "Gross", "Costs", "Net",
                   "Net exp.", "Win %", "Max DD", "Interpretable"],
                  [[r["dimension"], r["group"], r["sessions_in_period"], r["trades"],
                    r["gross_pnl"], r["total_costs"], r["net_pnl"], r["net_expectancy"],
                    r["win_rate_pct"], r["max_drawdown_inr"], r["interpretable"]]
                   for r in rg["groups"]])
        trend = [r for r in rg["groups"] if r["dimension"] == "trend"]
        ch["regimes"] = charts.bars("Net expectancy by market trend regime (Rs/trade)",
                                    [r["group"] for r in trend],
                                    [r["net_expectancy"] for r in trend],
                                    [f"n={r['trades']}" for r in trend])
        doc.chart("regimes", ch["regimes"])
    else:
        doc.p("PENDING: not calculated.")

    # 10 --------------------------------------------------------------------------
    doc.h("10. Statistical uncertainty and sample-size limitations")
    if ok:
        o, b, s = results["oos"], results["bootstrap"], results["sample_size"]
        q = o["net_pnl_quantiles"] or {}
        doc.table(["Statistic", "Value"], [
            ["Eligible sessions", o["eligible_sessions"]],
            ["Sessions with trades", o["sessions_with_trades"]],
            ["Closed trades", o["trades"]], ["Wins / losses / zero",
                                              f"{o['wins']} / {o['losses']} / {o['breakeven_trades']}"],
            ["Win rate %", o["win_rate_pct"]], ["Average win", o["avg_win"]],
            ["Average loss", o["avg_loss"]], ["Expectancy (net)", o["expectancy_per_trade"]],
            ["Profit factor", o["profit_factor"] if o["profit_factor"] is not None
             else o["profit_factor_note"]],
            ["Max drawdown (Rs / %)", f"{_fmt(o['max_drawdown'])} / {_fmt(o['max_drawdown_pct'])}"],
            ["Average holding (min)", o["avg_holding_minutes"]],
            ["Max consecutive losses", o["max_consecutive_losses"]],
            ["Net P&L per trade quantiles", ", ".join(f"{k} {v}" for k, v in q.items())
             or "n/a"],
            ["Per-session mean / sd (not annualised)", o["session_mean_over_sd"]],
            ["Bootstrap", f"{b['method']}; block {b.get('block_length')}; {b['resamples']} "
                          f"resamples; seed {b['seed']}"],
            ["CI of net expectancy", f"[{_fmt(b['ci_low'])}, {_fmt(b['ci_high'])}]"
             if b["status"] == "OK" else b.get("reason")],
            ["Effective sample size", s.get("effective_n")],
            ["Design effect (dependence)", s.get("design_effect")],
            ["Minimum detectable effect (Rs/trade)", s.get("min_detectable_effect")],
            ["Trades needed for effect (Rs: n)", ", ".join(
                f"{k}: {v}" for k, v in (s.get("trades_needed_for_effect") or {}).items())
             or "n/a"],
        ])
        doc.p("Reading: " + (s.get("reading") or s.get("note", "")))
        doc.p("Win rate alone is not evidence of an edge. Profit factor is undefined with no "
              "losing trades and is never shown as infinity. Results are not annualised.")
        ch["distribution"] = charts.bars(
            "Distribution of net P&L per trade (Rs)",
            [f"{h['from']:.0f}" for h in o["net_pnl_histogram"]],
            [float(h["count"]) for h in o["net_pnl_histogram"]]) \
            if o["net_pnl_histogram"] else charts.unavailable(
                "Distribution of net P&L per trade", "no closed trades")
        doc.chart("distribution", ch["distribution"])
    else:
        doc.p("PENDING: not calculated. The Phase 6 forward paper-trading window "
              "(20-30 sessions) is an operational check, not proof of profitability.")

    # 11 --------------------------------------------------------------------------
    doc.h("11. Comparison with predefined baselines")
    if ok:
        rows = []
        o = results["oos"]
        rows.append(["ORB frozen baseline", "OK", o["trades"], o["total_gross_pnl"],
                     o["total_costs"], o["total_net_pnl"], o["expectancy_per_trade"],
                     "the strategy under evaluation"])
        for k, v in results["comparison"].items():
            rows.append([k, v["status"], v.get("trades"), v.get("gross_pnl"),
                         v.get("total_costs"), v.get("net_pnl"), v.get("net_expectancy"),
                         v.get("relevance") or v.get("reason")])
        doc.table(["Baseline", "Status", "Trades", "Gross", "Costs", "Net", "Net exp.",
                   "Why relevant"], rows)
        doc.p("Same out-of-sample sessions, initial capital per test window and cost model "
              "for all rows.")
    else:
        doc.p("PENDING: not calculated.")

    # 12 --------------------------------------------------------------------------
    doc.h("12. Known biases, limitations and unresolved defects")
    doc.table(["Issue", "Effect"], [[x["issue"], x["effect"]] for x in results["limitations"]])
    issues = list(KNOWN_OPEN_ISSUES)
    if results["meta"]["code_drift"]:
        issues.insert(0, "CODE DRIFT: decision-path code differs from the frozen baseline; "
                         "these results are not the frozen strategy version.")
    doc.ul(issues)

    # 13 --------------------------------------------------------------------------
    doc.h("13. Final evidence-based assessment")
    doc.table(["Criterion", "Status", "Detail"],
              [[c["criterion"], c["status"], c["detail"]] for c in verdict["criteria"]])
    doc.p(f"{verdict['verdict']}: {verdict['statement']}")
    if verdict["verdict"] == "INCONCLUSIVE" and ok:
        need = results["sample_size"].get("trades_needed_for_effect") or {}
        doc.p("Additional observations are needed. " + (
            "Approximate trades needed to tell these expectancies (Rs/trade) apart from zero: "
            + ", ".join(f"{k}: {v}" for k, v in need.items()) + "." if need else ""))
    doc.p("No parameter was selected or changed by this evaluation. Any modified strategy "
          "needs a new frozen baseline and a fresh, untouched test period.")
    return doc, ch


FILES = {"folds": "folds.csv", "cost_scenarios": "cost_scenarios.csv",
         "sensitivity_summary": "sensitivity_summary.csv",
         "sensitivity_by_fold": "sensitivity_by_fold.csv", "regimes": "regimes.csv",
         "regime_sessions": "regime_sessions.csv", "comparison": "comparison_baselines.csv"}


def write_report(results: dict, frozen_path: Path, out_root: Path) -> Path:
    frozen_body = json.loads(Path(frozen_path).read_text())
    rid = run_id(results)
    root = Path(out_root) / results["meta"]["baseline_name"] / rid
    if root.exists():
        shutil.rmtree(root)  # same inputs -> same folder; rebuilt from scratch
    (root / "charts").mkdir(parents=True)
    doc, ch = build_report(results, frozen_body)
    (root / "report.md").write_text(doc.markdown())
    (root / "report.html").write_text(doc.html())
    for name, svg in ch.items():
        (root / "charts" / f"{name}.svg").write_text(svg)
    shutil.copyfile(frozen_path, root / "frozen_baseline.json")
    (root / "frozen_baseline.json").chmod(0o444)
    summary = {k: v for k, v in results.items() if k not in ("oos_result", "folds", "audit")}
    summary["run_id"] = rid
    summary["disclaimer"] = DISCLAIMER
    if results["status"] == "OK":
        summary["sensitivity"] = {k: v for k, v in results["sensitivity"].items()
                                  if k not in ("summary", "by_fold")}
        summary["regimes"] = {"proxy": results["regimes"]["proxy"],
                              "rules": results["regimes"]["rules"]}
        summary["comparison"] = {k: {kk: vv for kk, vv in v.items() if kk != "rows"}
                                 for k, v in results["comparison"].items()}
    _dump(root / "summary.json", summary)
    _dump(root / "data_quality.json", results.get("audit"))
    if results["status"] != "OK":
        return root
    oos = results["oos_result"]
    for name, file in [("trades", "oos_trades.csv"), ("rejected", "oos_rejected_signals.csv"),
                       ("daily_equity", "oos_equity_daily.csv"),
                       ("shadow", "oos_signal_diagnostics.csv")]:
        oos.table(name).to_csv(root / file, index=False)
    tables = {
        "folds": results["folds"],
        "cost_scenarios": [{"scenario": k, **v} for k, v in results["cost_scenarios"].items()],
        "sensitivity_summary": results["sensitivity"].get("summary", []),
        "sensitivity_by_fold": results["sensitivity"].get("by_fold", []),
        "regimes": results["regimes"]["groups"],
        "regime_sessions": results["regimes"]["sessions"],
        "comparison": [{"baseline": k, **r} for k, v in results["comparison"].items()
                       for r in v.get("rows", [])],
    }
    for key, rows in tables.items():
        pd.DataFrame(rows).to_csv(root / FILES[key], index=False)
    return root

