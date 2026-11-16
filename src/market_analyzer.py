"""Market data ingestion and multi-timeframe KPI analysis CLI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from dotenv import load_dotenv

LOG = logging.getLogger("market_analyzer")
TIMEFRAMES = {"1M": 1, "6M": 6, "1Y": 12}
RISK_FREE_RATE = 0.02
TRADING_DAYS = 252


@dataclass
class KPIResult:
    ticker: str
    timeframe: str
    start_date: str
    end_date: str
    observations: int
    total_return_pct: float | None
    annualized_volatility_pct: float | None
    sharpe_ratio: float | None
    maximum_drawdown_pct: float | None


def load_config() -> tuple[str, list[str], str]:
    """Load settings from .env, with sensible defaults."""
    load_dotenv()
    db_path = os.getenv("DB_PATH", "market.db").strip()
    tickers_raw = os.getenv("TICKERS", "AAPL,MSFT,NVDA,SPY")
    period = os.getenv("HISTORY_PERIOD", "2y").strip() or "2y"
    tickers = [t.strip().upper() for t in tickers_raw.split(",") if t.strip()]
    if not tickers:
        raise ValueError("TICKERS must contain at least one ticker.")
    return db_path, tickers, period


def sqlite_file_path(db_path: str) -> str:
    """Accept both market.db and SQLAlchemy-style sqlite:///market.db."""
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///"):]
    elif db_path.startswith("sqlite://"):
        raise ValueError("Use DB_PATH=market.db or sqlite:///market.db")
    if db_path == ":memory:":
        return db_path
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_file_path(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_prices (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            volume INTEGER,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(date)")
    conn.commit()
    return conn


def normalize_yfinance_frame(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize a yfinance result to the columns stored in SQLite."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    # yfinance may return a MultiIndex even for a single ticker.
    if isinstance(frame.columns, pd.MultiIndex):
        if ticker in frame.columns.get_level_values(-1):
            frame = frame.xs(ticker, axis=1, level=-1)
        else:
            frame.columns = frame.columns.get_level_values(0)

    frame = frame.rename(columns={c: str(c).lower().replace(" ", "_") for c in frame.columns})
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"Downloaded data for {ticker} is missing columns: {missing}")

    out = frame[required].copy().dropna(subset=["close"])
    out.index = pd.to_datetime(out.index).tz_localize(None)
    out["date"] = out.index.strftime("%Y-%m-%d")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0).astype("int64")
    for col in ["open", "high", "low", "close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"])
    return out.reset_index(drop=True)[["date", "open", "high", "low", "close", "volume"]]


