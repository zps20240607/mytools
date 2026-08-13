"use strict";

const $ = (id) => document.getElementById(id);
let items = [];
let stats = {};
let cleanPlan = null; // {title, payload, dry}

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

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 ** 2) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(2) + " GB";
}

function kindTag(i) {
  if (i.kind === "git") return '<span class="tag git">.git 历史</span>';
  if (i.safe) return '<span class="tag safe">可清理</span>';
  return '<span class="tag careful">需谨慎</span>';
}

function kindGroup(kind) {
  if (kind === "node_modules") return "node_modules";
  if (kind === "pycache") return "pycache";
  if (kind === "git") return "git";
  const modelKinds = ["ollama", "lmstudio"];
  return modelKinds.includes(kind) ? "models" : "fixed";
}

function filtered() {
  const q = $("search").value.trim().toLowerCase();
  const kf = $("kindFilter").value;
  return items.filter((i) => {
    if (q && !i.path.toLowerCase().includes(q) && !i.label.toLowerCase().includes(q)) return false;
    if (kf && kindGroup(i.kind) !== kf) return false;
    return true;
  });
}

function render() {
  const s = stats;
  $("stats").innerHTML = `
    <span class="chip">总占用 <b>${fmtBytes(s.total_bytes || 0)}</b></span>
    <span class="chip ok">可清理 <b>${fmtBytes(s.cleanable_bytes || 0)}</b></span>
    <span class="chip">项目 <b>${s.items || 0}</b></span>`;
  const list = filtered();
  $("tbody").innerHTML = list.map((i) => `
    <tr>
      <td>${kindTag(i)} ${esc(i.label)}</td>
      <td class="path" title="${esc(i.path)}">${esc(i.path)}</td>
      <td class="size">${fmtBytes(i.size)}</td>
      <td class="size">${i.files.toLocaleString()}</td>
      <td class="size">
        ${i.kind === "git"
          ? `<button class="btn small" data-gc="${esc(i.id)}" ${i.size < 50 * 1024 * 1024 ? "disabled title='小于 50 MB 无需压缩'" : ""}>git gc</button>`
          : i.safe
            ? `<button class="btn small" data-clean="${esc(i.id)}">清理</button>`
            : `<button class="btn small" disabled title="模型/仓库数据，重新获取成本高">不自动清理</button>`}
      </td>
    </tr>`).join("");
  $("empty").hidden = list.length > 0;
  const scanned = s.scanned_at || "—";
  $("footer").textContent = `上次扫描 ${scanned} · 预览模式：${$("dryRun").checked ? "开启（不会真正删除）" : "关闭（会真正删除！）"} · 每 20 秒自动刷新`;
  bindRows();
}

async function load() {
  try {
    const data = await api("/api/scan");
    items = data.items || [];
    stats = data.stats || {};
    render();
  } catch (e) {
    $("empty").hidden = false;
    $("empty").textContent = "加载失败: " + e.message;
  }
}

function bindRows() {
  document.querySelectorAll("[data-clean]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = items.find((i) => i.id === btn.dataset.clean);
      if (!item) return;
      openClean(`清理 · ${item.label}`, { id: item.id, dry_run: $("dryRun").checked });
    });
  });
  document.querySelectorAll("[data-gc]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const res = await api("/api/git-gc", "POST", { id: btn.dataset.gc });
        toast(res.message || "git gc 完成", res.ok);
        setTimeout(load, 1500);
      } catch (e) {
        toast("git gc 失败: " + e.message, false);
      }
      btn.disabled = false;
    });
  });
}

/* ---- 清理弹窗 ---- */

function openClean(title, payload) {
  cleanPlan = { title, payload };
  $("cleanTitle").textContent = title;
  $("cleanLog").textContent = payload.dry_run
    ? "预览模式：不会真正删除，只报告可释放空间。"
    : "警告：将真正删除以下目录，且无法恢复！";
  $("modalClean").hidden = false;
}

async function runClean() {
  if (!cleanPlan) return;
  const btn = $("runClean");
  btn.disabled = true;
  $("cleanLog").textContent = "执行中…";
  try {
    const res = await api("/api/clean", "POST", cleanPlan.payload);
    if (res.results) {
      $("cleanLog").innerHTML = res.results.map((r) =>
        `<div class="${r.ok ? "ok" : "fail"}">${r.ok ? "✔" : "✘"} ${esc(r.message || r.id)}</div>`).join("");
    } else {
      $("cleanLog").innerHTML = `<div class="${res.ok ? "ok" : "fail"}">${esc(res.message || (res.ok ? "完成" : "失败"))}</div>`;
    }
    toast(res.summary || res.message || "完成", res.ok);
    setTimeout(load, 1500);
  } catch (e) {
    $("cleanLog").textContent = "执行失败: " + e.message;
    toast(e.message, false);
  }
  btn.disabled = false;
}

function openCleanAll() {
  const safeItems = items.filter((i) => i.safe);
  if (safeItems.length === 0) {
    toast("没有可清理的项目", false);
    return;
  }
  const dry = $("dryRun").checked;
  const total = safeItems.reduce((a, i) => a + i.size, 0);
  openClean(`${dry ? "预览" : "清理"} · 全部可清理项（${safeItems.length} 项，${fmtBytes(total)}）`,
    { ids: safeItems.map((i) => i.id), dry_run: dry });
}

/* ---- 扫描范围 ---- */

let rootsConfig = [];

async function openRoots() {
  try {
    const data = await api("/api/config");
    rootsConfig = data.config.roots || [];
    renderRoots();
    $("modalRoots").hidden = false;
  } catch (e) { toast(e.message, false); }
}

function renderRoots() {
  $("rootsList").innerHTML = rootsConfig.map((r) => `
    <li class="root-row"><span>${esc(r)}</span><button class="btn small" data-root="${esc(r)}">移除</button></li>`).join("");
  document.querySelectorAll("[data-root]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      rootsConfig = rootsConfig.filter((x) => x !== btn.dataset.root);
      try {
        await api("/api/config", "POST", { roots: rootsConfig });
        renderRoots();
        toast("已更新，正在重新扫描");
        setTimeout(load, 1500);
      } catch (e) { toast(e.message, false); }
    });
  });
}

/* ---- 事件 ---- */

$("search").addEventListener("input", render);
$("kindFilter").addEventListener("change", render);
$("dryRun").addEventListener("change", render);
$("rescan").addEventListener("click", async () => {
  try {
    await api("/api/rescan", "POST", {});
    toast("扫描已开始");
    setTimeout(load, 2000);
  } catch (e) { toast(e.message, false); }
});
$("cleanAll").addEventListener("click", openCleanAll);
$("rootsBtn").addEventListener("click", openRoots);
$("closeRoots").addEventListener("click", () => { $("modalRoots").hidden = true; });
$("closeClean").addEventListener("click", () => { $("modalClean").hidden = true; });
$("runClean").addEventListener("click", runClean);
$("addRoot").addEventListener("click", async () => {
  const v = $("newRoot").value.trim();
  if (!v) return;
  if (!rootsConfig.includes(v)) rootsConfig.push(v);
  $("newRoot").value = "";
  try {
    await api("/api/config", "POST", { roots: rootsConfig });
    renderRoots();
    toast("已更新，正在重新扫描");
    setTimeout(load, 1500);
  } catch (e) { toast(e.message, false); }
});
$("newRoot").addEventListener("keydown", (e) => { if (e.key === "Enter") $("addRoot").click(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("modalRoots").hidden = true; $("modalClean").hidden = true; }
});

load();
setInterval(load, 20000);
