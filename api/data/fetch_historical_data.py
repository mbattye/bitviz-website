import requests
from pathlib import Path
from datetime import datetime, timedelta
import time
import pandas as pd

def fetch_historical_data():
    # CoinGecko API endpoint for Bitcoin historical data
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    
    # Parameters for the API request - get last year of data
    params = {
        'vs_currency': 'gbp',
        'days': '365',  # Get last year of data
        'interval': 'daily'
    }
    
    # Add headers to mimic a browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        # Read existing CSV data
        cwd = Path(__file__).resolve().parent
        csv_path = cwd / 'bitcoin_historical.csv'
        
        if csv_path.exists():
            df_historical = pd.read_csv(csv_path)
            df_historical['Date'] = pd.to_datetime(df_historical['Date'])
            df_historical = df_historical.sort_values('Date')
            last_historical_date = df_historical['Date'].max()
            print(f"Found existing data up to: {last_historical_date}")
        else:
            df_historical = pd.DataFrame(columns=['Date', 'Close'])
            last_historical_date = datetime(2014, 9, 17)  # Bitcoin's early history
            print("No existing data found, starting from:", last_historical_date)
        
        # Add a small delay to respect rate limits
        time.sleep(1)
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        # Process the API data
        api_prices = []
        for timestamp, price in data['prices']:
            date = datetime.fromtimestamp(timestamp / 1000)
            api_prices.append([date, price])
        
        # Convert API data to DataFrame
        df_recent = pd.DataFrame(api_prices, columns=['Date', 'Close'])
        df_recent['Date'] = pd.to_datetime(df_recent['Date'])
        df_recent = df_recent.sort_values('Date')
        
        print(f"Fetched recent data from {df_recent['Date'].min()} to {df_recent['Date'].max()}")
        
        # Merge historical and recent data
        # Keep historical data up to the last historical date
        df_historical = df_historical[df_historical['Date'] < last_historical_date]
        
        # Add recent data
        df_combined = pd.concat([df_historical, df_recent])
        
        # Remove duplicates and sort
        df_combined = df_combined.drop_duplicates(subset=['Date'], keep='last')
        df_combined = df_combined.sort_values('Date')
        
        # Check for gaps in the data
        date_range = pd.date_range(start=df_combined['Date'].min(), end=df_combined['Date'].max(), freq='D')
        missing_dates = date_range.difference(df_combined['Date'])
        
        if not missing_dates.empty:
            print(f"Found {len(missing_dates)} missing dates")
            # Forward fill missing values
            df_combined = df_combined.set_index('Date').reindex(date_range).fillna(method='ffill').reset_index()
            df_combined = df_combined.rename(columns={'index': 'Date'})
        
        # Format dates to YYYY-MM-DD
        df_combined['Date'] = df_combined['Date'].dt.strftime('%Y-%m-%d')
        
        # Save the combined data without index
        df_combined.to_csv(csv_path, index=False)
        print(f"Historical data updated successfully. Data range: {df_combined['Date'].min()} to {df_combined['Date'].max()}")
        print(f"Total data points: {len(df_combined)}")
        
    except Exception as e:
        print(f"Error fetching data: {e}")

if __name__ == "__main__":
    fetch_historical_data()
