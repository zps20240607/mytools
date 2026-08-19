"use strict";

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
  return data;
}

function toast(msg, ok = true, ms = 3000) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + (ok ? "ok" : "err");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

function money(v) { return "$" + (v || 0).toFixed(4); }
function tokens(v) { return (v || 0).toLocaleString(); }
function rmb(v, rate) { return "¥" + ((v || 0) * (rate || 7.2)).toFixed(2); }
function priceUnit(price) { return (price && price.currency === "cny") ? "¥" : "$"; }

async function load() {
  const days = $("days").value;
  try {
    const data = await api("/api/stats?days=" + days);
    render(data);
  } catch (e) {
    $("cards").innerHTML = "";
    $("chart").innerHTML = "";
    $("byTool").innerHTML = "";
    $("byModel").innerHTML = "";
    $("notice").textContent = "加载失败: " + e.message;
  }
}

function render(data) {
  const s = data.summary;
  const rate = ((data.prices || {}).rate || {}).cny_per_usd || 7.2;
  $("stats").innerHTML = `
    <span class="chip ok">估算成本 <b>${money(s.estimated_usd)}</b></span>
    <span class="chip rmb">RMB <b>${rmb(s.estimated_usd, rate)}</b></span>
    <span class="chip">上报成本 <b>${money(s.reported_cost)}</b></span>
    <span class="chip">Token <b>${tokens(s.total)}</b></span>
    <span class="chip">记录 <b>${tokens(s.records)}</b></span>`;
  $("cards").innerHTML = `
    <div class="card"><b class="money">${money(s.estimated_usd)}</b> <b class="money rmb">${rmb(s.estimated_usd, rate)}</b><span>估算总成本（近 ${s.days || "全部"} 天）</span></div>
    <div class="card"><b>${tokens(s.input + s.cached + s.cache_write)}</b><span>输入 Token（含缓存）</span></div>
    <div class="card"><b>${tokens(s.output)}</b><span>输出 Token</span></div>
    <div class="card"><b>${tokens(s.reasoning)}</b><span>推理 Token</span></div>
    <div class="card"><b>${tokens(s.records)}</b><span>记录条数</span></div>
    <div class="card"><b>${money(s.reported_cost)}</b><span>工具上报成本（对照）</span></div>`;

  const daily = [...(data.daily || [])].reverse();
  const maxCost = Math.max(0.0001, ...daily.map((d) => d.estimated_usd));
  $("chart").innerHTML = daily.map((d) => `
    <div class="bar-wrap" title="${esc(d.day)} · ${money(d.estimated_usd)} · ${tokens(d.total)} Token">
      <div class="bar" style="height:${Math.max(2, (d.estimated_usd / maxCost) * 100)}%"></div>
      <div class="bar-label">${esc(d.day.slice(5))}</div>
    </div>`).join("") || '<span class="empty">暂无数据</span>';

  $("byTool").innerHTML = data.by_tool.map((r) => `
    <tr><td>${esc(r.key)}</td><td class="money">${money(r.estimated_usd)}</td>
    <td class="num">${tokens(r.total)}</td><td class="num">${tokens(r.records)}</td>
    <td class="num">${money(r.reported_cost)}</td></tr>`).join("");

  $("byModel").innerHTML = data.by_model.map((r) => `
    <tr><td class="model">${esc(r.key)}</td><td>${esc(r.price_name)}</td>
    <td class="money">${money(r.estimated_usd)}</td><td class="num">${tokens(r.total)}</td>
    <td class="num">${priceUnit(r.price)}${r.price.input}/M</td><td class="num">${priceUnit(r.price)}${r.price.output}/M</td></tr>`).join("");

  const unknown = (data.models || []).filter((m) => m.price_name.indexOf("默认") >= 0);
  $("unknownSec").hidden = unknown.length === 0;
  $("unknownCount").textContent = unknown.length + " 个模型 / " + tokens(unknown.reduce((a, m) => a + (m.records || 0), 0)) + " 条记录未匹配，可在「价格表」中补充规则后自动重新估算";
  $("unknownList").innerHTML = unknown.map((m) => `
    <tr><td class="model">${esc(m.model)}</td><td class="num">${tokens(m.total)}</td>
    <td class="num">${tokens(m.records)}</td></tr>`).join("");

  $("footer").textContent = `数据库（只读）: ${data.db || "—"} · 每 60 秒自动刷新 · 估算口径：input×输入价 + output×输出价 + cached×缓存价 + cache_write×写入价 + reasoning×输出价`;
}

/* ---- 价格表 ---- */

let cachedPrices = null;

async function openPrices() {
  try {
    const data = await api("/api/prices");
    cachedPrices = data.prices;
    $("pricesText").value = JSON.stringify(data.prices, null, 2);
    $("modalPrices").hidden = false;
  } catch (e) { toast("读取价格表失败: " + e.message, false); }
}

async function savePrices(pricesObj) {
  try {
    const res = await api("/api/prices", "POST", { prices: pricesObj });
    toast("价格表已保存");
    $("modalPrices").hidden = true;
    load();
  } catch (e) { toast("保存失败: " + e.message, false); }
}

$("refresh").addEventListener("click", load);
$("days").addEventListener("change", load);
$("pricesBtn").addEventListener("click", openPrices);
$("closePrices").addEventListener("click", () => { $("modalPrices").hidden = true; });
$("savePrices").addEventListener("click", () => {
  let obj = null;
  try { obj = JSON.parse($("pricesText").value); }
  catch (e) { toast("JSON 格式错误: " + e.message, false); return; }
  if (!obj || !Array.isArray(obj.rules) || !obj.fallback) {
    toast("价格表必须包含 rules 数组与 fallback 对象", false);
    return;
  }
  savePrices(obj);
});
$("resetPrices").addEventListener("click", async () => {
  const data = await api("/api/prices");
  cachedPrices = data.prices;
  $("pricesText").value = JSON.stringify(data.prices, null, 2);
  toast("已恢复为当前保存的价格表");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("modalPrices").hidden = true;
});

load();
setInterval(load, 60000);
