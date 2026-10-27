"use strict";

/* polysim dashboard. Vanilla JS, no build step. All data comes from the local /api. */

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");

const state = {
  view: "markets",
  meta: null,
  query: "",
  tag: "all",
  events: null,
  eventsError: null,
  drawer: null,
  form: { strategy: "complementary", data: "synthetic", hours: 6, seed: 42, stress: true, params: {} },
  result: null,
  running: false,
  runError: null,
  history: [],
  subtab: "trades",
  compare: null,
  compareBusy: false,
  compareError: null,
  paper: { sessions: null, selected: null, detail: null, error: null },
};
let paperTimer = null;

/* ---- helpers ------------------------------------------------------------- */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function request(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const fmt = {
  usd(v, signed = false) {
    if (v === null || v === undefined || Number.isNaN(v)) return "-";
    const abs = Math.abs(v);
    const body = abs >= 1e6 ? `${(abs / 1e6).toFixed(2)}M` : abs.toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    const sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
    return `${sign}$${body}`;
  },
  vol(v) {
    if (!v) return "$0 Vol.";
    if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M Vol.`;
    if (v >= 1e3) return `$${Math.round(v / 1e3)}K Vol.`;
    return `$${Math.round(v)} Vol.`;
  },
  pct(v, digits = 1) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "-";
    return `${(v * 100).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
  },
  chance(p) {
    if (p === null || p === undefined) return "-";
    if (p < 0.005) return "<1%";
    if (p > 0.995) return ">99%";
    return `${Math.round(p * 100)}%`;
  },
  cents(p) {
    if (p === null || p === undefined) return "-";
    const c = p * 100;
    if (c < 0.1) return "<0.1¢";
    return `${c >= 10 ? c.toFixed(1).replace(/\.0$/, "") : c.toFixed(1)}¢`;
  },
  num(v, digits = 2) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "-";
    return v.toLocaleString("en-US", { maximumFractionDigits: digits });
  },
  date(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  },
  ago(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  },
  clock(ms) {
    return new Date(ms).toLocaleTimeString("en-US", { hour12: false });
  },
  elapsed(ms) {
    const total = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(total / 3600), m = Math.floor(total / 60) % 60, sec = total % 60;
    return `+${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  },
};

function signClass(v) {
  return v > 0 ? "yes" : v < 0 ? "no" : "";
}

function pretty(name) {
  const words = name.replace(/[_-]/g, " ").replace(/\busd\b/g, "USD").replace(/\bms\b/g, "(ms)");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function dataTag(label) {
  return label === "synthetic"
    ? `<span class="tag tag-synthetic">SYNTHETIC DATA</span>`
    : `<span class="tag tag-recorded">RECORDED DATA</span>`;
}

function iconFor(src, title) {
  const letter = esc((title || "?").trim().charAt(0).toUpperCase());
  if (!src) return `<div class="icon">${letter}</div>`;
  return `<img class="icon" src="${esc(src)}" alt="" loading="lazy"
    onerror="this.outerHTML='<div class=&quot;icon&quot;>${letter}</div>'">`;
}

/* ---- theme ----------------------------------------------------------------- */

function currentTheme() {
  return document.documentElement.dataset.theme || "auto";
}

function setTheme(next) {
  if (next === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = next;
  try { localStorage.setItem("pmsim-theme", next); } catch (e) { /* storage may be blocked */ }
  $("#theme-btn").textContent = `Theme: ${next}`;
  if (state.view === "backtest" && state.result) drawEquity();
}

$("#theme-btn").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  setTheme(order[(order.indexOf(currentTheme()) + 1) % order.length]);
});
$("#theme-btn").textContent = `Theme: ${currentTheme()}`;

/* ---- routing ---------------------------------------------------------------- */

function go(view) {
  closeDrawer();
  state.view = view;
  if (parseHash().view !== view) history.replaceState(null, "", `#${view}`);
  document.querySelectorAll(".tab").forEach((t) => {
    t.setAttribute("aria-selected", String(t.dataset.view === view));
  });
  clearInterval(paperTimer);
  paperTimer = null;
  if (view === "paper") startPaperPolling();
  render();
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => go(t.dataset.view)));
window.addEventListener("hashchange", () => {
  const { view } = parseHash();
  if (view !== state.view) go(view);
});

let searchTimer = null;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = e.target.value.trim();
    state.tag = "all";
    if (state.view !== "markets") go("markets");
    loadEvents();
  }, 220);
});

function render() {
  const views = { markets: viewMarkets, backtest: viewBacktest, compare: viewCompare, paper: viewPaper };
  app.innerHTML = `<div class="view">${views[state.view]()}</div>`;
  if (state.view === "backtest" && state.result) drawEquity();
}

/* ---- markets ----------------------------------------------------------------- */

async function loadEvents() {
  state.events = null;
  state.eventsError = null;
  if (state.view === "markets") render();
  try {
    const q = encodeURIComponent(state.query);
    state.events = await request(`/api/events?limit=120&q=${q}`);
  } catch (err) {
    state.eventsError = err.message;
  }
  if (state.view === "markets") render();
}

function gauge(p) {
  const v = Math.min(1, Math.max(0, p ?? 0));
  const r = 26, cx = 32, cy = 32;
  const angle = Math.PI * (1 - v);
  const x = cx + r * Math.cos(angle), y = cy - r * Math.sin(angle);
  const color = v >= 0.5 ? "var(--yes)" : "var(--no)";
  return `<div class="gauge">
    <svg viewBox="0 0 64 36" aria-hidden="true">
      <path d="M6 32 A26 26 0 0 1 58 32" fill="none" stroke="var(--line)" stroke-width="5" stroke-linecap="round"/>
      ${v > 0.005 ? `<path d="M6 32 A26 26 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)}" fill="none"
        stroke="${color}" stroke-width="5" stroke-linecap="round"/>` : ""}
    </svg>
    <div class="gauge-value num">${fmt.chance(p)}</div>
    <div class="gauge-label">chance</div>
  </div>`;
}

