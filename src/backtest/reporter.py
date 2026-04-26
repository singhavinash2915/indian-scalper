"""Backtest report generator.

Takes a ``BacktestResult`` and produces:
  - Console summary (CAGR, max DD, Sharpe, win rate, exit attribution)
  - JSON dump for programmatic use / dashboard embedding
  - Trade ledger CSV
  - HTML report with embedded equity curve + monthly heatmap
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backtest.harness import BacktestResult
from backtest.trades import Trade

IST = ZoneInfo("Asia/Kolkata")


def compute_cagr(start: float, end: float, days: int) -> float:
    """Annualised return assuming N calendar days. CAGR = 0 if days <= 0."""
    if start <= 0 or end <= 0 or days <= 0:
        return 0.0
    years = days / 365.25
    if years <= 0:
        return 0.0
    return (math.pow(end / start, 1 / years) - 1) * 100.0


def exit_attribution(trades: list[Trade]) -> dict[str, dict]:
    """Group trades by exit_reason (read from optional attribute) + bucket
    pnl/count/win-rate. Falls back to a single 'trade' bucket when the
    Trade dataclass doesn't expose exit_reason."""
    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        reason = (getattr(t, "exit_reason", None) or "trade").split(":")[0]
        b = buckets[reason]
        b["count"] += 1
        b["pnl"] += t.pnl
        if t.pnl > 0:
            b["wins"] += 1
    out: dict[str, dict] = {}
    total_count = sum(b["count"] for b in buckets.values()) or 1
    for reason, b in buckets.items():
        out[reason] = {
            **b,
            "count_pct": round(100 * b["count"] / total_count, 1),
            "win_rate_pct": round(100 * b["wins"] / b["count"], 1) if b["count"] else 0,
        }
    return dict(sorted(out.items(), key=lambda kv: -abs(kv[1]["pnl"])))


def monthly_pnl(trades: list[Trade]) -> dict[str, float]:
    """Calendar-month P&L bucket → ₹ pnl."""
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.exit_ts is None:
            continue
        ts = t.exit_ts.astimezone(IST) if t.exit_ts.tzinfo else t.exit_ts
        out[ts.strftime("%Y-%m")] += t.pnl
    return dict(sorted(out.items()))


def consecutive_loss_streak(trades: list[Trade]) -> int:
    """Max consecutive losing trades (in chronological order)."""
    max_streak = current = 0
    for t in sorted(trades, key=lambda x: x.exit_ts or datetime.min.replace(tzinfo=IST)):
        if t.pnl < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def avg_win_loss(trades: list[Trade]) -> tuple[float, float, float]:
    """Returns (avg_win, avg_loss, profit_factor)."""
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = sum(losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    return avg_win, avg_loss, pf


def top_symbols(trades: list[Trade], n: int = 5) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Top-N winners and top-N losers by total per-symbol P&L."""
    by_sym: dict[str, float] = defaultdict(float)
    for t in trades:
        by_sym[t.symbol] += t.pnl
    sorted_syms = sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True)
    return sorted_syms[:n], sorted_syms[-n:][::-1]


# --------------------------------------------------------------------- #
# Top-level report builder                                              #
# --------------------------------------------------------------------- #

def build_report(
    result: BacktestResult,
    *,
    from_date: date,
    to_date: date,
    interval: str,
    out_dir: str | Path = "data/backtest/reports",
    label: str | None = None,
) -> dict[str, Any]:
    """Build the full report and write artifacts. Returns a dict you can
    JSON-serialise or render in the dashboard."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    days = max(1, (to_date - from_date).days)
    cagr = compute_cagr(result.starting_equity, result.final_equity, days)
    avg_w, avg_l, pf = avg_win_loss(result.trades)
    streak = consecutive_loss_streak(result.trades)
    attrib = exit_attribution(result.trades)
    monthly = monthly_pnl(result.trades)
    winners, losers = top_symbols(result.trades, n=5)

    summary = {
        "label": label or f"{from_date}_{to_date}_{interval}",
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "interval": interval,
        "days": days,
        "starting_equity": result.starting_equity,
        "final_equity": result.final_equity,
        "total_return_pct": result.total_return_pct,
        "cagr_pct": cagr,
        "max_dd_pct": result.metrics.get("max_dd_pct", 0.0),
        "sharpe": result.metrics.get("sharpe", 0.0),
        "win_rate_pct": result.metrics.get("win_rate", 0.0),
        "trades_total": len(result.trades),
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "profit_factor": pf if pf != float("inf") else None,
        "max_consecutive_losses": streak,
        "avg_holding_minutes": result.metrics.get("avg_holding_minutes", 0.0),
        "exit_attribution": attrib,
        "monthly_pnl": monthly,
        "top_winners": [{"symbol": s, "pnl": p} for s, p in winners],
        "top_losers": [{"symbol": s, "pnl": p} for s, p in losers],
        "ticks_processed": result.timestamps_processed,
        "ticks_skipped": result.ticks_skipped,
    }

    # Persist JSON.
    json_path = out_dir / f"{summary['label']}.json"
    json_path.write_text(json.dumps(summary, indent=2, default=str))

    # Trade ledger CSV.
    csv_path = out_dir / f"{summary['label']}_trades.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "side", "qty", "entry_price", "entry_ts",
            "exit_price", "exit_ts", "exit_reason", "pnl", "pnl_pct",
            "holding_minutes",
        ])
        for t in sorted(result.trades, key=lambda x: x.exit_ts or datetime.min.replace(tzinfo=IST)):
            w.writerow([
                t.symbol, getattr(t, "side", ""),
                t.qty, t.entry_price, t.entry_ts,
                t.exit_price, t.exit_ts,
                getattr(t, "exit_reason", ""),
                t.pnl, getattr(t, "pnl_pct", 0.0),
                getattr(t, "holding_minutes", 0),
            ])

    # HTML report with inline charts.
    html_path = out_dir / f"{summary['label']}.html"
    html_path.write_text(render_html(summary, result))

    summary["artifacts"] = {
        "json": str(json_path), "csv": str(csv_path), "html": str(html_path),
    }
    return summary


