"use strict";

const $ = (id) => document.getElementById(id);
let account = { authed: false };
let localRepos = [];
let currentBranchRepo = null; // {id, path, name}

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

function toast(msg, ok = true, ms = 3200) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast " + (ok ? "ok" : "err");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

/* ---- 账号 ---- */

async function loadAccount() {
  try {
    account = await api("/api/account");
  } catch (e) {
    account = { authed: false, error: e.message };
  }
  renderAuth();
}

function renderAuth() {
  const el = $("authBanner");
  if (account.authed) {
    el.className = "banner ok";
    el.innerHTML = `
      <span>已连接 GitHub：<b>${esc(account.login)}</b>${account.name ? "（" + esc(account.name) + "）" : ""}</span>
      <span class="who">凭据已用 Windows DPAPI 加密保存（${esc(account.token_masked || "")}）</span>
      <button class="btn small ghost" id="logoutSmall">断开</button>`;
    $("logoutSmall").addEventListener("click", async () => {
      await api("/api/logout", "POST", {});
      account = { authed: false };
      renderAuth();
      toast("已清除凭据");
    });
  } else {
    el.className = "banner";
    el.innerHTML = `
      <span>连接 GitHub：粘贴 Personal Access Token（需要 repo 权限）</span>
      <input id="tokenInput" type="password" placeholder="ghp_… 或 github_pat_…">
      <button class="btn primary" id="authBtn">验证并保存</button>
      <span class="who">Token 只保存在本机（DPAPI 加密），可在「设置」中清除。</span>`;
    $("authBtn").addEventListener("click", async () => {
      const token = $("tokenInput").value.trim();
      if (!token) { toast("请输入 Token", false); return; }
      try {
        const res = await api("/api/auth", "POST", { token });
        account = { authed: true, login: res.login, name: res.name, token_masked: res.token_masked };
        renderAuth();
        toast("连接成功：" + res.login);
        loadLocal();
      } catch (e) {
        toast("验证失败: " + e.message, false);
      }
    });
  }
}

/* ---- 本地仓库 ---- */

async function loadLocal() {
  try {
    const data = await api("/api/repos");
    localRepos = data.repos || [];
    $("localGrid").innerHTML = localRepos.map((r) => `
      <div class="card">
        <div class="card-top">
          <span class="repo-name" title="${esc(r.path)}">${esc(r.name)}</span>
          <span class="badge ${r.dirty ? "dirty" : "clean"}">${r.dirty ? r.dirty + " 变更" : "干净"}</span>
          ${r.github ? '<span class="badge gh">已连 GitHub</span>' : ""}
        </div>
        <div class="path" title="${esc(r.path)}">${esc(r.path)}</div>
        <div class="path">⎇ ${esc(r.branch || "-")}${r.remote ? " · " + esc(r.remote) : " · 无远程"}</div>
        <div class="actions">
          <button class="btn small" data-branch="${esc(r.id)}">分支管理</button>
          <button class="btn small" data-snap="${esc(r.id)}">快照</button>
          <button class="btn small" data-push="${esc(r.id)}">推送</button>
          <button class="btn small primary" data-publish="${esc(r.id)}">发布到 GitHub</button>
        </div>
      </div>`).join("");
    $("localEmpty").hidden = localRepos.length > 0;
    bindLocal();
  } catch (e) {
    $("localEmpty").hidden = false;
    $("localEmpty").textContent = "加载失败: " + e.message;
  }
}

function bindLocal() {
  document.querySelectorAll("[data-branch]").forEach((btn) => {
    btn.addEventListener("click", () => openBranch(btn.dataset.branch));
  });
  document.querySelectorAll("[data-snap]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const res = await api("/api/repo/action", "POST", { id: btn.dataset.snap, action: "snapshot" });
        toast(res.steps ? res.steps.map((s) => s.detail).join(" · ") : "快照完成", res.ok);
        loadLocal();
      } catch (e) { toast("快照失败: " + e.message, false); }
    });
  });
  document.querySelectorAll("[data-push]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const res = await api("/api/repo/action", "POST", { id: btn.dataset.push, action: "push" });
        toast(res.message || "推送完成", res.ok);
      } catch (e) { toast("推送失败: " + e.message, false); }
    });
  });
  document.querySelectorAll("[data-publish]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const repo = localRepos.find((r) => r.id === btn.dataset.publish);
      if (!repo) return;
      openPublish(repo.path, repo.name);
    });
  });
}

/* ---- GitHub 仓库列表 ---- */