def ingest_ticker(conn: sqlite3.Connection, ticker: str, period: str = "2y") -> int:
    """Download and upsert one ticker; returns number of rows received."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Run: pip install -r requirements.txt") from exc

    LOG.info("Downloading %s (%s of daily history)", ticker, period)
    raw = yf.download(
        ticker,
        period=period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    data = normalize_yfinance_frame(raw, ticker)
    if data.empty:
        LOG.warning("No data returned for %s", ticker)
        return 0

    rows = [
        (ticker, r.date, float(r.open) if pd.notna(r.open) else None,
         float(r.high) if pd.notna(r.high) else None,
         float(r.low) if pd.notna(r.low) else None,
         float(r.close), int(r.volume) if pd.notna(r.volume) else None)
        for r in data.itertuples(index=False)
    ]
    conn.executemany("""
        INSERT INTO daily_prices (ticker, date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume
    """, rows)
    conn.commit()
    LOG.info("%s: %d rows upserted", ticker, len(rows))
    return len(rows)


def ingest(db_path: str, tickers: Iterable[str], period: str = "2y") -> dict[str, int]:
    results: dict[str, int] = {}
    with connect_db(db_path) as conn:
        for ticker in tickers:
            try:
                results[ticker] = ingest_ticker(conn, ticker, period)
            except Exception:
                LOG.exception("Failed to ingest %s", ticker)
                results[ticker] = 0
    return results


def calculate_kpis(prices: pd.DataFrame, ticker: str, timeframe: str,
                   months: int, risk_free_rate: float = RISK_FREE_RATE) -> KPIResult:
    """Calculate KPIs using the last close on/before the cutoff as the base price."""
    if prices.empty:
        return KPIResult(ticker, timeframe, "", "", 0, None, None, None, None)

    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date", keep="last")
    end_date = df["date"].max()
    cutoff = end_date - pd.DateOffset(months=months)

    # Include one observation at/before the cutoff to calculate the period return.
    before = df[df["date"] <= cutoff].tail(1)
    after = df[df["date"] > cutoff]
    window = pd.concat([before, after]).drop_duplicates("date").sort_values("date")
    if len(window) < 2:
        return KPIResult(ticker, timeframe, cutoff.date().isoformat(),
                         end_date.date().isoformat(), len(window), None, None, None, None)

    close = window["close"].astype(float)
    daily_returns = close.pct_change().dropna()
    total_return = (close.iloc[-1] / close.iloc[0]) - 1.0
    volatility = (daily_returns.std(ddof=1) * np.sqrt(TRADING_DAYS)
                  if len(daily_returns) >= 2 else np.nan)
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / TRADING_DAYS) - 1.0
    daily_std = daily_returns.std(ddof=1)
    sharpe = (((daily_returns.mean() - daily_rf) / daily_std) * np.sqrt(TRADING_DAYS)
              if pd.notna(daily_std) and daily_std > 0 else np.nan)
    running_peak = close.cummax()
    drawdown = (close / running_peak) - 1.0
    max_drawdown = drawdown.min()

    def clean(value):
        return float(value) if pd.notna(value) and np.isfinite(value) else None

    return KPIResult(
        ticker=ticker,
        timeframe=timeframe,
        start_date=window["date"].iloc[0].date().isoformat(),
        end_date=end_date.date().isoformat(),
        observations=len(window),
        total_return_pct=clean(total_return * 100),
        annualized_volatility_pct=clean(volatility * 100),
        sharpe_ratio=clean(sharpe),
        maximum_drawdown_pct=clean(max_drawdown * 100),
    )


def calculate_all(db_path: str, tickers: Iterable[str]) -> list[KPIResult]:
    results: list[KPIResult] = []
    with connect_db(db_path) as conn:
        for ticker in tickers:
            prices = pd.read_sql_query(
                "SELECT date, close FROM daily_prices WHERE ticker = ? ORDER BY date",
                conn, params=(ticker,),
            )
            if prices.empty:
                LOG.warning("No stored data for %s; run the ingest command first.", ticker)
                continue
            for timeframe, months in TIMEFRAMES.items():
                results.append(calculate_kpis(prices, ticker, timeframe, months))
    return results


def print_results(results: list[KPIResult]) -> None:
    if not results:
        print("No KPI results. Ingest market data first.")
        return
    headers = ["Ticker", "Period", "Start", "End", "Return %", "Volatility %", "Sharpe", "Max DD %"]
    rows = []
    for r in results:
        fmt = lambda x: "N/A" if x is None else f"{x:.2f}"
        rows.append([r.ticker, r.timeframe, r.start_date, r.end_date,
                     fmt(r.total_return_pct), fmt(r.annualized_volatility_pct),
                     fmt(r.sharpe_ratio), fmt(r.maximum_drawdown_pct)])
    widths = [max(len(str(row[i])) for row in [headers] + rows) for i in range(len(headers))]
    print(" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(" | ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download daily market data to SQLite and calculate investment KPIs."
    )
    parser.add_argument("--db", help="Override DB_PATH from .env")
    parser.add_argument("--tickers", help="Comma-separated ticker override")
    parser.add_argument("--period", help="yfinance history period (default from .env or 2y)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="Download/upsert historical daily prices")
    sub.add_parser("kpis", help="Calculate 1M, 6M and 1Y KPIs from SQLite")
    sub.add_parser("run", help="Run ingestion followed by KPI calculation")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        env_db, env_tickers, env_period = load_config()
        db_path = args.db or env_db
        tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
                   if args.tickers else env_tickers)
        period = args.period or env_period

        if args.command in ("ingest", "run"):
            results = ingest(db_path, tickers, period)
            for ticker, count in results.items():
                print(f"{ticker}: {count} rows upserted")
            if args.command == "ingest":
                return 0 if any(results.values()) else 1

        if args.command in ("kpis", "run"):
            results = calculate_all(db_path, tickers)
            print_results(results)
            if args.command == "kpis" and not results:
                return 1
        return 0
    except Exception as exc:
        LOG.exception("Application failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
