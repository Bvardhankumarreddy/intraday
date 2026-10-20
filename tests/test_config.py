import json
from datetime import time

import pytest
from pydantic import ValidationError

from app.config import DataProviderName, Settings, load_settings


def test_defaults_match_spec():
    s = Settings()
    assert s.capital.initial_capital == 500
    assert (s.screening.min_price, s.screening.max_price) == (50, 100)
    assert s.screening.min_avg_traded_value == 5_000_000
    assert s.screening.max_spread_pct == 0.10
    assert s.strategy.opening_range_minutes == 15
    assert s.strategy.relative_volume_threshold == 1.5
    assert s.strategy.min_reward_risk == 2.0
    assert s.risk.risk_pct == 0.5
    assert s.broker.name == "groww"
    assert s.data.provider is DataProviderName.MOCK


def test_yaml_then_env_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("capital:\n  initial_capital: 1000\nrisk:\n  risk_pct: 1.0\n")
    monkeypatch.setenv("NSE_RISK__RISK_PCT", "0.25")
    s = load_settings(cfg)
    assert s.capital.initial_capital == 1000  # from YAML
    assert s.risk.risk_pct == 0.25  # env overrides YAML
    assert s.risk.max_trades_per_day == 2  # default kept


def test_missing_explicit_config_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "values",
    [
        {"capital": {"initial_capital": 0}},
        {"capital": {"initial_capital": -500}},
        {"risk": {"risk_pct": 0}},
        {"risk": {"risk_pct": 50}},
        {"screening": {"min_price": 100, "max_price": 50}},
        {"strategy": {"eod_exit_time": "16:00"}},
        {"market": {"session_open": "15:30", "session_close": "09:15"}},
    ],
)
def test_invalid_values_rejected(values):
    with pytest.raises(ValidationError):
        Settings(**values)


def test_secrets_not_allowed_in_yaml(tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("broker_api_key: abc\n")
    with pytest.raises(ValueError, match="environment"):
        load_settings(cfg)


def test_secret_is_masked(monkeypatch):
    monkeypatch.setenv("NSE_BROKER_API_KEY", "super-secret-value")
    s = Settings()
    assert "super-secret-value" not in repr(s)
    assert s.broker_api_key.get_secret_value() == "super-secret-value"


def test_example_settings_file_loads():
    from app.config import PROJECT_ROOT

    s = load_settings(PROJECT_ROOT / "config" / "settings.example.yaml")
    assert s.strategy.eod_exit_time == time(15, 10)


def test_version_is_portable_across_install_locations(monkeypatch, tmp_path):
    """Phase 9 fix: relocating the whole project must not change the version."""
    import app.config as config
    from app.config import PROJECT_ROOT, Settings, settings_from_values

    here = Settings()
    original = here.version()
    moved_root = tmp_path / "srv" / "nse-intraday-agent"
    data = json.loads(json.dumps(here.model_dump(mode="json")).replace(str(PROJECT_ROOT),
                                                                        str(moved_root)))
    moved = settings_from_values(data)
    monkeypatch.setattr(config, "PROJECT_ROOT", moved_root)
    assert moved.version() == original
    changed = settings_from_values({**data, "risk": {**data["risk"], "max_daily_loss": 10}})
    assert changed.version() != original  # real configuration changes still count