def render_html(summary: dict, result: BacktestResult) -> str:
    """Standalone HTML report — embedded equity curve + monthly heatmap."""
    eq = result.equity_curve
    eq_x = [r.get("ts", "") for r in eq]
    eq_y = [float(r.get("equity", 0)) for r in eq]
    monthly = summary.get("monthly_pnl", {})
    months = list(monthly.keys())
    pnl_vals = [round(v, 0) for v in monthly.values()]

    def fmt_inr(v): return f"₹{v:,.0f}"
    def fmt_pct(v): return f"{v:+.2f}%"

    pf = summary.get("profit_factor")
    pf_str = "∞" if pf is None else f"{pf:.2f}"

    attrib_rows = "\n".join(
        f"  <tr><td>{r}</td><td class='r'>{a['count']}</td>"
        f"<td class='r'>{a['count_pct']}%</td>"
        f"<td class='r {'pos' if a['pnl'] >= 0 else 'neg'}'>{fmt_inr(a['pnl'])}</td>"
        f"<td class='r'>{a['win_rate_pct']}%</td></tr>"
        for r, a in summary["exit_attribution"].items()
    )
    winners_rows = "\n".join(
        f"  <tr><td>{w['symbol']}</td><td class='r pos'>{fmt_inr(w['pnl'])}</td></tr>"
        for w in summary["top_winners"]
    )
    losers_rows = "\n".join(
        f"  <tr><td>{l['symbol']}</td><td class='r neg'>{fmt_inr(l['pnl'])}</td></tr>"
        for l in summary["top_losers"]
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Backtest · {summary['label']}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ background:#0a0b10; color:#eaecf0; font-family:Inter,system-ui,sans-serif;
         margin:0; padding:24px; }}
  h1 {{ font-size:18px; letter-spacing:-0.01em; margin:0 0 4px; }}
  .sub {{ color:#8d93a3; font-size:12px; margin-bottom:24px; font-family:JetBrains Mono,monospace; }}
  .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:20px; }}
  .kpi {{ background:rgba(255,255,255,0.025); border:1px solid rgba(255,255,255,0.08);
          border-radius:12px; padding:14px 16px; }}
  .kpi-label {{ font-size:10px; letter-spacing:0.18em; text-transform:uppercase;
                color:#5a5f6e; font-weight:600; }}
  .kpi-value {{ font-family:JetBrains Mono,monospace; font-size:22px; font-weight:600;
                margin-top:6px; }}
  .panel {{ background:rgba(255,255,255,0.025); border:1px solid rgba(255,255,255,0.08);
            border-radius:12px; padding:14px 18px; margin-bottom:14px; }}
  h2 {{ font-size:11px; letter-spacing:0.2em; text-transform:uppercase;
         color:#8d93a3; font-weight:700; margin:0 0 10px; }}
  table {{ width:100%; border-collapse:collapse; font-family:JetBrains Mono,monospace;
            font-size:12px; }}
  th, td {{ padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.05); }}
  th {{ color:#5a5f6e; font-size:10px; letter-spacing:0.15em; text-transform:uppercase;
         text-align:left; font-family:Inter,sans-serif; }}
  td.r {{ text-align:right; }}
  .pos {{ color:#34d399; }} .neg {{ color:#fb7185; }}
  #equity, #monthly {{ width:100%; height:280px; }}
</style></head><body>
  <h1>Backtest Report  ·  {summary['label']}</h1>
  <div class="sub">{summary['from_date']} → {summary['to_date']}  ·  {summary['interval']}  ·  {summary['days']} days  ·  {summary['trades_total']} trades</div>

  <div class="grid">
    <div class="kpi"><div class="kpi-label">Final Equity</div><div class="kpi-value">{fmt_inr(summary['final_equity'])}</div></div>
    <div class="kpi"><div class="kpi-label">Total Return</div><div class="kpi-value {'pos' if summary['total_return_pct'] >= 0 else 'neg'}">{fmt_pct(summary['total_return_pct'])}</div></div>
    <div class="kpi"><div class="kpi-label">CAGR</div><div class="kpi-value {'pos' if summary['cagr_pct'] >= 0 else 'neg'}">{fmt_pct(summary['cagr_pct'])}</div></div>
    <div class="kpi"><div class="kpi-label">Max Drawdown</div><div class="kpi-value neg">{summary['max_dd_pct']:.2f}%</div></div>
    <div class="kpi"><div class="kpi-label">Sharpe</div><div class="kpi-value">{summary['sharpe']:.2f}</div></div>
    <div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-value">{summary['win_rate_pct']:.1f}%</div></div>
    <div class="kpi"><div class="kpi-label">Profit Factor</div><div class="kpi-value">{pf_str}</div></div>
    <div class="kpi"><div class="kpi-label">Max Consec. Losses</div><div class="kpi-value">{summary['max_consecutive_losses']}</div></div>
  </div>

  <div class="panel">
    <h2>Equity Curve</h2>
    <div id="equity"></div>
  </div>
  <div class="panel">
    <h2>Monthly P&amp;L</h2>
    <div id="monthly"></div>
  </div>

  <div class="grid" style="grid-template-columns:1fr 1fr 1fr;">
    <div class="panel">
      <h2>Exit Attribution</h2>
      <table>
        <thead><tr><th>Reason</th><th class="r">N</th><th class="r">%</th><th class="r">P&amp;L</th><th class="r">Win%</th></tr></thead>
        <tbody>{attrib_rows}</tbody>
      </table>
    </div>
    <div class="panel">
      <h2>Top 5 Winners</h2>
      <table><thead><tr><th>Symbol</th><th class="r">P&amp;L</th></tr></thead><tbody>{winners_rows}</tbody></table>
    </div>
    <div class="panel">
      <h2>Top 5 Losers</h2>
      <table><thead><tr><th>Symbol</th><th class="r">P&amp;L</th></tr></thead><tbody>{losers_rows}</tbody></table>
    </div>
  </div>

<script>
const eqX = {eq_x!r};
const eqY = {eq_y!r};
Plotly.newPlot('equity', [{{
  x: eqX, y: eqY, type:'scatter', mode:'lines',
  line: {{color:'#f5a623', width:2}}, fill:'tozeroy',
  fillcolor:'rgba(245,166,35,0.06)', name:'Equity'
}}], {{
  paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)',
  font: {{color:'#eaecf0', family:'JetBrains Mono', size:11}},
  margin: {{l:60, r:20, t:6, b:36}},
  xaxis: {{gridcolor:'rgba(255,255,255,0.04)'}},
  yaxis: {{gridcolor:'rgba(255,255,255,0.04)', tickformat:',.0f'}}
}}, {{displayModeBar:false}});

const months = {months!r};
const pnls = {pnl_vals!r};
Plotly.newPlot('monthly', [{{
  x: months, y: pnls, type:'bar',
  marker: {{color: pnls.map(v => v >= 0 ? '#34d399' : '#fb7185')}}
}}], {{
  paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)',
  font: {{color:'#eaecf0', family:'JetBrains Mono', size:11}},
  margin: {{l:60, r:20, t:6, b:36}},
  xaxis: {{gridcolor:'rgba(255,255,255,0.04)'}},
  yaxis: {{gridcolor:'rgba(255,255,255,0.04)', tickformat:',.0f'}}
}}, {{displayModeBar:false}});
</script>
</body></html>"""
