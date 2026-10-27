"use strict";

/*
 * polysim dashboard. Vanilla JS, no build step. Live data comes from Polymarket's public
 * API through the local server; every trade goes to the local paper account.
 * Clicks are routed through data-act="name" attributes to the ACTIONS table.
 */

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");
const VIEWS = ["markets", "event", "portfolio", "bots", "lab"];

const state = {
  route: { view: "markets", arg: "", params: new URLSearchParams() },
  portfolio: null,
  markets: { data: null, error: null, tag: "", query: "" },
  event: null,
  bots: { data: null, error: null, open: {} },
  builder: null,
  lab: null,
};
const timers = new Set();

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
    const body = abs >= 1e6 ? `${(abs / 1e6).toFixed(2)}M`
      : abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const sign = v < 0 ? "-" : signed && v > 0 ? "+" : "";
    return `${sign}$${body}`;
  },
  vol(v) {
    if (!v) return "$0 Vol.";
    if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M Vol.`;
    if (v >= 1e3) return `$${Math.round(v / 1e3)}K Vol.`;
    return `$${Math.round(v)} Vol.`;
  },
  pct(v, digits = 1, signed = false) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "-";
    const s = (v * 100).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return `${signed && v > 0 ? "+" : ""}${s}%`;
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
    if (c > 0 && c < 0.1) return "<0.1¢";
    return `${Number.isInteger(Math.round(c * 10) / 10) ? Math.round(c) : c.toFixed(1)}¢`;
  },
  num(v, digits = 2) {
    if (v === null || v === undefined || !Number.isFinite(v)) return "-";
    return v.toLocaleString("en-US", { maximumFractionDigits: digits });
  },
  date(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  },
  time(ms) {
    return new Date(ms).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  },
  ago(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  },
  elapsed(ms) {
    const t = Math.max(0, Math.round(ms / 1000));
    const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = t % 60;
    return `+${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  },
};

const signClass = (v) => (v > 0 ? "yes" : v < 0 ? "no" : "");

function iconFor(src, title, cls = "") {
  const letter = esc((title || "?").trim().charAt(0).toUpperCase());
  if (!src) return `<div class="icon ${cls}">${letter}</div>`;
  return `<img class="icon ${cls}" src="${esc(src)}" alt="" loading="lazy"
    onerror="this.outerHTML='<div class=&quot;icon ${cls}&quot;>${letter}</div>'">`;
}

function dataTag(label) {
  const text = { synthetic: "SIMULATED DATA", recorded: "RECORDED BOOKS", historical: "REAL PRICE HISTORY" }[label];
  return `<span class="tag tag-${esc(label)}">${text || esc(label)}</span>`;
}

function toast(message, bad = false) {
  const el = document.createElement("div");
  el.className = `toast${bad ? " bad" : ""}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 3500);
}

function every(ms, fn) {
  const id = setInterval(() => { if (!document.hidden) fn(); }, ms);
  timers.add(id);
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function confirmBox(title, body, okLabel, onOk, extra = "") {
  $("#modal-root").innerHTML = `<div class="overlay" data-act="close-modal">
    <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}" data-stop>
      <h2>${esc(title)}</h2><p class="muted" style="margin:0 0 16px">${esc(body)}</p>${extra}
      <div class="row" style="justify-content:flex-end;margin-top:16px">
        <button class="ghost-btn" data-act="close-modal" type="button">Keep it</button>
        <button class="primary-btn inline" id="modal-ok" type="button">${esc(okLabel)}</button>
      </div></div></div>`;
  $("#modal-ok").addEventListener("click", () => { onOk(); $("#modal-root").innerHTML = ""; });
}

/* ---- charts ---------------------------------------------------------------- */

function lineChart(el, pts, opts = {}) {
  if (!el) return;
  const w = el.clientWidth, h = el.clientHeight;
  if (!pts.length || !w) { el.innerHTML = `<p class="muted">${esc(opts.empty || "No data yet.")}</p>`; return; }
  const pad = { l: opts.padLeft ?? 56, r: 12, t: 10, b: 24 };
  const xs = pts.map((p) => p[0]);
  let ys = pts.map((p) => p[1]);
  if (opts.base !== undefined) ys = ys.concat([opts.base]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = opts.yMin ?? Math.min(...ys), y1 = opts.yMax ?? Math.max(...ys);
  if (y1 - y0 < 1e-9) { y0 -= 0.01; y1 += 0.01; }
  if (opts.yMin === undefined) { const s = y1 - y0; y0 -= s * 0.08; y1 += s * 0.08; }
  const sx = (x) => pad.l + (x - x0) / (x1 - x0 || 1) * (w - pad.l - pad.r);
  const sy = (y) => pad.t + (1 - (y - y0) / (y1 - y0)) * (h - pad.t - pad.b);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
  const area = `${line}L${sx(pts.at(-1)[0]).toFixed(1)},${h - pad.b}L${sx(pts[0][0]).toFixed(1)},${h - pad.b}Z`;
  const yticks = Array.from({ length: 4 }, (_, i) => y0 + (y1 - y0) * (i + 0.5) / 4);
  const xticks = Array.from({ length: 5 }, (_, i) => x0 + (x1 - x0) * i / 4);
  const color = opts.color || "var(--accent)";
  const gid = `g${Math.random().toString(36).slice(2, 8)}`;
  const markers = (opts.markers || []).map(([x, y, side]) => `<circle cx="${sx(x)}" cy="${sy(y)}" r="5"
    fill="${side === "BUY" ? "var(--yes)" : "var(--no)"}" stroke="var(--card)" stroke-width="2"/>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts.label || "chart")}">
    <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.2"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    ${yticks.map((t) => `<line class="grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${sy(t)}" y2="${sy(t)}"/>
      <text class="axis" x="${pad.l - 8}" y="${sy(t) + 4}" text-anchor="end">${esc(opts.yFmt ? opts.yFmt(t) : t.toFixed(2))}</text>`).join("")}
    ${xticks.map((t) => `<text class="axis" x="${sx(t)}" y="${h - 6}" text-anchor="middle">${esc(opts.xFmt ? opts.xFmt(t) : t.toFixed(1))}</text>`).join("")}
    ${opts.base !== undefined ? `<line class="base" x1="${pad.l}" x2="${w - pad.r}" y1="${sy(opts.base)}" y2="${sy(opts.base)}"/>` : ""}
    <path d="${area}" fill="url(#${gid})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round"/>
    ${markers}
    <line class="cross" y1="${pad.t}" y2="${h - pad.b}" stroke="var(--line-2)" visibility="hidden"/>
    <circle class="dot" r="4" fill="${color}" stroke="var(--card)" stroke-width="2" visibility="hidden"/>
    <rect x="${pad.l}" y="0" width="${Math.max(0, w - pad.l - pad.r)}" height="${h}" fill="transparent" class="hit"/>
  </svg><div class="tooltip hidden"></div>`;
  const hit = $(".hit", el), cross = $(".cross", el), dot = $(".dot", el), tip = $(".tooltip", el);
  hit.addEventListener("mousemove", (e) => {
    const mx = e.clientX - el.getBoundingClientRect().left;
    let best = 0;
    pts.forEach((p, i) => { if (Math.abs(sx(p[0]) - mx) < Math.abs(sx(pts[best][0]) - mx)) best = i; });
    const [px, py] = [sx(pts[best][0]), sy(pts[best][1])];
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("visibility", "visible");
    tip.classList.remove("hidden");
    tip.style.left = `${px}px`; tip.style.top = `${py}px`;
    tip.textContent = opts.tip ? opts.tip(pts[best]) : `${pts[best][1]}`;
  });
  hit.addEventListener("mouseleave", () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.classList.add("hidden");
  });
}

function gauge(p) {
  const v = Math.min(1, Math.max(0, p ?? 0));
  const angle = Math.PI * (1 - v);
  const x = 32 + 26 * Math.cos(angle), y = 32 - 26 * Math.sin(angle);
  const color = v >= 0.5 ? "var(--yes)" : "var(--no)";
  return `<div class="gauge"><svg viewBox="0 0 64 36" aria-hidden="true">
      <path d="M6 32 A26 26 0 0 1 58 32" fill="none" stroke="var(--line)" stroke-width="5" stroke-linecap="round"/>
      ${v > 0.005 ? `<path d="M6 32 A26 26 0 0 1 ${x.toFixed(2)} ${y.toFixed(2)}" fill="none" stroke="${color}" stroke-width="5" stroke-linecap="round"/>` : ""}
    </svg><div class="gauge-value num">${fmt.chance(p)}</div><div class="gauge-label">chance</div></div>`;
}

/* ---- theme and header ------------------------------------------------------------ */

function setTheme(next) {
  if (next === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = next;
  try { localStorage.setItem("pmsim-theme", next); } catch (e) { /* storage may be blocked */ }
  $("#theme-btn").textContent = next === "auto" ? "Auto" : next === "dark" ? "Dark" : "Light";
  render();
}
$("#theme-btn").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const cur = document.documentElement.dataset.theme || "auto";
  setTheme(order[(order.indexOf(cur) + 1) % 3]);
});
$("#theme-btn").textContent = { light: "Light", dark: "Dark" }[document.documentElement.dataset.theme] || "Auto";

async function refreshPortfolio() {
  try {
    state.portfolio = await request("/api/portfolio");
    $("#balance-portfolio b").textContent = fmt.usd(state.portfolio.equity);
    $("#balance-cash b").textContent = fmt.usd(state.portfolio.cash);
  } catch (err) { /* header keeps last values */ }
  return state.portfolio;
}

const searchInput = $("#search");
searchInput.addEventListener("input", debounce(() => {
  const q = searchInput.value.trim();
  location.hash = q ? `#markets?q=${encodeURIComponent(q)}` : "#markets";
}, 300));

/* ---- routing ------------------------------------------------------------------------ */

function parseHash() {
  const raw = (location.hash || "#markets").slice(1);
  const [path, query = ""] = raw.split("?");
  const [view, ...rest] = path.split("/");
  return { view: VIEWS.includes(view) ? view : "markets", arg: decodeURIComponent(rest.join("/")), params: new URLSearchParams(query) };
}