async function loadGh() {
  try {
    const data = await api("/api/gh/repos", "POST", {});
    const repos = data.repos || [];
    $("ghGrid").innerHTML = repos.map((r) => `
      <div class="card">
        <div class="card-top">
          <span class="repo-name" title="${esc(r.full_name)}">${esc(r.name)}</span>
          ${r.private ? '<span class="badge priv">私有</span>' : '<span class="badge gh">公开</span>'}
        </div>
        <div class="desc">${esc(r.description || "（无描述）")}</div>
        <div class="path">默认分支 ${esc(r.default_branch || "-")} · 更新于 ${esc(r.updated_at)}</div>
        <div class="actions">
          <button class="btn small" data-open="${esc(r.html_url)}">打开网页</button>
          <button class="btn small primary" data-clone="${esc(r.clone_url)}">克隆到本地</button>
        </div>
      </div>`).join("");
    $("ghEmpty").hidden = repos.length > 0;
    bindGh();
  } catch (e) {
    $("ghEmpty").hidden = false;
    $("ghEmpty").textContent = "加载失败: " + e.message + "（请先连接 GitHub）";
  }
}

function bindGh() {
  document.querySelectorAll("[data-open]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const url = btn.dataset.open;
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.click();
    });
  });
  document.querySelectorAll("[data-clone]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const res = await api("/api/gh/clone", "POST", { clone_url: btn.dataset.clone });
        toast(res.message || "克隆完成");
        loadLocal();
      } catch (e) { toast("克隆失败: " + e.message, false); }
      btn.disabled = false;
    });
  });
}

/* ---- 发布 ---- */

function openPublish(path, name) {
  $("pubPath").value = path || "";
  $("pubName").value = name || "";
  $("pubDesc").value = "";
  $("pubMessage").value = "";
  $("pubPrivate").checked = settings.defPrivate;
  $("pubOverwrite").checked = false;
  $("pubLog").textContent = "等待执行…";
  $("modalPublish").hidden = false;
}

async function runPublish() {
  const btn = $("runPublish");
  btn.disabled = true;
  $("pubLog").textContent = "执行中…";
  try {
    const res = await api("/api/publish", "POST", {
      path: $("pubPath").value.trim(),
      name: $("pubName").value.trim(),
      private: $("pubPrivate").checked,
      description: $("pubDesc").value.trim(),
      message: $("pubMessage").value.trim(),
      overwrite_remote: $("pubOverwrite").checked,
    });
    $("pubLog").innerHTML = (res.steps || []).map((s) =>
      `<div class="${s.ok ? "ok" : "fail"}">${s.ok ? "✔" : "✘"} [${esc(s.step)}] ${esc(s.detail)}</div>`).join("");
    toast(res.html_url ? ("发布成功: " + res.html_url) : "发布完成", res.ok);
    loadLocal();
  } catch (e) {
    $("pubLog").textContent = "发布失败: " + e.message;
    toast("发布失败: " + e.message, false);
  }
  btn.disabled = false;
}

/* ---- 分支管理 ---- */

async function openBranch(repoId) {
  const repo = localRepos.find((r) => r.id === repoId);
  if (!repo) return;
  currentBranchRepo = repo;
  $("branchTitle").textContent = "分支管理 · " + repo.name;
  $("conflictBox").innerHTML = "";
  $("newBranch").value = "";
  await refreshBranches();
  $("modalBranch").hidden = false;
}

async function refreshBranches() {
  if (!currentBranchRepo) return;
  try {
    const data = await api(`/api/repo/${currentBranchRepo.id}/branches`);
    const branches = data.branches || [];
    $("branchBody").innerHTML = branches.map((b) => {
      const status = b.current
        ? '<span class="badge gh">当前</span>'
        : (b.merged ? '<span class="badge clean">已合并</span>' : '<span class="badge dirty">未合并</span>');
      const ab = (b.ahead || b.behind)
        ? `${b.ahead}/${b.behind}` : "-";
      return `
      <tr>
        <td class="branch">${esc(b.name)}</td>
        <td>${status}</td>
        <td class="num">${ab}</td>
        <td class="branch">${esc(b.upstream || "-")}</td>
        <td class="num">
          ${!b.current ? `<button class="btn small" data-switch="${esc(b.name)}">切换</button>` : ""}
          ${!b.current ? `<button class="btn small" data-merge="${esc(b.name)}">合并到当前</button>` : ""}
          ${!b.current ? `<button class="btn small" data-del="${esc(b.name)}">删除</button>` : ""}
        </td>
      </tr>`;
    }).join("");
    bindBranch();
  } catch (e) {
    toast("读取分支失败: " + e.message, false);
  }
}

