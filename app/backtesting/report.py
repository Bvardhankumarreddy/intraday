"""Write backtest artifacts to ``<output_dir>/<run_id>/``.

The run ID is a hash of the dataset ID, config version, strategy and
execution-model versions and the period, so identical inputs give the same
directory and byte-identical files (no wall-clock times inside).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pandas as pd

from app.backtesting.dataset import HistoricalDataset
from app.backtesting.engine import BacktestResult
from app.config import Settings

DISCLAIMER = ("SIMULATED BACKTEST - not actual trading results. Candle-based fills are "
              "approximations. Past simulated performance does not predict future results.")


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def run_id(result: BacktestResult) -> str:
    key = "|".join([result.dataset_id, result.config_version, result.strategy_version,
                    result.execution_model_version, str(result.start), str(result.end)])
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=_json_default) + "\n")


def write_report(result: BacktestResult, metrics: dict, dataset: HistoricalDataset,
                 settings: Settings, out_dir: Path | None = None,
                 oos: dict | None = None, sensitivity_rows: list[dict] | None = None,
                 walk_forward_rows: list[dict] | None = None) -> Path:
    root = Path(out_dir or settings.backtest.output_dir) / run_id(result)
    root.mkdir(parents=True, exist_ok=True)

    quality = dataset.quality_report()
    quality.pop("loaded_at", None)  # keep files reproducible
    _dump(root / "config.json", {
        "disclaimer": DISCLAIMER,
        "run_id": run_id(result),
        "period": {"start": result.start, "end": result.end, "sessions": len(result.sessions)},
        "dataset_id": result.dataset_id,
        "config_version": result.config_version,
        "strategy_version": result.strategy_version,
        "execution_model_version": result.execution_model_version,
        "fee_schedule_id": result.fee_schedule_id,
        "settings": settings.model_dump(
            mode="json", exclude={"broker_api_key", "broker_api_secret",
                                   "broker_totp_secret", "llm_api_key",
                                   "whatsapp_token"}),
    })
    _dump(root / "dataset_quality.json", quality)
    _dump(root / "metrics.json", {"disclaimer": DISCLAIMER, **metrics})
    for name, file in [("trades", "trades.csv"), ("orders", "orders.csv"), ("fills", "fills.csv"),
                       ("rejected", "rejected_signals.csv"), ("daily_equity", "equity_daily.csv"),
                       ("equity", "equity_intraday.csv")]:
        result.table(name).to_csv(root / file, index=False)

    trades = result.table("trades")
    cost_cols = ["brokerage", "stt", "stamp_duty", "exchange_transaction", "ipft",
                 "sebi_turnover_fee", "gst", "spread_cost", "slippage_cost", "total_costs"]
    _dump(root / "costs.json", {
        c: str(sum((Decimal(str(v)) for v in trades[c]), Decimal(0))) if not trades.empty else "0"
        for c in cost_cols
    })
    if oos is not None:
        _dump(root / "out_of_sample.json", {"disclaimer": DISCLAIMER, **oos})
    if sensitivity_rows is not None:
        pd.DataFrame(sensitivity_rows).to_csv(root / "sensitivity.csv", index=False)
    if walk_forward_rows is not None:
        pd.DataFrame(walk_forward_rows).to_csv(root / "walk_forward.csv", index=False)
    (root / "assumptions.md").write_text(
        f"# Assumptions and limitations\n\n**{DISCLAIMER}**\n\n"
        + "".join(f"- {a}\n" for a in result.assumptions)
        + f"- Evidence: {metrics['evidence']} ({metrics['trades']} trades; minimum "
          f"{metrics['min_trades_for_evidence']}).\n"
    )
    return root

