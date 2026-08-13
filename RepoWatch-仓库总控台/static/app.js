"use strict";

const $ = (id) => document.getElementById(id);
let repos = [];
let sortMode = "time";
let snapTarget = null; // null=全部, 否则 {id, name}

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
  if (!res.ok) {
    const msg = data && data.error ? data.error : ("HTTP " + res.status);
    throw new Error(msg);
  }
  return data;
}

function toast(msg, ok = true, ms = 2600) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + (ok ? "ok" : "err");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

function relTime(iso) {
  if (!iso) return "无提交";
  const t = new Date(iso.replace("T", " ").replace(/-/g, "/"));
  if (isNaN(t)) return iso;
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
  return iso.slice(0, 10);
}

function repoBadges(r) {
  let html = `<span class="branch" title="当前分支">⎇ ${esc(r.branch)}</span>`;
  if (r.dirty > 0) {
    const parts = [];
    if (r.staged) parts.push(r.staged + " 暂存");
    if (r.unstaged) parts.push(r.unstaged + " 修改");
    if (r.untracked) parts.push(r.untracked + " 新增");
    html += `<span class="badge dirty" title="${esc(parts.join(" / "))}">${r.dirty} 变更</span>`;
  } else {
    html += `<span class="badge clean">干净</span>`;
  }
  if (r.ahead) html += `<span class="badge ahead">领先 ${r.ahead}</span>`;
  if (r.behind) html += `<span class="badge behind">落后 ${r.behind}</span>`;
  return html;
}

function repoCard(r) {
  const msg = r.last_message || "（空仓库）";
  return `
  <div class="card ${r.dirty ? "dirty" : ""}">
    <div class="card-top">
      <span class="repo-name" title="${esc(r.path)}">${esc(r.name)}</span>
      ${repoBadges(r)}
    </div>
    <div class="path" title="点击在资源管理器打开" data-open="${esc(r.id)}">${esc(r.path)}</div>
    <div class="last">
      <span class="msg" title="${esc(msg)}">${esc(msg.length > 60 ? msg.slice(0, 60) + "…" : msg)}</span>
      <span class="meta">${esc(r.last_hash || "-")} · ${esc(r.last_author || "-")} · ${relTime(r.last_time)}</span>
    </div>
    ${r.remote ? `<div class="remote" title="${esc(r.remote)}">↗ ${esc(r.remote)}</div>` : `<div class="remote">↗ 无远程</div>`}
    <div class="card-actions">
      <button class="btn small" data-act="editor" data-id="${esc(r.id)}">编辑器</button>
      <button class="btn small" data-act="terminal" data-id="${esc(r.id)}">终端</button>
      <button class="btn small" data-act="folder" data-id="${esc(r.id)}">目录</button>
      <button class="btn small primary" data-act="snap" data-id="${esc(r.id)}">快照</button>
    </div>
  </div>`;
}

function filtered() {
  const q = $("search").value.trim().toLowerCase();
  let list = repos.filter((r) =>
    !q || r.name.toLowerCase().includes(q) || r.path.toLowerCase().includes(q));
  if (sortMode === "name") {
    list.sort((a, b) => a.name.localeCompare(b.name, "zh"));
  } else if (sortMode === "dirty") {
    list.sort((a, b) => (b.dirty - a.dirty) || a.name.localeCompare(b.name, "zh"));
  } else {
    list.sort((a, b) => (b.last_time || "").localeCompare(a.last_time || ""));
  }
  return list;
}

function render() {
  const data = repos;
  const st = data._stats || { total: 0, dirty_repos: 0, total_changes: 0 };
  $("stats").innerHTML = `
    <span class="chip">仓库 <b>${st.total}</b></span>
    <span class="chip warn">有变更 <b>${st.dirty_repos}</b></span>
    <span class="chip">未提交变更 <b>${st.total_changes}</b></span>`;
  const list = filtered();
  $("grid").innerHTML = list.map(repoCard).join("");
  $("empty").hidden = list.length > 0;
  const scanned = data._scanned_at || "—";
  const errs = (data._errors || []).length;
  $("footer").textContent = `上次扫描 ${scanned} · ${errs ? errs + " 个仓库存在读取错误" : "全部正常"} · 每 15 秒自动刷新列表`;
  bindCards();
}

async function load() {
  try {
    const data = await api("/api/repos");
    repos = data.repos || [];
    repos._stats = data.stats || {};
    repos._errors = data.errors || [];
    repos._scanned_at = data.scanned_at;
    render();
  } catch (e) {
    $("grid").innerHTML = "";
    $("empty").hidden = false;
    $("empty").textContent = "加载失败: " + e.message;
  }
}