function bindBranch() {
  document.querySelectorAll("[data-switch]").forEach((btn) => {
    btn.addEventListener("click", () => branchAction("switch", { name: btn.dataset.switch }));
  });
  document.querySelectorAll("[data-merge]").forEach((btn) => {
    btn.addEventListener("click", () => branchAction("merge", { name: btn.dataset.merge }));
  });
  document.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.del;
      const data = await api(`/api/repo/${currentBranchRepo.id}/branches`);
      const target = (data.branches || []).find((b) => b.name === name);
      let force = false;
      if (target && !target.merged) {
        force = confirm(`分支「${name}」还没有合并，删除会丢失这些提交。确定强制删除吗？`);
        if (!force) return;
      } else if (!confirm(`确定删除分支「${name}」吗？`)) {
        return;
      }
      branchAction("delete_branch", { name, force });
    });
  });
}

async function branchAction(action, payload) {
  if (!currentBranchRepo) return;
  try {
    const res = await api("/api/repo/action", "POST", {
      id: currentBranchRepo.id, action, ...payload });
    if (res.conflict && res.files) {
      $("conflictBox").innerHTML = `
        <div class="conflict">
          <b>${esc(res.message)}</b><br>冲突文件：${res.files.map(esc).join("、")}<br>
          <button class="btn small danger" id="abortMerge">放弃合并</button>
        </div>`;
      $("abortMerge").addEventListener("click", () => branchAction("abort_merge", {}));
    } else {
      $("conflictBox").innerHTML = "";
      toast(res.message || "操作完成", res.ok !== false);
    }
    await refreshBranches();
    loadLocal();
  } catch (e) {
    toast("操作失败: " + e.message, false);
  }
}

/* ---- 设置 ---- */

let settings = { roots: [], defPrivate: false };

async function openSettings() {
  try {
    const data = await api("/api/config");
    settings = { roots: data.config.roots || [], defPrivate: !!data.config.default_private };
    renderRoots();
    $("defPrivate").checked = settings.defPrivate;
    $("modalSettings").hidden = false;
  } catch (e) { toast(e.message, false); }
}

function renderRoots() {
  $("rootsList").innerHTML = settings.roots.map((r) => `
    <li class="root-row"><span>${esc(r)}</span><button class="btn small" data-root="${esc(r)}">移除</button></li>`).join("");
  document.querySelectorAll("[data-root]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      settings.roots = settings.roots.filter((x) => x !== btn.dataset.root);
      await api("/api/config", "POST", { roots: settings.roots });
      renderRoots();
      loadLocal();
    });
  });
}

/* ---- 全局事件 ---- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const which = tab.dataset.tab;
    $("localView").hidden = which !== "local";
    $("ghView").hidden = which !== "github";
    if (which === "github") loadGh();
  });
});
$("refreshBtn").addEventListener("click", () => { loadLocal(); loadAccount(); });
$("settingsBtn").addEventListener("click", openSettings);
$("closeSettings").addEventListener("click", () => { $("modalSettings").hidden = true; });
$("closePublish").addEventListener("click", () => { $("modalPublish").hidden = true; });
$("runPublish").addEventListener("click", runPublish);
$("closeBranch").addEventListener("click", () => { $("modalBranch").hidden = true; });
$("createBranch").addEventListener("click", async () => {
  const name = $("newBranch").value.trim();
  if (!name) { toast("请输入分支名", false); return; }
  await branchAction("create_branch", { name });
  $("newBranch").value = "";
});
$("pullBtn").addEventListener("click", () => branchAction("pull", {}));
$("pushBtn").addEventListener("click", () => branchAction("push", {}));
$("snapBtn").addEventListener("click", () => branchAction("snapshot", {}));
$("addRoot").addEventListener("click", async () => {
  const v = $("newRoot").value.trim();
  if (!v) return;
  if (!settings.roots.includes(v)) settings.roots.push(v);
  $("newRoot").value = "";
  await api("/api/config", "POST", { roots: settings.roots });
  renderRoots();
  loadLocal();
});
$("defPrivate").addEventListener("change", async () => {
  settings.defPrivate = $("defPrivate").checked;
  await api("/api/config", "POST", { default_private: settings.defPrivate });
});
$("logoutBtn").addEventListener("click", async () => {
  await api("/api/logout", "POST", {});
  account = { authed: false };
  renderAuth();
  toast("已清除 GitHub 凭据");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    ["modalPublish", "modalBranch", "modalSettings"].forEach((id) => { $(id).hidden = true; });
  }
});

loadAccount();
loadLocal();
