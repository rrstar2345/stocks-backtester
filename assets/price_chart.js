// Price chart component body. src/chart_component.py prepends the vendored Lightweight Charts
// module and an `LWC` object holding its public exports.
const { createChart, CandlestickSeries, createSeriesMarkers, CrosshairMode, ColorType } = LWC
//
// Time axis: each bar's time is its index (times 60 s, as the library wants UTC seconds), so
// overnight and holiday gaps are not drawn. `data.t` holds a label per bar for ticks and the crosshair.

const STATE = new WeakMap()
const STEP = 60

function token(host, name, fallback) {
  const v = getComputedStyle(host).getPropertyValue(name).trim()
  return v && v !== "unset" ? v : fallback
}

function readTheme(host) {
  return {
    text: token(host, "--st-text-color", "#262730"),
    grid: token(host, "--st-border-color-light", "#e6e9ef"),
    border: token(host, "--st-border-color", "#d5dae5"),
    up: token(host, "--st-green-color", "#16a34a"),
    down: token(host, "--st-red-color", "#dc2626"),
    flat: token(host, "--st-yellow-color", "#f59e0b"),
    entry: token(host, "--st-blue-color", "#2563eb"),
    font: token(host, "--st-font", "sans-serif"),
  }
}

function tickLabel(labels, time, type) {
  const label = labels[Math.round(time / STEP)]
  if (!label) return null
  const [day, clock] = label.split(" ")
  const idx = Math.round(time / STEP)
  const prevDay = idx > 0 ? labels[idx - 1].split(" ")[0] : null
  if (!clock || day !== prevDay) {
    const d = new Date(day + "T00:00:00Z")
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" })
  }
  return clock
}

function markerList(data, theme) {
  const color = { entry: theme.entry, pos: theme.up, neg: theme.down, flat: theme.flat }
  return data.markers.map((m) => ({
    time: m.i * STEP, position: m.pos, shape: m.shape, color: color[m.tone] ?? theme.text,
    text: m.text, size: 1,
  }))
}

function build(root, tip, data, theme) {
  const labels = data.t
  const chart = createChart(root, {
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: theme.text, fontFamily: theme.font },
    grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: theme.border },
    timeScale: {
      borderColor: theme.border, timeVisible: true, rightOffset: 4,
      tickMarkFormatter: (time, type) => tickLabel(labels, time, type),
    },
    localization: { timeFormatter: (time) => labels[Math.round(time / STEP)] ?? "" },
  })
  const series = chart.addSeries(CandlestickSeries, {
    upColor: theme.up, downColor: theme.down, borderVisible: false,
    wickUpColor: theme.up, wickDownColor: theme.down,
  })
  series.setData(labels.map((_, i) => ({
    time: i * STEP, open: data.o[i], high: data.h[i], low: data.l[i], close: data.c[i],
  })))
  const markers = createSeriesMarkers(series, markerList(data, theme))

  // Tooltip for the trades marked on the hovered bar.
  const byBar = new Map()
  data.markers.forEach((m) => {
    if (!byBar.has(m.i)) byBar.set(m.i, new Set())
    byBar.get(m.i).add(m.trade)
  })
  chart.subscribeCrosshairMove((param) => {
    const trades = param.time !== undefined && param.point ? byBar.get(Math.round(param.time / STEP)) : null
    if (!trades || !trades.size) { tip.hidden = true; return }
    tip.replaceChildren()
    for (const k of trades) {
      const info = data.tips[k]
      const box = document.createElement("div")
      box.className = "trade"
      for (const [label, value] of info.rows) {
        const row = document.createElement("div")
        const a = document.createElement("span")
        const b = document.createElement("span")
        a.className = "k"; a.textContent = label
        b.className = "v"; b.textContent = value
        row.append(a, b)
        box.append(row)
      }
      tip.append(box)
    }
    tip.hidden = false
    const pad = 16, w = tip.offsetWidth, h = tip.offsetHeight
    const x = param.point.x + pad + w > root.clientWidth ? param.point.x - pad - w : param.point.x + pad
    const y = Math.min(Math.max(param.point.y - h / 2, 0), Math.max(root.clientHeight - h, 0))
    tip.style.left = `${Math.max(x, 0)}px`
    tip.style.top = `${y}px`
  })

  chart.timeScale().fitContent()
  return { chart, series, markers }
}

export default function (component) {
  const { data, parentElement } = component
  const host = parentElement.host ?? parentElement
  const root = parentElement.querySelector("#chart")
  const tip = parentElement.querySelector("#tip")
  if (!root || !tip || !data) return

  const theme = readTheme(host)
  let state = STATE.get(parentElement)
  if (state && state.id !== data.id) {
    state.chart.remove()
    state = null
  }
  if (!state) {
    state = { id: data.id, ...build(root, tip, data, theme) }
    STATE.set(parentElement, state)
  } else {
    // Same data, new theme (or just a rerun): recolor without resetting the zoom.
    state.chart.applyOptions({
      layout: { textColor: theme.text, fontFamily: theme.font },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      rightPriceScale: { borderColor: theme.border },
      timeScale: { borderColor: theme.border },
    })
    state.series.applyOptions({
      upColor: theme.up, downColor: theme.down, wickUpColor: theme.up, wickDownColor: theme.down,
    })
    state.markers.setMarkers(markerList(data, theme))
  }

  return () => {
    const s = STATE.get(parentElement)
    if (s) { s.chart.remove(); STATE.delete(parentElement) }
  }
}
