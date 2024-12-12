import yfinance as yf
from pathlib import Path

# Fetch BTC-GBP data
btc = yf.download('BTC-GBP', start='2014-09-17', end=None).reset_index()

cwd = Path(__file__).resolve().parent

# Save only the necessary columns
btc[['Date', 'Close']].to_csv(cwd/'bitcoin_historical.csv', index=True)
