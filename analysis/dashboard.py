#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard from claude-meter normalized JSONL data."""

import argparse
import datetime as dt
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze_normalized_log as anl


def _fmt_tokens(n):
    """Format token count with K/M suffix."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _downsample(series, max_points=500):
    """Downsample a time series using largest-triangle-three-buckets (simplified).

    Keeps first and last points, then picks the point with the highest
    utilization change in each bucket to preserve peaks and dips.
    """
    if len(series) <= max_points:
        return series
    result = [series[0]]
    bucket_size = (len(series) - 2) / (max_points - 2)
    for i in range(1, max_points - 1):
        start = int(round(1 + (i - 1) * bucket_size))
        end = int(round(1 + i * bucket_size))
        bucket = series[start:end]
        if not bucket:
            continue
        # Pick point with max absolute utilization (preserves peaks)
        best = max(bucket, key=lambda p: abs(p["utilization"]))
        result.append(best)
    result.append(series[-1])
    return result


def _build_dashboard_data(records):
    """Build the data payload to embed in the HTML dashboard."""
    token_summary = anl.build_token_summary(records)
    budget_estimates = anl.build_session_budget_estimates(records)
    ts_5h = anl.build_utilization_time_series(records, window="5h")
    ts_7d = anl.build_utilization_time_series(records, window="7d")
    window_summary = anl.summarize_windows(records)
    estimate_band = anl.build_estimate_band(records)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "token_summary": token_summary,
        "budget_estimates": budget_estimates,
        "time_series_5h": _downsample(ts_5h),
        "time_series_7d": _downsample(ts_7d),
        "window_summary": window_summary,
        "estimate_band": estimate_band,
    }


def _generate_html(data):
    """Generate the complete HTML dashboard string."""
    ts = data["token_summary"]
    budget = data["budget_estimates"]
    generated_at = data["generated_at"][:19].replace("T", " ") + " UTC"

    plan_tier = ts.get("plan_tier") or "unknown"
    first_ts = (ts.get("first_timestamp") or "")[:10]
    last_ts = (ts.get("last_timestamp") or "")[:10]
    api_calls = ts["api_calls"]
    date_range = f"{first_ts} to {last_ts}" if first_ts and last_ts else "N/A"

    windows = ts.get("windows", {})
    w5h = windows.get("5h", {})
    w7d = windows.get("7d", {})
    cur_5h = round(w5h.get("current", 0) * 100, 1)
    peak_5h = round(w5h.get("peak", 0) * 100, 1)
    cur_7d = round(w7d.get("current", 0) * 100, 1)
    peak_7d = round(w7d.get("peak", 0) * 100, 1)

    input_tok = ts["input_tokens"]
    output_tok = ts["output_tokens"]
    cache_read = ts["cache_read_tokens"]
    cache_create = ts["cache_create_tokens"]
    total_tok = input_tok + output_tok + cache_read + cache_create
    cache_pct = ts["cache_read_pct"]

    # Budget sections
    budget_rows = []
    for wname in sorted(budget.keys()):
        est = budget[wname]
        count = est.get("sessions", 0)
        if count == 0:
            budget_rows.append(
                f'<tr><td>{wname}</td><td colspan="4" class="muted">Not enough data</td></tr>'
            )
        elif count == 1:
            budget_rows.append(
                f'<tr><td>{wname}</td><td>${est["median"]:,.0f}</td>'
                f'<td>${est["median"]:,.0f}</td><td>-</td><td>1</td></tr>'
            )
        else:
            budget_rows.append(
                f'<tr><td>{wname}</td>'
                f'<td>${est["min"]:,.0f} - ${est["max"]:,.0f}</td>'
                f'<td>${est["median"]:,.0f}</td>'
                f'<td>${est.get("p25", 0):,.0f} - ${est.get("p75", 0):,.0f}</td>'
                f'<td>{count}</td></tr>'
            )

    budget_html = "\n".join(budget_rows) if budget_rows else (
        '<tr><td colspan="5" class="muted">Not enough data</td></tr>'
    )

    # Model breakdown
    model_rows = []
    for model, mdata in sorted(
        ts.get("models", {}).items(), key=lambda x: -x[1]["calls"]
    ):
        model_rows.append(
            f'<tr><td>{model}</td><td>{mdata["calls"]:,}</td>'
            f'<td>{_fmt_tokens(mdata["input"])}</td>'
            f'<td>{_fmt_tokens(mdata["output"])}</td></tr>'
        )
    model_html = "\n".join(model_rows) if model_rows else (
        '<tr><td colspan="4" class="muted">No model data</td></tr>'
    )

    # Embed time series data for Chart.js
    chart_data = json.dumps({
        "ts_5h": data["time_series_5h"],
        "ts_7d": data["time_series_7d"],
    })

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-meter dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #1a1b26;
    --surface: #24283b;
    --border: #414868;
    --text: #c0caf5;
    --text-muted: #565f89;
    --accent: #7aa2f7;
    --accent2: #bb9af7;
    --green: #9ece6a;
    --yellow: #e0af68;
    --red: #f7768e;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 0;
  }}
  .header {{
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 24px;
  }}
  .header h1 {{
    font-size: 18px;
    font-weight: 600;
    color: var(--accent);
  }}
  .header .meta {{
    font-size: 13px;
    color: var(--text-muted);
  }}
  .header .meta span {{
    margin-right: 16px;
  }}
  .header .meta strong {{
    color: var(--text);
  }}
  .container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px;
  }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
  }}
  .card h2 {{
    font-size: 14px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-bottom: 16px;
  }}
  .card.full {{
    grid-column: 1 / -1;
  }}
  .gauge-row {{
    display: flex;
    gap: 32px;
    flex-wrap: wrap;
  }}
  .gauge {{
    flex: 1;
    min-width: 180px;
  }}
  .gauge-label {{
    font-size: 13px;
    color: var(--text-muted);
    margin-bottom: 6px;
  }}
  .gauge-bar {{
    height: 8px;
    background: var(--bg);
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 4px;
    position: relative;
  }}
  .gauge-bar .fill {{
    height: 100%;
    border-radius: 4px;
    transition: width 0.3s;
  }}
  .gauge-bar .peak-marker {{
    position: absolute;
    top: -2px;
    width: 2px;
    height: 12px;
    background: var(--red);
    border-radius: 1px;
  }}
  .gauge-value {{
    font-size: 28px;
    font-weight: 700;
  }}
  .gauge-peak {{
    font-size: 12px;
    color: var(--text-muted);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }}
  th {{
    text-align: left;
    font-weight: 600;
    color: var(--text-muted);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  td {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
  }}
  tr:last-child td {{
    border-bottom: none;
  }}
  td.muted {{
    color: var(--text-muted);
    font-style: italic;
  }}
  .total-row td {{
    font-weight: 700;
    border-top: 2px solid var(--border);
  }}
  .chart-container {{
    position: relative;
    height: 300px;
  }}
  .notice {{
    font-size: 11px;
    color: var(--text-muted);
    text-align: center;
    padding: 16px;
    border-top: 1px solid var(--border);
    margin-top: 24px;
  }}
  .color-green {{ color: var(--green); }}
  .color-yellow {{ color: var(--yellow); }}
  .color-red {{ color: var(--red); }}
  @media (max-width: 700px) {{
    .grid {{ grid-template-columns: 1fr; }}
    .header {{ flex-direction: column; gap: 8px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>claude-meter</h1>
  <div class="meta">
    <span>Plan: <strong>{plan_tier}</strong></span>
    <span>Period: <strong>{date_range}</strong></span>
    <span>API calls: <strong>{api_calls:,}</strong></span>
    <span>Updated: <strong>{generated_at}</strong></span>
  </div>
</div>

<div class="container">

  <!-- Utilization Gauges -->
  <div class="grid">
    <div class="card">
      <h2>Current Utilization</h2>
      <div class="gauge-row">
        <div class="gauge">
          <div class="gauge-label">5-hour window</div>
          <div class="gauge-bar">
            <div class="fill" style="width: {min(cur_5h, 100)}%; background: {'var(--green)' if cur_5h < 50 else 'var(--yellow)' if cur_5h < 80 else 'var(--red)'}"></div>
            <div class="peak-marker" style="left: {min(peak_5h, 100)}%"></div>
          </div>
          <div class="gauge-value {'color-green' if cur_5h < 50 else 'color-yellow' if cur_5h < 80 else 'color-red'}">{cur_5h}%</div>
          <div class="gauge-peak">Peak: {peak_5h}%</div>
        </div>
        <div class="gauge">
          <div class="gauge-label">7-day window</div>
          <div class="gauge-bar">
            <div class="fill" style="width: {min(cur_7d, 100)}%; background: {'var(--green)' if cur_7d < 50 else 'var(--yellow)' if cur_7d < 80 else 'var(--red)'}"></div>
            <div class="peak-marker" style="left: {min(peak_7d, 100)}%"></div>
          </div>
          <div class="gauge-value {'color-green' if cur_7d < 50 else 'color-yellow' if cur_7d < 80 else 'color-red'}">{cur_7d}%</div>
          <div class="gauge-peak">Peak: {peak_7d}%</div>
        </div>
      </div>
    </div>

    <!-- Token Usage -->
    <div class="card">
      <h2>Token Usage</h2>
      <table>
        <tr><th>Type</th><th>Tokens</th></tr>
        <tr><td>Input</td><td>{input_tok:,}</td></tr>
        <tr><td>Output</td><td>{output_tok:,}</td></tr>
        <tr><td>Cache read</td><td>{cache_read:,} ({cache_pct}%)</td></tr>
        <tr><td>Cache create</td><td>{cache_create:,}</td></tr>
        <tr class="total-row"><td>Total</td><td>{total_tok:,}</td></tr>
      </table>
    </div>
  </div>

  <!-- Budget Estimates -->
  <div class="grid">
    <div class="card full">
      <h2>Budget Estimates (per window cycle)</h2>
      <table>
        <tr><th>Window</th><th>Range</th><th>Median</th><th>p25-p75</th><th>Sessions</th></tr>
        {budget_html}
      </table>
    </div>
  </div>

  <!-- Utilization Chart -->
  <div class="grid">
    <div class="card full">
      <h2>Utilization Over Time</h2>
      <div class="chart-container">
        <canvas id="utilizationChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Per-Model Breakdown -->
  <div class="grid">
    <div class="card full">
      <h2>Per-Model Breakdown</h2>
      <table>
        <tr><th>Model</th><th>Calls</th><th>Input Tokens</th><th>Output Tokens</th></tr>
        {model_html}
      </table>
    </div>
  </div>

  <div class="notice">
    This dashboard contains real, non-anonymized usage data from a single user.
  </div>
</div>

<script>
const DATA = {chart_data};

function buildChart() {{
  const ctx = document.getElementById('utilizationChart');
  if (!ctx) return;

  const ts5h = DATA.ts_5h || [];
  const ts7d = DATA.ts_7d || [];

  if (ts5h.length === 0 && ts7d.length === 0) {{
    ctx.parentElement.innerHTML = '<p style="color: var(--text-muted); text-align: center; padding: 60px 0;">No utilization data available</p>';
    return;
  }}

  new Chart(ctx, {{
    type: 'line',
    data: {{
      datasets: [
        {{
          label: '5h window',
          data: ts5h.map(p => ({{ x: p.timestamp, y: (p.utilization * 100).toFixed(1) }})),
          borderColor: '#7aa2f7',
          backgroundColor: 'rgba(122, 162, 247, 0.1)',
          borderWidth: 1.5,
          pointRadius: ts5h.length < 60 ? 2 : 0,
          fill: true,
          tension: 0.1,
        }},
        {{
          label: '7d window',
          data: ts7d.map(p => ({{ x: p.timestamp, y: (p.utilization * 100).toFixed(1) }})),
          borderColor: '#bb9af7',
          backgroundColor: 'rgba(187, 154, 247, 0.1)',
          borderWidth: 1.5,
          pointRadius: ts7d.length < 60 ? 2 : 0,
          fill: true,
          tension: 0.1,
        }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{
        mode: 'index',
        intersect: false,
      }},
      scales: {{
        x: {{
          type: 'category',
          ticks: {{
            color: '#565f89',
            maxTicksLimit: 12,
            maxRotation: 45,
            callback: function(value, index) {{
              const label = this.getLabelForValue(value);
              if (!label) return '';
              return label.substring(5, 16).replace('T', ' ');
            }}
          }},
          grid: {{ color: 'rgba(65, 72, 104, 0.3)' }},
        }},
        y: {{
          min: 0,
          max: 100,
          ticks: {{
            color: '#565f89',
            callback: v => v + '%',
          }},
          grid: {{ color: 'rgba(65, 72, 104, 0.3)' }},
        }}
      }},
      plugins: {{
        legend: {{
          labels: {{ color: '#c0caf5' }}
        }},
        tooltip: {{
          callbacks: {{
            title: function(items) {{
              if (!items.length) return '';
              const raw = items[0].raw;
              return raw.x ? raw.x.replace('T', ' ').substring(0, 19) : '';
            }},
            label: function(item) {{
              return item.dataset.label + ': ' + item.raw.y + '%';
            }}
          }}
        }}
      }}
    }}
  }});
}}

// Merge x-axis labels from both series for category scale
(function() {{
  const ts5h = DATA.ts_5h || [];
  const ts7d = DATA.ts_7d || [];
  const allTimestamps = new Set();
  ts5h.forEach(p => allTimestamps.add(p.timestamp));
  ts7d.forEach(p => allTimestamps.add(p.timestamp));
  const sortedLabels = Array.from(allTimestamps).sort();

  // Rebuild data indexed by timestamp for alignment
  const map5h = {{}};
  ts5h.forEach(p => {{ map5h[p.timestamp] = p.utilization; }});
  const map7d = {{}};
  ts7d.forEach(p => {{ map7d[p.timestamp] = p.utilization; }});

  DATA.ts_5h = sortedLabels.map(ts => ({{ timestamp: ts, utilization: map5h[ts] ?? null }}));
  DATA.ts_7d = sortedLabels.map(ts => ({{ timestamp: ts, utilization: map7d[ts] ?? null }}));
}})();

buildChart();
</script>

</body>
</html>"""


