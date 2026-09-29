// quant-core console. No build step, no dependencies: talks to the control API on the same origin.
// Every limit and the kill switch are enforced server-side (agent/src/control + qc-risk); the checks
// here only explain things earlier, they are never the gate.
"use strict";

const API = "/api";
const POLL_MS = 2000;
// Every limit is tighter when lower, except the wash-trade window (wider catches more).
const WIDER_IS_TIGHTER = new Set(["wash_trade_window_ms"]);

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const n = Object.assign(document.createElement(tag), props);
  n.append(...kids.filter((k) => k != null));
  return n;
};

let online = null;
let lastRulesJson = "";
let lastDecisions = [];

async function api(path, opts = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 5000);
  try {
    const res = await fetch(API + path, {
      ...opts,
      signal: ctrl.signal,
      headers: opts.body ? { "content-type": "application/json" } : undefined,
    });
    const text = await res.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = { error: text }; }
    if (!res.ok) {
      const err = new Error((body && (body.error || body.message)) || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return body;
  } finally {
    clearTimeout(t);
  }
}

function setOnline(ok, why) {
  if (ok === online && ok) return;
  online = ok;
  const b = $("banner");
  b.hidden = ok;
  if (!ok) b.textContent = `Control API unreachable (${why}). Start it with "just control-api". Retrying every ${POLL_MS / 1000}s.`;
  document.querySelector("#order-form button[type=submit]").disabled = !ok;
  if (!ok) {
    const m = $("mode");
    m.className = "badge badge-unknown";
    m.textContent = "OFFLINE";
  }
}

const num = (s) => (s == null || s === "" ? NaN : Number(s));
const fmt = (s) => (s == null ? "-" : String(s));

function meter(label, used, limit, unit) {
  const u = num(used), l = num(limit);
  const wrap = el("div", { className: "meter" });
  const value = el("div", { className: "meter-value" },
    Number.isFinite(u) ? `${unit}${used}` : "n/a",
    el("small", {}, Number.isFinite(l) ? ` / ${unit}${limit}` : " / no limit"));
  wrap.append(el("div", { className: "meter-label" }, el("span", {}, label),
    el("span", {}, Number.isFinite(u) && l > 0 ? `${Math.round((u / l) * 100)}%` : "")), value);
  if (Number.isFinite(u) && l > 0) {
    // low/high/optimum give native green/amber/red colouring.
    wrap.append(el("meter", { min: 0, max: l, value: Math.min(u, l), low: l * 0.6, high: l * 0.85, optimum: 0,
      ariaLabel: `${label}: ${used} of ${limit}` }));
  } else if (!Number.isFinite(u)) {
    wrap.append(el("div", { className: "muted small" }, "Not reported by the control API"));
  }
  return wrap;
}

function renderStatus(s) {
  const mode = { sim: ["SIM", "badge-sim"], paper: ["PAPER", "badge-paper"], external: ["LIVE", "badge-live"] }[s.mode]
    || [String(s.mode || "?").toUpperCase(), "badge-unknown"];
  const m = $("mode");
  m.textContent = mode[0];
  m.className = `badge ${mode[1]}`;
  m.title = s.mode === "external" ? "External venue: orders reach your broker adapter" : "No real broker is reached";

  const kill = $("kill");
  const engaged = !!(s.killSwitch && s.killSwitch.engaged);
  kill.classList.toggle("engaged", engaged);
  kill.textContent = engaged ? "Kill switch engaged" : "Kill switch";
  kill.disabled = engaged;

  const t = s.today || {};
  const L = s.limits || {};
  $("today-date").textContent = t.date ? `${t.date} (UTC)` : "";
  $("meters").replaceChildren(
    meter("Daily notional", t.notional, L.max_daily_notional, "$"),
    meter("Daily loss", t.loss ?? t.dailyLoss, L.max_daily_loss, "$"),
    meter("Order rate (/s)", t.orderRate, L.max_order_rate_per_sec, ""),
  );
  $("position").textContent = fmt(s.position);
  $("decisions-count").textContent = fmt(t.decisions);
  $("orders-count").textContent = fmt(t.ordersAccepted);
  $("halted").textContent = s.halted || (engaged ? "kill_switch" : "no");
}

function isTightening(key, from, to) {
  const f = num(from), v = num(to);
  if (!Number.isFinite(f) || !Number.isFinite(v)) return null;
  return WIDER_IS_TIGHTER.has(key) ? v >= f : v <= f;
}