function eventCard(ev, index) {
  const single = ev.markets.length === 1;
  const m0 = ev.markets[0];
  const recorded = ev.markets.some((m) => m.recorded);
  let body;
  if (single) {
    const [yesName, noName] = m0.outcomes;
    body = `<div class="yesno">
      <span class="y"><em>${esc(yesName)}</em> <b class="num">${fmt.cents(m0.yes_price)}</b></span>
      <span class="n"><em>${esc(noName)}</em> <b class="num">${fmt.cents(m0.no_price)}</b></span>
    </div>`;
  } else {
    const rows = ev.markets.slice(0, 3).map((m) => `
      <div class="outcome-row">
        <span class="label">${esc(m.label)}</span>
        <span class="pct num">${fmt.chance(m.yes_price)}</span>
        <span class="mini"><span class="tag-yes">Yes</span><span class="tag-no">No</span></span>
      </div>`).join("");
    const more = ev.markets.length > 3 ? `<span class="muted" style="font-size:12px">+${ev.markets.length - 3} more</span>` : "";
    body = `<div class="outcome-rows">${rows}${more}</div>`;
  }
  return `<button class="card" data-event="${index}" type="button">
    <div class="card-top">
      ${iconFor(ev.icon, ev.title)}
      <div class="card-title">${esc(ev.title)}</div>
      ${single ? gauge(m0.yes_price) : ""}
    </div>
    ${body}
    <div class="card-foot">
      <span class="num">${fmt.vol(ev.volume_24h)} 24h</span>
      <span>${recorded ? `<span class="tag tag-recorded">Book recorded</span>` : esc(fmt.date(ev.end))}</span>
    </div>
  </button>`;
}

function viewMarkets() {
  if (state.eventsError) return `<div class="error">${esc(state.eventsError)}</div>`;
  if (!state.events) {
    const sk = Array.from({ length: 9 }, () => `<div class="skeleton" style="height:188px"></div>`).join("");
    return `<div class="markets-head"><h1>Markets</h1></div><div class="grid">${sk}</div>`;
  }
  const all = state.events.events;
  if (!all.length) {
    return state.query
      ? `<div class="empty"><h2>No markets match "${esc(state.query)}"</h2><p>Try a shorter search or clear it.</p></div>`
      : `<div class="empty"><h2>No market catalog yet</h2>
          <p>Fetch market metadata from Polymarket's public API to fill this page.</p>
          <code>uv run pmsim fetch --limit 500</code></div>`;
  }
  const counts = new Map();
  all.forEach((e) => e.tags.forEach((t) => counts.set(t, (counts.get(t) || 0) + 1)));
  const tags = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([t]) => t);
  const shown = state.tag === "all" ? all
    : state.tag === "recorded" ? all.filter((e) => e.markets.some((m) => m.recorded))
    : all.filter((e) => e.tags.includes(state.tag));
  const chip = (id, label) => `<button class="chip" data-tag="${esc(id)}" aria-pressed="${state.tag === id}">${esc(label)}</button>`;
  const fetched = state.events.fetched ? `Prices as of ${fmt.ago(state.events.fetched)}` : "";
  return `
    <div class="markets-head">
      <h1>${state.query ? `Results for "${esc(state.query)}"` : "Markets"}</h1>
      <span class="muted" style="font-size:12px">${state.events.total} events. ${fetched}</span>
    </div>
    <div class="chips">${chip("all", "All")}${chip("recorded", "Recorded books")}${tags.map((t) => chip(t, pretty(t))).join("")}</div>
    <div class="grid">${shown.map((e) => eventCard(e, all.indexOf(e))).join("")}</div>
    ${shown.length ? "" : `<div class="empty"><h2>Nothing in this category</h2></div>`}`;
}

app.addEventListener("click", (e) => {
  const tag = e.target.closest("[data-tag]");
  if (tag) { state.tag = tag.dataset.tag; render(); return; }
  const card = e.target.closest("[data-event]");
  if (card) openDrawer(state.events.events[Number(card.dataset.event)]);
});

/* ---- market drawer with recorded order book -------------------------------------- */

function openDrawer(ev) {
  const first = ev.markets.find((m) => m.recorded) || ev.markets[0];
  state.drawer = { ev, market: first, side: "yes", book: null, bookError: null };
  renderDrawer();
  loadBook();
}

function closeDrawer() {
  state.drawer = null;
  $("#drawer-root").innerHTML = "";
}

async function loadBook() {
  const d = state.drawer;
  if (!d) return;
  d.book = null;
  d.bookError = null;
  const token = d.side === "yes" ? d.market.yes_token : d.market.no_token;
  if (!d.market.recorded || !token) {
    d.bookError = "No recorded book for this market yet.";
    renderDrawer();
    return;
  }
  renderDrawer();
  try {
    d.book = await request(`/api/book?token=${encodeURIComponent(token)}`);
  } catch (err) {
    d.bookError = err.message;
  }
  if (state.drawer === d) renderDrawer();
}

function bookLadder(book) {
  const maxTotal = Math.max(book.asks.at(-1)?.total || 0, book.bids.at(-1)?.total || 0) || 1;
  const row = (lv, kind) => `<div class="book-row ${kind}">
      <div class="bar" style="width:${(lv.total / maxTotal * 100).toFixed(1)}%"></div>
      <span class="num ${kind === "ask" ? "no" : "yes"}">${fmt.cents(lv.price)}</span>
      <span class="num r">${fmt.num(lv.shares)}</span>
      <span class="num r">${fmt.usd(lv.total)}</span>
    </div>`;
  const asks = [...book.asks].reverse().map((l) => row(l, "ask")).join("");
  const bids = book.bids.map((l) => row(l, "bid")).join("");
  const last = book.last ? `Last ${fmt.cents(book.last.price)}` : "No trades recorded";
  const spread = book.spread === null ? "One-sided book" : `Spread ${fmt.cents(book.spread)}`;
  return `<div class="book">
    <div class="book-head"><span>Price</span><span class="r" style="text-align:right">Shares</span><span style="text-align:right">Total</span></div>
    ${asks || `<div class="muted" style="padding:8px 12px">No asks</div>`}
    <div class="book-mid"><span>${last}</span><span>${spread}</span></div>
    ${bids || `<div class="muted" style="padding:8px 12px">No bids</div>`}
  </div>`;
}

