"""Frozen baseline: the exact strategy configuration, code and evaluation
protocol used for the primary Phase 7 evaluation.

``freeze`` writes ``<baselines_dir>/<name>.json`` once (read-only, never
overwritten). It contains:

- the full non-secret settings, with paths stored relative to the project
  so the file still loads after a move to a server;
- a portable **strategy fingerprint**: a SHA-256 of every setting that can
  change a trading decision, cost or risk check. Paths, output folders,
  logging and live-data plumbing are excluded. The fee schedule and holiday
  files are included by content hash;
- a **strategy-code hash** over the decision-path source files (the project
  is not a git repository, so this is the code version);
- the strategy and execution-model versions;
- the fee schedule ID, version, verification date and every source;
- the predeclared protocol (full text and SHA-256);
- a self-hash of the whole file.

``load_frozen`` rebuilds the settings from the file alone (environment and
YAML overrides are ignored), then re-verifies the self-hash and fingerprint.
The code hash is compared with the current code. A difference is reported as
drift, and ``run`` refuses to treat it as the same strategy version.

Nothing here changes a parameter. A modified strategy needs a new baseline
name and a fresh evaluation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backtesting.engine import STRATEGY_VERSION
from app.backtesting.execution import execution_model_label
from app.config import PROJECT_ROOT, Settings, settings_from_values
from app.costs.schedule import load_fee_schedule, schedule_path
from app.robustness.protocol import Protocol, load_protocol, protocol_from_text

FORMAT = "nse-orb-frozen-baseline/1"
SECRETS = {"broker_api_key", "broker_api_secret", "broker_totp_secret", "llm_api_key",
           "whatsapp_token"}
# Sections that can change a decision, a fill, a cost or a risk check.
FINGERPRINT_SECTIONS = ("market", "capital", "risk", "screening", "strategy", "scoring",
                        "execution", "cost_review", "backtest", "broker")
# Path-valued settings inside those sections (replaced by content hashes or dropped).
_FINGERPRINT_DROP = {("market", "holidays_file"), ("broker", "fee_schedule_file"),
                     ("backtest", "output_dir")}
# Source files on the decision path. Their combined hash is the code version.
CODE_FILES = (
    "app/strategies/*.py", "app/screening/*.py", "app/scan/*.py", "app/costs/*.py",
    "app/risk/*.py", "app/backtesting/engine.py", "app/backtesting/execution.py",
    "app/backtesting/dataset.py", "app/data/models.py", "app/data/validation.py",
    "app/data/memory_provider.py", "app/utils/market_calendar.py",
)
# Adaptations needed to run the live configuration on historical candles.
# They are applied at run time, recorded in every report, and are NOT part of
# the strategy fingerprint.
HISTORICAL_ADAPTATIONS = {
    "screening.require_spread": (False, "historical candles carry no bid/ask quotes; the spread "
                                        "screen is reported as unavailable and costs use the "
                                        "assumed spread"),
    "cost_review.apply_current_rates_to_past_dates": (
        True, "the broker tariff is verified only from its retrieval date; current rates are "
              "applied to earlier sessions and labelled retroactive"),
}
_MARK = "<PROJECT_ROOT>"


class BaselineError(ValueError):
    pass


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _file_sha(path: Path | None) -> str | None:
    return _sha_bytes(Path(path).read_bytes()) if path and Path(path).exists() else None


def fee_schedule_file(settings: Settings) -> Path:
    return settings.broker.fee_schedule_file or schedule_path(settings.broker.name)


def strategy_fingerprint(settings: Settings) -> tuple[str, dict]:
    """(sha256, payload) over decision-relevant settings, portable across
    machines (no absolute paths; fee and holiday files by content)."""
    data = settings.model_dump(mode="json", include=set(FINGERPRINT_SECTIONS))
    for section, key in _FINGERPRINT_DROP:
        data[section].pop(key, None)
    data["_fee_schedule_sha256"] = _file_sha(fee_schedule_file(settings))
    data["_holidays_sha256"] = _file_sha(settings.market.holidays_file)
    return _sha_bytes(_canonical(data)), data


def code_hash(root: Path = PROJECT_ROOT) -> tuple[str, list[str]]:
    files = sorted({p for pattern in CODE_FILES for p in root.glob(pattern) if p.is_file()})
    digest = hashlib.sha256()
    names = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        names.append(rel)
        digest.update(rel.encode() + b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest(), names


def _relativize(obj: Any) -> Any:
    root = str(PROJECT_ROOT)
    if isinstance(obj, dict):
        return {k: _relativize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relativize(v) for v in obj]
    if isinstance(obj, str) and (obj == root or obj.startswith(root + os.sep)):
        return _MARK + obj[len(root):]
    return obj


def _absolutize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _absolutize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_absolutize(v) for v in obj]
    if isinstance(obj, str) and obj.startswith(_MARK):
        return str(PROJECT_ROOT) + obj[len(_MARK):]
    return obj


def _fee_info(settings: Settings) -> dict:
    schedule = load_fee_schedule(settings)
    sources = [{"item": "brokerage", "effective_from": schedule.brokerage.effective_from,
                **schedule.brokerage.source.model_dump(mode="json")},
               {"item": "gst", "effective_from": schedule.gst.effective_from,
                **schedule.gst.source.model_dump(mode="json")}]
    sources += [{"item": c.kind.value, "effective_from": c.effective_from,
                 **c.source.model_dump(mode="json")} for c in schedule.charges]
    return {"schedule_id": schedule.schedule_id, "broker": schedule.broker,
            "version": schedule.version, "verified_on": schedule.verified_on,
            "illustrative": schedule.illustrative, "sources": sources}


def build_frozen(name: str, settings: Settings, protocol_path: Path,
                 created_at: datetime) -> dict:
    _, protocol_text, protocol_sha = load_protocol(protocol_path)
    fingerprint, _ = strategy_fingerprint(settings)
    code, files = code_hash()
    body = {
        "format": FORMAT,
        "name": name,
        "created_at": created_at.isoformat(),
        "strategy_version": STRATEGY_VERSION,
        "execution_model_version": execution_model_label(
            settings.backtest.target_trade_through_pct),
        "strategy_fingerprint": fingerprint,
        "config_version": settings.version(),  # machine-specific; for cross-reference only
        "code_hash": code,
        "code_files": files,
        "fee_schedule": json.loads(_canonical(_fee_info(settings))),
        "historical_adaptations": {k: {"value": v, "reason": why}
                                   for k, (v, why) in HISTORICAL_ADAPTATIONS.items()},
        "protocol_sha256": protocol_sha,
        "protocol_text": protocol_text,
        "settings": _relativize(settings.model_dump(mode="json", exclude=SECRETS)),
    }
    body["frozen_sha256"] = _sha_bytes(_canonical(body))
    return body


def freeze(name: str, settings: Settings, protocol_path: Path, out_dir: Path,
           created_at: datetime) -> Path:
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise BaselineError("baseline name may contain letters, digits, '-', '_' and '.' only")
    path = Path(out_dir) / f"{name}.json"
    if path.exists():
        raise BaselineError(f"{path} already exists. A frozen baseline is never overwritten; "
                            "choose a new name for a new strategy version.")
    body = build_frozen(name, settings, protocol_path, created_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # read-only
    return path


@dataclass(frozen=True)
class FrozenBaseline:
    path: Path
    body: dict
    settings: Settings  # exactly the frozen settings (live configuration)
    protocol: Protocol
    code_drift: bool
    current_code_hash: str

    @property
    def name(self) -> str:
        return self.body["name"]

    @property
    def fingerprint(self) -> str:
        return self.body["strategy_fingerprint"]

    def historical_settings(self) -> Settings:
        """Frozen settings plus the declared historical-data adaptations."""
        data = self.settings.model_dump()
        for dotted, spec in self.body["historical_adaptations"].items():
            section, key = dotted.split(".")
            data[section][key] = spec["value"]
        return settings_from_values(data)


def load_frozen(path: Path) -> FrozenBaseline:
    path = Path(path)
    if not path.exists():
        raise BaselineError(f"no frozen baseline at {path}; create one with "
                            "`python -m app.robustness freeze --name <name>`")
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("format") != FORMAT:
        raise BaselineError(f"{path} is not a frozen baseline ({FORMAT})")
    claimed = body.get("frozen_sha256")
    unsigned = {k: v for k, v in body.items() if k != "frozen_sha256"}
    if claimed != _sha_bytes(_canonical(unsigned)):
        raise BaselineError(f"{path} was modified after freezing (self-hash mismatch)")
    settings = settings_from_values(_absolutize(body["settings"]))
    fingerprint, _ = strategy_fingerprint(settings)
    if fingerprint != body["strategy_fingerprint"]:
        raise BaselineError(
            "frozen settings no longer reproduce the recorded strategy fingerprint. The fee "
            "schedule or holiday file has changed since freezing (they are part of the "
            "strategy version). Freeze a new baseline.")
    protocol, _, protocol_sha = protocol_from_text(body["protocol_text"])
    if protocol_sha != body["protocol_sha256"]:
        raise BaselineError("embedded protocol does not match its recorded hash")
    current, _ = code_hash()
    return FrozenBaseline(path, body, settings, protocol, current != body["code_hash"], current)
