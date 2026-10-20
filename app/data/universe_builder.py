"""Build ``config/universe.csv`` from data instead of by hand.

    python -m app.data.universe_builder [--top 50] [--dry-run] [--force]

The universe is the pool the screener checks every session. This tool picks
it with fixed, recorded rules, using only data up to the build date. It
never uses returns or "recent winners".

1. Groww's public instrument master (no login): NSE, CASH segment, series EQ,
   instrument type EQ, intraday allowed, buy and sell allowed; exchange-traded
   funds (symbol or name containing ETF or BEES) excluded.
2. The last ``lookback_days`` calendar days of DAILY candles per candidate,
   through the existing read-only Groww adapter (allowlisted data calls
   only, retries, circuit breaker). The pace is 0.25 s per call, i.e. 240
   per minute, under Groww's documented 300 per minute.
3. Keep the stock if its last close is within ``screening.min_price`` x
   (1 - band) to ``screening.max_price`` x (1 + band); band 20%, because
   prices drift and the screener applies the exact range each session.
4. Keep it if its average daily traded value (close x volume) over the last
   ``screening.liquidity_lookback_sessions`` sessions is at least
   ``screening.min_avg_traded_value``, with that many sessions present.
5. Rank by average traded value and keep the top N (default 50, sized for
   the live rate limit).

The written file starts with comment lines recording the build time,
sources, rules and counts. A full JSON report of every candidate and its
reason is kept in ``data/universe/``. An existing non-empty universe is
never overwritten without ``--force``; the previous file is archived.

Limitations: it is built from symbols listed TODAY, so stocks delisted
earlier are absent (survivorship). It is a starting pool, not a
recommendation. Review it before use, and do not change it during an
evaluation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.config import PROJECT_ROOT, load_settings
from app.data.models import DataError, Instrument
from app.utils.fs import private_dir, write_preserving

INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
PRICE_BAND = 0.20


def static_candidates(instruments: pd.DataFrame) -> pd.DataFrame:
    df = instruments
    keep = ((df["exchange"] == "NSE") & (df["segment"] == "CASH") & (df["series"] == "EQ")
            & (df["instrument_type"] == "EQ") & (df["is_intraday"] == 1)
            & (df["buy_allowed"] == 1) & (df["sell_allowed"] == 1))
    # Exchange-traded funds share the EQ series; they are baskets, not company stocks.
    etf = (df["trading_symbol"].astype(str).str.contains("ETF|BEES", case=False, regex=True)
           | df["name"].fillna("").astype(str).str.contains(r"\bETF\b|BEES", case=False,
                                                           regex=True))
    out = df.loc[keep & ~etf, ["trading_symbol", "name", "isin"]] \
        .drop_duplicates("trading_symbol")
    return out.sort_values("trading_symbol").reset_index(drop=True)


def evaluate(symbol: str, frame: pd.DataFrame, settings, today: date) -> dict:
    s = settings.screening
    lo, hi = s.min_price * (1 - PRICE_BAND), s.max_price * (1 + PRICE_BAND)
    f = frame[frame["timestamp"].dt.date < today].tail(s.liquidity_lookback_sessions)
    if len(f) < s.liquidity_lookback_sessions:
        return {"symbol": symbol, "keep": False,
                "reason": f"only {len(f)} sessions (< {s.liquidity_lookback_sessions})"}
    close = float(f.iloc[-1]["close"])
    atv = float((f["close"] * f["volume"]).mean())
    row = {"symbol": symbol, "last_close": round(close, 2), "avg_traded_value": round(atv),
           "last_session": f.iloc[-1]["timestamp"].date().isoformat()}
    if not lo <= close <= hi:
        return {**row, "keep": False, "reason": f"last close {close:.2f} outside {lo:.0f}-{hi:.0f}"}
    if atv < s.min_avg_traded_value:
        return {**row, "keep": False,
                "reason": f"avg traded value {atv:,.0f} < {s.min_avg_traded_value:,.0f}"}
    return {**row, "keep": True, "reason": "passes price band and liquidity"}


def build(settings, calendar, provider_factory, instruments: pd.DataFrame, top: int,
          today: date, limit: int | None = None, progress=print) -> dict:
    cands = static_candidates(instruments)
    if limit:
        cands = cands.head(limit)
    names = dict(zip(cands["trading_symbol"], cands["name"].fillna("")))
    provider = provider_factory([Instrument(sym, names[sym] or sym) for sym in names])
    start = today - timedelta(days=45)
    rows, errors = [], 0
    for i, sym in enumerate(names, 1):
        try:
            series = provider.get_candles(sym, "1d", start, today - timedelta(days=1))
            rows.append(evaluate(sym, series.frame, settings, today))
        except DataError as exc:
            errors += 1
            rows.append({"symbol": sym, "keep": False, "reason": f"no usable data: {exc}"[:200]})
            if "circuit OPEN" in str(exc):
                raise RuntimeError("Groww data circuit opened (repeated failures); stopped "
                                   "without writing a universe. Retry later.") from exc
        if progress and i % 100 == 0:
            progress(f"  {i}/{len(names)} checked")
    kept = sorted((r for r in rows if r["keep"]), key=lambda r: -r["avg_traded_value"])
    selected = kept[:top]
    for r in selected:
        r["name"] = names[r["symbol"]]
    sessions = [r["last_session"] for r in rows if r.get("last_session")]
    return {"built_at": datetime.now(calendar.tz).isoformat(), "as_of": today.isoformat(),
            "data_through": max(sessions) if sessions else None,
            "source": f"{INSTRUMENT_CSV_URL} + Groww daily candles (read-only)",
            "rules": {"exchange": "NSE", "series": "EQ", "intraday_allowed": True,
                      "etfs_excluded": True,
                      "price_band": [round(settings.screening.min_price * (1 - PRICE_BAND), 2),
                                     round(settings.screening.max_price * (1 + PRICE_BAND), 2)],
                      "min_avg_traded_value": settings.screening.min_avg_traded_value,
                      "liquidity_sessions": settings.screening.liquidity_lookback_sessions,
                      "rank": "average traded value, descending", "top": top},
            "counts": {"static_candidates": len(names), "data_errors": errors,
                       "passed": len(kept), "selected": len(selected)},
            "selected": selected, "all": rows}


def write(result: dict, path: Path, report_dir: Path) -> Path:
    r = result
    lines = [f"# Built {r['built_at']} with daily data through {r['data_through']} (python -m "
             "app.data.universe_builder). Review before use; do not change during an evaluation.",
             f"# Source: {r['source']}",
             f"# Rules: {json.dumps(r['rules'], sort_keys=True)}",
             f"# Counts: {json.dumps(r['counts'], sort_keys=True)}",
             "# Survivorship: built from currently listed symbols. Not a recommendation.",
             "symbol,name"]
    lines += [f"{x['symbol']},{x['name'].replace(',', ' ')}" for x in r["selected"]]
    write_preserving(Path(path), "\n".join(lines) + "\n")
    report = private_dir(report_dir) / f"universe_{r['as_of']}.json"
    write_preserving(report, json.dumps(r, indent=2, default=str) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.data.universe_builder")
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true", help="show the selection, write nothing")
    ap.add_argument("--force", action="store_true", help="replace a non-empty universe file")
    ap.add_argument("--limit", type=int, help="check only the first N candidates (testing)")
    args = ap.parse_args(argv)

    from app.data.factory import groww_provider, load_universe
    from app.utils.market_calendar import MarketCalendar

    settings = load_settings()
    cal = MarketCalendar(settings.market)
    path = Path(settings.data.universe_file)
    try:
        existing = len(load_universe(path))
    except DataError:
        existing = 0
    if existing and not args.force and not args.dry_run:
        print(f"REFUSED: {path} already lists {existing} symbols; use --force to replace it "
              "(the old file is archived). Do not change it during an evaluation.")
        return 2
    if settings.broker_api_key is None or settings.broker_totp_secret is None and \
            settings.broker_api_secret is None:
        print("ERROR: Groww data credentials missing (NSE_BROKER_API_KEY + TOTP or secret)")
        return 1
    print("Downloading the Groww instrument master (public file) ...")
    instruments = pd.read_csv(INSTRUMENT_CSV_URL, low_memory=False)
    paced = settings.model_copy(update={"data": settings.data.model_copy(update={
        "min_seconds_between_calls": max(0.25, settings.data.min_seconds_between_calls)})})
    today = datetime.now(cal.tz).date()
    print(f"{len(static_candidates(instruments))} NSE EQ intraday-eligible symbols; fetching "
          "daily candles (about 4 per second) ...")
    shared = {}

    def factory(inst):  # one login, reused by the provider (only factory.py builds it)
        p = groww_provider(paced, cal, inst, shared.get("client"))
        shared["client"] = p.client
        return p

    result = build(paced, cal, factory, instruments, args.top, today, args.limit)
    c = result["counts"]
    print(f"checked {c['static_candidates']}, data errors {c['data_errors']}, passed "
          f"{c['passed']}, selected {c['selected']}")
    for x in result["selected"]:
        print(f"  {x['symbol']:14} close {x['last_close']:>8}  avg traded value "
              f"Rs {x['avg_traded_value']:>15,}  {x['name']}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    report = write(result, path, PROJECT_ROOT / "data" / "universe")
    print(f"Written {path} ({c['selected']} symbols); full report {report}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - one clear line, no traceback, no secrets
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