function renderDrawer() {
  const d = state.drawer;
  if (!d) return;
  const { ev, market } = d;
  const outcomes = ev.markets.map((m, i) => `
    <button class="outcome-item" data-market="${i}" aria-pressed="${m === market}" type="button">
      <span><span class="label" style="font-weight:600">${esc(m.label)}</span><br>
        <span class="muted" style="font-size:12px">${m.recorded ? "Recorded book available" : esc(m.status)}</span></span>
      <span class="big num">${fmt.chance(m.yes_price)}</span>
      <span class="mini"><span class="tag-yes">${fmt.cents(m.yes_price)}</span><span class="tag-no">${fmt.cents(m.no_price)}</span></span>
    </button>`).join("");
  let bookHtml;
  if (d.bookError) bookHtml = `<p class="muted" style="margin:0">${esc(d.bookError)}</p>
      <p class="muted" style="margin:8px 0 0;font-size:12px">Record it with <span class="num">pmsim record --markets "${esc(ev.tags[0] ? "tag:" + ev.tags[0] : "slug")}" --hours 1</span>.</p>`;
  else if (!d.book) bookHtml = `<div class="skeleton" style="height:260px"></div>`;
  else bookHtml = bookLadder(d.book) + `<p class="muted" style="font-size:12px;margin:10px 0 0">Book as of ${esc(new Date(d.book.as_of).toLocaleString())}, end of the recording.</p>`;
  const [yesName, noName] = market.outcomes;
  $("#drawer-root").innerHTML = `
    <div class="overlay" data-close></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="${esc(ev.title)}">
      <div class="drawer-head">
        ${iconFor(ev.icon, ev.title)}
        <div><h2>${esc(ev.title)}</h2>
          <span class="muted num" style="font-size:12px">${fmt.vol(ev.volume)} total. ${esc(ev.end ? "Ends " + fmt.date(ev.end) : "")}</span></div>
        <button class="ghost-btn" data-close type="button">Close</button>
      </div>
      <div class="drawer-body">
        ${ev.markets.length > 1 ? `<div class="panel"><div class="panel-body outcome-list">${outcomes}</div></div>` : ""}
        <div class="panel">
          <div class="panel-head">
            <h3 class="panel-title">Order book${ev.markets.length > 1 ? `: ${esc(market.label)}` : ""}</h3>
            <div class="segmented" style="width:180px">
              <button data-side="yes" aria-pressed="${d.side === "yes"}">Trade ${esc(yesName)}</button>
              <button data-side="no" aria-pressed="${d.side === "no"}">Trade ${esc(noName)}</button>
            </div>
          </div>
          <div class="panel-body">${bookHtml}</div>
        </div>
        <div class="panel"><div class="panel-body stack">
          <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">
            <span class="muted">Taker fee rate</span><span class="num">${ev.fee_rate ? fmt.pct(ev.fee_rate, 0) + " of p(1-p)" : "Fee free"}</span>
          </div>
          <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">
            <span class="muted">Type</span><span>${ev.neg_risk ? "Neg-risk group (one outcome wins)" : "Binary market"}</span>
          </div>
          <button class="primary-btn" data-backtest type="button">Backtest on recorded data</button>
        </div></div>
      </div>
    </aside>`;
}

$("#drawer-root").addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) { closeDrawer(); return; }
  const d = state.drawer;
  if (!d) return;
  const mk = e.target.closest("[data-market]");
  if (mk) { d.market = d.ev.markets[Number(mk.dataset.market)]; loadBook(); return; }
  const side = e.target.closest("[data-side]");
  if (side) { d.side = side.dataset.side; loadBook(); return; }
  if (e.target.closest("[data-backtest]")) {
    closeDrawer();
    state.form.data = "recorded";
    go("backtest");
  }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ---- backtest ----------------------------------------------------------------------- */

function strategyMeta(name) {
  return state.meta?.strategies.find((s) => s.name === name);
}

function paramInputs(s) {
  return s.params.map((p) => {
    const value = state.form.params[s.name]?.[p.name];
    const id = `p-${p.name}`;
    if (p.type === "bool") {
      const checked = value ?? p.default;
      return `<label class="switch field" style="grid-column:1/-1">
        <span style="font-size:12px;font-weight:600;color:var(--muted)">${esc(pretty(p.name))}</span>
        <input type="checkbox" data-param="${p.name}" ${checked ? "checked" : ""}></label>`;
    }
    const bounds = ["ge", "gt", "le", "lt"].filter((b) => p[b] !== undefined)
      .map((b) => ({ ge: ">=", gt: ">", le: "<=", lt: "<" }[b] + " " + p[b])).join(", ");
    return `<div class="field"><label for="${id}">${esc(pretty(p.name))}</label>
      <input class="input" id="${id}" data-param="${p.name}" type="number" step="any"
        value="${esc(value ?? p.default)}">
      ${bounds ? `<span class="field-help">${esc(bounds)}</span>` : ""}</div>`;
  }).join("");
}

