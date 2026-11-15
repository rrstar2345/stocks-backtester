"""Price chart: a Streamlit custom component (v2) around TradingView Lightweight Charts.

The library is vendored (assets/vendor/) and inlined into the component script, so the chart
works offline. Payloads come from `src.chart_data.build_payload`.
"""
import re
from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).resolve().parent.parent / "assets"
CHART_HEIGHT = 520      # price only
CHART_HEIGHT_RSI = 680  # price pane above an RSI pane

_HTML = """
<div id="wrap">
  <div id="chart"></div>
  <div id="tip" hidden></div>
</div>
"""

_CSS = f"""
:host {{ display: block; }}
#wrap {{ position: relative; width: 100%; height: {CHART_HEIGHT}px; }}
#chart {{ position: absolute; inset: 0; }}
#tip {{
  position: absolute; z-index: 5; pointer-events: none; max-width: 280px;
  display: flex; flex-direction: column; gap: 8px; padding: 8px 10px;
  background: var(--st-secondary-background-color); color: var(--st-text-color);
  border: 1px solid var(--st-border-color); border-radius: var(--st-base-radius);
  font-family: var(--st-font); font-size: 12px; line-height: 1.5;
}}
#tip[hidden] {{ display: none; }}
#tip .trade > div {{ display: flex; justify-content: space-between; gap: 12px; }}
#tip .k {{ opacity: 0.7; }}
#tip .v {{ text-align: right; font-variant-numeric: tabular-nums; }}
"""


def _script() -> str:
    """Vendored library + component body in one module. The minified bundle ends in an
    `export {a as createChart, ...}` list; turn it into an `LWC` object the body reads from."""
    lib = (ASSETS / "vendor" / "lightweight-charts.standalone.production.mjs").read_text(encoding="utf-8").rstrip()
    match = re.search(r"export\s*\{([^}]*)\}\s*;?\s*$", lib)
    if not match:
        raise RuntimeError("Unexpected export list at the end of the vendored Lightweight Charts bundle")
    pairs = [item.split(" as ") for item in match.group(1).split(",") if item.strip()]
    exports = ",".join(f"{(p[-1]).strip()}:{p[0].strip()}" for p in pairs)
    body = (ASSETS / "price_chart.js").read_text(encoding="utf-8")
    return f"{lib[:match.start()]}\nconst LWC = {{{exports}}};\n{body}\n"


# Declared once at import time (re-registering the name inside a function misbehaves).
_PRICE_CHART = st.components.v2.component("stocks_price_chart", html=_HTML, css=_CSS, js=_script())


def price_chart(payload: dict, *, chart_id: str, key: str, focus: dict | None = None):
    """Candlestick chart with trade markers. `chart_id` changes when the data does (a new run or a
    different symbol); the chart keeps its zoom across reruns while the id stays the same. An RSI
    pane is added under the candles when the payload carries `rsi`.

    `focus` is `{"n": trade number, "seq": counter}`: the chart zooms to that trade, enlarges its
    markers and scrolls into view once per `seq`, so reruns do not undo the user's own zooming."""
    height = CHART_HEIGHT_RSI if payload.get("rsi") else CHART_HEIGHT
    return _PRICE_CHART(data={**payload, "id": chart_id, "height": height, "focus": focus}, key=key, height=height)