function route() {
  timers.forEach(clearInterval);
  timers.clear();
  $("#modal-root").innerHTML = "";
  state.route = parseHash();
  const { view } = state.route;
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.view === view || (view === "event" && t.dataset.view === "markets");
    if (on) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
  });
  ({ markets: enterMarkets, event: enterEvent, portfolio: enterPortfolio, bots: enterBots, lab: enterLab })[view]();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

let lastRendered = "";

function render() {
  const { view, arg } = state.route;
  const html = ({ markets: viewMarkets, event: viewEvent, portfolio: viewPortfolio, bots: viewBots, lab: viewLab })[view]();
  const key = `${view}/${arg}`;
  app.innerHTML = `<div class="${key === lastRendered ? "" : "view"}">${html}</div>`;
  lastRendered = key;
  afterRender();
}

function afterRender() {
  const { view } = state.route;
  if (view === "event") drawEventChart();
  if (view === "lab") drawLabChart();
  if (view === "bots") drawBacktestChart();
}

/* ---- markets -------------------------------------------------------------------------- */

function enterMarkets() {
  const p = state.route.params;
  state.markets.query = p.get("q") || "";
  state.markets.tag = p.get("tag") || "";
  if (searchInput.value.trim() !== state.markets.query) searchInput.value = state.markets.query;
  state.markets.data = null;
  render();
  loadMarkets();
  every(30_000, () => loadMarkets(true));
}

async function loadMarkets(quiet = false) {
  const m = state.markets;
  try {
    const q = encodeURIComponent(m.query), tag = encodeURIComponent(m.tag);
    m.data = await request(`/api/live/events?limit=120&q=${q}&tag=${tag}`);
    m.error = null;
  } catch (err) { if (!quiet) m.error = err.message; }
  if (state.route.view === "markets") render();
}

function eventCard(ev) {
  const single = ev.markets.length === 1;
  const m0 = ev.markets[0];
  const [yesP, noP] = m0.prices.length === 2 ? m0.prices : [m0.prices[0], null];
  const body = single
    ? `<div class="yesno">
        <span class="y"><em>${esc(m0.outcomes[0])}</em> <b class="num">${fmt.cents(yesP)}</b></span>
        <span class="n"><em>${esc(m0.outcomes[1] || "No")}</em> <b class="num">${fmt.cents(noP)}</b></span></div>`
    : `<div class="outcome-rows">${ev.markets.slice(0, 3).map((m) => `
        <div class="outcome-row"><span class="label">${esc(m.label)}</span>
          <span class="pct num">${fmt.chance(m.prices[0])}</span>
          <span class="mini"><span class="tag-yes">Yes</span><span class="tag-no">No</span></span></div>`).join("")}
        ${ev.markets.length > 3 ? `<span class="muted small">+${ev.markets.length - 3} more</span>` : ""}</div>`;
  return `<a class="card" href="#event/${encodeURIComponent(ev.slug)}">
    <div class="card-top">${iconFor(ev.icon, ev.title)}<div class="card-title">${esc(ev.title)}</div>${single ? gauge(yesP) : ""}</div>
    ${body}
    <div class="card-foot"><span class="num">${fmt.vol(ev.volume_24h)} 24h</span><span>${esc(fmt.date(ev.end))}</span></div></a>`;
}

function viewMarkets() {
  const m = state.markets;
  if (m.error) {
    return `<div class="error">${esc(m.error)}</div>
      <button class="ghost-btn" data-act="retry-markets" type="button">Try again</button>`;
  }
  if (!m.data) {
    const sk = Array.from({ length: 12 }, () => `<div class="skeleton" style="height:196px"></div>`).join("");
    return `<div class="page-head"><h1 class="page">Markets</h1></div><div class="grid">${sk}</div>`;
  }
  const chip = (tag, label) => `<a class="chip" href="#markets?tag=${encodeURIComponent(tag)}${m.query ? `&q=${encodeURIComponent(m.query)}` : ""}"
    aria-pressed="${m.tag === tag}">${esc(label)}</a>`;
  const label = (t) => t.replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase());
  const events = m.data.events;
  return `<div class="page-head">
      <h1 class="page">${m.query ? `Results for "${esc(m.query)}"` : "Live markets"}</h1>
      <span class="muted small">${m.data.total} open events, live from Polymarket. Updated ${fmt.ago(m.data.as_of)}.</span>
    </div>
    <div class="chips">${chip("", "Trending")}${m.data.tags.map((t) => chip(t, label(t))).join("")}</div>
    ${events.length ? `<div class="grid">${events.map(eventCard).join("")}</div>`
      : `<div class="empty"><h2>Nothing matches</h2><p>Try a shorter search or another category.</p></div>`}`;
}

/* ---- market (event) page ---------------------------------------------------------------- */

function enterEvent() {
  const slug = state.route.arg;
  const p = state.route.params;
  state.event = {
    slug, data: null, error: null, mi: 0, oi: 0, interval: "1w", history: null, book: null,
    side: p.get("side") === "SELL" ? "SELL" : "BUY", type: "market", amount: "", price: "", shares: "",
    quote: null, busy: false, preselect: p.get("token") || "",
  };
  render();
  loadEvent();
  every(3000, () => { loadBook(); });
  every(15_000, () => refreshPortfolio().then(() => state.route.view === "event" && renderTradeOnly()));
}

async function loadEvent() {
  const e = state.event;
  try {
    e.data = await request(`/api/live/event/${encodeURIComponent(e.slug)}`);
    if (e.preselect) {
      e.data.markets.forEach((m, mi) => {
        const oi = m.tokens.indexOf(e.preselect);
        if (oi >= 0) { e.mi = mi; e.oi = oi; }
      });
    }
  } catch (err) { e.error = err.message; }
  if (state.route.view !== "event") return;
  render();
  if (e.data) { loadHistory(); loadBook(); refreshPortfolio().then(renderTradeOnly); }
}

const selMarket = () => state.event.data.markets[state.event.mi];
const selToken = () => selMarket().tokens[state.event.oi];

async function loadHistory() {
  const e = state.event;
  try {
    const out = await request(`/api/live/history?token=${encodeURIComponent(selToken())}&interval=${e.interval}`);
    e.history = out.points;
  } catch (err) { e.history = []; }
  if (state.route.view === "event") { drawEventChart(); }
}

async function loadBook() {
  const e = state.event;
  if (!e || !e.data) return;
  try {
    e.book = await request(`/api/live/book?token=${encodeURIComponent(selToken())}`);
    const el = $("#book-body");
    if (el) el.innerHTML = bookLadder(e.book);
    const hdr = $("#chance-now");
    if (hdr && e.book.mid !== null) hdr.textContent = fmt.chance(e.book.mid);
  } catch (err) { /* keep showing the last book */ }
}

function bookLadder(book) {
  const maxTotal = Math.max(book.asks.at(-1)?.total || 0, book.bids.at(-1)?.total || 0) || 1;
  const row = (lv, kind) => `<div class="book-row ${kind}">
      <div class="bar" style="width:${(lv.total / maxTotal * 100).toFixed(1)}%"></div>
      <span class="num ${kind === "ask" ? "no" : "yes"}">${fmt.cents(lv.price)}</span>
      <span class="num r">${fmt.num(lv.shares)}</span><span class="num r">${fmt.usd(lv.total)}</span></div>`;
  const asks = book.asks.slice(0, 8).reverse().map((l) => row(l, "ask")).join("");
  const bids = book.bids.slice(0, 8).map((l) => row(l, "bid")).join("");
  return `<div class="book">
    <div class="book-head"><span>Price</span><span class="r">Shares</span><span class="r">Total</span></div>
    ${asks || `<div class="muted small" style="padding:8px 12px">No asks</div>`}
    <div class="book-mid"><span>Last ${fmt.cents(book.last)}</span><span>Spread ${book.spread === null ? "-" : fmt.cents(book.spread)}</span></div>
    ${bids || `<div class="muted small" style="padding:8px 12px">No bids</div>`}</div>`;
}

function positionsFor(tokens) {
  const pf = state.portfolio;
  if (!pf) return { positions: [], orders: [] };
  return {
    positions: pf.positions.filter((p) => tokens.includes(p.token)),
    orders: pf.orders.filter((o) => tokens.includes(o.token)),
  };
}

function viewEvent() {
  const e = state.event;
  if (e.error) return `<div class="error">${esc(e.error)}</div><a class="link" href="#markets">Back to markets</a>`;
  if (!e.data) {
    return `<div class="market"><div class="market-main"><div class="skeleton" style="height:80px"></div>
      <div class="skeleton" style="height:360px"></div></div><div class="skeleton" style="height:420px"></div></div>`;
  }
  const ev = e.data, m = selMarket();
  const multi = ev.markets.length > 1;
  const intervals = ["1h", "6h", "1d", "1w", "1m", "max"].map((iv) => `<button data-act="interval" data-iv="${iv}"
    aria-pressed="${e.interval === iv}" type="button">${iv === "max" ? "All" : iv.toUpperCase()}</button>`).join("");
  const rows = multi ? `<div class="panel"><div class="outcome-table">
      <div class="between" style="padding:12px 16px"><span class="label">Outcome</span><span class="label">Chance</span></div>
      ${ev.markets.map((mk, i) => `<div class="outcome-line" data-act="pick-market" data-mi="${i}" aria-selected="${i === e.mi}">
        <span class="name">${esc(mk.label)}</span><span class="pct num">${fmt.chance(mk.prices[0])}</span>
        <button class="buy-yes" data-act="quick-buy" data-mi="${i}" data-oi="0" type="button">Buy Yes ${fmt.cents(mk.prices[0])}</button>
        <button class="buy-no" data-act="quick-buy" data-mi="${i}" data-oi="1" type="button">Buy No ${fmt.cents(mk.prices[1])}</button>
      </div>`).join("")}</div></div>` : "";
  const outcomeName = m.outcomes[e.oi];
  return `<div class="market">
    <div class="market-main">
      <div class="market-head">${iconFor(ev.icon, ev.title, "lg")}
        <div><h1>${esc(ev.title)}</h1><div class="market-meta">
          <span class="num">${fmt.vol(ev.volume)}</span><span>${ev.end ? `Ends ${esc(fmt.date(ev.end))}` : ""}</span>
          <span class="tag tag-blue">LIVE DATA</span></div></div></div>
      <div class="panel"><div class="panel-body">
        <div class="between" style="flex-wrap:wrap">
          <div><div class="label">${esc(multi ? `${m.label} / ${outcomeName}` : outcomeName)}</div>
            <span class="chance-big num" id="chance-now">${fmt.chance(m.prices[e.oi])}</span> <span class="muted">chance</span></div>
          <div class="segmented">${intervals}</div>
        </div>
        <div class="chart" id="event-chart" style="margin-top:12px"></div>
      </div></div>
      ${rows}
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Order book: ${esc(multi ? `${m.label}, ` : "")}${esc(outcomeName)}</h2>
          <span class="muted small">Live, refreshes every 3 s</span></div>
        <div class="panel-body" id="book-body">${e.book ? bookLadder(e.book) : `<div class="skeleton" style="height:260px"></div>`}</div></div>
      <details class="panel"><summary class="panel-body" style="cursor:pointer"><b>Rules</b></summary>
        <div class="panel-body rules">${esc(ev.description || "No rules text provided.")}</div></details>
    </div>
    <aside class="trade" id="trade">${tradeHtml()}</aside>
  </div>`;
}

