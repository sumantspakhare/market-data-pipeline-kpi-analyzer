# Market Data Pipeline & KPI Analyzer

A small Python CLI for downloading daily stock OHLCV data from Yahoo Finance, caching it in SQLite, and calculating 1-month, 6-month and 1-year performance KPIs.

## Features

- Downloads at least two years of daily history by default for `AAPL`, `MSFT`, `NVDA`, and `SPY`.
- Stores adjusted daily OHLCV values in SQLite.
- Uses `(ticker, date)` as a primary key and SQLite UPSERT, so repeated ingestion does not create duplicate rows.
- Calculates total return, annualized volatility, Sharpe ratio, and maximum drawdown for 1M, 6M and 1Y.
- Configurable through a `.env` file and CLI overrides.

## Setup

Python 3.10+ is recommended.

### 1. Create and activate a virtual environment

**Windows PowerShell**
```powershell
py -m venv .venv
.venv\\Scripts\\Activate.ps1
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

Copy `.env.example` to `.env` and adjust the values if needed.

**Windows**
```powershell
Copy-Item .env.example .env
```

**macOS / Linux**
```bash
cp .env.example .env
```

Example:
```env
DB_PATH=market.db
TICKERS=AAPL,MSFT,NVDA,SPY
HISTORY_PERIOD=2y
```

`DB_PATH` accepts either `market.db` or `sqlite:///market.db`.

## Usage

Run commands from the project root.

### Download and store market data

```bash
python src/market_analyzer.py ingest
```

### Calculate KPIs from the local database

```bash
python src/market_analyzer.py kpis
```

### Run the complete pipeline

```bash
python src/market_analyzer.py run
```

### Override configuration from the command line

```bash
python src/market_analyzer.py --tickers AAPL,TSLA --period 5y run
python src/market_analyzer.py --db data/market.db kpis
```

Use `--verbose` before the subcommand for detailed logs:
```bash
python src/market_analyzer.py --verbose ingest
```

## KPI definitions and assumptions

The latest available stored date for each ticker is the end date. Each timeframe's cutoff is calculated by subtracting one, six, or twelve calendar months from that date. The most recent close on or before the cutoff is used as the starting price, followed by all later closes through the end date. This avoids incorrectly using the first trading day after the cutoff as the base.

| KPI | Formula / assumption |
|---|---|
| Total Return | `(last close / starting close - 1) × 100` |
| Annualized Volatility | Sample standard deviation of daily close-to-close returns × `sqrt(252)` × 100 |
| Sharpe Ratio | `(mean daily return - daily risk-free rate) / sample daily standard deviation × sqrt(252)` |
| Maximum Drawdown | Minimum of `(close / running peak close - 1)` over the selected period × 100 |

The annual risk-free rate is 2%, converted to an effective daily rate as `(1.02)^(1/252) - 1`. Annualization assumes 252 trading days. A Sharpe ratio is reported as `N/A` when return variability is zero or cannot be estimated. Volatility also requires at least two daily returns.

**Price adjustment:** yfinance is called with `auto_adjust=True`, so OHLC values (including `Close`) are adjusted for splits and distributions. The resulting return series is therefore a total-return-oriented adjusted-price series, rather than raw unadjusted closing prices.

**Data availability:** Yahoo Finance data availability, symbol coverage, and rate limits are controlled by Yahoo/yfinance. The program logs per-ticker failures and continues with other tickers. The `2y` setting requests two years of daily history; it does not guarantee every calendar day has a trading observation.

## Database schema

```sql
CREATE TABLE daily_prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume INTEGER,
    PRIMARY KEY (ticker, date)
);
```

The primary key plus `INSERT ... ON CONFLICT DO UPDATE` makes ingestion idempotent.

## Project structure

```text
market_data_kpi_analyzer/
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── src/
│   └── market_analyzer.py
└── tests/
    └── test_kpis.py
```

## Testing

Install pytest if it is not already available:
```bash
pip install pytest
pytest -q
```

Tests cover KPI calculations and SQLite upsert behavior without requiring a network connection.
