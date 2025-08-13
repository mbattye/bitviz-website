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

        # Cycle windows (approximate last 4 years)
        window_days = 365 * 4
        start_idx = max(0, len(closes) - window_days)
        closes_4y = closes[start_idx:]
        dates_4y = dates[start_idx:]
        if closes_4y:
            max_idx = max(range(len(closes_4y)), key=lambda i: closes_4y[i])
            min_idx = min(range(len(closes_4y)), key=lambda i: closes_4y[i])
            days_since_cycle_top = (dates[-1] - dates_4y[max_idx]).days
            days_since_cycle_bottom = (dates[-1] - dates_4y[min_idx]).days
        else:
            days_since_cycle_top = None
            days_since_cycle_bottom = None

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
            'days_since_cycle_top': days_since_cycle_top,
            'days_since_cycle_bottom': days_since_cycle_bottom,
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

def _bc_chart(chart: str, timespan: str, cache_dir: Path, max_age_min=10) -> Optional[dict]:
    cache_file = cache_dir / f'bc_{chart}_{timespan}.json'
    cached = _cached_json(cache_file, timedelta(minutes=max_age_min))
    if cached:
        return cached
    url = f'https://api.blockchain.info/charts/{chart}'
    r = requests.get(url, params={'timespan': timespan, 'format': 'json', 'cors': 'true'}, timeout=20)
    r.raise_for_status()
    data = r.json()
    _write_cache(cache_file, data)
    return data

def _moving_average(series, window: int):
    if not series or window <= 1:
        return series
    out = []
    acc = 0.0
    q = []
    for v in series:
        q.append(v)
        acc += v
        if len(q) > window:
            acc -= q.pop(0)
        out.append(acc / len(q))
    return out

@app.route('/api/miner-economics')
def miner_economics():
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Pull charts (10 min cache)
        hr = _bc_chart('hash-rate', '1year', cache_dir, 10)
        rev = _bc_chart('miners-revenue', '1year', cache_dir, 10)
        fees = _bc_chart('transaction-fees-usd', '1year', cache_dir, 10)

        # Extract y series aligned by date
        def to_map(obj):
            vals = obj.get('values', []) if obj else []
            return {int(pt.get('x')): float(pt.get('y')) for pt in vals if 'x' in pt and 'y' in pt}

        hr_map = to_map(hr)
        rev_map = to_map(rev)
        fees_map = to_map(fees)

        # Build sorted date list present in hashrate
        dates = sorted(hr_map.keys())
        hr_series = [hr_map[d] for d in dates]
        hr_ma7 = _moving_average(hr_series, 7)

        latest_date = dates[-1] if dates else None
        latest_hr_ma7 = hr_ma7[-1] if hr_ma7 else None
        latest_rev = None
        latest_fees = None
        if latest_date:
            # Select closest date available in revenue/fees not exceeding latest_date
            def latest_not_after(m):
                ks = [k for k in m.keys() if k <= latest_date]
                return m[max(ks)] if ks else None
            latest_rev = latest_not_after(rev_map)
            latest_fees = latest_not_after(fees_map)

        fees_pct = None
        if latest_rev and latest_rev != 0 and latest_fees is not None:
            fees_pct = (latest_fees / latest_rev) * 100.0

        payload = {
            'hashrate_7d_avg': round(latest_hr_ma7, 2) if latest_hr_ma7 is not None else None,
            'miners_revenue_usd': round(latest_rev, 0) if latest_rev is not None else None,
            'fees_usd': round(latest_fees, 0) if latest_fees is not None else None,
            'fees_pct_of_revenue': round(fees_pct, 2) if fees_pct is not None else None,
        }
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/fx-rate')
def fx_rate():
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / 'fx_usdgbp_cache.json'
        cached = _cached_json(cache_file, timedelta(hours=6))
        if cached and 'gbp_per_usd' in cached:
            return jsonify(cached)
        r = requests.get('https://api.exchangerate.host/latest', params={'base':'USD','symbols':'GBP'}, timeout=15)
        r.raise_for_status()
        j = r.json()
        rate = float(j['rates']['GBP'])
        data = {'gbp_per_usd': rate}
        _write_cache(cache_file, data)
        return jsonify(data)
    except Exception:
        # Fallback conservative
        return jsonify({'gbp_per_usd': 0.78}), 200