function renderTradeOnly() {
  const el = $("#trade");
  if (el && state.route.view === "event" && state.event?.data) el.innerHTML = tradeHtml();
}

function tradeHtml() {
  const e = state.event, ev = e.data, m = selMarket();
  const multi = ev.markets.length > 1;
  const tokens = m.tokens;
  const mine = positionsFor(tokens);
  const held = mine.positions.find((p) => p.token === selToken());
  const buy = e.side === "BUY";
  const market = e.type === "market";
  const pf = state.portfolio;
  let inputs;
  if (market) {
    inputs = buy
      ? `<div class="field"><div class="between"><label for="amt">Amount</label>
          <span class="muted small">Cash ${fmt.usd(pf?.free_cash)}</span></div>
          <div class="input-wrap"><span class="prefix">$</span>
          <input class="input big" id="amt" data-trade="amount" inputmode="decimal" placeholder="0" value="${esc(e.amount)}"></div>
          <div class="quick">${[1, 20, 100].map((v) => `<button data-act="add-amount" data-v="${v}" type="button">+$${v}</button>`).join("")}
          <button data-act="max-amount" type="button">Max</button></div></div>`
      : `<div class="field"><div class="between"><label for="amt">Shares</label>
          <span class="muted small">You hold ${fmt.num(held?.shares || 0)}</span></div>
          <input class="input big" id="amt" data-trade="amount" inputmode="decimal" placeholder="0" value="${esc(e.amount)}">
          <div class="quick">${[0.25, 0.5].map((f) => `<button data-act="share-frac" data-f="${f}" type="button">${f * 100}%</button>`).join("")}
          <button data-act="share-frac" data-f="1" type="button">Max</button></div></div>`;
  } else {
    inputs = `<div class="form-grid">
      <div class="field"><label for="lp">Limit price (¢)</label>
        <input class="input" id="lp" data-trade="price" inputmode="decimal" placeholder="${fmt.num((e.book?.[buy ? "best_bid" : "best_ask"] ?? m.prices[e.oi]) * 100, 1)}" value="${esc(e.price)}"></div>
      <div class="field"><label for="ls">Shares</label>
        <input class="input" id="ls" data-trade="shares" inputmode="decimal" placeholder="${m.min_size}" value="${esc(e.shares)}"></div></div>
      <p class="field-help" style="margin:0">Rests in the book until the live price reaches it. Minimum ${m.min_size} shares.</p>`;
  }
  const q = e.quote;
  let preview = "";
  if (market && q && q.shares > 0) {
    preview = buy
      ? `<div class="preview"><div class="between"><span>Avg price</span><span class="num">${fmt.cents(q.avg_price)}</span></div>
          <div class="between"><span>Shares</span><span class="num">${fmt.num(q.shares)}</span></div>
          <div class="between"><span>Fee</span><span class="num">${fmt.usd(q.fee)}</span></div>
          <div class="between"><span>To win</span><span class="to-win num">${fmt.usd(q.payout_if_win)}</span></div></div>`
      : `<div class="preview"><div class="between"><span>Avg price</span><span class="num">${fmt.cents(q.avg_price)}</span></div>
          <div class="between"><span>You receive</span><span class="to-win num">${fmt.usd(q.proceeds)}</span></div></div>`;
  } else if (!market && Number(e.price) > 0 && Number(e.shares) > 0) {
    const total = Number(e.price) / 100 * Number(e.shares);
    preview = `<div class="preview"><div class="between"><span>Total</span><span class="num">${fmt.usd(total)}</span></div>
      ${buy ? `<div class="between"><span>To win</span><span class="to-win num">${fmt.usd(Number(e.shares))}</span></div>` : ""}</div>`;
  }
  const verb = `${buy ? "Buy" : "Sell"} ${m.outcomes[e.oi]}`;
  const yourPos = mine.positions.length || mine.orders.length ? `<div class="panel"><div class="panel-head"><h2 class="panel-title">Your position</h2></div>
    <div class="panel-body stack">
      ${mine.positions.map((p) => `<div class="between"><span><span class="tag ${p.outcome === m.outcomes[0] ? "tag-yes" : "tag-no"}">${esc(p.outcome)}</span>
          <span class="num">${fmt.num(p.shares)} @ ${fmt.cents(p.avg_price)}</span></span>
          <span class="num ${signClass(p.pnl)}">${fmt.usd(p.value)} (${fmt.usd(p.pnl, true)})</span></div>`).join("")}
      ${mine.orders.map((o) => `<div class="between small"><span>Limit ${o.side === "BUY" ? "buy" : "sell"} ${esc(o.outcome)}
          <span class="num">${fmt.num(o.shares)} @ ${fmt.cents(o.price)}</span></span>
          <button class="ghost-btn small" data-act="remove-limit" data-id="${o.id}" type="button">Cancel</button></div>`).join("")}
    </div></div>` : "";
  return `<div class="panel trade-panel">
      <div class="trade-target">${iconFor(ev.icon, ev.title, "sm")}<span>${esc(multi ? m.label : ev.title)}</span></div>
      <div class="side-tabs"><div class="tabbar">
          <button class="st" data-act="side" data-side="BUY" aria-pressed="${buy}" type="button">Buy</button>
          <button class="st" data-act="side" data-side="SELL" aria-pressed="${!buy}" type="button">Sell</button></div>
        <select data-trade="type" aria-label="Order type">
          <option value="market" ${market ? "selected" : ""}>Market</option><option value="limit" ${market ? "" : "selected"}>Limit</option></select></div>
      <div class="outcome-pick">${m.outcomes.map((o, i) => `<button class="${i === 0 ? "y" : "n"}" data-act="pick-outcome" data-oi="${i}"
          aria-pressed="${e.oi === i}" type="button">${esc(o)} ${fmt.cents(m.prices[i])}</button>`).join("")}</div>
      ${inputs}${preview}
      <button class="primary-btn" data-act="submit-trade" type="button" ${e.busy ? "disabled" : ""}>${e.busy ? "Placing order" : esc(verb)}</button>
      <p class="disclaimer">Paper trading: fills against the live Polymarket book, nothing is sent to Polymarket.</p>
    </div>
    ${yourPos}
    <div class="panel"><div class="panel-body stack">
      <b>Automate this market</b>
      <span class="muted small">Build a bot that buys and sells ${esc(m.outcomes[e.oi])} for you with simple price rules, then backtest it on real history.</span>
      <a class="ghost-btn" style="display:grid;place-items:center" href="#bots/new?event=${encodeURIComponent(ev.slug)}&token=${encodeURIComponent(selToken())}">Create a bot</a>
    </div></div>`;
}

function drawEventChart() {
  const e = state.event;
  const el = $("#event-chart");
  if (!el || !e?.data) return;
  if (e.history === null) { el.innerHTML = `<div class="skeleton" style="height:100%"></div>`; return; }
  const pts = e.history;
  const short = ["1h", "6h", "1d"].includes(e.interval);
  lineChart(el, pts, {
    yMin: Math.max(0, Math.min(...pts.map((p) => p[1])) - 0.03),
    yMax: Math.min(1, Math.max(...pts.map((p) => p[1])) + 0.03), padLeft: 44,
    yFmt: (v) => `${Math.round(v * 100)}%`,
    xFmt: (t) => new Date(t).toLocaleString("en-US", short ? { hour: "2-digit", minute: "2-digit", hour12: false } : { month: "short", day: "numeric" }),
    tip: (p) => `${new Date(p[0]).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}  ${fmt.chance(p[1])}`,
    empty: "No price history for this window.", label: "price history",
  });
}

const refreshQuote = debounce(async () => {
  const e = state.event;
  if (!e || e.type !== "market" || !(Number(e.amount) > 0)) { if (e) { e.quote = null; renderPreviewOnly(); } return; }
  try {
    e.quote = await request("/api/live/quote", { event_slug: e.slug, token: selToken(), side: e.side, amount: Number(e.amount) });
  } catch (err) { e.quote = null; }
  renderPreviewOnly();
}, 250);

function renderPreviewOnly() {
  const active = document.activeElement?.dataset?.trade;
  const pos = document.activeElement?.selectionStart;
  renderTradeOnly();
  if (active) {
    const el = document.querySelector(`[data-trade="${active}"]`);
    if (el) { el.focus(); try { el.setSelectionRange(pos, pos); } catch (x) { /* not a text input */ } }
  }
}

