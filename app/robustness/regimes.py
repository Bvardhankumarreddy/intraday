"""Market-regime and stock-level grouping (rules predeclared in the protocol).

Session classification uses a market proxy: the benchmark symbol's own
5-minute candles when it is in the dataset, otherwise the equal-weight
average across all strategy symbols (labelled as such):

- trend: proxy open-to-close return >= +threshold -> strong_up,
  <= -threshold -> strong_down, otherwise range_bound.
- volatility: proxy (high - low) / open vs the median of the previous
  ``lookback_sessions`` sessions: >= high ratio -> high_volatility,
  <= low ratio -> low_volatility, otherwise normal_volatility.
- liquidity: sum of close x volume over all strategy symbols vs the same
  trailing median, with its own ratios.
- Sessions without enough prior history are ``unclassified``.

Classifications describe what a session turned out to be. They group results
after the fact and are never used to make a trading decision.

Trades are also grouped by symbol, entry-price band, month and quarter.
Every group is reported, including negative ones; groups below
``min_trades_to_interpret`` trades are marked too small to interpret.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from app.backtesting.dataset import HistoricalDataset, SymbolData
from app.backtesting.engine import TradeRecord
from app.data.models import local_day_keys
from app.robustness.protocol import RegimeRules


def _session_stats(intraday: pd.DataFrame) -> pd.DataFrame:
    g = intraday.assign(_d=local_day_keys(intraday["timestamp"])).groupby("_d", sort=True)
    out = pd.DataFrame({"open": g["open"].first(), "close": g["close"].last(),
                        "high": g["high"].max(), "low": g["low"].min()})
    out["ret_pct"] = (out["close"] / out["open"] - 1) * 100
    out["range_pct"] = (out["high"] - out["low"]) / out["open"] * 100
    out["traded_value"] = (intraday["close"] * intraday["volume"]).groupby(
        local_day_keys(intraday["timestamp"])).sum()
    out.index = [pd.Timestamp(d).date() for d in out.index]
    return out


def proxy_series(dataset: HistoricalDataset, benchmark: SymbolData | None) -> tuple[pd.DataFrame, str]:
    per = {s: _session_stats(d.intraday) for s, d in dataset.symbols.items()}
    liquidity = pd.concat([p["traded_value"] for p in per.values()], axis=1).sum(axis=1)
    if benchmark is not None:
        b = _session_stats(benchmark.intraday)
        frame = pd.DataFrame({"ret_pct": b["ret_pct"], "range_pct": b["range_pct"]})
        label = f"benchmark {benchmark.instrument.symbol}"
    else:
        frame = pd.DataFrame({
            "ret_pct": pd.concat([p["ret_pct"] for p in per.values()], axis=1).mean(axis=1),
            "range_pct": pd.concat([p["range_pct"] for p in per.values()], axis=1).mean(axis=1)})
        label = "equal-weight average of the strategy universe (no benchmark symbol in data)"
    frame["liquidity"] = liquidity
    return frame.sort_index(), label


def classify_sessions(proxy: pd.DataFrame, rules: RegimeRules) -> pd.DataFrame:
    out = pd.DataFrame(index=proxy.index)
    t = rules.trend_threshold_pct
    out["trend"] = np.where(proxy["ret_pct"] >= t, "strong_up",
                            np.where(proxy["ret_pct"] <= -t, "strong_down", "range_bound"))
    n = rules.lookback_sessions
    # Median of the previous n sessions only (shift excludes the session itself).
    vol_ref = proxy["range_pct"].shift(1).rolling(n, min_periods=n).median()
    liq_ref = proxy["liquidity"].shift(1).rolling(n, min_periods=n).median()
    vr, lr = proxy["range_pct"] / vol_ref, proxy["liquidity"] / liq_ref
    out["volatility"] = np.select([vr.isna(), vr >= rules.high_volatility_ratio,
                                   vr <= rules.low_volatility_ratio],
                                  ["unclassified", "high_volatility", "low_volatility"],
                                  "normal_volatility")
    out["liquidity"] = np.select([lr.isna(), lr >= rules.high_liquidity_ratio,
                                  lr <= rules.low_liquidity_ratio],
                                 ["unclassified", "high_liquidity", "low_liquidity"],
                                 "normal_liquidity")
    out["proxy_return_pct"] = proxy["ret_pct"].round(3)
    out["proxy_range_pct"] = proxy["range_pct"].round(3)
    return out


def price_band(price: Decimal, bands: list[float]) -> str:
    p = float(price)
    for lo, hi in zip(bands, bands[1:]):
        if lo <= p < hi:
            return f"{lo:g}-{hi:g}"
    return f"<{bands[0]:g}" if p < bands[0] else f">={bands[-1]:g}"


def _group_stats(trades: list[TradeRecord], min_n: int) -> dict:
    trades = sorted(trades, key=lambda t: t.exit_time)
    n = len(trades)
    net = [t.net_pnl for t in trades]
    cum = peak = Decimal(0)
    dd = Decimal(0)
    for x in net:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    total = sum(net, Decimal(0))
    return {"trades": n, "sessions": len({t.session for t in trades}),
            "gross_pnl": float(sum((t.gross_pnl for t in trades), Decimal(0))),
            "total_costs": float(sum((t.total_costs for t in trades), Decimal(0))),
            "net_pnl": float(total),
            "net_expectancy": round(float(total) / n, 4) if n else None,
            "win_rate_pct": round(100 * sum(x > 0 for x in net) / n, 2) if n else None,
            "max_drawdown_inr": float(dd),
            "interpretable": n >= min_n}


def group_trades(trades: list[TradeRecord], regimes: pd.DataFrame, rules: RegimeRules,
                 sessions: list[date]) -> list[dict]:
    keys = {
        "trend": lambda t: regimes["trend"].get(t.session, "unclassified"),
        "volatility": lambda t: regimes["volatility"].get(t.session, "unclassified"),
        "liquidity": lambda t: regimes["liquidity"].get(t.session, "unclassified"),
        "symbol": lambda t: t.symbol,
        "price_band": lambda t: price_band(t.entry_price, rules.price_bands),
        "month": lambda t: t.session.strftime("%Y-%m"),
        "quarter": lambda t: f"{t.session.year}-Q{(t.session.month - 1) // 3 + 1}",
    }
    # Session counts per regime (incl. sessions without trades) for context.
    session_counts: dict[tuple[str, str], int] = defaultdict(int)
    for d in sessions:
        for dim in ("trend", "volatility", "liquidity"):
            session_counts[(dim, regimes[dim].get(d, "unclassified"))] += 1
    rows = []
    for dim, fn in keys.items():
        groups: dict[str, list[TradeRecord]] = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t)
        labels = set(groups)
        if dim in ("trend", "volatility", "liquidity"):
            labels |= {lbl for (d, lbl) in session_counts if d == dim}
        for label in sorted(labels):
            rows.append({"dimension": dim, "group": label,
                         "sessions_in_period": session_counts.get((dim, label)),
                         **_group_stats(groups.get(label, []), rules.min_trades_to_interpret)})
    return rows
