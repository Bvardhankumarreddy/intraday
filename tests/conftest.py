from datetime import date

import pytest

from app.config import MarketConfig
from app.utils.market_calendar import MarketCalendar

# A test-only holiday. Not a claim about the real NSE calendar.
TEST_HOLIDAY = date(2025, 3, 14)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Keep the developer's NSE_* env vars and .env out of tests."""
    import os

    for key in list(os.environ):
        if key.startswith("NSE_"):
            monkeypatch.delenv(key)
    # Never read the developer's real .env (credentials, provider) in tests.
    monkeypatch.setenv("NSE_ENV_FILE", str(tmp_path / "no.env"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture(scope="session", autouse=True)
def _project_data_untouched():
    """Fail the run if any test writes into the project's real data/ folder."""
    from app.config import PROJECT_ROOT

    root = PROJECT_ROOT / "data"

    def listing() -> set[str]:
        # data/logs is written by the scheduled jobs, not by tests.
        return {str(p) for p in root.rglob("*") if "logs" not in p.relative_to(root).parts} \
            if root.exists() else set()

    before = listing()
    yield
    added = sorted(listing() - before)
    assert not added, f"tests wrote into {root}: {added[:5]}"


@pytest.fixture
def calendar() -> MarketCalendar:
    return MarketCalendar(MarketConfig(), holidays={TEST_HOLIDAY: "Test holiday"})