function viewBacktest() {
  if (!state.meta) return `<div class="skeleton" style="height:480px"></div>`;
  const f = state.form;
  const s = strategyMeta(f.strategy);
  const rec = state.meta.recorded;
  const strategies = state.meta.strategies.map((x) => `
    <button class="strategy" data-strategy="${x.name}" aria-pressed="${x.name === f.strategy}" type="button">
      <b>${esc(x.title)}</b><span>${esc(x.blurb)}</span></button>`).join("");
  const synthetic = f.data === "synthetic";
  const history = state.history.map((h, i) => `
    <button data-history="${i}" type="button">
      <span>${esc(strategyMeta(h.strategy)?.title || h.strategy)}<br>
        <span class="muted">${h.label}${h.data.hours ? `, ${h.data.hours} h, seed ${h.data.seed}` : ""}</span></span>
      <span class="num ${signClass(h.metrics.net_pnl_usd)}">${fmt.usd(h.metrics.net_pnl_usd, true)}</span>
    </button>`).join("");
  return `<div class="bt">
    <aside class="sidebar">
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Strategy</h2></div>
        <div class="panel-body strategy-list">${strategies}</div></div>
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Data</h2></div>
        <div class="panel-body stack">
          <div class="segmented">
            <button data-source="synthetic" aria-pressed="${synthetic}" type="button">Synthetic</button>
            <button data-source="recorded" aria-pressed="${!synthetic}" ${rec.available ? "" : "disabled"} type="button">Recorded</button>
          </div>
          ${synthetic ? `<div class="params">
            <div class="field"><label for="hours">Hours</label>
              <input class="input" id="hours" data-form="hours" type="number" min="0.25" max="72" step="0.25" value="${f.hours}"></div>
            <div class="field"><label for="seed">Seed</label>
              <input class="input" id="seed" data-form="seed" type="number" min="0" step="1" value="${f.seed}"></div>
          </div>` : `<p class="muted" style="margin:0;font-size:12px">${rec.markets} markets in the catalog,
            ${fmt.num(Object.values(rec.rows).reduce((a, b) => a + b, 0), 0)} recorded rows.</p>`}
          ${rec.available ? "" : `<p class="field-help" style="margin:0">Record live books with <span class="num">pmsim record</span> to unlock recorded data.</p>`}
        </div></div>
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Parameters</h2>
          <button class="ghost-btn" data-reset type="button" style="height:28px;font-size:12px">Reset</button></div>
        <div class="panel-body params">${s ? paramInputs(s) : ""}</div></div>
      <div class="panel"><div class="panel-body stack">
        <label class="switch"><span><b style="font-size:13px">Stress test</b><br>
          <span class="muted" style="font-size:12px">Also run 3x latency, half depth, double fees</span></span>
          <input type="checkbox" data-form="stress" ${f.stress ? "checked" : ""}></label>
        <button class="primary-btn" id="run-btn" type="button" ${state.running ? "disabled" : ""}>
          ${state.running ? "Running backtest" : "Run backtest"}</button>
      </div></div>
      ${history ? `<div class="panel"><div class="panel-head"><h2 class="panel-title">This session</h2></div>
        <div class="panel-body history">${history}</div></div>` : ""}
    </aside>
    <section class="results">${resultsHtml()}</section>
  </div>`;
}

function resultsHtml() {
  const err = state.runError ? `<div class="error">${esc(state.runError)}</div>` : "";
  if (state.running) {
    return err + `<div class="skeleton" style="height:40px;width:50%"></div>
      <div class="skeleton" style="height:96px"></div>
      <div class="skeleton" style="height:300px"></div>
      <div class="two"><div class="skeleton" style="height:200px"></div><div class="skeleton" style="height:200px"></div></div>`;
  }
  const r = state.result;
  if (!r) {
    return err + `<div class="panel"><div class="empty">
      <h2>Run a backtest</h2>
      <p>Pick a strategy on the left, choose synthetic or recorded data, and run it. Results show PnL,
        risk, stress tests and every trade.</p></div></div>`;
  }
  const m = r.metrics;
  const meta = strategyMeta(r.strategy);
  const dataLine = r.data.kind === "synthetic" ? `${r.data.hours} h, seed ${r.data.seed}` : "recorded books";
  return err + `
    <div class="result-head">
      <h1>${esc(meta?.title || r.strategy)}</h1>${dataTag(r.label)}
      <span class="muted" style="font-size:12px">${esc(dataLine)}. Ran in ${r.elapsed_s}s.</span>
    </div>
    <div class="kpis">
      <div class="kpi hero"><span class="k">Net PnL</span>
        <span class="v ${signClass(m.net_pnl_usd)}">${fmt.usd(m.net_pnl_usd, true)}</span>
        <span class="s">${fmt.pct(m.return_on_capital, 2)} on ${fmt.usd(r.start_cash)}</span></div>
      <div class="kpi"><span class="k">Sharpe</span><span class="v">${fmt.num(m.sharpe, 2)}</span>
        <span class="s">annualized</span></div>
      <div class="kpi"><span class="k">Max drawdown</span><span class="v">${fmt.usd(m.max_drawdown_usd)}</span>
        <span class="s">${fmt.pct(m.max_drawdown_pct, 2)} of peak</span></div>
      <div class="kpi"><span class="k">Hit rate</span><span class="v">${fmt.pct(m.hit_rate, 0)}</span>
        <span class="s">${m.closed_positions} closed positions</span></div>
    </div>
    <div class="panel">
      <div class="panel-head"><h2 class="panel-title">Equity</h2>
        <span class="muted num" style="font-size:12px">Fill rate ${fmt.pct(m.fill_rate, 0)}. Idle cash ${fmt.pct(m.idle_cash_fraction, 0)}.</span></div>
      <div class="panel-body"><div class="chart" id="equity"></div></div>
    </div>
    <div class="two">${stressPanel(r)}${edgePanel(r)}</div>
    <div class="panel">
      <div class="subtabs" role="tablist">
        <button class="subtab" data-subtab="trades" aria-selected="${state.subtab === "trades"}">Trades (${r.fill_count})</button>
        <button class="subtab" data-subtab="issues" aria-selected="${state.subtab === "issues"}">Issues (${r.failures.length})</button>
      </div>
      <div class="panel-body">${state.subtab === "trades" ? tradesTable(r) : issuesList(r)}</div>
    </div>`;
}