async function submitTrade() {
  const e = state.event;
  const body = { event_slug: e.slug, token: selToken(), side: e.side, type: e.type };
  if (e.type === "market") body.amount = Number(e.amount);
  else { body.price = Number(e.price) / 100; body.shares = Number(e.shares); }
  if (e.type === "market" && !(body.amount > 0)) { toast(e.side === "BUY" ? "Enter an amount in dollars." : "Enter how many shares to sell.", true); return; }
  if (e.type === "limit" && !(body.price > 0 && body.shares > 0)) { toast("Enter a limit price and number of shares.", true); return; }
  e.busy = true; renderTradeOnly();
  try {
    const out = await request("/api/live/trade", body);
    state.portfolio = out.portfolio;
    $("#balance-portfolio b").textContent = fmt.usd(out.portfolio.equity);
    $("#balance-cash b").textContent = fmt.usd(out.portfolio.cash);
    const f = out.filled;
    const outcome = selMarket().outcomes[e.oi];
    if (f) toast(`${f.side === "BUY" ? "Bought" : "Sold"} ${fmt.num(f.shares)} ${outcome} at ${fmt.cents(f.avg_price)} avg.`);
    if (out.order) toast(`Limit order placed: ${fmt.num(out.order.shares)} ${outcome} at ${fmt.cents(out.order.price)}.`);
    e.amount = ""; e.shares = ""; e.quote = null;
  } catch (err) { toast(err.message, true); }
  e.busy = false;
  renderTradeOnly();
  loadBook();
}

/* ---- portfolio ---------------------------------------------------------------------------- */

function enterPortfolio() {
  state.pfTab = state.pfTab || "positions";
  render();
  refreshPortfolio().then(() => state.route.view === "portfolio" && render());
  every(10_000, () => refreshPortfolio().then(() => state.route.view === "portfolio" && render()));
}

