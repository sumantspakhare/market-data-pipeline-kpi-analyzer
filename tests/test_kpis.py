import sqlite3

import pandas as pd
import pytest

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.market_analyzer import calculate_kpis, connect_db

def test_calculate_kpis_flat_series_has_zero_return_and_volatility():
    dates = pd.bdate_range("2025-01-01", periods=270)
    prices = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "close": [100.0] * len(dates)})
    result = calculate_kpis(prices, "TEST", "1Y", 12)
    assert result.total_return_pct == pytest.approx(0.0)
    assert result.annualized_volatility_pct == pytest.approx(0.0)
    assert result.sharpe_ratio is None
    assert result.maximum_drawdown_pct == pytest.approx(0.0)


def test_calculate_kpis_positive_trend():
    dates = pd.bdate_range("2025-01-01", periods=270)
    values = [100.0 + i for i in range(len(dates))]
    prices = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "close": values})
    result = calculate_kpis(prices, "TEST", "1Y", 12)
    assert result.total_return_pct > 0
    assert result.annualized_volatility_pct >= 0
    assert result.maximum_drawdown_pct == pytest.approx(0.0)


def test_database_primary_key_prevents_duplicate_dates(tmp_path):
    db = str(tmp_path / "test.db")
    with connect_db(db) as conn:
        conn.execute(
            "INSERT INTO daily_prices (ticker,date,close) VALUES (?,?,?)",
            ("TEST", "2025-01-02", 100.0),
        )
        conn.execute("""
            INSERT INTO daily_prices (ticker,date,close) VALUES (?,?,?)
            ON CONFLICT(ticker,date) DO UPDATE SET close=excluded.close
        """, ("TEST", "2025-01-02", 101.0))
        count = conn.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0]
        close = conn.execute("SELECT close FROM daily_prices").fetchone()[0]
    assert count == 1
    assert close == 101.0
