from flask import Flask, render_template, jsonify
import csv
from datetime import datetime, timedelta
from pathlib import Path
import time
import json
import requests
import math
from typing import Optional

app = Flask(__name__)

def _read_prices_from_csv(csv_path: Path):
    closes = []
    dates = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dates.append(datetime.strptime(row['Date'], '%Y-%m-%d'))
                closes.append(float(row['Close']))
            except Exception:
                continue
    return dates, closes

def _get_spot_price_gbp_cached(cache_dir: Path):
    cache_file = cache_dir / 'spot_gbp_cache.json'
    now = datetime.utcnow()
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            ts = datetime.fromisoformat(cached.get('fetched_at'))
            if (now - ts) < timedelta(minutes=5) and 'gbp' in cached:
                return float(cached['gbp'])
        except Exception:
            pass
    try:
        resp = requests.get('https://api.coingecko.com/api/v3/simple/price', params={'ids':'bitcoin','vs_currencies':'gbp'}, timeout=15)
        resp.raise_for_status()
        spot = float(resp.json()['bitcoin']['gbp'])
        cache_file.write_text(json.dumps({'fetched_at': now.isoformat(), 'gbp': spot}))
        return spot
    except Exception:
        return None

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/bitcoin-price')
def bitcoin_price():
    return render_template('bitcoin-price.html')

@app.route('/bitcoin-metrics')
def bitcoin_metrics():
    return render_template('bitcoin-metrics.html')

@app.route('/api/nodes-latest')
def nodes_latest():
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / 'nodes_latest_cache.json'
        now = datetime.utcnow()

        # Serve cache if fresh (<24h)
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    cached = json.load(f)
                fetched_at = datetime.fromisoformat(cached.get('fetched_at'))
                if (now - fetched_at) < timedelta(hours=24) and 'data' in cached:
                    return jsonify(cached['data'])
            except Exception:
                pass

        # Fetch from Bitnodes
        resp = requests.get('https://bitnodes.io/api/v1/snapshots/latest/', timeout=20)
        resp.raise_for_status()
        data = resp.json()

        # Write cache
        with open(cache_file, 'w') as f:
            json.dump({'fetched_at': now.isoformat(), 'data': data}, f)

        return jsonify(data)
    except Exception as e:
        # On failure, try stale cache
        cache_file = Path(__file__).parent / 'data' / 'nodes_latest_cache.json'
        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    cached = json.load(f)
                if 'data' in cached:
                    return jsonify(cached['data'])
            except Exception:
                pass
        # Fallback: return empty structure so UI can render without error
        return jsonify({'nodes': {}}), 200

@app.route('/api/market-structure')
def market_structure():
    try:
        data_dir = Path(__file__).parent / 'data'
        csv_path = data_dir / 'bitcoin_historical.csv'
        if not csv_path.exists():
            return jsonify({'error':'historical csv not found'}), 404

        # Output cache (5 minutes)
        cache_file = data_dir / 'market_structure_cache.json'
        now = datetime.utcnow()
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                ts = datetime.fromisoformat(cached.get('fetched_at'))
                if (now - ts) < timedelta(minutes=5) and 'metrics' in cached:
                    return jsonify(cached['metrics'])
            except Exception:
                pass

        dates, closes = _read_prices_from_csv(csv_path)
        if len(closes) < 210:
            return jsonify({'error':'not enough data'}), 400

        # Sort by date just in case
        combined = sorted(zip(dates, closes), key=lambda x: x[0])
        dates = [d for d,_ in combined]
        closes = [c for _,c in combined]

        last_close = closes[-1]

        # Spot price (GBP), fallback to last close
        spot = _get_spot_price_gbp_cached(data_dir) or last_close

        # Daily returns
        rets = []
        for i in range(1, len(closes)):
            try:
                rets.append((closes[i]/closes[i-1]) - 1.0)
            except ZeroDivisionError:
                rets.append(0.0)

        # Volatility (annualized) over last 30/90 trading days
        def ann_vol(window):
            if len(rets) < window:
                return None
            window_rets = rets[-window:]
            mean = sum(window_rets)/len(window_rets)
            var = sum((r-mean)**2 for r in window_rets)/(len(window_rets)-1)
            std = math.sqrt(var)
            return std * math.sqrt(365) * 100.0

        vol_30 = ann_vol(30)
        vol_90 = ann_vol(90)

        # ATH drawdown
        ath = max(closes)
        drawdown_pct = ((spot - ath)/ath) * 100.0

        # Percent of days above current spot
        days_above = sum(1 for c in closes if c > spot)
        pct_days_above = (days_above/len(closes)) * 100.0

        # 200D SMA and Mayer Multiple
        sma200 = sum(closes[-200:]) / 200.0
        mayer = spot / sma200
        sma_dist_pct = ((spot - sma200)/sma200) * 100.0

        metrics = {
            'spot_gbp': round(spot, 2),
            'ath_gbp': round(ath, 2),
            'drawdown_from_ath_pct': round(drawdown_pct, 2),
            'volatility_30d_annualized_pct': round(vol_30, 2) if vol_30 is not None else None,
            'volatility_90d_annualized_pct': round(vol_90, 2) if vol_90 is not None else None,
            'pct_days_above_current_pct': round(pct_days_above, 2),
            'sma200_gbp': round(sma200, 2),
            'mayer_multiple': round(mayer, 3),
            'sma200_distance_pct': round(sma_dist_pct, 2),
            'as_of_date': dates[-1].strftime('%Y-%m-%d'),
        }

        cache_file.write_text(json.dumps({'fetched_at': now.isoformat(), 'metrics': metrics}))
        return jsonify(metrics)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _cached_json(path: Path, max_age: timedelta) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        ts = datetime.fromisoformat(raw.get('fetched_at'))
        if datetime.utcnow() - ts < max_age and 'data' in raw:
            return raw['data']
    except Exception:
        return None
    return None