function renderRules(rules) {
  const json = JSON.stringify(rules);
  if (json === lastRulesJson) return; // do not wipe what the operator is typing
  lastRulesJson = json;
  const box = $("rules");
  box.replaceChildren();
  for (const [key, value] of Object.entries(rules)) {
    const id = `rule-${key}`;
    const msg = el("p", { className: "msg", role: "status" });
    const input = el("input", { id, name: "value", value, inputMode: "decimal", required: true, autocomplete: "off" });
    const btn = el("button", { type: "submit" }, "Tighten");
    input.addEventListener("input", () => {
      const ok = isTightening(key, value, input.value);
      btn.disabled = ok === false;
      msg.className = "msg" + (ok === false ? " warn" : "");
      msg.textContent = ok === false
        ? "Loosening is not allowed from here. It needs a human edit to config/limits with two approvals."
        : "";
    });
    const form = el("form", {}, input, btn);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (input.value === value) return;
      btn.disabled = true;
      try {
        const r = await api("/rules", { method: "POST", body: JSON.stringify({ key, value: input.value }) });
        msg.className = "msg ok";
        msg.textContent = `Tightened ${r && r.from ? r.from : value} -> ${r && r.to ? r.to : input.value}. Takes effect when the bridge restarts.`;
        lastRulesJson = "";
        setTimeout(refreshRules, 1500);
      } catch (err) {
        msg.className = "msg bad";
        msg.textContent = err.message;
        btn.disabled = false;
      }
    });
    box.append(el("div", { className: "rule" },
      el("label", { className: "rule-key", htmlFor: id }, key),
      el("span", { className: "rule-dir" }, WIDER_IS_TIGHTER.has(key) ? "Tighter = higher" : "Tighter = lower"),
      form, msg));
  }
  if (!box.children.length) box.append(el("p", { className: "empty" }, "No limits found."));
}

function renderFeed(entries) {
  const feed = $("feed");
  const ordersOnly = $("orders-only").checked;
  const rows = (entries || []).filter((e) => !ordersOnly || (e.result && e.result.accepted));
  if (!rows.length) {
    feed.replaceChildren(el("li", { className: "empty" }, ordersOnly ? "No accepted orders yet." : "No decisions yet."));
    return;
  }
  feed.replaceChildren(...rows.map((e) => {
    const d = e.decision || {};
    const r = e.request || {};
    const res = e.result;
    const ts = r.ts_ns ? new Date(r.ts_ns / 1e6).toLocaleTimeString() : "";
    const outcome = !res ? el("span", { className: "pill" }, "no order")
      : res.accepted ? el("span", { className: "pill ok" }, `accepted #${res.client_order_id ?? "?"}`)
      : el("span", { className: "pill bad" }, `rejected: ${res.risk_reject || res.halted || "unknown"}`);
    return el("li", {},
      el("div", { className: "meta" },
        el("span", { className: `act act-${d.action}` }, d.action || "?"),
        el("span", {}, [r.instrument, d.qty && `${d.qty} @ ${d.limit_price}`].filter(Boolean).join(" ")),
        outcome,
        el("span", {}, [ts, e.mode, e.model].filter(Boolean).join(" · "))),
      el("p", { className: "rationale" }, d.rationale || "(no rationale)"));
  }));
}

async function refreshRules() {
  try { renderRules(await api("/rules")); } catch { /* status poll reports the outage */ }
}

async function tick() {
  try {
    const [status, decisions] = await Promise.all([api("/status"), api("/decisions?limit=50")]);
    setOnline(true);
    renderStatus(status);
    lastDecisions = decisions;
    renderFeed(decisions);
    if (!lastRulesJson) refreshRules();
  } catch (err) {
    setOnline(false, err.name === "AbortError" ? "timeout" : err.message);
  } finally {
    setTimeout(tick, POLL_MS);
  }
}

$("orders-only").addEventListener("change", () => renderFeed(lastDecisions));

$("order-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const out = $("order-result");
  const intent = {
    request_id: globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : `ui-${Date.now()}`,
    instrument: f.get("instrument").trim().toUpperCase(),
    side: f.get("side"),
    qty: f.get("qty").trim(),
    limit_price: f.get("limit_price").trim(),
    time_in_force: f.get("time_in_force"),
    reason: f.get("reason").trim(),
  };
  out.className = "result";
  out.textContent = "Submitting...";
  try {
    const r = await api("/orders", { method: "POST", body: JSON.stringify(intent) });
    out.className = "result " + (r.accepted ? "ok" : "bad");
    out.textContent = r.accepted
      ? `Accepted by risk, client order #${r.client_order_id ?? "?"}.`
      : `Rejected: ${r.risk_reject || r.halted || "unknown reason"}.`;
  } catch (err) {
    out.className = "result bad";
    out.textContent = err.message;
  }
});

$("kill").addEventListener("click", () => $("kill-dialog").showModal());
$("kill-dialog").addEventListener("close", async () => {
  if ($("kill-dialog").returnValue !== "confirm") return;
  try {
    await api("/kill", { method: "POST", body: JSON.stringify({ reason: $("kill-reason").value }) });
  } catch (err) {
    alert(`Kill switch request failed: ${err.message}\nIf the API is down, create the kill file by hand (default ops/live/state/KILL).`);
  }
});

tick();
