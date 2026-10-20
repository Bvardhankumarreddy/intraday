"""Phase 1 command-line entry point: data-pipeline check.

Usage:
    python -m app.main [--config PATH] [--date YYYY-MM-DD] [--interval 5m]

Loads settings, reports the session status, then fetches and validates
candles for every instrument from the configured provider. It does not
generate signals yet and never places orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from app.config import load_settings
from app.data.factory import build_provider
from app.data.models import DataError
from app.utils.logging import configure_logging, get_logger
from app.utils.market_calendar import MarketCalendar

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NSE intraday research agent - data check")
    parser.add_argument("--config", help="path to settings YAML")
    parser.add_argument("--date", type=date.fromisoformat, help="session date (default: latest)")
    parser.add_argument("--interval", default=None, help="candle interval (default: strategy)")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    configure_logging(settings.log_level)
    calendar = MarketCalendar(settings.market)
    now = datetime.now(calendar.tz)

    day = args.date or now.date()
    if not calendar.is_trading_day(day) or now < calendar.session_bounds(day)[0]:
        day = calendar.previous_trading_day(day)
    as_of = calendar.session_bounds(day)[1] if args.date or day != now.date() else now
    provider = build_provider(settings, calendar, as_of=as_of)
    interval = args.interval or settings.strategy.candle_interval

    print(f"Exchange session now : {calendar.session_status(now).value} ({now:%Y-%m-%d %H:%M %Z})")
    if not calendar.holidays_loaded:
        print("WARNING: no NSE holiday list loaded - weekdays are assumed to be trading days.")
    print(f"Data provider        : {provider.name} [{provider.mode.value.upper()}]")
    print(f"Session evaluated    : {day}  interval {interval}")
    print(f"Capital / risk       : Rs {settings.capital.initial_capital:,.2f} / "
          f"{settings.risk.risk_pct}% per trade")
    print()

    lookback_start = day - timedelta(days=45)
    failures = 0
    for inst in provider.list_instruments():
        try:
            candles = provider.get_candles(inst.symbol, interval, day, day)
            daily = provider.get_candles(inst.symbol, "1d", lookback_start, day)
        except DataError as exc:
            failures += 1
            print(f"{inst.symbol:<12} REJECTED  {exc}")
            log.warning("data rejected", extra={"symbol": inst.symbol, "error": str(exc)})
            continue
        done = candles.completed(as_of)
        last = done.iloc[-1] if not done.empty else None
        prior = daily.frame[daily.frame["timestamp"].dt.date < day].tail(
            settings.screening.liquidity_lookback_sessions
        )
        avg_value = (prior["close"] * prior["volume"]).mean() if not prior.empty else float("nan")
        status = "ok" if inst.tradable else "NOT TRADABLE"
        price = f"{last['close']:>8.2f} @ {last['timestamp']:%H:%M}" if last is not None else "n/a"
        print(
            f"{inst.symbol:<12} {status:<12} last {price}  "
            f"{len(done):>3} candles  avg traded value Rs {avg_value / 1e5:,.1f} lakh"
            + (f"  [{len(candles.warnings)} warnings]" if candles.warnings else "")
        )

    print("\nPrices above are", provider.mode.value.upper(), "data. No signals are generated in Phase 1.")
    return 1 if failures and failures == len(provider.list_instruments()) else 0


if __name__ == "__main__":
    sys.exit(main())