function stressPanel(r) {
  if (!r.stress) {
    return `<div class="panel"><div class="panel-head"><h2 class="panel-title">Stress test</h2></div>
      <div class="panel-body"><p class="muted" style="margin:0">Turn on the stress test to rerun this strategy
      with 3x latency, half the book depth and double fees. It passes only if every run stays positive.</p></div></div>`;
  }
  const rows = r.stress.rows;
  const max = Math.max(...rows.map((x) => Math.abs(x.net_pnl)), 1e-9);
  const names = { base: "Base", latency_x3: "Latency x3", "depth_x0.5": "Depth x0.5", fees_x2: "Fees x2" };
  const bars = rows.map((x) => {
    const w = Math.abs(x.net_pnl) / max * 50;
    const left = x.net_pnl >= 0 ? 50 : 50 - w;
    const color = x.net_pnl >= 0 ? "var(--yes)" : "var(--no)";
    return `<div class="stress-row"><span>${names[x.stress] || x.stress}</span>
      <div class="stress-track"><div class="stress-zero"></div>
        <div class="stress-bar" style="left:${left}%;width:${Math.max(w, 0.4)}%;background:${color}"></div></div>
      <span class="num ${signClass(x.net_pnl)}" style="text-align:right">${fmt.usd(x.net_pnl, true)}</span></div>`;
  }).join("");
  const verdict = r.stress.passed
    ? `<span class="tag tag-yes">PASSED</span>` : `<span class="tag tag-no">FAILED</span>`;
  return `<div class="panel"><div class="panel-head"><h2 class="panel-title">Stress test</h2>${verdict}</div>
    <div class="panel-body">${bars}
      ${r.label === "synthetic" ? `<p class="field-help" style="margin:10px 0 0">Synthetic result. It shows the simulator works, not that the strategy makes money.</p>` : ""}
    </div></div>`;
}

function edgePanel(r) {
  const m = r.metrics;
  const theory = m.theoretical_edge_usd, real = m.net_pnl_usd;
  const max = Math.max(Math.abs(theory), Math.abs(real), 1e-9);
  const bar = (v, color) => `<div class="edge-bar" style="width:${Math.max(Math.abs(v) / max * 100, 0.6)}%;background:${color}"></div>`;
  const capture = theory ? fmt.pct(real / theory, 0) : "-";
  const legs = r.legs;
  return `<div class="panel"><div class="panel-head"><h2 class="panel-title">Theory vs realized edge</h2>
      <span class="muted num" style="font-size:12px">Capture ${capture}</span></div>
    <div class="panel-body">
      <div class="edge-row"><span class="muted">Theoretical</span>${bar(theory, "var(--accent)")}<span class="num" style="text-align:right">${fmt.usd(theory, true)}</span></div>
      <div class="edge-row"><span class="muted">Realized</span>${bar(real, real >= 0 ? "var(--yes)" : "var(--no)")}<span class="num ${signClass(real)}" style="text-align:right">${fmt.usd(real, true)}</span></div>
      <div class="facts">
        <div><span class="k">Leg groups</span><span class="v">${legs ? `${legs.complete}/${legs.groups}` : "-"}</span></div>
        <div><span class="k">Unwind cost</span><span class="v">${fmt.usd(m.unwind_cost_usd)}</span></div>
        <div><span class="k">Resolution losses</span><span class="v">${m.resolution_losses}</span></div>
        <div><span class="k">Ann. return, locked</span><span class="v">${fmt.pct(m.annualized_return_on_locked, 0)}</span></div>
        <div><span class="k">Avg edge per entry</span><span class="v">${fmt.usd(m.avg_edge_per_entry_usd)}</span></div>
        <div><span class="k">Orders</span><span class="v">${r.orders}</span></div>
      </div>
    </div></div>`;
}

function tradesTable(r) {
  if (!r.fills.length) return `<p class="muted" style="margin:0">No fills in this run.</p>`;
  const rows = r.fills.map((f) => `<tr>
      <td class="num muted">${fmt.elapsed(f.ts - r.t0)}</td>
      <td class="truncate" title="${esc(f.token)}">${esc(f.token)}</td>
      <td><span class="tag ${f.side === "BUY" ? "tag-yes" : "tag-no"}">${f.side}</span></td>
      <td class="num right">${fmt.cents(f.price)}</td>
      <td class="num right">${fmt.num(f.size)}</td>
      <td class="muted">${f.liquidity}</td>
      <td class="num right">${fmt.usd(f.fee)}</td></tr>`).join("");
  const more = r.fill_count > r.fills.length ? `<p class="field-help">${r.fill_count - r.fills.length} more fills not shown.</p>` : "";
  return `<div class="table-wrap"><table><thead><tr><th>Elapsed</th><th>Token</th><th>Side</th>
    <th class="right">Price</th><th class="right">Shares</th><th>Liquidity</th><th class="right">Fee</th></tr></thead>
    <tbody>${rows}</tbody></table></div>${more}`;
}

function issuesList(r) {
  if (!r.failures.length) return `<p class="muted" style="margin:0">No rejects or resolution events.</p>`;
  return `<div class="feed">${r.failures.map((d) => `
    <div class="feed-item"><span class="num muted">${fmt.elapsed(d.ts - r.t0)}</span>
      <span><span class="tag ${d.kind === "reject" ? "tag-no" : ""}">${esc(d.kind)}</span></span>
      <span><b>${esc(d.reason)}</b><br><span class="detail">${esc(d.detail)}</span></span></div>`).join("")}</div>`;
}

