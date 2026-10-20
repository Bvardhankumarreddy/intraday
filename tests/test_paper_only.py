"""Paper-only execution boundary (Phase 8). All market data here is synthetic
and the Groww SDK is a fake; no real provider or account is contacted.

These tests establish, for this codebase:
- only ``app/data/groww_provider.py`` imports the broker SDK;
- the SDK object is reachable only through a market-data allowlist proxy;
  order/account methods raise ``BlockedSdkCall``;
- a complete paper session driven through the real Groww adapter code
  (against a fake SDK that records order calls) produces a paper trade and
  ZERO order-endpoint calls;
- no execution mode other than "paper" can be configured;
- the paper engine has no parameter through which an order client could be
  injected.
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings, settings_from_values
from app.data.groww_provider import (SDK_ALLOWLIST, BlockedSdkCall, GrowwMarketDataProvider,
                                     GrowwSdkClient, data_only_sdk)
from app.data.models import Instrument
from app.paper.engine import PaperEngine
from app.paper.scheduler import step_times
from app.paper.store import PaperStore
from tests.backtest_helpers import make_dataset, scenario
from tests.ops_helpers import ORDER_METHODS, fake_growwapi_module
from tests.paper_helpers import NEUTRAL, TARGET, paper_settings

APP = Path(__file__).resolve().parent.parent / "app"
DAY = date(2025, 3, 12)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_only_the_provider_imports_the_broker_sdk():
    offenders = [str(p.relative_to(APP)) for p in APP.rglob("*.py")
                 if any(m == "growwapi" or m.startswith("growwapi.") for m in _imports(p))]
    assert offenders == ["data/groww_provider.py"]


def test_decision_and_paper_code_never_import_the_broker_adapter():
    allowed = {"data/factory.py", "data/groww_provider.py"}
    offenders = [str(p.relative_to(APP)) for p in APP.rglob("*.py")
                 if "app.data.groww_provider" in _imports(p)
                 and str(p.relative_to(APP)) not in allowed]
    assert offenders == []


def test_no_order_method_names_called_anywhere_in_app():
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ORDER_METHODS:
                pytest.fail(f"{path}: attribute access .{node.attr}")


def test_sdk_proxy_blocks_order_and_account_methods():
    module, record = fake_growwapi_module(dataset=None)
    proxy = data_only_sdk(module.GrowwAPI("t"))
    for name in ORDER_METHODS:
        with pytest.raises(BlockedSdkCall):
            getattr(proxy, name)
    assert record["order_calls"] == []
    assert SDK_ALLOWLIST == {"get_quote", "get_historical_candle_data", "EXCHANGE_NSE",
                             "SEGMENT_CASH"}
    assert repr(proxy) == "<DataOnlyGrowwAPI>" and type(proxy).__slots__ == ()


def test_sdk_client_holds_no_raw_sdk_object(monkeypatch):
    module, _ = fake_growwapi_module(dataset=None)
    monkeypatch.setitem(sys.modules, "growwapi", module)
    client = GrowwSdkClient("token", api_secret="not-a-real-secret")
    def exposes(value, name) -> bool:
        try:
            return hasattr(value, name)  # proxy raises BlockedSdkCall instead of returning
        except BlockedSdkCall:
            return False

    for value in vars(client).values():
        for name in ORDER_METHODS:
            if exposes(type(value), name) or exposes(value, name):
                pytest.fail(f"{type(value).__name__} exposes {name}")
    with pytest.raises(BlockedSdkCall):
        client._api.place_order(trading_symbol="X")


def test_full_paper_session_through_groww_adapter_calls_no_order_endpoint(
        calendar, tmp_path, monkeypatch):
    s = paper_settings(tmp_path)
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])})
    module, record = fake_growwapi_module(ds)
    monkeypatch.setitem(sys.modules, "growwapi", module)
    client = GrowwSdkClient("token", api_secret="not-a-real-secret", timeout=7)
    provider = GrowwMarketDataProvider(client, calendar, [Instrument("SYNTH", "Synth Ltd")], s,
                                       sleep=lambda _: None)
    store = PaperStore(tmp_path / "paper.db")
    engine = PaperEngine(provider, s, calendar, store)
    open_dt, _ = calendar.session_bounds(DAY)
    engine.open_session(DAY, open_dt - timedelta(minutes=15))
    for t in step_times(calendar, DAY, engine.step_td, timedelta(seconds=1)):
        engine.step(t)
    closed = store.positions(DAY)
    assert len(closed) == 1 and closed[0]["state"] == "CLOSED"
    assert closed[0]["net_pnl"] == "15.69"  # identical to the Phase 4/5 reference
    assert record["order_calls"] == []      # no order/account endpoint was reached
    assert record["timeouts"] and set(record["timeouts"]) == {7}  # every call had a timeout


def test_execution_mode_cannot_be_live(monkeypatch):
    assert Settings().execution_mode == "paper"
    with pytest.raises(ValidationError):
        Settings(execution_mode="live")
    monkeypatch.setenv("NSE_EXECUTION_MODE", "live")
    with pytest.raises(ValidationError):
        load_settings()


def test_engine_rejects_non_paper_mode_and_takes_no_order_client(calendar, tmp_path):
    params = set(inspect.signature(PaperEngine.__init__).parameters)
    assert params == {"self", "provider", "settings", "calendar", "store", "risk_engine",
                      "recorder"}
    s = settings_from_values(paper_settings(tmp_path).model_dump())
    bad = s.model_copy(update={"execution_mode": "live"})  # bypasses validation on purpose
    with pytest.raises(ValueError, match="unsupported execution mode"):
        PaperEngine(None, bad, calendar, PaperStore(":memory:"))