function viewPortfolio() {
  const pf = state.portfolio;
  if (!pf) return `<div class="skeleton" style="height:420px"></div>`;
  const tab = state.pfTab;
  const tabBtn = (id, label) => `<button class="subtab" data-act="pf-tab" data-tab="${id}" aria-selected="${tab === id}" type="button">${label}</button>`;
  let body;
  if (tab === "positions") {
    body = pf.positions.length ? `<div class="table-wrap"><table><thead><tr><th>Market</th><th>Outcome</th>
        <th class="right">Shares</th><th class="right">Avg</th><th class="right">Now</th><th class="right">Value</th><th class="right">P&amp;L</th><th></th></tr></thead>
      <tbody>${pf.positions.map((p) => `<tr class="clickable" data-act="open-event" data-slug="${esc(p.event_slug)}" data-token="${esc(p.token)}">
        <td class="truncate" title="${esc(p.label)}">${esc(p.label)}</td>
        <td><span class="tag ${p.outcome === "No" ? "tag-no" : "tag-yes"}">${esc(p.outcome)}</span></td>
        <td class="num right">${fmt.num(p.shares)}</td><td class="num right">${fmt.cents(p.avg_price)}</td>
        <td class="num right">${fmt.cents(p.price)}</td><td class="num right">${fmt.usd(p.value)}</td>
        <td class="num right ${signClass(p.pnl)}">${fmt.usd(p.pnl, true)} <span class="small">(${fmt.pct(p.pnl_pct, 1, true)})</span></td>
        <td class="right"><button class="ghost-btn small" data-act="sell-position" data-slug="${esc(p.event_slug)}" data-token="${esc(p.token)}" type="button">Sell</button></td>
      </tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><h2>No positions yet</h2><p>Pick a market and buy Yes or No with paper money. Prices and fills come from the live Polymarket book.</p>
          <a class="primary-btn inline" style="display:inline-grid;place-items:center" href="#markets">Browse markets</a></div>`;
  } else if (tab === "orders") {
    body = pf.orders.length ? `<div class="table-wrap"><table><thead><tr><th>Market</th><th>Side</th><th>Outcome</th>
        <th class="right">Price</th><th class="right">Shares</th><th></th></tr></thead><tbody>
      ${pf.orders.map((o) => `<tr><td class="truncate">${esc(o.label)}</td><td>${o.side === "BUY" ? "Buy" : "Sell"}</td><td>${esc(o.outcome)}</td>
        <td class="num right">${fmt.cents(o.price)}</td><td class="num right">${fmt.num(o.shares)}</td>
        <td class="right"><button class="ghost-btn small" data-act="remove-limit" data-id="${o.id}" type="button">Cancel</button></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted" style="margin:0">No open limit orders.</p>`;
  } else {
    body = pf.trades.length ? `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Market</th><th>Type</th><th>Side</th>
        <th class="right">Shares</th><th class="right">Avg price</th><th class="right">Total</th><th>By</th></tr></thead><tbody>
      ${pf.trades.map((t) => `<tr><td class="num muted">${new Date(t.ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
        <td class="truncate">${esc(t.label)} / ${esc(t.outcome)}</td><td>${esc(t.kind)}</td>
        <td><span class="tag ${t.side === "BUY" ? "tag-yes" : "tag-no"}">${t.side === "BUY" ? "Buy" : "Sell"}</span></td>
        <td class="num right">${fmt.num(t.shares)}</td><td class="num right">${fmt.cents(t.avg_price)}</td>
        <td class="num right">${fmt.usd(t.notional)}</td><td class="muted">${t.bot ? "Bot" : "You"}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted" style="margin:0">No trades yet.</p>`;
  }
  return `<div class="page-head"><h1 class="page">Portfolio</h1>
      <button class="ghost-btn danger" data-act="reset-account" type="button">Reset paper account</button></div>
    <div class="tiles">
      <div class="tile hero"><span class="k">Portfolio value</span><span class="v">${fmt.usd(pf.equity)}</span>
        <span class="small ${signClass(pf.pnl)}">${fmt.usd(pf.pnl, true)} all time</span></div>
      <div class="tile"><span class="k">Cash</span><span class="v">${fmt.usd(pf.cash)}</span><span class="small muted">${fmt.usd(pf.free_cash)} free</span></div>
      <div class="tile"><span class="k">Positions</span><span class="v">${fmt.usd(pf.positions_value)}</span><span class="small muted">${pf.positions.length} open</span></div>
      <div class="tile"><span class="k">Realized P&amp;L</span><span class="v ${signClass(pf.realized)}">${fmt.usd(pf.realized, true)}</span>
        <span class="small muted">Started with ${fmt.usd(pf.starting_cash)}</span></div>
    </div>
    ${pf.errors?.length ? `<div class="note" style="margin-bottom:16px">${esc(pf.errors.at(-1))}</div>` : ""}
    <div class="panel"><div class="subtabs">${tabBtn("positions", "Positions")}${tabBtn("orders", `Open orders (${pf.orders.length})`)}${tabBtn("history", "History")}</div>
      <div class="panel-body">${body}</div></div>`;
}

/* ---- bots ------------------------------------------------------------------------------------ */

function enterBots() {
  if (state.route.arg === "new") { enterBuilder(); return; }
  state.builder = null;
  render();
  loadBots();
  every(5000, loadBots);
}

async function loadBots() {
  try { state.bots.data = await request("/api/bots"); state.bots.error = null; } catch (err) { state.bots.error = err.message; }
  if (state.route.view === "bots" && state.route.arg !== "new") render();
}

function viewBots() {
  if (state.route.arg === "new") return viewBuilder();
  const b = state.bots;
  if (b.error) return `<div class="error">${esc(b.error)}</div>`;
  if (!b.data) return `<div class="skeleton" style="height:320px"></div>`;
  const head = `<div class="page-head"><h1 class="page">Bots</h1>
    <a class="primary-btn inline" style="display:inline-grid;place-items:center" href="#bots/new">New bot</a></div>`;
  if (!b.data.bots.length) {
    return head + `<div class="panel"><div class="empty"><h2>Let a bot trade for you</h2>
      <p>A bot watches one live market and follows a simple rule, like "buy Yes when it drops to 40¢, sell at 48¢".
      Backtest it on real price history first, then start it with paper money.</p>
      <a class="primary-btn inline" style="display:inline-grid;place-items:center" href="#bots/new">Build your first bot</a></div></div>`;
  }
  const cards = b.data.bots.map((bot) => {
    const st = bot.stats, inst = bot.instrument;
    const open = b.open[bot.id];
    return `<div class="panel bot">
      <div class="between"><b>${esc(bot.name)}</b><span class="status ${bot.running ? "on" : "off"}">${bot.running ? "Running" : "Stopped"}</span></div>
      <a class="link small" href="#event/${encodeURIComponent(inst.event_slug)}?token=${encodeURIComponent(inst.token)}">${esc(inst.label)} / ${esc(inst.outcome)}</a>
      <span class="muted small">${esc(bot.summary)} Checks every ${bot.interval_s}s.</span>
      <div class="bot-stats">
        <div><span class="k">P&amp;L</span><span class="v ${signClass(st.pnl)}">${fmt.usd(st.pnl, true)}</span></div>
        <div><span class="k">Holding</span><span class="v">${fmt.num(st.shares)} sh</span></div>
        <div><span class="k">Trades</span><span class="v">${st.trades}</span></div>
      </div>
      <div class="row">
        <button class="${bot.running ? "ghost-btn" : "primary-btn inline"}" data-act="bot" data-id="${bot.id}" data-do="${bot.running ? "stop" : "start"}" type="button">${bot.running ? "Stop" : "Start"}</button>
        <button class="ghost-btn" data-act="toggle-log" data-id="${bot.id}" type="button">${open ? "Hide activity" : "Activity"}</button>
        <button class="ghost-btn danger" data-act="delete-bot" data-id="${bot.id}" type="button" style="margin-left:auto">Delete</button>
      </div>
      ${open ? `<div class="feed">${bot.log.length ? bot.log.map((l) => `<div class="feed-item"><span class="num muted">${fmt.time(l.ts)}</span>
          <span><span class="tag ${l.kind === "buy" ? "tag-yes" : l.kind === "sell" ? "tag-no" : l.kind === "error" ? "tag-warn" : ""}">${esc(l.kind)}</span></span>
          <span class="detail">${esc(l.detail)}</span></div>`).join("") : `<p class="muted small">No activity yet.</p>`}</div>` : ""}
    </div>`;
  }).join("");
  return head + `<div class="bot-grid">${cards}</div>
    <p class="field-help" style="margin-top:16px">Running bots trade on your paper account while this dashboard is open. They never send orders to Polymarket.</p>`;
}

/* ---- bot builder ------------------------------------------------------------------------------ */

const TEMPLATES = {
  dip: { title: "Buy the dip", text: "Buy when the price falls to your level, sell on the bounce.", entry: "below" },
  breakout: { title: "Ride a breakout", text: "Buy when the price climbs past your level, sell higher.", entry: "above" },
};

function enterBuilder() {
  const p = state.route.params;
  state.builder = {
    query: "", results: null, event: null, mi: 0, oi: 0, mid: null, template: "dip",
    entry: "", tp: "", sl: "", useTp: true, useSl: true, order: "25", max: "100", cooldown: "60", interval: "10",
    name: "", window: "1w", backtest: null, busy: false, error: null,
  };
  render();
  if (p.get("event")) selectBuilderEvent(p.get("event"), p.get("token"));
  else searchBuilder("");
}

async function searchBuilder(q) {
  const b = state.builder;
  b.query = q;
  try { b.results = (await request(`/api/live/events?limit=12&q=${encodeURIComponent(q)}`)).events; } catch (err) { b.error = err.message; }
  if (state.builder !== b) return;
  const typing = document.activeElement?.id === "b-search";
  render();
  if (typing) { const el = $("#b-search"); el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
}

async function selectBuilderEvent(slug, token) {
  const b = state.builder;
  try {
    b.event = await request(`/api/live/event/${encodeURIComponent(slug)}`);
    b.mi = 0; b.oi = 0;
    if (token) b.event.markets.forEach((m, mi) => { const oi = m.tokens.indexOf(token); if (oi >= 0) { b.mi = mi; b.oi = oi; } });
    await refreshBuilderPrice(true);
  } catch (err) { b.error = err.message; }
  if (state.builder === b) render();
}

async function refreshBuilderPrice(autofill) {
  const b = state.builder;
  const token = b.event.markets[b.mi].tokens[b.oi];
  try {
    const book = await request(`/api/live/book?token=${encodeURIComponent(token)}`);
    b.mid = book.mid ?? book.last ?? b.event.markets[b.mi].prices[b.oi];
  } catch (err) { b.mid = b.event.markets[b.mi].prices[b.oi]; }
  if (autofill) applyTemplate();
}

function applyTemplate() {
  const b = state.builder;
  const c = Math.round((b.mid ?? 0.5) * 100);
  const clamp = (v) => Math.min(98, Math.max(1, Math.round(v)));
  if (b.template === "dip") { b.entry = clamp(c - 3); b.tp = clamp(c + 3); b.sl = clamp(c - 10); }
  else { b.entry = clamp(c + 2); b.tp = clamp(c + 8); b.sl = clamp(c - 3); }
  b.backtest = null;
}

function builderRule() {
  const b = state.builder;
  return {
    entry: TEMPLATES[b.template].entry,
    entry_price: Number(b.entry) / 100,
    take_profit: b.useTp && b.tp !== "" ? Number(b.tp) / 100 : null,
    stop_loss: b.useSl && b.sl !== "" ? Number(b.sl) / 100 : null,
    order_usd: Number(b.order), max_position_usd: Number(b.max), cooldown_s: Number(b.cooldown),
  };
}

function builderSentence() {
  const b = state.builder;
  if (!b.event) return "Pick a market to start.";
  const m = b.event.markets[b.mi];
  const out = esc(m.outcomes[b.oi]);
  const r = builderRule();
  const parts = [`Buy <b>$${esc(b.order)}</b> of <b>${out}</b> when it ${r.entry === "below" ? "drops to" : "climbs to"} <b>${esc(b.entry)}¢</b> or ${r.entry === "below" ? "lower" : "higher"}.`];
  const exits = [];
  if (r.take_profit !== null) exits.push(`at <b>${esc(b.tp)}¢</b> to take profit`);
  if (r.stop_loss !== null) exits.push(`at <b>${esc(b.sl)}¢</b> to cut losses`);
  parts.push(exits.length ? `Sell everything ${exits.join(" or ")}.` : "Hold until the market resolves.");
  parts.push(`Never hold more than <b>$${esc(b.max)}</b>. Wait ${esc(b.cooldown)}s between trades and check the price every ${esc(b.interval)}s.`);
  return parts.join(" ");
}

function viewBuilder() {
  const b = state.builder;
  if (!b) return "";
  const err = b.error ? `<div class="error">${esc(b.error)}</div>` : "";
  let marketStep;
  if (b.event) {
    const ev = b.event;
    const picks = ev.markets.flatMap((m, mi) => m.outcomes.map((o, oi) => `<button class="pick" data-act="b-pick" data-mi="${mi}" data-oi="${oi}"
        aria-pressed="${b.mi === mi && b.oi === oi}" type="button">${iconFor(ev.icon, ev.title, "sm")}
        <span>${esc(ev.markets.length > 1 ? `${m.label}: ` : "")}${esc(o)}</span><span class="num">${fmt.cents(m.prices[oi])}</span></button>`)).join("");
    marketStep = `<div class="between"><b>${esc(ev.title)}</b><button class="link" data-act="b-change" type="button">Change market</button></div>
      <div class="pick-list">${picks}</div>`;
  } else {
    const list = b.results === null ? `<div class="skeleton" style="height:200px"></div>`
      : b.results.map((ev) => `<button class="pick" data-act="b-event" data-slug="${esc(ev.slug)}" type="button">
          ${iconFor(ev.icon, ev.title, "sm")}<span>${esc(ev.title)}</span><span class="muted small">${fmt.vol(ev.volume_24h)}</span></button>`).join("");
    marketStep = `<input class="input" id="b-search" placeholder="Search live markets" value="${esc(b.query)}" style="font-family:var(--font)">
      <div class="pick-list">${list}</div>`;
  }
  const ready = !!b.event;
  const num = (id, label, key, help, extra = "") => `<div class="field"><label for="${id}">${label}</label>
    <input class="input" id="${id}" data-b="${key}" inputmode="decimal" value="${esc(b[key])}" ${extra}>
    ${help ? `<span class="field-help">${help}</span>` : ""}</div>`;
  const bt = b.backtest;
  const btHtml = bt ? `<div class="verdict ${bt.metrics.net_pnl_usd > 0 ? "good" : bt.metrics.net_pnl_usd < 0 ? "bad" : ""}">${esc(bt.summary)}</div>
      <div class="chart short" id="bt-chart"></div>
      <div class="facts" style="margin-top:0;border-top:0;padding-top:0">
        <div><span class="k">Trades</span><span class="v">${bt.fill_count}</span></div>
        <div><span class="k">Max drawdown</span><span class="v">${fmt.usd(bt.metrics.max_drawdown_usd)}</span></div>
        <div><span class="k">Closed trades won</span><span class="v">${bt.metrics.closed_positions ? fmt.pct(bt.metrics.hit_rate, 0) : "Still holding"}</span></div></div>
      <p class="field-help" style="margin:0">Real prices from Polymarket; the order book around each price is modeled, so real fills may differ.</p>` : "";
  return `<div class="page-head"><h1 class="page">New bot</h1><a class="link" href="#bots">Back to bots</a></div>${err}
  <div class="builder">
    <div class="builder-steps">
      <div class="panel"><div class="panel-body stack"><h2 class="step-title"><span class="step-num">1</span>Choose a market</h2>${marketStep}</div></div>
      <div class="panel"><div class="panel-body stack"><h2 class="step-title"><span class="step-num">2</span>Choose a plan</h2>
        <div class="templates">${Object.entries(TEMPLATES).map(([id, t]) => `<button class="template" data-act="b-template" data-t="${id}"
          aria-pressed="${b.template === id}" type="button"><b>${t.title}</b><span>${t.text}</span></button>`).join("")}</div>
        ${b.mid !== null ? `<span class="muted small">Current price: <b class="num">${fmt.cents(b.mid)}</b>. Levels below are filled in around it; change anything.</span>` : ""}
      </div></div>
      <div class="panel"><div class="panel-body stack"><h2 class="step-title"><span class="step-num">3</span>Set the rules</h2>
        <div class="form-grid">
          ${num("b-entry", `${b.template === "dip" ? "Buy at or below" : "Buy at or above"} (¢)`, "entry", "The price that triggers a buy.")}
          ${num("b-order", "Spend per buy ($)", "order", "Dollars spent each time it buys.")}
          <div class="field"><label class="switch"><span class="label">Take profit (¢)</span><input type="checkbox" data-b-toggle="useTp" ${b.useTp ? "checked" : ""}></label>
            <input class="input" data-b="tp" inputmode="decimal" value="${esc(b.tp)}" ${b.useTp ? "" : "disabled"} aria-label="Take profit in cents">
            <span class="field-help">Sell everything when buyers pay this much.</span></div>
          <div class="field"><label class="switch"><span class="label">Stop loss (¢)</span><input type="checkbox" data-b-toggle="useSl" ${b.useSl ? "checked" : ""}></label>
            <input class="input" data-b="sl" inputmode="decimal" value="${esc(b.sl)}" ${b.useSl ? "" : "disabled"} aria-label="Stop loss in cents">
            <span class="field-help">Sell everything if the price falls this low.</span></div>
          ${num("b-max", "Max position ($)", "max", "It stops buying above this exposure.")}
          ${num("b-cool", "Wait between trades (s)", "cooldown", "")}
          ${num("b-int", "Check price every (s)", "interval", "")}
          <div class="field"><label for="b-name">Name</label><input class="input" id="b-name" data-b="name" placeholder="Optional" value="${esc(b.name)}" style="font-family:var(--font)"></div>
        </div>
      </div></div>
    </div>
    <aside class="trade"><div class="panel"><div class="panel-body stack">
      <b>Your bot will</b>
      <div class="sentence">${builderSentence()}</div>
      <div class="field"><label>Test it on real history</label>
        <div class="segmented">${["1d", "1w", "1m", "max"].map((w) => `<button data-act="b-window" data-w="${w}" aria-pressed="${b.window === w}" type="button">${w === "max" ? "All" : w.toUpperCase()}</button>`).join("")}</div></div>
      <button class="ghost-btn" data-act="b-backtest" type="button" ${ready && !b.busy ? "" : "disabled"} style="height:40px">${b.busy === "backtest" ? "Testing" : "Backtest on past prices"}</button>
      ${btHtml}
      <button class="primary-btn" data-act="b-create" data-start="1" type="button" ${ready && !b.busy ? "" : "disabled"}>Start bot</button>
      <button class="ghost-btn" data-act="b-create" data-start="0" type="button" ${ready && !b.busy ? "" : "disabled"}>Save without starting</button>
      <p class="disclaimer">Bots trade your paper account using the live Polymarket book. No real orders.</p>
    </div></div></aside>
  </div>`;
}

function drawBacktestChart() {
  const b = state.builder;
  if (!b?.backtest) return;
  const bt = b.backtest;
  lineChart($("#bt-chart"), bt.prices, {
    padLeft: 40, yFmt: (v) => `${Math.round(v * 100)}¢`,
    xFmt: (t) => new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" }),
    tip: (p) => `${new Date(p[0]).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}  ${fmt.cents(p[1])}`,
    markers: bt.trades_on_chart, label: "price history with bot trades",
  });
}

async function builderBacktest() {
  const b = state.builder;
  const m = b.event.markets[b.mi];
  b.busy = "backtest"; b.error = null; render();
  try {
    b.backtest = await request("/api/bots/backtest", { event_slug: b.event.slug, token: m.tokens[b.oi], rule: builderRule(), interval: b.window });
  } catch (err) { b.error = err.message; b.backtest = null; }
  b.busy = false;
  if (state.builder === b) render();
}

async function builderCreate(start) {
  const b = state.builder;
  const m = b.event.markets[b.mi];
  b.busy = "create"; b.error = null; render();
  try {
    await request("/api/bots/new", {
      event_slug: b.event.slug, token: m.tokens[b.oi], rule: builderRule(), name: b.name || undefined,
      interval_s: Number(b.interval), start,
    });
    toast(start ? "Bot started. It trades your paper account." : "Bot saved.");
    location.hash = "#bots";
  } catch (err) { b.error = err.message; b.busy = false; render(); }
}

/* ---- strategy lab ------------------------------------------------------------------------------- */

function enterLab() {
  const p = state.route.params;
  if (!state.lab) {
    state.lab = {
      tab: "run", meta: null, algo: null, data: "synthetic", hours: 6, seed: 42, preset: "Balanced", params: {},
      stress: true, result: null, running: false, started: 0, error: null, subtab: "trades", history: [],
      compare: null, comparing: false,
    };
  }
  const lab = state.lab;
  if (p.get("tab")) lab.tab = p.get("tab");
  if (p.get("algo")) lab.algo = p.get("algo");
  if (p.get("hours")) lab.hours = Number(p.get("hours")) || lab.hours;
  render();
  loadLabMeta().then(() => {
    if (p.get("run") === "1") { if (lab.tab === "compare") runCompare(); else runLab(); }
  });
  every(500, () => {
    if (lab.running || lab.comparing) { const el = $("#elapsed"); if (el) el.textContent = `${Math.round((Date.now() - lab.started) / 1000)}s`; }
  });
}

async function loadLabMeta() {
  const lab = state.lab;
  try {
    lab.meta = await request("/api/meta");
    if (!lab.algo || !lab.meta.strategies.some((s) => s.name === lab.algo)) lab.algo = lab.meta.strategies[0].name;
    lab.seed = lab.seed ?? lab.meta.defaults.seed;
  } catch (err) { lab.error = err.message; }
  if (state.route.view === "lab") render();
}

const algoMeta = () => state.lab.meta?.strategies.find((s) => s.name === state.lab.algo);

function labParams() {
  const lab = state.lab, s = algoMeta();
  const preset = s.presets[lab.preset] || {};
  const own = lab.params[s.name] || {};
  return { ...preset, ...own };
}

function viewLab() {
  const lab = state.lab;
  const tabs = `<div class="segmented" style="max-width:420px;margin-bottom:18px">
    <button data-act="lab-tab" data-tab="run" aria-pressed="${lab.tab === "run"}" type="button">Run an algorithm</button>
    <button data-act="lab-tab" data-tab="compare" aria-pressed="${lab.tab === "compare"}" type="button">Compare all</button></div>`;
  if (lab.error && !lab.meta) return tabs + `<div class="error">${esc(lab.error)}</div>`;
  if (!lab.meta) return tabs + `<div class="skeleton" style="height:480px"></div>`;
  return tabs + (lab.tab === "compare" ? viewCompare() : viewRun());
}

function viewRun() {
  const lab = state.lab, s = algoMeta(), rec = lab.meta.recorded;
  const builtIn = lab.meta.strategies.filter((x) => !x.custom), mine = lab.meta.strategies.filter((x) => x.custom);
  const algoBtn = (x) => `<button class="algo" data-act="lab-algo" data-name="${esc(x.name)}" aria-pressed="${x.name === lab.algo}" type="button">
      <div class="between"><b>${esc(x.title)}</b><span class="tag ${x.custom ? "tag-blue" : ""}">${esc(x.category)}</span></div>
      <span>${esc(x.error ? `Failed to load: ${x.error}` : x.blurb)}</span></button>`;
  const merged = labParams();
  const paramFields = s.params.map((p) => {
    const value = merged[p.name] ?? p.default;
    if (p.type === "bool") {
      return `<label class="switch field" style="grid-column:1/-1"><span><span class="label">${esc(p.title)}</span><br>
        <span class="field-help">${esc(p.help)}</span></span><input type="checkbox" data-param="${esc(p.name)}" ${value ? "checked" : ""}></label>`;
    }
    return `<div class="field"><label for="p-${esc(p.name)}">${esc(p.title)}</label>
      <input class="input" id="p-${esc(p.name)}" data-param="${esc(p.name)}" type="number" step="any" value="${esc(value)}">
      ${p.help ? `<span class="field-help">${esc(p.help)}</span>` : ""}</div>`;
  }).join("");
  const presets = Object.keys(s.presets).length
    ? `<div class="segmented">${["Cautious", "Balanced", "Aggressive"].map((name) => `<button data-act="lab-preset" data-p="${name}"
        aria-pressed="${lab.preset === name}" type="button">${name}</button>`).join("")}</div>` : "";
  const synthetic = lab.data === "synthetic";
  return `<div class="lab">
    <aside class="lab-side">
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Algorithm</h2></div>
        <div class="panel-body algo-list">${builtIn.map(algoBtn).join("")}
          ${mine.length ? `<span class="label" style="margin-top:6px">Your algorithms</span>${mine.map(algoBtn).join("")}` : ""}
          <details><summary class="link small" style="cursor:pointer">Add your own algorithm</summary>
            <p class="field-help">Copy <span class="num">algorithms/example_momentum.py</span> in
            <span class="num">${esc(lab.meta.algorithms_dir)}</span>, edit it, then reload this page.
            It appears here with its settings. Files that import trading libraries are refused.</p></details></div></div>
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Data</h2></div><div class="panel-body stack">
        <div class="segmented">
          <button data-act="lab-data" data-d="synthetic" aria-pressed="${synthetic}" type="button">Simulated</button>
          <button data-act="lab-data" data-d="recorded" aria-pressed="${!synthetic}" ${rec.available ? "" : "disabled"} type="button">Recorded</button></div>
        <span class="field-help">${synthetic ? "Generated markets with real quirks (gaps, stale quotes, halts). Fast and repeatable."
          : `Order books you recorded from Polymarket (${rec.markets} markets).`}${rec.available ? "" : " Record live books with pmsim record to unlock recorded data."}</span>
        ${synthetic ? `<div class="params">
          <div class="field"><label for="lab-hours">Hours</label><input class="input" id="lab-hours" data-lab="hours" type="number" min="0.25" max="72" step="0.25" value="${lab.hours}"></div>
          <div class="field"><label for="lab-seed">Seed</label><div class="row"><input class="input" id="lab-seed" data-lab="seed" type="number" min="0" value="${lab.seed}">
            <button class="ghost-btn" data-act="lab-shuffle" type="button" title="New random seed">New</button></div></div></div>` : ""}
      </div></div>
      <div class="panel"><div class="panel-head"><h2 class="panel-title">Settings</h2></div><div class="panel-body stack">
        ${presets}
        <details ${Object.keys(s.presets).length ? "" : "open"}><summary class="link small" style="cursor:pointer">Fine-tune every setting</summary>
          <div class="params" style="margin-top:12px">${paramFields || `<span class="muted small">This algorithm has no settings.</span>`}</div></details>
        <label class="switch"><span><b style="font-size:13px">Stress test</b><br><span class="field-help">Also rerun with 3x latency, half the depth and double fees.</span></span>
          <input type="checkbox" data-lab-toggle="stress" ${lab.stress ? "checked" : ""}></label>
        <button class="primary-btn" data-act="lab-run" type="button" ${lab.running ? "disabled" : ""}>${lab.running ? `Running <span id="elapsed">0s</span>` : "Run"}</button>
      </div></div>
      ${lab.history.length ? `<div class="panel"><div class="panel-head"><h2 class="panel-title">Earlier runs</h2></div><div class="panel-body stack">
        ${lab.history.map((h, i) => `<button class="algo" data-act="lab-history" data-i="${i}" type="button"><div class="between"><b>${esc(titleOf(h.strategy))}</b>
          <span class="num ${signClass(h.metrics.net_pnl_usd)}">${fmt.usd(h.metrics.net_pnl_usd, true)}</span></div>
          <span>${esc(dataLine(h))}</span></button>`).join("")}</div></div>` : ""}
    </aside>
    <section class="results">${labResults()}</section></div>`;
}

const titleOf = (name) => state.lab.meta?.strategies.find((s) => s.name === name)?.title || name;
const dataLine = (r) => (r.data.kind === "synthetic" ? `${r.data.hours} simulated hours, seed ${r.data.seed}` : "Recorded order books");

function labResults() {
  const lab = state.lab, s = algoMeta();
  const err = lab.error ? `<div class="error">${esc(lab.error)}</div>` : "";
  const how = `<div class="panel"><div class="panel-body stack"><div class="between"><b>${esc(s.title)}</b><span class="tag">${esc(s.category)}</span></div>
    <span class="muted">${esc(s.how)}</span></div></div>`;
  if (lab.running) {
    return err + how + `<div class="skeleton" style="height:56px"></div><div class="skeleton" style="height:96px"></div><div class="skeleton" style="height:300px"></div>`;
  }
  const r = lab.result;
  if (!r) return err + how + `<div class="panel"><div class="empty"><h2>Press Run to test ${esc(s.title)}</h2>
    <p>You'll get a plain-English verdict, profit and risk, an equity curve, stress tests, and every trade it made.</p></div></div>`;
  const m = r.metrics;
  const counts = r.issue_counts || [];
  return err + `<div class="result-head"><h1>${esc(titleOf(r.strategy))}</h1>${dataTag(r.label)}
      <span class="muted small">${esc(dataLine(r))}. Ran in ${r.elapsed_s}s.</span></div>
    <div class="verdict ${m.net_pnl_usd > 0 ? "good" : m.net_pnl_usd < 0 ? "bad" : ""}">${esc(r.summary)}
      ${r.label === "synthetic" ? `<br><span class="small muted">Simulated data shows how the algorithm behaves, not what it would earn live.</span>` : ""}</div>
    <div class="kpis">
      <div class="kpi hero"><span class="k">Net profit</span><span class="v ${signClass(m.net_pnl_usd)}">${fmt.usd(m.net_pnl_usd, true)}</span>
        <span class="s">${fmt.pct(m.return_on_capital, 2, true)} on ${fmt.usd(r.start_cash)}</span></div>
      <div class="kpi"><span class="k">Worst drop</span><span class="v">${fmt.usd(m.max_drawdown_usd)}</span><span class="s">${fmt.pct(m.max_drawdown_pct, 2)} of peak</span></div>
      <div class="kpi"><span class="k">Win rate</span><span class="v">${m.closed_positions ? fmt.pct(m.hit_rate, 0) : "-"}</span>
        <span class="s">${m.closed_positions ? `${m.closed_positions} closed positions` : "no position closed yet"}</span></div>
      <div class="kpi"><span class="k">Sharpe</span><span class="v">${fmt.num(m.sharpe, 2)}</span><span class="s">risk-adjusted</span></div></div>
    <div class="panel"><div class="panel-head"><h2 class="panel-title">Account value</h2>
      <span class="muted small num">Fill rate ${fmt.pct(m.fill_rate, 0)}. Idle cash ${fmt.pct(m.idle_cash_fraction, 0)}.</span></div>
      <div class="panel-body"><div class="chart" id="lab-chart"></div></div></div>
    <div class="two">${stressPanel(r)}${edgePanel(r)}</div>
    <div class="panel"><div class="subtabs">
        <button class="subtab" data-act="lab-sub" data-s="trades" aria-selected="${lab.subtab === "trades"}" type="button">Trades (${r.fill_count})</button>
        <button class="subtab" data-act="lab-sub" data-s="issues" aria-selected="${lab.subtab === "issues"}" type="button">Skipped orders and events (${counts.reduce((a, c) => a + c[1], 0)})</button></div>
      <div class="panel-body">${lab.subtab === "trades" ? tradesTable(r) : issuesList(r)}</div></div>`;
}

function stressPanel(r) {
  if (!r.stress) {
    return `<div class="panel"><div class="panel-head"><h2 class="panel-title">Stress test</h2></div><div class="panel-body">
      <p class="muted" style="margin:0">Turn on the stress test to rerun with 3x latency, half the book depth and double fees. It passes only if every run stays profitable.</p></div></div>`;
  }
  const rows = r.stress.rows;
  const max = Math.max(...rows.map((x) => Math.abs(x.net_pnl)), 1e-9);
  const names = { base: "Normal", latency_x3: "Slower (3x)", "depth_x0.5": "Thinner book", fees_x2: "Double fees" };
  const bars = rows.map((x) => {
    const w = Math.abs(x.net_pnl) / max * 50;
    return `<div class="bar-row"><span>${names[x.stress] || x.stress}</span><div class="bar-track"><div class="bar-zero"></div>
      <div class="bar-fill" style="left:${x.net_pnl >= 0 ? 50 : 50 - w}%;width:${Math.max(w, 0.4)}%;background:${x.net_pnl >= 0 ? "var(--yes)" : "var(--no)"}"></div></div>
      <span class="num ${signClass(x.net_pnl)}" style="text-align:right">${fmt.usd(x.net_pnl, true)}</span></div>`;
  }).join("");
  return `<div class="panel"><div class="panel-head"><h2 class="panel-title">Stress test</h2>
      <span class="tag ${r.stress.passed ? "tag-yes" : "tag-no"}">${r.stress.passed ? "PASSED" : "FAILED"}</span></div>
    <div class="panel-body">${bars}</div></div>`;
}

function edgePanel(r) {
  const m = r.metrics, theory = m.theoretical_edge_usd, real = m.net_pnl_usd;
  const max = Math.max(Math.abs(theory), Math.abs(real), 1e-9);
  const bar = (v, c) => `<div style="height:12px;border-radius:4px;width:${Math.max(Math.abs(v) / max * 100, 0.6)}%;background:${c}"></div>`;
  return `<div class="panel"><div class="panel-head"><h2 class="panel-title">Expected vs actual</h2>
      <span class="muted small num">${theory ? `Captured ${fmt.pct(real / theory, 0)}` : ""}</span></div>
    <div class="panel-body">
      <div class="bar-row" style="grid-template-columns:96px 1fr 96px"><span class="muted">Expected</span>${bar(theory, "var(--accent)")}<span class="num" style="text-align:right">${fmt.usd(theory, true)}</span></div>
      <div class="bar-row" style="grid-template-columns:96px 1fr 96px"><span class="muted">Actual</span>${bar(real, real >= 0 ? "var(--yes)" : "var(--no)")}<span class="num ${signClass(real)}" style="text-align:right">${fmt.usd(real, true)}</span></div>
      <div class="facts">
        <div><span class="k">Paired trades done</span><span class="v">${r.legs ? `${r.legs.complete}/${r.legs.groups}` : "-"}</span></div>
        <div><span class="k">Unwind cost</span><span class="v">${fmt.usd(m.unwind_cost_usd)}</span></div>
        <div><span class="k">Lost at resolution</span><span class="v">${m.resolution_losses}</span></div></div>
    </div></div>`;
}

function tradesTable(r) {
  if (!r.fills.length) return `<p class="muted" style="margin:0">No trades in this run.</p>`;
  return `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Market</th><th>Side</th>
    <th class="right">Price</th><th class="right">Shares</th><th>Type</th><th class="right">Fee</th></tr></thead><tbody>
    ${r.fills.map((f) => `<tr><td class="num muted">${fmt.elapsed(f.ts - r.t0)}</td><td class="truncate" title="${esc(f.token)}">${esc(f.token)}</td>
      <td><span class="tag ${f.side === "BUY" ? "tag-yes" : "tag-no"}">${f.side === "BUY" ? "Buy" : "Sell"}</span></td>
      <td class="num right">${fmt.cents(f.price)}</td><td class="num right">${fmt.num(f.size)}</td>
      <td class="muted">${f.liquidity === "taker" ? "Took liquidity" : "Resting order"}</td><td class="num right">${fmt.usd(f.fee)}</td></tr>`).join("")}
    </tbody></table></div>${r.fill_count > r.fills.length ? `<p class="field-help">${r.fill_count - r.fills.length} more trades not shown.</p>` : ""}`;
}

function issuesList(r) {
  const counts = r.issue_counts || [];
  if (!counts.length) return `<p class="muted" style="margin:0">Nothing was skipped.</p>`;
  return `<div class="issue-chips">${counts.map(([k, n]) => `<span class="tag ${k.startsWith("reject") ? "tag-no" : ""}">${esc(k.replace(/^reject: /, "skipped: "))} (${n})</span>`).join("")}</div>
    <div class="feed">${r.failures.map((d) => `<div class="feed-item"><span class="num muted">${fmt.elapsed(d.ts - r.t0)}</span>
      <span><span class="tag ${d.kind === "reject" ? "tag-no" : ""}">${d.kind === "reject" ? "skipped" : esc(d.kind)}</span></span>
      <span><b>${esc(d.reason)}</b><br><span class="detail">${esc(d.detail)}</span></span></div>`).join("")}</div>`;
}

function drawLabChart() {
  const lab = state.lab;
  if (!lab?.result || lab.tab !== "run") return;
  const r = lab.result;
  lineChart($("#lab-chart"), r.equity, {
    base: r.start_cash, padLeft: 72, yFmt: (v) => fmt.usd(v), xFmt: (t) => `${t.toFixed(1)}h`,
    tip: (p) => `${p[0].toFixed(2)}h  ${fmt.usd(p[1])}  (${fmt.usd(p[1] - r.start_cash, true)})`, label: "account value",
  });
}

async function runLab() {
  const lab = state.lab;
  lab.running = true; lab.started = Date.now(); lab.error = null; render();
  try {
    const r = await request("/api/run", {
      strategy: lab.algo, data: lab.data, hours: lab.hours, seed: lab.seed, stress: lab.stress, params: labParams(),
    });
    lab.result = r; lab.subtab = "trades";
    lab.history = [r, ...lab.history].slice(0, 8);
  } catch (err) { lab.error = err.message; }
  lab.running = false;
  if (state.route.view === "lab") {
    render();
    if (window.innerWidth <= 1080) $(".results")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function viewCompare() {
  const lab = state.lab, c = lab.compare, rec = lab.meta.recorded;
  const controls = `<div class="row" style="flex-wrap:wrap;align-items:end;margin-bottom:18px;gap:12px">
    <div class="field"><label>Data</label><div class="segmented" style="width:240px">
      <button data-act="lab-data" data-d="synthetic" aria-pressed="${lab.data === "synthetic"}" type="button">Simulated</button>
      <button data-act="lab-data" data-d="recorded" aria-pressed="${lab.data === "recorded"}" ${rec.available ? "" : "disabled"} type="button">Recorded</button></div></div>
    ${lab.data === "synthetic" ? `<div class="field" style="width:110px"><label for="c-hours">Hours</label>
      <input class="input" id="c-hours" data-lab="hours" type="number" min="0.25" max="72" step="0.25" value="${lab.hours}"></div>` : ""}
    <button class="primary-btn inline" data-act="lab-compare" type="button" ${lab.comparing ? "disabled" : ""}>${lab.comparing ? `Comparing <span id="elapsed">0s</span>` : "Compare all algorithms"}</button></div>`;
  const err = lab.error ? `<div class="error">${esc(lab.error)}</div>` : "";
  if (lab.comparing) return controls + err + `<div class="skeleton" style="height:360px"></div>`;
  if (!c) return controls + err + `<div class="panel"><div class="empty"><h2>Every algorithm, same data</h2>
    <p>Runs each algorithm with its default settings and shows what it expected to make next to what it actually made.</p></div></div>`;
  const max = Math.max(...c.rows.flatMap((r) => [Math.abs(r.theoretical), Math.abs(r.realized)]), 1e-9);
  const rows = c.rows.map((r) => {
    const w = (v) => Math.max(Math.abs(v) / max * 50, 0.4);
    return `<div class="bar-row" style="grid-template-columns:200px 1fr 110px;height:44px">
      <span><b>${esc(r.title)}</b></span>
      <div class="bar-track" style="height:30px"><div class="bar-zero"></div>
        <div class="bar-fill" style="top:2px;bottom:auto;height:11px;left:${r.theoretical >= 0 ? 50 : 50 - w(r.theoretical)}%;width:${w(r.theoretical)}%;border:1.5px solid var(--accent);background:var(--accent-soft)"></div>
        <div class="bar-fill" style="top:auto;bottom:2px;height:11px;left:${r.realized >= 0 ? 50 : 50 - w(r.realized)}%;width:${w(r.realized)}%;background:${r.realized >= 0 ? "var(--yes)" : "var(--no)"}"></div></div>
      <span class="num ${signClass(r.realized)}" style="text-align:right">${fmt.usd(r.realized, true)}</span></div>`;
  }).join("");
  return controls + err + `<div class="result-head" style="margin-bottom:16px"><h1>Expected vs actual</h1>${dataTag(c.rows[0]?.label)}
      <span class="muted small">Ran in ${c.elapsed_s}s.</span></div>
    <div class="panel"><div class="panel-head"><span class="muted small">Outline: expected profit. Solid: actual profit.</span></div>
      <div class="panel-body">${rows}</div></div>`;
}

async function runCompare() {
  const lab = state.lab;
  lab.comparing = true; lab.started = Date.now(); lab.error = null; render();
  try { lab.compare = await request("/api/compare", { data: lab.data, hours: lab.hours, seed: lab.seed }); }
  catch (err) { lab.error = err.message; }
  lab.comparing = false;
  if (state.route.view === "lab") render();
}

/* ---- actions ------------------------------------------------------------------------------------- */

const ACTIONS = {
  "close-modal": (el, e) => { if (e.target.closest("[data-stop]") && !e.target.closest("[data-act='close-modal']")) return; $("#modal-root").innerHTML = ""; },
  "retry-markets": () => { state.markets.data = null; render(); loadMarkets(); },
  "interval": (el) => { state.event.interval = el.dataset.iv; state.event.history = null; render(); loadHistory(); },
  "pick-market": (el, e) => {
    if (e.target.closest("button")) return;
    const ev = state.event; ev.mi = Number(el.dataset.mi); ev.oi = 0; ev.history = null; ev.book = null; ev.quote = null;
    render(); loadHistory(); loadBook();
  },
  "quick-buy": (el) => {
    const ev = state.event; ev.mi = Number(el.dataset.mi); ev.oi = Number(el.dataset.oi); ev.side = "BUY"; ev.type = "market";
    ev.history = null; ev.book = null; ev.quote = null; render(); loadHistory(); loadBook(); $("#amt")?.focus();
  },
  "pick-outcome": (el) => {
    const ev = state.event; ev.oi = Number(el.dataset.oi); ev.history = null; ev.book = null; ev.quote = null;
    render(); loadHistory(); loadBook(); refreshQuote();
  },
  "side": (el) => { state.event.side = el.dataset.side; state.event.amount = ""; state.event.quote = null; renderTradeOnly(); },
  "add-amount": (el) => { const e = state.event; e.amount = String((Number(e.amount) || 0) + Number(el.dataset.v)); renderTradeOnly(); refreshQuote(); },
  "max-amount": () => { const e = state.event; e.amount = String(Math.floor((state.portfolio?.free_cash || 0) * 100) / 100); renderTradeOnly(); refreshQuote(); },
  "share-frac": (el) => {
    const e = state.event, held = positionsFor([selToken()]).positions[0]?.shares || 0;
    e.amount = String(Math.floor(held * Number(el.dataset.f) * 100) / 100); renderTradeOnly(); refreshQuote();
  },
  "submit-trade": submitTrade,
  "remove-limit": async (el) => {
    try { state.portfolio = await request("/api/live/remove-limit", { id: Number(el.dataset.id) }); toast("Limit order cancelled."); }
    catch (err) { toast(err.message, true); }
    if (state.route.view === "portfolio") render(); else renderTradeOnly();
  },
  "pf-tab": (el) => { state.pfTab = el.dataset.tab; render(); },
  "open-event": (el, e) => { if (e.target.closest("button")) return; location.hash = `#event/${encodeURIComponent(el.dataset.slug)}?token=${encodeURIComponent(el.dataset.token)}`; },
  "sell-position": (el) => { location.hash = `#event/${encodeURIComponent(el.dataset.slug)}?token=${encodeURIComponent(el.dataset.token)}&side=SELL`; },
  "reset-account": () => confirmBox("Reset paper account?", "This clears every position, order and trade, stops all bots, and restarts with fresh cash.",
    "Reset account", async () => {
      const cash = Number($("#reset-cash")?.value) || 10000;
      try { state.portfolio = await request("/api/portfolio/reset", { cash }); toast("Account reset."); refreshPortfolio(); render(); }
      catch (err) { toast(err.message, true); }
    }, `<div class="field"><label for="reset-cash">Starting cash ($)</label><input class="input" id="reset-cash" value="10000" inputmode="decimal"></div>`),
  "bot": async (el) => {
    try { await request(`/api/bots/${el.dataset.id}/${el.dataset.do}`, {}); toast(el.dataset.do === "start" ? "Bot started." : "Bot stopped."); }
    catch (err) { toast(err.message, true); }
    loadBots();
  },
  "toggle-log": (el) => { state.bots.open[el.dataset.id] = !state.bots.open[el.dataset.id]; render(); },
  "delete-bot": (el) => confirmBox("Delete this bot?", "Its settings and activity log are removed. Shares it bought stay in your portfolio.", "Delete bot", async () => {
    try { await request(`/api/bots/${el.dataset.id}/delete`, {}); toast("Bot deleted."); } catch (err) { toast(err.message, true); }
    loadBots();
  }),
  "b-change": () => { const b = state.builder; b.event = null; b.backtest = null; b.mid = null; render(); searchBuilder(b.query); },
  "b-event": (el) => selectBuilderEvent(el.dataset.slug),
  "b-pick": async (el) => { const b = state.builder; b.mi = Number(el.dataset.mi); b.oi = Number(el.dataset.oi); await refreshBuilderPrice(true); render(); },
  "b-template": (el) => { state.builder.template = el.dataset.t; applyTemplate(); render(); },
  "b-window": (el) => { state.builder.window = el.dataset.w; render(); },
  "b-backtest": builderBacktest,
  "b-create": (el) => builderCreate(el.dataset.start === "1"),
  "lab-tab": (el) => { state.lab.tab = el.dataset.tab; state.lab.error = null; render(); },
  "lab-algo": (el) => { state.lab.algo = el.dataset.name; state.lab.result = null; state.lab.error = null; render(); },
  "lab-data": (el) => { if (!el.disabled) { state.lab.data = el.dataset.d; render(); } },
  "lab-shuffle": () => { state.lab.seed = Math.floor(Math.random() * 100000); render(); },
  "lab-preset": (el) => { const lab = state.lab; lab.preset = el.dataset.p; delete lab.params[lab.algo]; render(); },
  "lab-run": runLab,
  "lab-compare": runCompare,
  "lab-sub": (el) => { state.lab.subtab = el.dataset.s; render(); },
  "lab-history": (el) => { const lab = state.lab; lab.result = lab.history[Number(el.dataset.i)]; lab.algo = lab.result.strategy; render(); },
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (el && ACTIONS[el.dataset.act]) ACTIONS[el.dataset.act](el, e);
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.trade && state.event) {
    if (t.dataset.trade === "type") return;
    state.event[t.dataset.trade] = t.value;
    if (t.dataset.trade === "amount") refreshQuote(); else renderPreviewOnly();
  } else if (t.id === "b-search") {
    debouncedBuilderSearch(t.value);
  } else if (t.dataset.b && state.builder) {
    state.builder[t.dataset.b] = t.value;
    state.builder.backtest = null;
    const s = $(".sentence");
    if (s) s.innerHTML = builderSentence();
  } else if (t.dataset.lab && state.lab) {
    state.lab[t.dataset.lab] = Number(t.value);
  } else if (t.dataset.param && state.lab) {
    const lab = state.lab, s = algoMeta();
    const spec = s.params.find((p) => p.name === t.dataset.param);
    lab.params[s.name] = { ...(lab.params[s.name] || {}), [spec.name]: spec.type === "bool" ? t.checked : t.value === "" ? null : Number(t.value) };
  }
});

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.dataset.trade === "type" && state.event) { state.event.type = t.value; state.event.quote = null; renderTradeOnly(); }
  if (t.dataset.bToggle && state.builder) { state.builder[t.dataset.bToggle] = t.checked; state.builder.backtest = null; render(); }
  if (t.dataset.labToggle && state.lab) state.lab[t.dataset.labToggle] = t.checked;
  if (t.dataset.param && t.type === "checkbox" && state.lab) {
    const lab = state.lab, s = algoMeta();
    lab.params[s.name] = { ...(lab.params[s.name] || {}), [t.dataset.param]: t.checked };
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#modal-root").innerHTML = "";
  if (e.key === "Enter" && e.target.dataset?.trade && e.target.dataset.trade !== "type") submitTrade();
});

const debouncedBuilderSearch = debounce((q) => searchBuilder(q), 300);

let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(afterRender, 120); });

/* ---- boot ------------------------------------------------------------------------------------------ */

refreshPortfolio();
setInterval(() => { if (!document.hidden && state.route.view !== "portfolio") refreshPortfolio(); }, 20_000);
route();
