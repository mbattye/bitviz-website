from flask import render_template

def init_routes(app):
    @app.route('/bitcoin-price')
    def bitcoin_price():
        return render_template('bitcoin-price.html')

    @app.route('/bitcoin-metrics')
    def bitcoin_metrics():
        return render_template('bitcoin-metrics.html')