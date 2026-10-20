"""Phase 7 data-quality and bias audit for a historical dataset.

Builds on ``HistoricalDataset.quality_report()`` (Phase 4 per-session
validation: invalid OHLC, NaN, duplicate/out-of-order rows, candles outside
the session, non-trading days, more than the allowed missing candles. Such
sessions are excluded, never repaired). Adds:

- **Missing sessions**: regular NSE trading days inside the dataset's range
  on which a symbol has no rows at all. They are never filled, and a
  missing session is never read as "the strategy would have avoided a loss".
- **Suspected unadjusted corporate actions**: an overnight move (previous
  accepted session's last close to the next session's first open) beyond
  ``CORPORATE_ACTION_GAP_PCT``. NSE price bands make such a move very
  unlikely without a split, bonus or similar adjustment. Policy: the
  symbol's data is **truncated at the first suspect**. Later sessions are
  dropped, so relative-volume and price-band baselines never mix pre- and
  post-adjustment prices or volumes. Every truncation is reported.
- **Point-in-time universe**: an optional ``universe_membership.csv``
  (symbol, from, to) in the data folder restricts each session to its
  members at that date. Without it, the universe is the fetch-time list,
  which is a survivorship-bias limitation.
- **Special sessions** in the data are reported. They are never traded.
- A list of known biases and limitations with their likely effect.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from app.backtesting.dataset import HistoricalDataset, SymbolData
from app.data.models import local_day_keys
from app.utils.market_calendar import MarketCalendar

CORPORATE_ACTION_GAP_PCT = 20.0
MEMBERSHIP_FILE = "universe_membership.csv"


def _session_edges(intraday: pd.DataFrame) -> pd.DataFrame:
    days = local_day_keys(intraday["timestamp"])
    g = intraday.assign(_day=days).groupby("_day", sort=True)
    return pd.DataFrame({"first_open": g["open"].first(), "last_close": g["close"].last()})


def corporate_action_suspects(dataset: HistoricalDataset,
                              gap_pct: float = CORPORATE_ACTION_GAP_PCT) -> list[dict]:
    out = []
    for sym, data in sorted(dataset.symbols.items()):
        edges = _session_edges(data.intraday)
        prev_close = edges["last_close"].shift(1)
        move = (edges["first_open"] / prev_close - 1) * 100
        for day, pct in move.dropna().items():
            if abs(pct) > gap_pct:
                ratio = float(edges.loc[day, "first_open"] / prev_close[day])
                out.append({"symbol": sym, "session": pd.Timestamp(day).date(),
                            "overnight_move_pct": round(float(pct), 2),
                            "price_ratio": round(ratio, 4)})
    return out


def apply_corporate_action_policy(dataset: HistoricalDataset,
                                  suspects: list[dict]) -> tuple[HistoricalDataset, list[dict]]:
    """Truncate each suspect symbol before its first suspected adjustment."""
    first: dict[str, date] = {}
    for s in suspects:
        first.setdefault(s["symbol"], s["session"])
    if not first:
        return dataset, []
    symbols: dict[str, SymbolData] = {}
    actions = []
    for sym, data in dataset.symbols.items():
        cut = first.get(sym)
        if cut is None:
            symbols[sym] = data
            continue
        cutoff = pd.Timestamp(cut).tz_localize(data.intraday["timestamp"].dt.tz)
        kept = data.intraday[data.intraday["timestamp"] < cutoff].reset_index(drop=True)
        dropped = sorted({d for d in data.accepted_sessions if d >= cut})
        actions.append({"symbol": sym, "first_suspect_session": cut,
                        "sessions_dropped": len(dropped),
                        "policy": "truncated: sessions from the suspected adjustment onward "
                                  "are not used"})
        if kept.empty:
            continue
        quality = [replace(q, accepted=False,
                           reasons=q.reasons + ("dropped: suspected unadjusted corporate action "
                                                f"from {cut}",))
                   if q.accepted and q.session >= cut else q for q in data.sessions]
        symbols[sym] = replace(data, intraday=kept, sessions=quality)
    key = dataset.dataset_id + "|ca-truncate|" + ",".join(f"{k}:{v}" for k, v in
                                                         sorted(first.items()))
    new_id = hashlib.sha256(key.encode()).hexdigest()[:16]
    return replace(dataset, symbols=symbols, dataset_id=new_id), actions


MembershipRows = list[tuple[str, date, date]]


def load_membership(directory: Path) -> tuple[MembershipRows | None, dict | None]:
    """Point-in-time universe rows from ``universe_membership.csv`` (columns
    symbol, from, to; ``to`` empty = still a member). (None, None) if absent."""
    path = Path(directory) / MEMBERSHIP_FILE
    if not path.exists():
        return None, None
    table = pd.read_csv(path, dtype=str).fillna("")
    missing = {"symbol", "from", "to"} - set(table.columns)
    if missing:
        raise ValueError(f"{path} lacks columns {sorted(missing)}")
    rows = [(r["symbol"].strip(), date.fromisoformat(r["from"].strip()),
             date.fromisoformat(r["to"].strip()) if r["to"].strip() else date.max)
            for _, r in table.iterrows()]
    info = {"file": path.name, "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return rows, info


def membership_fn(rows: MembershipRows | None) -> Callable[[date], set[str]] | None:
    if rows is None:
        return None

    def members(day: date) -> set[str]:
        return {sym for sym, start, end in rows if start <= day <= end}

    return members


def missing_sessions(dataset: HistoricalDataset, calendar: MarketCalendar) -> dict[str, list]:
    """Regular trading days in the dataset range with no rows for a symbol."""
    days = dataset.sessions()
    if not days:
        return {}
    expected = [d for d in calendar.trading_days(days[0], days[-1])
                if calendar.is_regular_session(d)]
    out = {}
    for sym, data in sorted(dataset.symbols.items()):
        seen = {s.session for s in data.sessions}
        gaps = [d for d in expected if d not in seen]
        if gaps:
            out[sym] = gaps
    return out


def audit_dataset(dataset: HistoricalDataset, calendar: MarketCalendar, manifest: dict | None,
                  membership_info: dict | None, ca_actions: list[dict],
                  suspects: list[dict], benchmark: str | None) -> dict:
    quality = dataset.quality_report()
    quality.pop("loaded_at", None)
    excluded = quality.get("excluded_sessions", [])
    reason_counts = Counter(r.split(":")[0].split(" (")[0] for e in excluded for r in e["reasons"])
    sessions = dataset.sessions()
    special = [d for d in sessions if calendar.special_session(d) is not None]
    gaps = missing_sessions(dataset, calendar)
    return {
        "dataset_id": dataset.dataset_id,
        "source": dataset.source,
        "symbols": len(dataset.symbols),
        "first_session": sessions[0] if sessions else None,
        "last_session": sessions[-1] if sessions else None,
        "sessions_with_data": len(sessions),
        "per_symbol": quality["symbols"],
        "excluded_sessions": excluded,
        "excluded_reason_counts": dict(sorted(reason_counts.items())),
        "rejected_symbols": quality["rejected_symbols"],
        "missing_sessions": {k: [d.isoformat() for d in v] for k, v in gaps.items()},
        "missing_session_count": sum(len(v) for v in gaps.values()),
        "special_sessions_in_data": special,
        "corporate_action_suspects": suspects,
        "corporate_action_actions": ca_actions,
        "point_in_time_universe": membership_info,
        "benchmark_symbol_present": benchmark,
        "fetch_manifest": manifest,
        "holidays_loaded": calendar.holidays_loaded,
        "holiday_years_covered": sorted({d.year for d in calendar.holidays}),
    }


def known_limitations(audit: dict, has_daily_file: bool) -> list[dict]:
    """Biases that cannot be removed with the available data, and their
    likely direction. Written for the report; never used to adjust results."""
    out = [
        {"issue": "No historical bid/ask quotes",
         "effect": "Spread is a constant assumption (execution.assumed_spread_pct), and the "
                   "spread screen cannot be applied. Real spreads in Rs 50-100 stocks vary and "
                   "widen at the open, when ORB entries happen. Net results are probably "
                   "optimistic; see the spread_x2 and spread_x3 scenarios."},
        {"issue": "Candle-only fills (execution model candle-v1)",
         "effect": "Entries at the next candle's open, stops and targets from candle "
                   "high/low. Queue position, partial fills and intrabar order are unknown. "
                   "Target fills on a touch are optimistic (target_trade_through scenario); "
                   "the stop-first rule for ambiguous candles is pessimistic."},
    ]
    if audit.get("point_in_time_universe") is None:
        out.append({"issue": "Survivorship and selection bias in the universe",
                    "effect": "The symbol list is the one supplied at fetch time. Stocks "
                              "that were delisted, suspended or renamed during the period "
                              "are missing, and choosing stocks by their current price or "
                              "popularity favours past winners. The screener still applies "
                              "the price band point-in-time, but only to listed symbols. "
                              "Results are probably optimistic. Provide "
                              f"{MEMBERSHIP_FILE} for a point-in-time universe."})
    manifest = audit.get("fetch_manifest") or {}
    out.append({"issue": "Corporate-action adjustment status unknown",
                "effect": manifest.get("adjustment_note") or
                "The data source does not document whether candles are adjusted for "
                "splits/bonuses. Overnight moves beyond "
                f"{CORPORATE_ACTION_GAP_PCT:g}% are flagged and the symbol truncated there; "
                "smaller adjustments (e.g. dividends) are not detected."})
    if not has_daily_file:
        out.append({"issue": "Liquidity from aggregated intraday data",
                    "effect": "Average traded value is approximated as close x volume from "
                              "intraday candles. The first 20 sessions cannot pass the "
                              "liquidity screen and are warm-up only."})
    if audit.get("missing_session_count"):
        out.append({"issue": "Missing sessions",
                    "effect": f"{audit['missing_session_count']} symbol-sessions have no data "
                              "and are not traded. If data gaps coincide with volatile days, "
                              "results can be biased either way. They are never counted as "
                              "avoided losses."})
    if audit.get("excluded_sessions"):
        out.append({"issue": "Excluded invalid sessions",
                    "effect": f"{len(audit['excluded_sessions'])} symbol-sessions failed "
                              "validation and were excluded (listed in data_quality.json)."})
    out.append({"issue": "Short history",
                "effect": "Groww provides about 3 months of 5-minute candles. A few dozen "
                          "sessions cannot represent different years, rate cycles or "
                          "market crises. Regime results are descriptive only."})
    return out
