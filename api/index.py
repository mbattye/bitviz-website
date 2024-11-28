from flask import Flask, render_template, send_from_directory, abort
from datetime import datetime, time

app = Flask(__name__, template_folder='templates')

# @app.route('/css/<situation>.css')
# def css_file():
#     situations = ['light', 'dark']
#     # Get the current time
#     current_time = datetime.now().time()
#     set_light_time = time(10, 0, 0)
#     set_dark_time = time(20, 0, 0)

#     try:
#         if current_time > set_light_time:
#             return send_from_directory('static/css/styles_', f'{situations[0]}.css')
#         elif current_time > set_dark_time:
#             return send_from_directory('static/css/styles_', f'{situations[1]}.css')

#     except FileNotFoundError:
#         abort(404) # or provide a default css

@app.route('/css/<situation>.css')
def css_file(situation):
    try:
        return send_from_directory('static/css', f'{situation}.css')
    except FileNotFoundError:
        return send_from_directory('static/css', 'styles_light.css')

@app.route('/')
def index():
    situation_variable = 'styles_dark'
    return render_template('index.html', situation_variable=situation_variable)

@app.route('/bitcoin-price')
def bitcoin_price():
    return render_template('bitcoin-price.html')

@app.route('/bitcoin-metrics')
def bitcoin_metrics():
    return render_template('bitcoin-metrics.html')