from flask import Flask, render_template, jsonify
import csv
from datetime import datetime, timedelta
from pathlib import Path
import time

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/bitcoin-price')
def bitcoin_price():
    return render_template('bitcoin-price.html')

@app.route('/api/bitcoin-historical/<range>')
def get_historical_data(range):
    print(f"Received request for range: {range}")
    
    try:
        # Load the CSV file
        csv_path = Path(__file__).parent / 'data' / 'bitcoin_historical.csv'
        
        # Convert range to days
        days_map = {
            '1M': 30,
            '3M': 90,
            '6M': 180,
            '1Y': 365,
            'ALL': None
        }
        
        days = days_map.get(range)
        cutoff_date = None
        if days:
            cutoff_date = datetime.now() - timedelta(days=days)
        
        prices = []
        with open(csv_path, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                date = datetime.strptime(row['Date'], '%Y-%m-%d')
                if cutoff_date and date < cutoff_date:
                    continue
                # Convert date to timestamp in milliseconds
                timestamp = int(time.mktime(date.timetuple()) * 1000)
                price = float(row['Close'])
                prices.append([timestamp, price])
        
        return jsonify({'prices': prices})
            
    except Exception as e:
        print(f"Error in handler: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run()