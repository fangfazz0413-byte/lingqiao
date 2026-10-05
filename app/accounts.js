/* 灵桥 3.3 · Claude Code 双账号会话：把另一个账号的会话条目补过来（只复制侧栏条目，聊天记录不动）。
   点开左侧 Claude Code 下面的「双账号会话」时才加载；接口都带 X-Bridge-Token 头，token 不进 URL。 */
(function () {
  "use strict";
  // Windows 上没有 ⌘Q：Claude 桌面版关掉窗口只是缩到托盘，要在托盘图标上右键退出。
  const ON_WINDOWS = typeof navigator !== "undefined" && /Windows/.test(navigator.userAgent || "");
  const QUIT = ON_WINDOWS ? "在右下角托盘里右键 Claude 图标、选「退出」" : "在桌面版里按 ⌘Q 完全退出";
  const QUIT_DONE = ON_WINDOWS ? "我已经在托盘里把 Claude 桌面版退出了" : "我已经在 Claude 桌面版里按 ⌘Q 完全退出了";
  const WINDOW_TEXT = { 3: "最近 3 小时", 5: "最近 5 小时", 24: "最近 24 小时", 48: "最近 2 天", 168: "最近 7 天", 0: "不限时间" };
  const S = { host: null, visible: false, status: null, preview: null, previewKey: "", busy: false, timer: null, lastRun: null,
    form: null, modal: null, error: "" };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function notify(message, ok = true) { if (typeof window.toast === "function") window.toast(message, ok); }
  function el(selector) { return S.host ? S.host.querySelector(selector) : null; }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtTime(ms) {
    if (!ms) return "—";
    const d = new Date(ms);
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function fmtBytes(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round((n || 0) / 1024)) + " KB"; }

  async function api(url, { method = "GET", body, timeoutMs = 180000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers = new Headers();
    headers.set("X-Bridge-Token", window.BRIDGE_TOKEN || "");
    if (body !== undefined) headers.set("Content-Type", "application/json");
    try {
      const response = await fetch(url, { method, headers, signal: controller.signal, cache: "no-store", credentials: "same-origin",
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      let data;
      try { data = await response.json(); } catch { throw new Error(`服务返回了无效数据（HTTP ${response.status}）`); }
      if (!response.ok || data?.error) throw new Error(data?.error || `HTTP ${response.status}`);
      return data;
    } finally { clearTimeout(timer); }
  }

  /* ---------- 纯函数（测试用 _t 暴露） ---------- */
  function planKey(form) { return JSON.stringify([form?.source || "", form?.target || "", Number(form?.window ?? 24), !!form?.both]); }
  function targets(accounts) { return (accounts || []).filter(a => a.target_org); }
  function defaultForm(status) {
    // 默认：最近在用的账号 → 另一个有名字的账号，两边互相补齐（切到哪个账号都能看到全部会话）。
    const usable = targets(status?.accounts);
    const labeled = usable.filter(a => a.label);
    const pool = labeled.length >= 2 ? labeled : usable;
    const source = pool.find(a => a.recent) || pool[0];
    const target = pool.find(a => a !== source);
    return { source: source?.id || "", target: target?.id || "", window: 24, both: true };
  }
  function canSync(state) {
    const claude = state?.status?.claude;
    if (state?.busy || !state?.preview || state.preview.nothing) return false;
    if (state.previewKey !== planKey(state.form)) return false;
    return claude?.running === false;
  }
  function syncBlockReason(state) {
    if (state?.busy) return "正在处理…";
    if (!state?.preview || state.previewKey !== planKey(state.form)) return "先点「预览」看看要复制哪些";
    if (state.preview.nothing) return "两边已经一致，不用同步";
    const claude = state.status?.claude;
    if (claude?.running === null || claude?.running === undefined) return "没法确认 Claude 桌面版有没有退出";
    if (claude.running) return `Claude 桌面版还开着：先${QUIT}`;
    return "";
  }
  function totals(plans) {
    return (plans || []).reduce((sum, p) => ({ copy: sum.copy + (p.counts?.copy || 0), update: sum.update + (p.counts?.update || 0) }), { copy: 0, update: 0 });
  }
  function skippedCount(counts) {
    return ["no_transcript", "deleted", "no_id", "duplicate", "conflict"].reduce((n, k) => n + (counts?.[k] || 0), 0);
  }
  function claudeText(claude) {
    if (!claude || claude.running === null || claude.running === undefined) return { cls: "warn", text: `没法确认 Claude 桌面版有没有退出${claude?.error ? "（" + claude.error + "）" : ""}，先不能同步。预览不受影响。` };
    if (claude.running) return { cls: "bad", text: `Claude 桌面版还开着（${claude.count || claude.blocking?.length || 1} 个进程）。同步前先${QUIT}——在桌面版里跑着的 AI 对话也会一起停。预览不受影响。` };
    return { cls: "ok", text: "Claude 桌面版已经退出，可以同步。" };
  }

  /* ---------- 弹窗 ---------- */
  function confirmDialog({ title, body, ok = "确定", check = "" }) {
    return new Promise(resolve => {
      const mask = el("#as-modal");
      if (!mask) { resolve(false); return; }
      mask.innerHTML = `<div class="modal as-modal" role="dialog" aria-modal="true"><h3>${esc(title)}</h3><div class="m-body">${body}</div>
        ${check ? `<label class="as-check as-modal-check"><input type="checkbox" id="as-modal-check"> ${esc(check)}</label>` : ""}
        <div class="m-actions"><button class="btn" data-modal="no">取消</button><button class="btn as-primary" data-modal="yes" ${check ? "disabled" : ""}>${esc(ok)}</button></div></div>`;
      mask.classList.add("show");
      const done = value => { mask.classList.remove("show"); mask.innerHTML = ""; S.modal = null; resolve(value); };
      S.modal = done;
      const box = mask.querySelector("#as-modal-check");
      box?.addEventListener("change", () => { mask.querySelector('[data-modal="yes"]').disabled = !box.checked; });
      mask.querySelector('[data-modal="no"]')?.addEventListener("click", () => done(false));
      mask.querySelector('[data-modal="yes"]')?.addEventListener("click", () => done(true));
    });
  }

  /* ---------- 动作 ---------- */
  function requestBody(form) {
    return { source: form.source, target: form.target, window_hours: Number(form.window), both: !!form.both };
  }
  async function runPreview(deps = {}) {
    const call = deps.api || api;
    const form = { ...S.form };
    if (!form.source || !form.target) { notify("先选好两个账号", false); return null; }
    if (form.source === form.target) { notify("来源和目标不能是同一个账号", false); return null; }
    S.busy = true; render();
    try {
      const result = await call("/api/accounts/preview", { method: "POST", body: requestBody(form) });
      S.preview = result; S.previewKey = planKey(form);
      if (result.claude) S.status = { ...(S.status || {}), claude: result.claude };
      return result;
    } catch (error) { notify("预览失败：" + error.message, false); return null; }
    finally { S.busy = false; render(); }
  }
  async function requestSync(deps = {}) {
    const call = deps.api || api, ask = deps.confirm || confirmDialog;
    if (!canSync(S)) { notify(syncBlockReason(S) || "现在不能同步", false); return null; }
    const plans = S.preview.plans || [], sum = totals(plans), backup = S.preview.backup || S.status?.backup || {};
    const lines = plans.map(p => `· ${esc(p.source_label)} → <b>${esc(p.target_label)}</b>：复制 <b>${p.counts.copy}</b> 条，刷新标题 <b>${p.counts.update}</b> 个`).join("<br>");
    const ok = await ask({ title: "先备份，再同步？", ok: "备份并同步", check: QUIT_DONE,
      body: `会先把整个 <code>claude-code-sessions</code> 备份到<br><code>${esc(backup.dir || "")}</code>${backup.fallback ? "（设置的备份目录现在不在，先放在灵桥本地）" : ""}，逐个文件核对；然后：<br>${lines}<br>只复制侧栏条目，聊天记录本身不动；目标账号删过的会话不会复活。做完可以在「同步记录」里撤销。` });
    if (!ok) return null;
    S.busy = true; render();
    try {
      const result = await call("/api/accounts/sync", { method: "POST", body: { ...requestBody(S.form), confirm: true } });
      if (result.nothing) { notify("两边已经一致，没有要补的"); S.preview = null; S.previewKey = ""; }
      else {
        S.lastRun = result.run; S.preview = null; S.previewKey = "";
        notify(`同步好了：复制 ${result.run.totals.copied} 条、刷新标题 ${result.run.totals.updated} 个` + (result.run.totals.failed ? `，${result.run.totals.failed} 条没成` : ""), !result.run.totals.failed);
      }
      return result;
    } catch (error) { notify("同步没做：" + error.message, false); return null; }
    finally { S.busy = false; await loadStatus(deps); }
  }
  async function requestUndo(runId, deps = {}) {
    const call = deps.api || api, ask = deps.confirm || confirmDialog;
    const ok = await ask({ title: "撤销这次同步？", ok: "撤销", check: QUIT_DONE,
      body: "会先再备份一次，然后把这次复制进去、之后<b>没被桌面版动过</b>的会话条目挪到灵桥的撤销区（不删除），刷新过的标题改回原样。<br>桌面版里打开过的条目会保留不动，结果里会列出来。" });
    if (!ok) return null;
    S.busy = true; render();
    try {
      const result = await call("/api/accounts/undo", { method: "POST", body: { run_id: runId } });
      const undone = result.run.undone;
      S.lastRun = result.run;
      notify(`撤销好了：挪走 ${undone.moved.length} 条、标题改回 ${undone.restored.length} 个` + (undone.kept.length ? `、${undone.kept.length} 条没动` : ""));
      return result;
    } catch (error) { notify("撤销没做：" + error.message, false); return null; }
    finally { S.busy = false; await loadStatus(deps); }
  }
  async function openClaude(deps = {}) {
    try { await (deps.api || api)("/api/accounts/open-claude", { method: "POST", body: {} }); notify("已经让 Claude 桌面版打开；在桌面版里切到目标账号就能看到"); }
    catch (error) { notify("打开失败：" + error.message, false); }
  }
  async function loadStatus(deps = {}) {
    try {
      S.status = await (deps.api || api)("/api/accounts/status");
      S.error = "";
      if (!S.form || !S.status.accounts.some(a => a.id === S.form.source) || !S.status.accounts.some(a => a.id === S.form.target)) S.form = defaultForm(S.status);
    } catch (error) { S.error = error.message; }
    render();
  }
  async function pollClaude() {
    clearTimeout(S.timer);
    if (!S.visible) return;
    try {
      const claude = await api("/api/accounts/claude");
      if (S.status) { const before = S.status.claude?.running; S.status.claude = claude; if (before !== claude.running) render(); }
    } catch { /* 下次再查 */ }
    S.timer = setTimeout(pollClaude, document.hidden ? 10000 : 3000);
  }

  /* ---------- 渲染 ---------- */
  function accountCard(a) {
    const orgNote = a.ambiguous ? `<div class="as-dim">有 ${a.orgs.filter(o => o.sessions).length} 个组织目录都有会话，默认补到最近用的 ${esc(a.target_org?.slice(0, 8))}…</div>` : "";
    const noTarget = a.target_org ? "" : `<div class="as-warn-text">这个账号下还没有会话，不能当目标：先用它在桌面版里新建一条会话</div>`;
    return `<div class="as-acc${a.recent ? " recent" : ""}${a.label ? "" : " other"}">
      <div class="as-acc-name">${esc(a.name)}${a.recent ? ' <span class="as-chip run">最近活跃</span>' : ""}</div>
      <div class="as-acc-id">${esc(a.short)}…</div>
      <div class="as-acc-stats"><b>${a.sessions}</b> 条会话 · 删过 ${a.tombstones} · 最近活动 ${esc(fmtTime(a.newest))}</div>${orgNote}${noTarget}</div>`;
  }
  function options(accounts, selected, onlyTargets) {
    return (accounts || []).filter(a => !onlyTargets || a.target_org)
      .map(a => `<option value="${esc(a.id)}"${a.id === selected ? " selected" : ""}>${esc(a.name)}（${esc(a.short)}…）</option>`).join("");
  }
  function itemList(items, kind) {
    if (!items?.length) return '<div class="as-dim">没有</div>';
    return `<ul class="as-list">${items.map(item => kind === "update"
      ? `<li><span class="as-old">${esc(item.title_old || "（无标题）")}</span> → <b>${esc(item.title_new)}</b></li>`
      : `<li>${esc(item.title || "（无标题）")}<span class="as-dim"> · ${esc(fmtTime(item.last))}${item.reason_text ? " · " + esc(item.reason_text) : ""}</span></li>`).join("")}</ul>`;
  }
  function planCard(p) {
    const c = p.counts || {};
    const skipped = skippedCount(c);
    return `<div class="as-card as-plan">
      <div class="as-plan-head"><b>${esc(p.source_label)} → ${esc(p.target_label)}</b><span class="as-dim">补到组织目录 ${esc(String(p.target_org).slice(0, 8))}… · ${esc(WINDOW_TEXT[p.window_hours] || "")}</span></div>
      <div class="as-counts"><span class="as-chip ${c.copy ? "ok" : "none"}">新复制 ${c.copy}</span><span class="as-chip ${c.update ? "run" : "none"}">刷新标题 ${c.update}</span>
        <span class="as-chip none">已在 ${c.already}</span><span class="as-chip ${skipped ? "warn" : "none"}">跳过 ${skipped}</span>
        <span class="as-chip none">不在时间范围 ${c.out_of_window}</span>${c.unreadable ? `<span class="as-chip warn">读不了 ${c.unreadable}</span>` : ""}</div>
      <details ${c.copy ? "open" : ""}><summary>新复制 ${c.copy} 条</summary>${itemList(p.copy)}</details>
      <details ${c.update ? "open" : ""}><summary>刷新标题 ${c.update} 个（来源那边聊出了新标题）</summary>${itemList(p.update, "update")}</details>
      <details><summary>跳过 ${skipped} 条（聊天记录不在本机 / 目标账号删过 / 同名冲突…）</summary>${itemList(p.skipped)}</details>
      <details><summary>已经在目标账号里 ${c.already} 条</summary>${itemList(p.already)}</details></div>`;
  }
  function runCard(run) {
    if (!run) return "";
    const t = run.totals || {}, b = run.backup || {}, undone = run.undone;
    return `<div class="as-card as-done${undone ? " undone" : ""}">
      <div class="as-plan-head"><b>${undone ? "这次同步已撤销" : "同步好了"}</b><span class="as-dim">${esc(run.at)} · ${esc(run.direction)}</span></div>
      <div class="as-counts"><span class="as-chip ok">复制 ${t.copied}</span><span class="as-chip run">刷新标题 ${t.updated}</span>${t.failed ? `<span class="as-chip bad">没成 ${t.failed}</span>` : ""}</div>
      <div class="as-line">备份：<code>${esc(b.path)}</code>（${b.files} 个文件，${esc(fmtBytes(b.bytes))}，已逐个核对${b.fallback ? "；设置的备份目录不在，放在了灵桥本地" : ""}）</div>
      ${(run.warnings || []).map(w => `<div class="as-warn-text">${esc(w)}</div>`).join("")}
      ${undone ? `<div class="as-line">撤销：挪走 ${undone.moved.length} 条、标题改回 ${undone.restored.length} 个${undone.kept.length ? `，${undone.kept.length} 条没动（${esc(undone.kept.map(k => k.title || k.file).slice(0, 3).join("、"))}…）` : ""}；挪走的在 <code>${esc(undone.holding)}</code></div>`
        : `<div class="as-line">下一步：打开 Claude 桌面版 → 切到「${esc((run.results || []).map(r => r.target_label).join("」「"))}」→ 侧栏里应能看到这些会话。</div>
           <div class="as-actions"><button class="btn as-primary" data-act="open-claude">打开 Claude 桌面版</button><button class="btn" data-act="undo" data-run="${esc(run.id)}">撤销这次同步</button></div>`}</div>`;
  }
  function runsTable(runs) {
    if (!runs?.length) return '<div class="as-dim">还没有用灵桥同步过。</div>';
    return `<div class="as-runs">${runs.map(r => `<div class="as-run"><span class="as-run-at">${esc(r.at)}</span><span class="as-run-dir">${esc(r.direction)}</span>
      <span class="as-dim">复制 ${r.totals.copied} · 刷新 ${r.totals.updated}${r.totals.failed ? " · 没成 " + r.totals.failed : ""}</span>
      <span class="as-run-state">${r.undone ? '<span class="as-chip none">已撤销</span>' : `<button class="btn as-mini" data-act="undo" data-run="${esc(r.id)}">撤销</button>`}</span>
      <span class="as-run-backup" title="${esc(r.backup?.path)}">${esc(String(r.backup?.path || "").split("/").pop())}</span></div>`).join("")}</div>`;
  }
  function render() {
    if (!S.host || !S.visible) return;
    const status = S.status;
    if (!status) { el("#as-body").innerHTML = S.error ? `<div class="empty">读不到账号目录：${esc(S.error)}</div>` : '<div class="loading">正在读取账号目录…</div>'; return; }
    if (!status.available) { el("#as-body").innerHTML = `<div class="empty">${esc(status.reason || "没找到 Claude 桌面版的会话目录")}<br>其他功能不受影响。</div>`; return; }
    const claude = claudeText(status.claude), form = S.form || defaultForm(status);
    const block = syncBlockReason(S);
    const preview = S.preview && S.previewKey === planKey(form) ? S.preview : null;
    el("#as-body").innerHTML = `
      <div class="as-banner ${claude.cls}"><span class="as-dot"></span>${esc(claude.text)}</div>
      <div class="as-accounts">${status.accounts.filter(a => a.label).map(accountCard).join("")}</div>
      ${status.accounts.some(a => !a.label) ? `<details class="as-others"><summary>其它账号目录 ${status.accounts.filter(a => !a.label).length} 个（没起名字，多半是更早的账号）</summary><div class="as-accounts">${status.accounts.filter(a => !a.label).map(accountCard).join("")}</div></details>` : ""}
      <div class="as-card as-form">
        <label>从 <select id="as-src" aria-label="来源账号">${options(status.accounts, form.source, false)}</select></label>
        <button class="btn as-mini" data-act="swap" title="对调来源和目标" aria-label="对调来源和目标"><svg class="ic" aria-hidden="true"><use href="#i-swap"></use></svg></button>
        <label>到 <select id="as-dst" aria-label="目标账号">${options(status.accounts, form.target, true)}</select></label>
        <label class="as-check"><input type="checkbox" id="as-both" ${form.both ? "checked" : ""}> 两边互相补齐</label>
        <label>范围 <select id="as-win" aria-label="时间范围">${Object.keys(WINDOW_TEXT).map(Number).sort((a, b) => (a || 1e9) - (b || 1e9)).map(h => `<option value="${h}"${Number(form.window) === h ? " selected" : ""}>${WINDOW_TEXT[h]}</option>`).join("")}</select></label>
        <span class="as-spacer"></span>
        <button class="btn" data-act="preview" ${S.busy ? "disabled" : ""}>预览（只读）</button>
        <button class="btn as-primary" data-act="sync" ${canSync({ ...S, form }) ? "" : "disabled"} title="${esc(block)}">备份并同步</button>
        <div class="as-hint">${esc(block || "预览没问题，可以同步了。")}${form.both ? " · 两边互相补齐：两个账号都能看到全部会话。" : ""}</div>
      </div>
      <div id="as-preview">${preview ? (preview.nothing ? '<div class="as-card as-dim">两边已经一致，不用同步。</div>' : preview.plans.map(planCard).join("")) : ""}</div>
      <div id="as-result">${runCard(S.lastRun)}</div>
      <div class="as-card"><div class="as-plan-head"><b>同步记录</b><span class="as-dim">最近 10 次；每次的备份都在对应目录里，旁边有一份「灵桥同步记录.json」</span></div>${runsTable(status.runs)}</div>`;
    bind();
  }
  function bind() {
    const read = () => { S.form = { source: el("#as-src")?.value || "", target: el("#as-dst")?.value || "", window: Number(el("#as-win")?.value ?? 24), both: !!el("#as-both")?.checked }; render(); };
    ["#as-src", "#as-dst", "#as-win", "#as-both"].forEach(sel => el(sel)?.addEventListener("change", read));
    S.host.querySelectorAll("[data-act]").forEach(button => button.addEventListener("click", () => {
      const act = button.dataset.act;
      if (act === "preview") runPreview();
      else if (act === "sync") requestSync();
      else if (act === "undo") requestUndo(button.dataset.run);
      else if (act === "open-claude") openClaude();
      else if (act === "swap") { S.form = { ...S.form, source: S.form.target, target: S.form.source }; render(); }
    }));
  }
  function onKey(event) { if (S.modal && event.key === "Escape") { event.preventDefault(); S.modal(false); } }

  window.LingqiaoAccounts = {
    show(host) {
      S.host = host; S.visible = true;
      if (!host.querySelector(".as")) host.innerHTML = '<div class="as"><div class="headline">Claude Code 双账号会话</div><div class="subline">换账号后，桌面版侧栏只显示当前账号名下的会话。这里把另一个账号的会话条目补过来：<b>只复制侧栏条目</b>（local_*.json），聊天记录本身不动；每次写之前整份备份，可以撤销。</div><div id="as-body"></div><div class="modal-mask" id="as-modal"></div></div>';
      document.addEventListener("keydown", onKey);
      loadStatus().then(pollClaude);
    },
    hide() { S.visible = false; clearTimeout(S.timer); document.removeEventListener("keydown", onKey); if (S.modal) S.modal(false); },
    refresh() { S.preview = null; S.previewKey = ""; return loadStatus(); },
    _t: { esc, api, planKey, defaultForm, canSync, syncBlockReason, totals, skippedCount, claudeText, fmtTime, fmtBytes, planCard, runCard, runsTable,
      accountCard, requestSync, requestUndo, runPreview, state: S, render, WINDOW_TEXT },
  };
})();