function bindCards() {
  document.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const repo = repos.find((r) => r.id === id);
      if (!repo) return;
      const act = btn.dataset.act;
      if (act === "snap") {
        openSnap(repo);
        return;
      }
      btn.disabled = true;
      try {
        const res = await api(`/api/repos/${id}/open`, "POST", { mode: act });
        toast(res.message || "已打开", true);
      } catch (e) {
        toast("打开失败: " + e.message, false);
      }
      btn.disabled = false;
    });
  });
  document.querySelectorAll(".path[data-open]").forEach((el) => {
    el.addEventListener("click", async () => {
      const repo = repos.find((r) => r.id === el.dataset.open);
      if (!repo) return;
      try {
        await api(`/api/repos/${repo.id}/open`, "POST", { mode: "folder" });
      } catch (e) {
        toast("打开失败: " + e.message, false);
      }
    });
  });
}

/* ---------------- 快照弹窗 ---------------- */

function openSnap(repoOrNull) {
  snapTarget = repoOrNull;
  $("snapTitle").textContent = repoOrNull ? `快照 · ${repoOrNull.name}` : "全部快照（所有有变更的仓库）";
  $("snapMessage").value = "";
  $("snapLog").textContent = "等待执行…";
  $("modalSnap").hidden = false;
}

async function runSnap() {
  const msg = $("snapMessage").value.trim();
  const btn = $("runSnap");
  btn.disabled = true;
  const log = $("snapLog");
  log.textContent = "执行中…";
  try {
    const body = msg ? { message: msg } : {};
    let res;
    if (snapTarget) {
      res = await api(`/api/repos/${snapTarget.id}/snapshot`, "POST", body);
      log.innerHTML = formatSteps(snapTarget.name, res.steps || []);
    } else {
      res = await api("/api/snapshot-all", "POST", body);
      const parts = (res.results || []).map((r) =>
        `<div class="${r.ok ? "ok" : "fail"}">=== ${esc(r.repo)} ===</div>` +
        formatSteps(r.repo, r.steps || [], true));
      log.innerHTML = parts.join("<br>") || esc(res.message || "无");
    }
    toast(snapTarget ? (res.ok ? "快照完成" : "快照存在失败步骤") : (res.summary || "完成"), res.ok);
    load();
  } catch (e) {
    log.textContent = "执行失败: " + e.message;
    toast("快照失败: " + e.message, false);
  }
  btn.disabled = false;
}

function formatSteps(name, steps, plain = false) {
  if (plain) {
    return steps.map((s) => `${s.ok ? "✔" : "✘"} [${s.step}] ${s.detail}`).join("<br>");
  }
  return steps.map((s) =>
    `<div class="${s.ok ? "ok" : "fail"}">${s.ok ? "✔" : "✘"} [${esc(s.step)}] ${esc(s.detail)}</div>`).join("");
}

/* ---------------- 扫描范围弹窗 ---------------- */

let rootsConfig = [];

async function openRoots() {
  try {
    const data = await api("/api/config");
    rootsConfig = data.config.roots || [];
    renderRoots();
    $("modalRoots").hidden = false;
  } catch (e) {
    toast("读取配置失败: " + e.message, false);
  }
}

function renderRoots() {
  $("rootsList").innerHTML = rootsConfig.map((r) => `
    <li><span>${esc(r)}</span><button class="btn small danger" data-root="${esc(r)}">移除</button></li>`).join("");
  document.querySelectorAll("[data-root]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      rootsConfig = rootsConfig.filter((x) => x !== btn.dataset.root);
      try {
        await api("/api/config", "POST", { roots: rootsConfig });
        renderRoots();
        toast("已更新，正在重新扫描");
        setTimeout(load, 1200);
      } catch (e) { toast(e.message, false); }
    });
  });
}

/* ---------------- 事件绑定 ---------------- */

$("search").addEventListener("input", render);
$("sort").addEventListener("change", (e) => { sortMode = e.target.value; render(); });
$("rescan").addEventListener("click", async () => {
  try { await api("/api/rescan", "POST", {}); toast("扫描已开始"); setTimeout(load, 1500); }
  catch (e) { toast(e.message, false); }
});
$("snapshotAll").addEventListener("click", () => openSnap(null));
$("rootsBtn").addEventListener("click", openRoots);
$("closeRoots").addEventListener("click", () => { $("modalRoots").hidden = true; });
$("addRoot").addEventListener("click", async () => {
  const v = $("newRoot").value.trim();
  if (!v) return;
  if (!rootsConfig.includes(v)) rootsConfig.push(v);
  $("newRoot").value = "";
  try {
    await api("/api/config", "POST", { roots: rootsConfig });
    renderRoots();
    toast("已更新，正在重新扫描");
    setTimeout(load, 1200);
  } catch (e) { toast(e.message, false); }
});
$("closeSnap").addEventListener("click", () => { $("modalSnap").hidden = true; });
$("runSnap").addEventListener("click", runSnap);
$("newRoot").addEventListener("keydown", (e) => { if (e.key === "Enter") $("addRoot").click(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    $("modalRoots").hidden = true;
    $("modalSnap").hidden = true;
  }
});

load();
setInterval(load, 15000);