def _generate_no_data_html():
    """Generate HTML for when no data is available."""
    generated_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-meter dashboard</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #1a1b26;
    color: #c0caf5;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    margin: 0;
  }}
  .empty {{
    text-align: center;
    padding: 40px;
  }}
  .empty h1 {{
    font-size: 20px;
    color: #7aa2f7;
    margin-bottom: 12px;
  }}
  .empty p {{
    color: #565f89;
    font-size: 14px;
  }}
</style>
</head>
<body>
<div class="empty">
  <h1>claude-meter</h1>
  <p>No data yet.</p>
  <p>Start using Claude with the meter proxy to see usage data here.</p>
  <p style="margin-top: 24px; font-size: 11px;">Generated: {generated_at}</p>
</div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate a self-contained HTML dashboard from claude-meter data."
    )
    parser.add_argument(
        "log_dir",
        nargs="?",
        default=str(Path.home() / ".claude-meter"),
        help="Path to claude-meter log directory (default: ~/.claude-meter)",
    )
    parser.add_argument(
        "--output",
        default="index.html",
        help="Output HTML file path (default: index.html)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Open the dashboard in the default browser",
    )
    args = parser.parse_args()

    records = anl.load_records_multi(args.log_dir)

    output_path = Path(args.output)
    if records:
        data = _build_dashboard_data(records)
        html = _generate_html(data)
    else:
        html = _generate_no_data_html()

    output_path.write_text(html)
    print(f"Dashboard written to {output_path.resolve()}")

    if not records:
        print("No data found — dashboard shows empty state.")

    if args.open_browser:
        url = output_path.resolve().as_uri()
        try:
            webbrowser.open(url)
        except Exception:
            print(f"Could not open browser. Open manually: {url}")


if __name__ == "__main__":
    main()
