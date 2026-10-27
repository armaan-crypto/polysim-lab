"""Static HTML report for one run: metrics, equity curve, trade table, failure log."""

from __future__ import annotations

import base64
import io
from html import escape
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pmsim.analysis.metrics import Metrics, compute, usd  # noqa: E402
from pmsim.book.units import PRICE_SCALE, SIZE_SCALE  # noqa: E402
from pmsim.engine.backtest import BacktestResult  # noqa: E402

FAILURE_KINDS = ("reject", "resolution")
MAX_ROWS = 500

CSS = """
body { font: 14px/1.45 system-ui, sans-serif; margin: 2rem auto; max-width: 1100px;
       padding: 0 16px; color: #1d1d1f; background: #fff; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid #e5e5ea; }
th { background: #f5f5f7; }
.banner { padding: 10px 14px; border-radius: 6px; font-weight: 600; margin: 1rem 0; }
.synthetic { background: #fff4e5; color: #8a4b00; border: 1px solid #ffd59e; }
.recorded { background: #e8f4ff; color: #0b4f8a; border: 1px solid #b6dcff; }
img { max-width: 100%; }
"""


def equity_png(res: BacktestResult) -> str:
    """Base64 PNG of the equity curve in USD over hours."""
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=110)
    if res.equity:
        t0 = res.equity[0][0]
        ax.plot([(t - t0) / 3.6e6 for t, _ in res.equity], [usd(v) for _, v in res.equity])
    ax.set_xlabel("hours")
    ax.set_ylabel("equity (USD)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _metrics_table(m: Metrics) -> str:
    rows = "".join(
        f"<tr><th>{escape(k.replace('_', ' '))}</th><td>{_fmt(v)}</td></tr>"
        for k, v in m.as_dict().items()
    )
    return f"<table>{rows}</table>"


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:,.4f}"
    return escape(str(v))


def _trades_table(res: BacktestResult) -> str:
    head = "<tr><th>ts</th><th>token</th><th>side</th><th>price</th><th>size</th>"
    head += "<th>liquidity</th><th>fee USD</th></tr>"
    rows = [
        f"<tr><td>{f.ts}</td><td>{escape(f.token[:24])}</td><td>{f.side}</td>"
        f"<td>{f.price / PRICE_SCALE:.4f}</td><td>{f.size / SIZE_SCALE:,.2f}</td>"
        f"<td>{f.liquidity}</td><td>{usd(f.fee):.4f}</td></tr>"
        for f in res.fills[:MAX_ROWS]
    ]
    more = (
        f"<p>{len(res.fills) - MAX_ROWS} more fills not shown.</p>"
        if len(res.fills) > MAX_ROWS
        else ""
    )
    return f"<table>{head}{''.join(rows)}</table>{more}"


def _failure_log(res: BacktestResult) -> str:
    items = [d for d in res.decisions if d.kind in FAILURE_KINDS][:MAX_ROWS]
    if res.legs and res.legs.failed:
        items_html = [
            f"<li>{res.legs.failed} leg groups failed; unwind cost "
            f"${usd(res.unwind_cost):,.2f}</li>"
        ]
    else:
        items_html = []
    items_html += [
        f"<li><code>{d.ts}</code> {escape(d.kind)}: {escape(d.detail)}</li>" for d in items
    ]
    return "<ul>" + "".join(items_html) + "</ul>" if items_html else "<p>No failures.</p>"


def render(res: BacktestResult, title: str = "") -> str:
    m = compute(res)
    label = res.label
    banner = (
        "SYNTHETIC DATA: results describe the simulator, not live markets."
        if label == "synthetic"
        else "Recorded market data."
    )
    name = escape(title or f"{res.strategy} backtest")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title><style>{CSS}</style></head><body>
<h1>{name}</h1>
<div class="banner {label}">{banner}</div>
<h2>Metrics</h2>{_metrics_table(m)}
<h2>Equity curve</h2><img alt="equity curve" src="data:image/png;base64,{equity_png(res)}">
<h2>Trades</h2>{_trades_table(res)}
<h2>Failure log</h2>{_failure_log(res)}
</body></html>
"""


def write_report(res: BacktestResult, path: Path, title: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(res, title))
    return path
