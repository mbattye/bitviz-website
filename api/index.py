from flask import Flask, render_template, jsonify, request
import csv
from datetime import datetime, timedelta
from pathlib import Path
import time
import json
import requests
import math
from typing import Optional, List, Tuple

app = Flask(__name__)

# UK consumer reference values for /api/priced-in.
# Hardcoded approximations sourced from public ONS / Land Registry / BBPA data;
# refresh annually. Each entry includes an as_of label so the UI can show it.
PRICED_IN_REFERENCES = [
    {'key': 'milk_pint',     'gbp': 0.85,    'label': 'pint of milk',                  'plural': 'pints of milk',                  'as_of': '2025',     'source': 'ONS retail prices'},
    {'key': 'bread_loaf',    'gbp': 1.40,    'label': 'loaf of bread',                 'plural': 'loaves of bread',                'as_of': '2025',     'source': 'ONS retail prices'},
    {'key': 'weekly_shop',   'gbp': 55.0,    'label': 'weekly food shop (1 adult)',    'plural': 'weekly food shops',              'as_of': '2025',     'source': 'ONS LCFS (approx.)'},
    {'key': 'pint_beer',     'gbp': 5.20,    'label': 'pint of beer (pub)',            'plural': 'pints of beer',                  'as_of': '2025',     'source': 'BBPA (approx.)'},
    {'key': 'avg_uk_house',  'gbp': 290000,  'label': 'average UK house',              'plural': 'average UK houses',              'as_of': '2025',     'source': 'HM Land Registry HPI'},
    {'key': 'median_salary', 'gbp': 37500,   'label': 'median UK annual salary',       'plural': 'median UK annual salaries',      'as_of': '2025',     'source': 'ONS ASHE'},
]

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

@app.route('/bitcoin-for-humans')
def bitcoin_for_humans():
    return render_template('bitcoin-for-humans.html')

@app.route('/priced-in')
def priced_in_page():
    return render_template('priced-in.html')

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

def _get_gbp_per_usd(cache_dir: Path) -> Optional[float]:
    cache_file = cache_dir / 'fx_usdgbp_cache.json'
    cached = _cached_json(cache_file, timedelta(hours=6))
    if cached and 'gbp_per_usd' in cached:
        try:
            return float(cached['gbp_per_usd'])
        except Exception:
            pass
    try:
        r = requests.get('https://api.exchangerate.host/latest', params={'base':'USD','symbols':'GBP'}, timeout=15)
        r.raise_for_status()
        j = r.json()
        rate = float(j['rates']['GBP'])
        _write_cache(cache_file, {'gbp_per_usd': rate})
        return rate
    except Exception:
        return None