function drawEquity() {
  const el = $("#equity");
  const r = state.result;
  if (!el || !r) return;
  const pts = r.equity;
  const w = el.clientWidth, h = el.clientHeight;
  if (!pts.length || w === 0) { el.innerHTML = `<p class="muted">No equity samples.</p>`; return; }
  const pad = { l: 64, r: 12, t: 10, b: 24 };
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]).concat([r.start_cash]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs) || 1;
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1e-6) { y0 -= 1; y1 += 1; }
  const span = y1 - y0; y0 -= span * 0.08; y1 += span * 0.08;
  const sx = (x) => pad.l + (x - x0) / (x1 - x0 || 1) * (w - pad.l - pad.r);
  const sy = (y) => pad.t + (1 - (y - y0) / (y1 - y0)) * (h - pad.t - pad.b);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
  const area = `${line}L${sx(pts.at(-1)[0]).toFixed(1)},${h - pad.b}L${sx(pts[0][0]).toFixed(1)},${h - pad.b}Z`;
  const ticks = Array.from({ length: 4 }, (_, i) => y0 + (y1 - y0) * (i + 0.5) / 4);
  const xticks = Array.from({ length: 5 }, (_, i) => x0 + (x1 - x0) * i / 4);
  const up = pts.at(-1)[1] >= r.start_cash;
  const stroke = "var(--accent)";
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Equity curve, ${up ? "ending above" : "ending below"} starting cash">
    <defs><linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${stroke}" stop-opacity="0.22"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>
    ${ticks.map((t) => `<line class="grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${sy(t)}" y2="${sy(t)}"/>
      <text class="axis" x="${pad.l - 8}" y="${sy(t) + 4}" text-anchor="end">${fmt.usd(t)}</text>`).join("")}
    ${xticks.map((t) => `<text class="axis" x="${sx(t)}" y="${h - 6}" text-anchor="middle">${t.toFixed(1)}h</text>`).join("")}
    <line class="base" x1="${pad.l}" x2="${w - pad.r}" y1="${sy(r.start_cash)}" y2="${sy(r.start_cash)}"/>
    <path d="${area}" fill="url(#eqfill)"/>
    <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/>
    <line id="eq-cross" y1="${pad.t}" y2="${h - pad.b}" stroke="var(--line-2)" visibility="hidden"/>
    <circle id="eq-dot" r="4" fill="${stroke}" stroke="var(--card)" stroke-width="2" visibility="hidden"/>
    <rect x="${pad.l}" y="0" width="${w - pad.l - pad.r}" height="${h}" fill="transparent" id="eq-hit"/>
  </svg><div class="tooltip hidden" id="eq-tip"></div>`;
  const hit = $("#eq-hit", el), cross = $("#eq-cross", el), dot = $("#eq-dot", el), tip = $("#eq-tip", el);
  hit.addEventListener("mousemove", (e) => {
    const box = el.getBoundingClientRect();
    const mx = e.clientX - box.left;
    let best = 0;
    pts.forEach((p, i) => { if (Math.abs(sx(p[0]) - mx) < Math.abs(sx(pts[best][0]) - mx)) best = i; });
    const p = pts[best], px = sx(p[0]), py = sy(p[1]);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("visibility", "visible");
    tip.classList.remove("hidden");
    tip.style.left = `${px}px`; tip.style.top = `${py}px`;
    tip.textContent = `${p[0].toFixed(2)}h  ${fmt.usd(p[1])}  (${fmt.usd(p[1] - r.start_cash, true)})`;
  });
  hit.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.classList.add("hidden");
  });
}

function readParams() {
  const s = strategyMeta(state.form.strategy);
  const out = {};
  document.querySelectorAll("[data-param]").forEach((input) => {
    const spec = s.params.find((p) => p.name === input.dataset.param);
    if (!spec) return;
    out[spec.name] = spec.type === "bool" ? input.checked
      : input.value === "" ? null : Number(input.value);
  });
  state.form.params[s.name] = out;
  return out;
}

async function runBacktest() {
  const f = state.form;
  const params = readParams();
  state.running = true;
  state.runError = null;
  render();
  try {
    const result = await request("/api/run", {
      strategy: f.strategy, data: f.data, hours: f.hours, seed: f.seed, stress: f.stress, params,
    });
    state.result = result;
    state.subtab = "trades";
    state.history.unshift(result);
    state.history = state.history.slice(0, 8);
  } catch (err) {
    state.runError = err.message;
  }
  state.running = false;
  if (state.view === "backtest") {
    render();
    if (window.innerWidth <= 1080) $(".results")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

app.addEventListener("click", (e) => {
  if (state.view !== "backtest") return;
  const st = e.target.closest("[data-strategy]");
  if (st) { readParams(); state.form.strategy = st.dataset.strategy; render(); return; }
  const src = e.target.closest("[data-source]");
  if (src && !src.disabled) { readParams(); state.form.data = src.dataset.source; render(); return; }
  if (e.target.closest("[data-reset]")) { delete state.form.params[state.form.strategy]; render(); return; }
  if (e.target.closest("#run-btn")) { runBacktest(); return; }
  const sub = e.target.closest("[data-subtab]");
  if (sub) { state.subtab = sub.dataset.subtab; render(); return; }
  const h = e.target.closest("[data-history]");
  if (h) { state.result = state.history[Number(h.dataset.history)]; render(); }
});

app.addEventListener("change", (e) => {
  const key = e.target.dataset.form;
  if (!key) return;
  if (key === "stress") state.form.stress = e.target.checked;
  else state.form[key] = Number(e.target.value);
});

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.view === "backtest") drawEquity(); }, 120);
});

/* ---- compare ------------------------------------------------------------------------- */

function viewCompare() {
  const c = state.compare;
  const rec = state.meta?.recorded;
  const f = state.form;
  const controls = `<div class="controls">
    <div class="field"><label>Data</label>
      <div class="segmented" style="width:220px">
        <button data-cmp-source="synthetic" aria-pressed="${f.data === "synthetic"}" type="button">Synthetic</button>
        <button data-cmp-source="recorded" aria-pressed="${f.data === "recorded"}" ${rec?.available ? "" : "disabled"} type="button">Recorded</button>
      </div></div>
    ${f.data === "synthetic" ? `<div class="field"><label for="c-hours">Hours</label>
      <input class="input" id="c-hours" data-form="hours" type="number" min="0.25" max="72" step="0.25" value="${f.hours}"></div>
    <div class="field"><label for="c-seed">Seed</label>
      <input class="input" id="c-seed" data-form="seed" type="number" min="0" value="${f.seed}"></div>` : ""}
    <button class="primary-btn" id="cmp-btn" type="button" ${state.compareBusy ? "disabled" : ""}>
      ${state.compareBusy ? "Comparing" : "Compare strategies"}</button>
  </div>`;
  const err = state.compareError ? `<div class="error">${esc(state.compareError)}</div>` : "";
  if (state.compareBusy) return controls + err + `<div class="skeleton" style="height:360px"></div>`;
  if (!c) {
    return controls + err + `<div class="panel"><div class="empty"><h2>Compare all six strategies</h2>
      <p>Runs every strategy with default parameters on the same data and lines up theoretical edge against
      what each one actually made.</p></div></div>`;
  }
  const max = Math.max(...c.rows.flatMap((r) => [Math.abs(r.theoretical), Math.abs(r.realized)]), 1e-9);
  const neg = c.rows.some((r) => r.realized < 0 || r.theoretical < 0);
  const zero = neg ? 50 : 0;
  const scale = neg ? 50 : 100;
  const bar = (v, cls, extra = "") => {
    const w = Math.abs(v) / max * scale;
    const left = v >= 0 ? zero : zero - w;
    return `<div class="bar ${cls}" style="left:${left}%;width:${Math.max(w, 0.4)}%;${extra}"></div>`;
  };
  const chart = c.rows.map((r) => `<div class="cmp-row">
      <span><b>${esc(r.title)}</b><br><span class="muted num" style="font-size:12px">${fmt.usd(r.realized, true)}</span></span>
      <div class="cmp-bars"><div class="zero" style="left:${zero}%"></div>
        ${bar(r.theoretical, "theory")}
        ${bar(r.realized, "real", `background:${r.realized >= 0 ? "var(--yes)" : "var(--no)"}`)}</div>
    </div>`).join("");
  const rows = c.rows.map((r) => `<tr>
      <td><b>${esc(r.title)}</b></td>
      <td class="num right">${fmt.usd(r.theoretical, true)}</td>
      <td class="num right ${signClass(r.realized)}">${fmt.usd(r.realized, true)}</td>
      <td class="num right">${r.theoretical ? fmt.pct(r.capture, 0) : "-"}</td>
      <td class="num right">${r.fills}</td>
      <td class="num right">${fmt.num(r.sharpe, 2)}</td>
      <td class="num right">${fmt.usd(r.max_drawdown)}</td>
      <td class="num right">${fmt.pct(r.hit_rate, 0)}</td></tr>`).join("");
  const label = c.rows[0]?.label;
  return controls + err + `
    <div class="result-head" style="margin-bottom:16px"><h1>Theory vs realized</h1>${dataTag(label)}
      <span class="muted" style="font-size:12px">Ran in ${c.elapsed_s}s.</span></div>
    <div class="panel" style="margin-bottom:16px"><div class="panel-head"><h2 class="panel-title">Edge by strategy</h2>
      <div class="legend"><span><i style="border:1.5px solid var(--accent);background:var(--accent-soft)"></i>Theoretical</span>
        <span><i style="background:var(--yes)"></i>Realized</span></div></div>
      <div class="panel-body cmp-chart">${chart}</div></div>
    <div class="panel"><div class="panel-body table-wrap" style="max-height:none"><table>
      <thead><tr><th>Strategy</th><th class="right">Theoretical</th><th class="right">Realized</th>
        <th class="right">Capture</th><th class="right">Fills</th><th class="right">Sharpe</th>
        <th class="right">Max drawdown</th><th class="right">Hit rate</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>
    ${label === "synthetic" ? `<p class="field-help" style="margin-top:12px">Synthetic data. Arbitrage profits here come from mispricings the generator injects on purpose.</p>` : ""}`;
}

async function runCompare() {
  state.compareBusy = true;
  state.compareError = null;
  render();
  try {
    state.compare = await request("/api/compare", { data: state.form.data, hours: state.form.hours, seed: state.form.seed });
  } catch (err) {
    state.compareError = err.message;
  }
  state.compareBusy = false;
  if (state.view === "compare") render();
}

app.addEventListener("click", (e) => {
  if (state.view !== "compare") return;
  const src = e.target.closest("[data-cmp-source]");
  if (src && !src.disabled) { state.form.data = src.dataset.cmpSource; render(); return; }
  if (e.target.closest("#cmp-btn")) runCompare();
});

/* ---- paper trading ---------------------------------------------------------------------- */

async function loadPaper() {
  const p = state.paper;
  try {
    p.sessions = (await request("/api/paper")).sessions;
    if (!p.selected && p.sessions.length) p.selected = p.sessions[0].name;
    p.detail = p.selected ? await request(`/api/paper/${encodeURIComponent(p.selected)}?tail=150`) : null;
    p.error = null;
  } catch (err) {
    p.error = err.message;
  }
  if (state.view === "paper") render();
}

function startPaperPolling() {
  loadPaper();
  paperTimer = setInterval(() => { if (!document.hidden) loadPaper(); }, 3000);
}

function viewPaper() {
  const p = state.paper;
  if (p.error) return `<div class="error">${esc(p.error)}</div>`;
  if (!p.sessions) return `<div class="skeleton" style="height:420px"></div>`;
  if (!p.sessions.length) {
    return `<div class="panel"><div class="empty"><h2>No paper sessions yet</h2>
      <p>Paper trading runs a strategy on the live Polymarket feed with simulated fills. Start one in a terminal
      and it shows up here, updating every few seconds.</p>
      <code>uv run pmsim paper --strategy market_making --markets "tag:fed" --hours 1</code></div></div>`;
  }
  const list = p.sessions.map((s) => `<button class="session" data-session="${esc(s.name)}" aria-pressed="${s.name === p.selected}" type="button">
      <b>${esc(s.name)}</b>
      <span class="num">${fmt.usd(s.cash)} cash</span>
      <span class="muted" style="font-size:12px">Updated ${fmt.ago(s.updated)}. ${s.open_positions} open.</span></button>`).join("");
  const d = p.detail;
  let detail = `<div class="skeleton" style="height:420px"></div>`;
  if (d) {
    const fresh = Date.now() - d.updated < 90_000;
    const positions = d.positions.length ? `<div class="table-wrap"><table><thead><tr><th>Token</th>
        <th class="right">Shares</th><th class="right">Avg price</th><th class="right">Cost</th><th class="right">Realized</th></tr></thead>
        <tbody>${d.positions.map((x) => `<tr><td class="truncate" title="${esc(x.token)}">${esc(x.token)}</td>
          <td class="num right">${fmt.num(x.shares)}</td><td class="num right">${fmt.cents(x.avg_price)}</td>
          <td class="num right">${fmt.usd(x.cost)}</td><td class="num right ${signClass(x.realized)}">${fmt.usd(x.realized, true)}</td></tr>`).join("")}
        </tbody></table></div>` : `<p class="muted" style="margin:0">No open positions.</p>`;
    const feed = d.decisions.length ? `<div class="feed">${d.decisions.map((x) => `
        <div class="feed-item"><span class="num muted">${fmt.clock(x.ts)}</span>
          <span><span class="tag ${x.kind === "fill" ? "tag-yes" : x.kind === "reject" ? "tag-no" : ""}">${esc(x.kind)}</span></span>
          <span><b>${esc(x.reason)}</b><br><span class="detail">${esc(x.detail)}</span></span></div>`).join("")}</div>`
      : `<p class="muted" style="padding:0 16px 16px;margin:0">No decisions logged yet.</p>`;
    detail = `<div class="results">
      <div class="result-head"><h1>${esc(d.name)}</h1><span class="tag tag-recorded">LIVE FEED, SIMULATED FILLS</span>
        ${fresh ? `<span class="live">Running</span>` : `<span class="muted" style="font-size:12px">Last saved ${fmt.ago(d.updated)}</span>`}</div>
      <div class="kpis">
        <div class="kpi hero"><span class="k">Cash</span><span class="v">${fmt.usd(d.cash)}</span>
          <span class="s">started with ${fmt.usd(d.initial_cash)}</span></div>
        <div class="kpi"><span class="k">Realized PnL</span><span class="v ${signClass(d.realized)}">${fmt.usd(d.realized, true)}</span></div>
        <div class="kpi"><span class="k">Open positions</span><span class="v">${d.positions.length}</span></div>
        <div class="kpi"><span class="k">Closed</span><span class="v">${d.closed}</span></div>
      </div>
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Positions</h2></div><div class="panel-body">${positions}</div></div>
      <div class="panel"><div class="panel-head" style="padding-bottom:10px"><h2 class="panel-title">Decision log</h2>
        <span class="muted" style="font-size:12px">Newest first. Refreshes every 3s.</span></div>${feed}</div>
    </div>`;
  }
  return `<div class="paper"><aside class="session-list">${list}</aside>${detail}</div>`;
}

app.addEventListener("click", (e) => {
  if (state.view !== "paper") return;
  const s = e.target.closest("[data-session]");
  if (s) { state.paper.selected = s.dataset.session; state.paper.detail = null; render(); loadPaper(); }
});

/* ---- boot --------------------------------------------------------------------------------- */

const VIEWS = ["markets", "backtest", "compare", "paper"];

/* Deep links: #backtest?strategy=logical&hours=6&seed=7&stress=0&data=synthetic&run=1, #markets?q=fed */
function parseHash() {
  const [view, query = ""] = (location.hash || "#markets").slice(1).split("?");
  return { view: VIEWS.includes(view) ? view : "markets", params: new URLSearchParams(query) };
}

function applyLink(params) {
  const f = state.form;
  if (params.has("strategy")) f.strategy = params.get("strategy");
  if (params.has("data")) f.data = params.get("data") === "recorded" ? "recorded" : "synthetic";
  if (params.has("hours")) f.hours = Number(params.get("hours")) || f.hours;
  if (params.has("seed")) f.seed = Number(params.get("seed")) || 0;
  if (params.has("stress")) f.stress = params.get("stress") !== "0";
  if (params.has("q")) {
    state.query = params.get("q");
    $("#search").value = state.query;
  }
  return params.get("run") === "1";
}

async function boot() {
  const link = parseHash();
  const autorun = applyLink(link.params);
  go(link.view);
  loadEvents();
  try {
    state.meta = await request("/api/meta");
    if (!link.params.has("seed")) state.form.seed = state.meta.defaults.seed;
    if (!strategyMeta(state.form.strategy)) state.form.strategy = state.meta.strategies[0].name;
    const rec = state.meta.recorded;
    const rows = Object.values(rec.rows).reduce((a, b) => a + b, 0);
    $("#data-status").textContent = rec.available
      ? `${rec.markets} markets, ${fmt.num(rows, 0)} recorded rows`
      : rec.markets ? `${rec.markets} markets, no recordings` : "No market data yet";
  } catch (err) {
    $("#data-status").textContent = "API unavailable";
  }
  if (state.view !== "markets") render();
  if (autorun && state.meta) {
    if (state.view === "backtest") runBacktest();
    else if (state.view === "compare") runCompare();
  }
}

boot();