@app.route('/api/macro-context')
def macro_context():
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / 'macro_context_cache.json'
        cached = _cached_json(cache_file, timedelta(hours=6))
        if cached:
            return jsonify(cached)

        # Spot from our fx endpoint
        fx_resp = requests.get('http://localhost:5000/api/fx-rate', timeout=10)
        if not fx_resp.ok:
            gbp_per_usd = 0.78
        else:
            gbp_per_usd = float(fx_resp.json().get('gbp_per_usd', 0.78))

        # 30d and 1y changes using timeseries
        end = datetime.utcnow().date()
        start_30 = end - timedelta(days=30)
        start_365 = end - timedelta(days=365)
        def pct_change(start_date):
            url = 'https://api.exchangerate.host/timeseries'
            r = requests.get(url, params={'base':'USD','symbols':'GBP','start_date': start_date.isoformat(), 'end_date': end.isoformat()}, timeout=20)
            if not r.ok:
                return None, None
            j = r.json()
            rates = j.get('rates', {})
            if not rates:
                return None, None
            first_date = sorted(rates.keys())[0]
            last_date = sorted(rates.keys())[-1]
            first = float(rates[first_date]['GBP'])
            last = float(rates[last_date]['GBP'])
            return ((last/first) - 1.0) * 100.0, [float(rates[d]['GBP']) for d in sorted(rates.keys())]
        change_30d, series_30 = pct_change(start_30)
        change_1y, series_1y = pct_change(start_365)

        # 1y high/low and percentile position
        one_y_high = max(series_1y) if series_1y else None
        one_y_low = min(series_1y) if series_1y else None
        pct_in_range = None
        if series_1y and one_y_high is not None and one_y_low is not None and one_y_high != one_y_low:
            pct_in_range = ((gbp_per_usd - one_y_low) / (one_y_high - one_y_low)) * 100.0

        # Latest CPI YoY from World Bank (annual %). Cache separately for 24h within macro cache
        wb_cache = cache_dir / 'macro_cpi_cache.json'
        cpi_cached = _cached_json(wb_cache, timedelta(hours=24))
        if cpi_cached is None:
            def wb_latest(country):
                url = f'https://api.worldbank.org/v2/country/{country}/indicator/FP.CPI.TOTL.ZG'
                r = requests.get(url, params={'format':'json','per_page':1,'date':'2018:2035'}, timeout=20)
                if not r.ok:
                    return None, None
                j = r.json()
                if isinstance(j, list) and len(j) == 2 and j[1]:
                    entry = j[1][0]
                    return entry.get('date'), entry.get('value')
                return None, None
            uk_date, uk_cpi = wb_latest('GBR')
            us_date, us_cpi = wb_latest('USA')
            cpi_data = {
                'uk_cpi_yoy_date': uk_date,
                'uk_cpi_yoy_pct': uk_cpi,
                'us_cpi_yoy_date': us_date,
                'us_cpi_yoy_pct': us_cpi,
            }
            _write_cache(wb_cache, cpi_data)
        else:
            cpi_data = cpi_cached

        data = {
            'gbp_per_usd': round(gbp_per_usd, 6),
            'usd_per_gbp': round(1.0/gbp_per_usd, 6) if gbp_per_usd else None,
            'gbp_per_usd_change_30d_pct': round(change_30d, 2) if change_30d is not None else None,
            'gbp_per_usd_change_1y_pct': round(change_1y, 2) if change_1y is not None else None,
            'gbp_per_usd_1y_high': round(one_y_high, 4) if one_y_high is not None else None,
            'gbp_per_usd_1y_low': round(one_y_low, 4) if one_y_low is not None else None,
            'gbp_per_usd_position_in_1y_range_pct': round(pct_in_range, 2) if pct_in_range is not None else None,
            **cpi_data
        }
        _write_cache(cache_file, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/adoption-usage')
def adoption_usage():
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / 'adoption_usage_cache.json'
        cached = _cached_json(cache_file, timedelta(minutes=10))
        if cached:
            return jsonify(cached)

        # Active addresses and tx/day (30d window for recency)
        aa = _bc_chart('n-unique-addresses', '30days', cache_dir, 10)
        txd = _bc_chart('n-transactions', '30days', cache_dir, 10)
        fees_usd = _bc_chart('transaction-fees-usd', '30days', cache_dir, 10)

        def latest_value(chart_obj):
            vals = chart_obj.get('values', []) if chart_obj else []
            return float(vals[-1]['y']) if vals else None

        active_addresses = latest_value(aa)
        tx_per_day = latest_value(txd)
        fees_usd_latest = latest_value(fees_usd)
        avg_fee_per_tx_usd = None
        if tx_per_day and tx_per_day != 0 and fees_usd_latest is not None:
            avg_fee_per_tx_usd = fees_usd_latest / tx_per_day

        # Lightning capacity
        ln_capacity_btc = None
        try:
            # Primary: v1 lightning stats
            r = requests.get('https://mempool.space/api/v1/lightning/stats', timeout=15)
            if r.ok:
                j = r.json()
                cap_sats = j.get('capacity')
                if isinstance(cap_sats, (int, float)):
                    ln_capacity_btc = round(float(cap_sats) / 100_000_000.0, 2)
            if ln_capacity_btc is None:
                # Fallback: v2
                r2 = requests.get('https://mempool.space/api/v2/lightning/statistics', timeout=15)
                if r2.ok:
                    j2 = r2.json()
                    cap_sats2 = j2.get('total_capacity')
                    if isinstance(cap_sats2, (int, float)):
                        ln_capacity_btc = round(float(cap_sats2) / 100_000_000.0, 2)
        except Exception:
            pass

        data = {
            'active_addresses': round(active_addresses, 0) if active_addresses is not None else None,
            'transactions_per_day': round(tx_per_day, 0) if tx_per_day is not None else None,
            'avg_fee_per_tx_usd': round(avg_fee_per_tx_usd, 2) if avg_fee_per_tx_usd is not None else None,
            'ln_capacity_btc': ln_capacity_btc,
        }
        _write_cache(cache_file, data)
        return jsonify(data)
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