@app.route('/api/tip')
def tip_height():
    """Lightweight endpoint for the header block-height pill. Cached 30s."""
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / 'tip_height_cache.json'
        now = datetime.utcnow()
        # Reuse the existing tip_height_cache with a tighter staleness window
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                ts = datetime.fromisoformat(cached.get('fetched_at'))
                if (now - ts) < timedelta(seconds=30):
                    data = cached.get('data') or {}
                    h = data.get('height') if isinstance(data, dict) else None
                    if isinstance(h, int):
                        return jsonify({'height': h})
            except Exception:
                pass
        try:
            r = requests.get('https://mempool.space/api/blocks/tip/height', timeout=10)
            r.raise_for_status()
            height = int(r.text.strip())
            cache_file.write_text(json.dumps({'fetched_at': now.isoformat(), 'data': {'height': height}}))
            return jsonify({'height': height})
        except Exception:
            # Stale fallback
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text())
                    data = cached.get('data') or {}
                    h = data.get('height') if isinstance(data, dict) else None
                    if isinstance(h, int):
                        return jsonify({'height': h})
                except Exception:
                    pass
            return jsonify({'height': None}), 200
    except Exception:
        return jsonify({'height': None}), 200

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
        rate = _get_gbp_per_usd(cache_dir)
        return jsonify({'gbp_per_usd': rate})
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

        # Prefer frankfurter.app timeseries (robust free source)
        end = datetime.utcnow().date()
        start_30 = end - timedelta(days=30)
        start_365 = end - timedelta(days=365)
        def ff_series(start_date):
            url = f'https://api.frankfurter.app/{start_date.isoformat()}..{end.isoformat()}'
            try:
                r = requests.get(url, params={'from': 'USD', 'to': 'GBP'}, timeout=20)
                if not r.ok:
                    return None
                j = r.json()
                rates = j.get('rates', {})
                if not rates:
                    return None
                # sorted by date
                dates_sorted = sorted(rates.keys())
                return [float(rates[d]['GBP']) for d in dates_sorted]
            except Exception:
                return None

        series_30 = ff_series(start_30)
        series_1y = ff_series(start_365)

        def pct_change_from_series(series):
            if not series or len(series) < 2:
                return None
            first, last = series[0], series[-1]
            try:
                return ((last/first) - 1.0) * 100.0
            except Exception:
                return None

        change_30d = pct_change_from_series(series_30)
        change_1y = pct_change_from_series(series_1y)

        # Spot from frankfurter last rate if available, else helper
        if series_1y and len(series_1y) > 0:
            gbp_per_usd = series_1y[-1]
        else:
            gbp_per_usd = _get_gbp_per_usd(cache_dir) or 0.78

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
                try:
                    r = requests.get(url, params={'format':'json','per_page':1,'date':'2018:2035'}, timeout=20)
                    if not r.ok:
                        return None, None
                except Exception:
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
        }
        # Merge CPI fields only if available
        if cpi_data:
            data.update({
                'uk_cpi_yoy_date': cpi_data.get('uk_cpi_yoy_date'),
                'uk_cpi_yoy_pct': cpi_data.get('uk_cpi_yoy_pct'),
                'us_cpi_yoy_date': cpi_data.get('us_cpi_yoy_date'),
                'us_cpi_yoy_pct': cpi_data.get('us_cpi_yoy_pct'),
            })
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

