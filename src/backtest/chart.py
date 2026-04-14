import json
import os
from src.backtest.engine import BacktestResult
from src.utils.logger import logger


def _build_trade_rows(result: BacktestResult) -> str:
    rows = []
    for i, t in enumerate(result.trades[:50]):
        side_cls = "green" if t.side == "buy" else "red"
        pnl_cls = "green" if t.pnl > 0 else "red"
        pnl_prefix = "+" if t.pnl > 0 else ""
        rows.append(
            f'<tr><td>{i+1}</td>'
            f'<td class="{side_cls}">{t.side.upper()}</td>'
            f'<td>{t.entry_price:.2f}</td>'
            f'<td>{t.exit_price:.2f}</td>'
            f'<td>{t.size:.6f}</td>'
            f'<td class="{pnl_cls}">{pnl_prefix}{t.pnl:.2f}</td></tr>'
        )
    return "".join(rows)


def generate_chart(result: BacktestResult, df, symbol: str, timeframe: str, output_path: str = "backtest_report.html"):
    """Generate an HTML report with TradingView Lightweight Charts."""
    import pandas as pd

    # Prepare candlestick data
    candles = []
    for i in range(len(df)):
        row = df.iloc[i]
        ts = int(df.index[i].timestamp()) if hasattr(df.index[i], "timestamp") else int(row.get("timestamp", 0))
        candles.append({
            "time": ts,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })

    # Prepare trade markers
    markers = []
    for t in result.trades:
        entry_time = int(pd.Timestamp(t.entry_time).timestamp()) if not isinstance(t.entry_time, (int, float)) else t.entry_time
        exit_time = int(pd.Timestamp(t.exit_time).timestamp()) if not isinstance(t.exit_time, (int, float)) else t.exit_time

        markers.append({
            "time": entry_time,
            "position": "belowBar" if t.side == "buy" else "aboveBar",
            "color": "#26a69a" if t.side == "buy" else "#ef5350",
            "shape": "arrowUp" if t.side == "buy" else "arrowDown",
            "text": f"{t.side.upper()} @ {t.entry_price:.0f}",
        })
        markers.append({
            "time": exit_time,
            "position": "aboveBar" if t.pnl > 0 else "belowBar",
            "color": "#26a69a" if t.pnl > 0 else "#ef5350",
            "shape": "circle",
            "text": f"{'+'if t.pnl > 0 else ''}{t.pnl:.2f}",
        })

    # Prepare equity curve data
    equity_data = []
    for ts_str, bal in result.equity_curve:
        ts = int(pd.Timestamp(ts_str).timestamp()) if not isinstance(ts_str, (int, float)) else ts_str
        equity_data.append({"time": ts, "value": bal})

    pnl_sign = "+" if result.total_pnl >= 0 else ""
    pnl_color = "#26a69a" if result.total_pnl >= 0 else "#ef5350"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Backtest Report — {symbol}</title>
    <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background: #131722; color: #d1d4dc; font-family: -apple-system, sans-serif; padding: 20px; }}
        .header {{ margin-bottom: 20px; }}
        .header h1 {{ color: #fff; font-size: 24px; }}
        .header p {{ color: #787b86; margin-top: 5px; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
        .stat {{ background: #1e222d; border-radius: 8px; padding: 16px; }}
        .stat .label {{ color: #787b86; font-size: 12px; text-transform: uppercase; }}
        .stat .value {{ color: #fff; font-size: 20px; font-weight: 600; margin-top: 4px; }}
        .stat .value.green {{ color: #26a69a; }}
        .stat .value.red {{ color: #ef5350; }}
        .chart-container {{ background: #1e222d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
        .chart-label {{ color: #787b86; font-size: 13px; margin-bottom: 8px; }}
        #price-chart {{ width: 100%; height: 400px; }}
        #equity-chart {{ width: 100%; height: 200px; }}
        .trades {{ background: #1e222d; border-radius: 8px; padding: 16px; }}
        .trades h3 {{ color: #fff; margin-bottom: 12px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th {{ color: #787b86; text-align: left; padding: 8px; border-bottom: 1px solid #2a2e39; }}
        td {{ padding: 8px; border-bottom: 1px solid #2a2e39; }}
        .green {{ color: #26a69a; }}
        .red {{ color: #ef5350; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Backtest Report — {symbol} ({timeframe})</h1>
        <p>Powered by TradingView Lightweight Charts + Tavily Sentiment + ta Indicators</p>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="label">Starting Balance</div>
            <div class="value">{result.initial_balance:.2f} USDC</div>
        </div>
        <div class="stat">
            <div class="label">Final Balance</div>
            <div class="value">{result.final_balance:.2f} USDC</div>
        </div>
        <div class="stat">
            <div class="label">Total PnL</div>
            <div class="value {'green' if result.total_pnl >= 0 else 'red'}">{pnl_sign}{result.total_pnl:.2f} ({pnl_sign}{result.total_pnl_pct:.2f}%)</div>
        </div>
        <div class="stat">
            <div class="label">Win Rate</div>
            <div class="value {'green' if result.win_rate >= 50 else 'red'}">{result.win_rate:.1f}%</div>
        </div>
        <div class="stat">
            <div class="label">Trades</div>
            <div class="value">{result.wins}W / {result.losses}L</div>
        </div>
        <div class="stat">
            <div class="label">Max Drawdown</div>
            <div class="value red">-{result.max_drawdown_pct:.2f}%</div>
        </div>
        <div class="stat">
            <div class="label">Sharpe Ratio</div>
            <div class="value">{result.sharpe_ratio:.2f}</div>
        </div>
    </div>

    <div class="chart-container">
        <div class="chart-label">Price Chart with Trade Markers</div>
        <div id="price-chart"></div>
    </div>

    <div class="chart-container">
        <div class="chart-label">Equity Curve</div>
        <div id="equity-chart"></div>
    </div>

    <div class="trades">
        <h3>Trade History</h3>
        <table>
            <tr><th>#</th><th>Side</th><th>Entry</th><th>Exit</th><th>Size</th><th>PnL</th></tr>
            {_build_trade_rows(result)}
        </table>
    </div>

    <script>
        // Price Chart
        const chart = LightweightCharts.createChart(document.getElementById('price-chart'), {{
            layout: {{ background: {{ color: '#1e222d' }}, textColor: '#d1d4dc' }},
            grid: {{ vertLines: {{ color: '#2a2e39' }}, horzLines: {{ color: '#2a2e39' }} }},
            crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
            timeScale: {{ timeVisible: true, secondsVisible: false }},
        }});

        const candleSeries = chart.addCandlestickSeries({{
            upColor: '#26a69a', downColor: '#ef5350',
            borderDownColor: '#ef5350', borderUpColor: '#26a69a',
            wickDownColor: '#ef5350', wickUpColor: '#26a69a',
        }});
        candleSeries.setData({json.dumps(candles)});

        const markers = {json.dumps(markers)};
        if (markers.length > 0) candleSeries.setMarkers(markers);

        // Equity Chart
        const eqChart = LightweightCharts.createChart(document.getElementById('equity-chart'), {{
            layout: {{ background: {{ color: '#1e222d' }}, textColor: '#d1d4dc' }},
            grid: {{ vertLines: {{ color: '#2a2e39' }}, horzLines: {{ color: '#2a2e39' }} }},
            timeScale: {{ timeVisible: true, secondsVisible: false }},
        }});

        const eqSeries = eqChart.addAreaSeries({{
            lineColor: '#26a69a', topColor: 'rgba(38, 166, 154, 0.3)',
            bottomColor: 'rgba(38, 166, 154, 0.02)', lineWidth: 2,
        }});
        eqSeries.setData({json.dumps(equity_data)});

        // Sync time scales
        chart.timeScale().subscribeVisibleLogicalRangeChange(range => {{
            eqChart.timeScale().setVisibleLogicalRange(range);
        }});
        eqChart.timeScale().subscribeVisibleLogicalRangeChange(range => {{
            chart.timeScale().setVisibleLogicalRange(range);
        }});
    </script>
</body>
</html>"""

    out = os.path.join(os.getcwd(), output_path)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"Chart report saved to {out}")
    return out
