"use strict";
/* 主题切换：浅色 / 深色 / 跟随系统（auto）。localStorage 持久化。 */
(function () {
  const KEY = "ui-theme";
  const MODES = ["auto", "light", "dark"];
  const ICONS = { auto: "🌓", light: "☀️", dark: "🌙" };
  const TITLES = { auto: "跟随系统", light: "浅色", dark: "深色" };

  function current() {
    const saved = localStorage.getItem(KEY);
    return MODES.includes(saved) ? saved : "auto";
  }

  function apply(mode) {
    const el = document.documentElement;
    if (mode === "dark") el.setAttribute("data-theme", "dark");
    else if (mode === "light") el.setAttribute("data-theme", "light");
    else el.removeAttribute("data-theme");
    try { localStorage.setItem(KEY, mode); } catch (e) { /* 隐私模式忽略 */ }
    return mode;
  }

  function refreshBtn() {
    const btn = document.getElementById("themeBtn");
    if (!btn) return;
    const mode = current();
    btn.textContent = ICONS[mode];
    btn.title = "主题：" + TITLES[mode] + "（点击切换）";
  }

  function ensureBtn() {
    const btn = document.getElementById("themeBtn");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const next = MODES[(MODES.indexOf(current()) + 1) % MODES.length];
      apply(next);
      refreshBtn();
    });
    refreshBtn();
  }

  apply(current());
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureBtn);
  } else {
    ensureBtn();
  }
  window.__uiTheme = { current, apply };
})();