def _downsample(values, max_points=60):
    if not values or len(values) <= max_points:
        return values
    step = max(1, len(values) // max_points)
    out = [values[i] for i in range(0, len(values), step)]
    if out and out[-1] is not values[-1]:
        out.append(values[-1])
    return out

@app.route('/api/sparkline/<key>')
def sparkline(key):
    """Return a small array of y-values for a given sparkline key.
    Keys: price, hashrate, active-addresses, transactions, fx-gbpusd."""
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f'sparkline_{key}.json'
        now = datetime.utcnow()
        cached = _cached_json(cache_file, timedelta(minutes=15))
        if cached and 'values' in cached:
            return jsonify(cached)

        values = None
        if key == 'price':
            # CoinGecko market_chart for 30d, USD
            r = requests.get(
                'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
                params={'vs_currency':'usd','days':'30','interval':'daily'},
                timeout=20,
            )
            r.raise_for_status()
            j = r.json()
            prices = j.get('prices', [])
            values = [float(p[1]) for p in prices if isinstance(p, list) and len(p) >= 2]
        elif key in ('hashrate', 'active-addresses', 'transactions'):
            chart_map = {
                'hashrate': 'hash-rate',
                'active-addresses': 'n-unique-addresses',
                'transactions': 'n-transactions',
            }
            data = _bc_chart(chart_map[key], '30days', cache_dir, 15)
            vals = (data or {}).get('values', [])
            values = [float(pt['y']) for pt in vals if 'y' in pt]
        elif key == 'fx-gbpusd':
            end = now.date()
            start = end - timedelta(days=30)
            url = f'https://api.frankfurter.app/{start.isoformat()}..{end.isoformat()}'
            r = requests.get(url, params={'from':'USD','to':'GBP'}, timeout=15)
            r.raise_for_status()
            rates = (r.json() or {}).get('rates', {})
            keys_sorted = sorted(rates.keys())
            values = [float(rates[d]['GBP']) for d in keys_sorted]
        else:
            return jsonify({'error': f'unknown sparkline key: {key}'}), 404

        values = _downsample(values, 60) if values else []
        payload = {'values': values}
        _write_cache(cache_file, payload)
        return jsonify(payload)
    except Exception as e:
        # Try stale cache
        try:
            cache_file = Path(__file__).parent / 'data' / f'sparkline_{key}.json'
            if cache_file.exists():
                cached = json.loads(cache_file.read_text())
                d = cached.get('data') or {}
                if 'values' in d:
                    return jsonify(d)
        except Exception:
            pass
        return jsonify({'values': [], 'error': str(e)}), 200

@app.route('/api/bitcoin-historical/<range>')
def get_historical_data(range):
    """Historical BTC/GBP prices for the price page chart.

    Short ranges (1M/3M/6M/1Y): CoinGecko market_chart in GBP.
    ALL: merged CSV + recent CoinGecko via _load_btc_history_gbp, because
    CoinGecko's free API caps `days=max` for non-Pro users.
    """
    range_to_days = {
        '1M': '30',
        '3M': '90',
        '6M': '180',
        '1Y': '365',
    }
    cache_dir = Path(__file__).parent / 'data'
    cache_dir.mkdir(parents=True, exist_ok=True)

    if range == 'ALL':
        try:
            hist = _load_btc_history_gbp(cache_dir)
            if not hist:
                return jsonify({'error': 'history unavailable'}), 502
            prices = [[int(time.mktime(d.timetuple()) * 1000), p] for d, p in hist]
            return jsonify({'prices': prices, 'source': 'csv+coingecko'})
        except Exception as e:
            return jsonify({'error': str(e)}), 502

    days = range_to_days.get(range)
    if days is None:
        return jsonify({'error': f'Invalid range: {range}'}), 400

    cache_file = cache_dir / f'historical_{range}.json'
    ttl = timedelta(minutes=15) if range in ('1M', '3M', '6M') else timedelta(hours=6)
    cached = _cached_json(cache_file, ttl)
    if cached and 'prices' in cached:
        return jsonify(cached)

    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
            params={'vs_currency': 'gbp', 'days': days, 'interval': 'daily'},
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        prices = []
        for pt in j.get('prices', []):
            if isinstance(pt, list) and len(pt) >= 2:
                try:
                    prices.append([int(pt[0]), float(pt[1])])
                except Exception:
                    continue
        if not prices:
            raise RuntimeError('empty prices from upstream')
        payload = {'prices': prices, 'source': 'coingecko'}
        _write_cache(cache_file, payload)
        return jsonify(payload)
    except Exception as e:
        # Stale cache fallback
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                data = cached.get('data') or {}
                if 'prices' in data:
                    return jsonify(data)
            except Exception:
                pass
        return jsonify({'error': str(e)}), 502

# --------------------------------------------------------------------------- #
# Saver's view — /priced-in
# --------------------------------------------------------------------------- #

def _get_gold_oz_gbp(cache_dir: Path, gbp_per_usd: float) -> Optional[float]:
    """Latest gold price per troy ounce in GBP, derived from Yahoo GC=F * GBP/USD."""
    cache_file = cache_dir / 'gold_oz_gbp_cache.json'
    cached = _cached_json(cache_file, timedelta(hours=1))
    if cached and 'gbp' in cached:
        try:
            return float(cached['gbp'])
        except Exception:
            pass
    try:
        r = requests.get(
            'https://query2.finance.yahoo.com/v8/finance/chart/GC=F',
            params={'range': '2d', 'interval': '1d'},
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        if not r.ok:
            return None
        j = r.json()
        result = (j.get('chart') or {}).get('result') or []
        if not result:
            return None
        meta = result[0].get('meta') or {}
        usd = meta.get('regularMarketPrice')
        if usd is None:
            return None
        gbp = float(usd) * (gbp_per_usd or 0.78)
        _write_cache(cache_file, {'gbp': gbp})
        return gbp
    except Exception:
        return None

def _load_btc_history_gbp(cache_dir: Path) -> List[Tuple[datetime, float]]:
    """Daily BTC/GBP history back to 2014.

    Source: stitches the local bitcoin_historical.csv (GBP daily, 2014→2025)
    with a CoinGecko days=365 fetch for the recent gap. CoinGecko's free API
    caps history at 365 days, so we can't ask for `max` directly.
    Cached 24h.
    """
    cache_file = cache_dir / 'btc_history_gbp_cache.json'
    cached = _cached_json(cache_file, timedelta(hours=24))
    if cached and 'prices' in cached:
        out: List[Tuple[datetime, float]] = []
        for d, p in cached['prices']:
            try:
                out.append((datetime.fromisoformat(d), float(p)))
            except Exception:
                continue
        if out:
            return out

    # 1) CSV foundation
    csv_path = cache_dir / 'bitcoin_historical.csv'
    by_date: dict = {}
    if csv_path.exists():
        try:
            with open(csv_path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        d = datetime.strptime(row['Date'], '%Y-%m-%d')
                        by_date[d] = float(row['Close'])
                    except Exception:
                        continue
        except Exception:
            pass

    # 2) Recent year from CoinGecko (overwrites any overlap with the CSV)
    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
            params={'vs_currency': 'gbp', 'days': '365', 'interval': 'daily'},
            timeout=30,
        )
        if r.ok:
            j = r.json()
            for pt in j.get('prices', []):
                try:
                    ts_ms = int(pt[0])
                    val = float(pt[1])
                    # Normalize to midnight UTC so it merges cleanly with CSV daily dates
                    d = datetime.utcfromtimestamp(ts_ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
                    by_date[d] = val
                except Exception:
                    continue
    except Exception:
        pass

    result = sorted(by_date.items())
    if result:
        _write_cache(cache_file, {'prices': [[d.isoformat(), p] for d, p in result]})
    return result

def _load_ftse_monthly_gbp(cache_dir: Path) -> List[Tuple[datetime, float]]:
    """Monthly FTSE 100 closes from Yahoo Finance (^FTSE). Cached 24h.

    Yahoo's v8 chart endpoint caps `range=10y` at monthly resolution, which
    is enough for the DCA calculator. For start dates earlier than ~10 years
    ago, BTC contributions before Yahoo's window simply won't have a FTSE
    counterpart; the DCA endpoint already treats that gracefully.
    """
    cache_file = cache_dir / 'ftse_monthly_cache.json'
    cached = _cached_json(cache_file, timedelta(hours=24))
    if cached and 'prices' in cached:
        out: List[Tuple[datetime, float]] = []
        for d, p in cached['prices']:
            try:
                out.append((datetime.fromisoformat(d), float(p)))
            except Exception:
                continue
        if out:
            return out
    try:
        r = requests.get(
            'https://query2.finance.yahoo.com/v8/finance/chart/%5EFTSE',
            params={'range': '10y', 'interval': '1mo'},
            timeout=30,
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        if not r.ok:
            return []
        j = r.json()
        result_arr = (j.get('chart') or {}).get('result') or []
        if not result_arr:
            return []
        chart = result_arr[0]
        timestamps = chart.get('timestamp') or []
        quote = ((chart.get('indicators') or {}).get('quote') or [{}])[0]
        closes = quote.get('close') or []
        result: List[Tuple[datetime, float]] = []
        for ts, close in zip(timestamps, closes):
            if ts is None or close is None:
                continue
            try:
                d = datetime.utcfromtimestamp(int(ts)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                result.append((d, float(close)))
            except Exception:
                continue
        if result:
            _write_cache(cache_file, {'prices': [[d.isoformat(), p] for d, p in result]})
        return result
    except Exception:
        return []

def _series_value_at_or_before(series: List[Tuple[datetime, float]], when: datetime) -> Optional[float]:
    """Binary search the latest value <= when. Series must be sorted ascending."""
    if not series:
        return None
    lo, hi = 0, len(series) - 1
    if when < series[0][0]:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if series[mid][0] <= when:
            lo = mid
        else:
            hi = mid - 1
    return series[lo][1]

@app.route('/api/priced-in')
def api_priced_in():
    """Snapshot of BTC priced in everyday UK reference goods + sats-per-£."""
    try:
        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / 'priced_in_cache.json'
        cached = _cached_json(cache_file, timedelta(minutes=10))
        if cached:
            return jsonify(cached)

        spot = _get_spot_price_gbp_cached(cache_dir)
        fx = _get_gbp_per_usd(cache_dir) or 0.78
        gold_gbp = _get_gold_oz_gbp(cache_dir, fx)

        references = []
        for ref in PRICED_IN_REFERENCES:
            gbp = ref['gbp']
            references.append({
                'key': ref['key'],
                'label': ref['label'],
                'plural': ref['plural'],
                'unit_price_gbp': gbp,
                'units_per_btc': (spot / gbp) if (spot and gbp) else None,
                'sats_per_unit': (gbp / spot * 100_000_000) if spot else None,
                'as_of': ref['as_of'],
                'source': ref['source'],
            })
        if gold_gbp:
            references.append({
                'key': 'gold_oz',
                'label': 'ounce of gold',
                'plural': 'ounces of gold',
                'unit_price_gbp': round(gold_gbp, 2),
                'units_per_btc': (spot / gold_gbp) if (spot and gold_gbp) else None,
                'sats_per_unit': (gold_gbp / spot * 100_000_000) if spot else None,
                'as_of': 'live',
                'source': 'stooq XAUUSD × GBP/USD',
            })

        payload = {
            'spot_btc_gbp': round(spot, 2) if spot else None,
            'sats_per_pound': round(100_000_000 / spot, 0) if spot else None,
            'gbp_per_usd': round(fx, 4),
            'references': references,
        }
        _write_cache(cache_file, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/dca')
def api_dca():
    """Compute a Bitcoin DCA simulation and compare with cash + FTSE 100.

    Query params:
        monthly     £ contributed each month (default 100)
        start       YYYY-MM start of contributions (default 2018-01)
        cash_rate   Annual cash savings rate as %, default 3
    """
    try:
        monthly = float(request.args.get('monthly', 100))
        start_str = request.args.get('start', '2018-01')
        cash_rate_pct = float(request.args.get('cash_rate', 3.0))
        if monthly <= 0 or monthly > 1_000_000:
            return jsonify({'error': 'monthly out of range'}), 400
        try:
            start_dt = datetime.strptime(start_str, '%Y-%m')
        except ValueError:
            return jsonify({'error': 'start must be YYYY-MM'}), 400
        if start_dt < datetime(2013, 1, 1):
            start_dt = datetime(2013, 1, 1)  # CoinGecko coverage limit
        cash_rate = cash_rate_pct / 100.0

        cache_dir = Path(__file__).parent / 'data'
        cache_dir.mkdir(parents=True, exist_ok=True)

        btc_hist = _load_btc_history_gbp(cache_dir)
        ftse_hist = _load_ftse_monthly_gbp(cache_dir)

        if not btc_hist:
            return jsonify({'error': 'BTC history unavailable'}), 502

        # Generate monthly contribution dates from start to now (inclusive)
        end_dt = datetime.utcnow().replace(day=1)
        months: List[datetime] = []
        cursor = start_dt.replace(day=1)
        while cursor <= end_dt:
            months.append(cursor)
            if cursor.month == 12:
                cursor = cursor.replace(year=cursor.year + 1, month=1)
            else:
                cursor = cursor.replace(month=cursor.month + 1)

        btc_accum = 0.0
        ftse_shares = 0.0
        invested = 0.0
        btc_invested = 0.0
        ftse_invested = 0.0
        for m in months:
            invested += monthly
            p_btc = _series_value_at_or_before(btc_hist, m)
            if p_btc:
                btc_accum += monthly / p_btc
                btc_invested += monthly
            p_ftse = _series_value_at_or_before(ftse_hist, m)
            if p_ftse:
                ftse_shares += monthly / p_ftse
                ftse_invested += monthly

        # Latest values
        spot_btc = btc_hist[-1][1] if btc_hist else None
        spot_ftse = ftse_hist[-1][1] if ftse_hist else None

        btc_value = btc_accum * spot_btc if spot_btc else 0
        ftse_value = ftse_shares * spot_ftse if spot_ftse else 0

        # Cash: monthly compounding, contribution at end of month
        m_rate = cash_rate / 12.0
        cash_value = 0.0
        for _ in months:
            cash_value = cash_value * (1 + m_rate) + monthly

        payload = {
            'monthly': monthly,
            'start': start_dt.strftime('%Y-%m'),
            'months': len(months),
            'invested': round(invested, 2),
            'btc': {
                'accumulated': round(btc_accum, 8),
                'value_gbp': round(btc_value, 2),
                'multiplier': round(btc_value / invested, 2) if invested else None,
            },
            'cash': {
                'rate_pct': cash_rate_pct,
                'value_gbp': round(cash_value, 2),
                'multiplier': round(cash_value / invested, 2) if invested else None,
            },
            'ftse': {
                'value_gbp': round(ftse_value, 2) if spot_ftse else None,
                'multiplier': round(ftse_value / invested, 2) if invested and spot_ftse else None,
                'available': spot_ftse is not None,
            },
            'as_of_btc': btc_hist[-1][0].isoformat() if btc_hist else None,
            'as_of_ftse': ftse_hist[-1][0].isoformat() if ftse_hist else None,
            'spot_btc_gbp': round(spot_btc, 2) if spot_btc else None,
        }
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(threaded=True)