def _write_cache(path: Path, data: dict):
    try:
        path.write_text(json.dumps({'fetched_at': datetime.utcnow().isoformat(), 'data': data}))
    except Exception:
        pass

@app.route('/api/onchain-supply')
def onchain_supply():
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_cache = cache_dir / 'onchain_supply_cache.json'

        cached = _cached_json(out_cache, timedelta(minutes=10))
        if cached:
            return jsonify(cached)

        # 1) Current height via mempool.space
        height_cache = cache_dir / 'tip_height_cache.json'
        height = None
        cached_height = _cached_json(height_cache, timedelta(minutes=5))
        if cached_height and isinstance(cached_height.get('height'), int):
            height = cached_height['height']
        else:
            r = requests.get('https://mempool.space/api/blocks/tip/height', timeout=15)
            r.raise_for_status()
            height = int(r.text.strip())
            _write_cache(height_cache, {'height': height})

        # 2) Halving details
        HALVING_INTERVAL = 210_000
        epoch = height // HALVING_INTERVAL
        next_halving_height = (epoch + 1) * HALVING_INTERVAL
        blocks_to_halving = max(0, next_halving_height - height)
        # Estimate using 10 minutes per block
        minutes_to_halving = blocks_to_halving * 10
        eta_utc = (datetime.utcnow() + timedelta(minutes=minutes_to_halving)).isoformat()
        # Current subsidy (BTC)
        current_subsidy = 50.0 / (2 ** epoch)
        blocks_per_day = 144
        annual_issuance_btc = current_subsidy * blocks_per_day * 365

        # 3) Circulating / max supply via CoinGecko
        cg_cache = cache_dir / 'cg_supply_cache.json'
        cg = _cached_json(cg_cache, timedelta(minutes=10))
        if not cg:
            cg_resp = requests.get('https://api.coingecko.com/api/v3/coins/bitcoin', params={'localization':'false','tickers':'false','community_data':'false','developer_data':'false','sparkline':'false'}, timeout=20)
            cg_resp.raise_for_status()
            j = cg_resp.json()
            market = j.get('market_data', {})
            cg = {
                'circulating_supply': market.get('circulating_supply'),
                'max_supply': market.get('max_supply') or 21_000_000,
            }
            _write_cache(cg_cache, cg)

        circ = float(cg.get('circulating_supply') or 0)
        max_supply = float(cg.get('max_supply') or 21_000_000)
        circ_pct = (circ / max_supply) * 100.0 if max_supply else None

        payload = {
            'height': height,
            'epoch': epoch,
            'next_halving_height': next_halving_height,
            'blocks_to_halving': blocks_to_halving,
            'minutes_to_halving': minutes_to_halving,
            'eta_utc': eta_utc,
            'current_subsidy_btc': round(current_subsidy, 8),
            'annual_issuance_btc': round(annual_issuance_btc, 2),
            'circulating_supply': round(circ, 0) if circ else None,
            'max_supply': round(max_supply, 0) if max_supply else None,
            'circulating_pct_of_max': round(circ_pct, 2) if circ_pct is not None else None,
        }

        _write_cache(out_cache, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/bitcoin-historical/<range>')
def get_historical_data(range):
    print(f"Received request for range: {range}")
    
    try:
        # Load the CSV file
        csv_path = Path(__file__).parent / 'data' / 'bitcoin_historical.csv'
        
        if not csv_path.exists():
            print(f"CSV file not found at {csv_path}")
            return jsonify({'error': 'Historical data file not found'}), 404
        
        # Convert range to days
        days_map = {
            '1M': 30,
            '3M': 90,
            '6M': 180,
            '1Y': 365,
            'ALL': None
        }
        
        days = days_map.get(range)
        if days is None and range != 'ALL':
            return jsonify({'error': f'Invalid range: {range}'}), 400
            
        cutoff_date = None
        if days:
            cutoff_date = datetime.now() - timedelta(days=days)
        
        prices = []
        with open(csv_path, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                try:
                    date = datetime.strptime(row['Date'], '%Y-%m-%d')
                    if cutoff_date and date < cutoff_date:
                        continue
                    # Convert date to timestamp in milliseconds
                    timestamp = int(time.mktime(date.timetuple()) * 1000)
                    price = float(row['Close'])
                    prices.append([timestamp, price])
                except (ValueError, KeyError) as e:
                    print(f"Error processing row: {row}, Error: {e}")
                    continue
        
        if not prices:
            print(f"No data found for range: {range}")
            return jsonify({'error': 'No data available for the specified range'}), 404
            
        print(f"Returning {len(prices)} data points for range: {range}")
        return jsonify({'prices': prices})
            
    except Exception as e:
        print(f"Error in handler: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run()