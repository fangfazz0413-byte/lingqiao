/* 灵桥 3.4 · 会话导出 / 导入：在两台电脑的灵桥之间搬会话。
   入口都在会话列表里：每条会话旁边的「导出」，工具栏上的「导入会话」「批量导出」。用到时才加载。
   接口都带 X-Bridge-Token 头，token 不进 URL；压缩包的位置只来自本机的保存/打开对话框（pywebview）
   或灵桥列出的候选文件，页面不传文件路径。 */
(function () {
  "use strict";
  const TOOLS = ["claude", "codex", "zcode", "workbuddy"];
  const TOOL_NAMES = { claude: "Claude Code", codex: "Codex", zcode: "ZCode", workbuddy: "WorkBuddy" };
  const S = {
    mask: null, view: null, status: null,
    rows: [], picked: new Set(),
    exportItems: [], job: null, jobTimer: null, exportResult: null,
    candidates: null, plan: null, choices: new Map(), importResult: null, claude: null,
    runs: null, historyOpen: false, busy: false, confirm: null, claudeTimer: null,
  };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function notify(message, ok = true) { if (typeof window.toast === "function") window.toast(message, ok); }
  function el(selector) { return S.mask ? S.mask.querySelector(selector) : null; }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtTime(ms) {
    if (!ms) return "—";
    const d = new Date(ms);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  }
  function icon(tool) { return TOOL_NAMES[tool] ? `<img class="brand-icon tf-icon" src="/assets/icons/${tool}.png" alt="" aria-hidden="true">` : ""; }

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
  function nativeApi() { return window.pywebview?.api || null; }

  /* ---------- 纯函数（测试用 _t 暴露） ---------- */
  function keyOf(row) { return row.tool + ":" + row.src; }   // 进 HTML 属性的标识只用普通字符（控制字符会被浏览器换掉）
  function exportable(row) { return !row.missing_file; }
  function pickAll(rows, picked) {
    // 全选只选“自己的”会话：同步副本是这台上别的会话复制出来的，要的话单独勾。
    const next = new Set(picked);
    for (const row of rows || []) if (exportable(row) && !row.mirror) next.add(keyOf(row));
    return next;
  }
  function selectionSummary(rows, picked) {
    const chosen = (rows || []).filter(row => picked.has(keyOf(row)));
    const byTool = {};
    for (const row of chosen) byTool[row.tool] = (byTool[row.tool] || 0) + 1;
    return { count: chosen.length, bytes: chosen.reduce((n, row) => n + (Number(row.size) || 0), 0), byTool, items: chosen.map(row => ({ tool: row.tool, src: row.src })) };
  }
  function initialChoices(plan) {
    const choices = new Map();
    for (const item of plan?.items || []) {
      choices.set(item.id, { on: item.mode !== "skip", mode: item.mode === "skip" ? "" : item.mode, target: item.target || item.text_targets?.[0] || "" });
    }
    return choices;
  }
  function modeOptions(item) {
    const out = [];
    if (item.raw_ok) out.push({ value: "raw:" + item.tool, label: "原样导入" });
    for (const t of item.text_targets || []) out.push({ value: "text:" + t, label: `转文字 → ${TOOL_NAMES[t]}` });
    return out;
  }
  function chosenItems(plan, choices) {
    return (plan?.items || []).filter(item => item.mode !== "skip" && choices.get(item.id)?.on);
  }
  function importBody(plan, choices) {
    return { plan_id: plan.plan_id, confirm: true, items: chosenItems(plan, choices).map(item => {
      const c = choices.get(item.id);
      return { id: item.id, mode: c.mode, target: c.mode === "raw" ? item.tool : c.target };
    }) };
  }
  function needsClaudeQuit(plan, choices) {
    return chosenItems(plan, choices).some(item => {
      const c = choices.get(item.id);
      return (c.mode === "raw" && item.tool === "claude" && item.sidebar) || (c.mode === "text" && c.target === "claude");
    });
  }
  function importBlockReason(state) {
    if (state?.busy || state?.job?.status === "running") return "正在处理…";
    if (!state?.plan) return "先选一个压缩包";
    const picked = chosenItems(state.plan, state.choices);
    if (!picked.length) return "先勾选要导入的会话";
    if (needsClaudeQuit(state.plan, state.choices)) {
      const claude = state.claude || state.plan.claude;
      if (claude?.running === null || claude?.running === undefined) return "没法确认 Claude 桌面版有没有退出";
      if (claude.running) return "要往 Claude 桌面版侧栏加条目：先在桌面版里按 ⌘Q 完全退出";
    }
    return "";
  }
  function progressText(job) {
    if (!job) return "";
    const p = job.progress || {};
    const what = job.kind === "export" ? "打包" : "导入";
    const bytes = p.bytes ? ` · 已读 ${fmtBytes(p.bytes)}` : "";
    return `正在${what}：第 ${Math.min((p.done || 0) + 1, p.total || 1)} / ${p.total || 0} 条${p.title ? " · " + p.title : ""}${bytes}`;
  }
  function percent(job) {
    const p = job?.progress || {};
    return p.total ? Math.round(100 * Math.min(p.done || 0, p.total) / p.total) : 0;
  }

  /* ---------- 确认框（叠在导出/导入对话框上） ---------- */
  function confirmDialog({ title, body, ok = "确定", check = "" }) {
    return new Promise(resolve => {
      const host = el("#tf-confirm");
      if (!host) { resolve(false); return; }
      host.innerHTML = `<div class="modal tf-modal" role="dialog" aria-modal="true"><h3>${esc(title)}</h3><div class="m-body">${body}</div>
        ${check ? `<label class="tf-check tf-modal-check"><input type="checkbox" id="tf-modal-check"> ${esc(check)}</label>` : ""}
        <div class="m-actions"><button class="btn" data-modal="no">取消</button><button class="btn tf-primary" data-modal="yes" ${check ? "disabled" : ""}>${esc(ok)}</button></div></div>`;
      host.classList.add("show");
      const done = value => { host.classList.remove("show"); host.innerHTML = ""; S.confirm = null; resolve(value); };
      S.confirm = done;
      const box = host.querySelector("#tf-modal-check");
      box?.addEventListener("change", () => { host.querySelector('[data-modal="yes"]').disabled = !box.checked; });
      host.querySelector('[data-modal="no"]')?.addEventListener("click", () => done(false));
      host.querySelector('[data-modal="yes"]')?.addEventListener("click", () => done(true));
    });
  }

  /* ---------- 动作 ---------- */
  async function loadStatus(deps = {}) {
    try { S.status = await (deps.api || api)("/api/transfer/status"); if (S.status.job?.status === "running" && !S.job) { S.job = S.status.job; watchJob(S.job.id, deps); } }
    catch (error) { notify("导出 / 导入状态读不了：" + error.message, false); }
    render();
  }
  async function loadCandidates(deps = {}) {
    try { S.candidates = (await (deps.api || api)("/api/transfer/candidates")).items || []; }
    catch (error) { S.candidates = []; notify("找不到最近的压缩包：" + error.message, false); }
    render();
  }
  async function loadRuns(deps = {}) {
    try { S.runs = (await (deps.api || api)("/api/transfer/runs")).runs || []; }
    catch (error) { S.runs = []; }
    render();
  }
  function refreshList() { try { window.loadList?.(true); } catch { /* 列表刷新失败不影响结果 */ } }
  function watchJob(id, deps = {}) {
    clearTimeout(S.jobTimer);
    const call = deps.api || api;
    const tick = async () => {
      try {
        const job = await call("/api/transfer/job?id=" + encodeURIComponent(id));
        S.job = job;
        if (job.status === "running") { S.jobTimer = setTimeout(tick, 700); render(); return; }
        if (job.status === "done") {
          if (job.kind === "export") { S.exportResult = job.result; notify(`导出好了：${job.result.sessions} 条会话`); }
          else { S.importResult = job.result; S.plan = null; S.choices = new Map(); const c = job.result.counts || {}; notify(`导入好了：${c.imported || 0} 条` + (c.failed ? `，${c.failed} 条没成` : ""), !c.failed); refreshList(); }
          S.runs = null;
        } else notify((job.kind === "export" ? "导出" : "导入") + "没做完：" + job.error, false);
        render();
      } catch (error) { S.jobTimer = setTimeout(tick, 2000); }
    };
    S.jobTimer = setTimeout(tick, deps.immediate ? 0 : 300);
  }
  async function requestExport(rows, deps = {}) {
    const call = deps.api || api, ask = deps.confirm || confirmDialog, native = deps.native !== undefined ? deps.native : nativeApi();
    const items = (rows || []).filter(exportable).map(row => ({ tool: row.tool, src: row.src }));
    if (!items.length) { notify("先选要导出的会话", false); return null; }
    if (S.job?.status === "running") { notify("上一个导出或导入还没做完，等它结束再来", false); return null; }
    let handle = "";
    S.exportItems = rows.filter(exportable); S.exportResult = null;
    if (native?.transfer_choose_export) {
      const chosen = await native.transfer_choose_export(items.length);
      if (!chosen || chosen.cancelled) return null;
      if (chosen.error) { notify("选保存位置没成：" + chosen.error, false); return null; }
      handle = chosen.handle;
    } else {
      if (!S.status) await loadStatus(deps);
      openView("export");
      const ok = await ask({ title: "导出到哪里？", ok: "导出", body: `会保存到 <code>${esc(S.status?.export_dir || "下载")}</code>，文件名自动带上电脑名和时间。` });
      if (!ok) { if (!S.exportResult && !S.job) close(); return null; }
    }
    S.busy = true;
    openView("export");
    try {
      const result = await call("/api/transfer/export", { method: "POST", body: { items, ...(handle ? { handle } : {}) } });
      S.job = result.job; watchJob(result.job_id, deps);
      return result;
    } catch (error) { notify("导出没开始：" + error.message, false); return null; }
    finally { S.busy = false; render(); }
  }
  async function inspectHandle(handle, deps = {}) {
    S.busy = true; S.importResult = null; render();
    try {
      const plan = await (deps.api || api)("/api/transfer/inspect", { method: "POST", body: { handle } });
      S.plan = plan; S.choices = initialChoices(plan); S.claude = plan.claude;
      pollClaude();
      return plan;
    } catch (error) { notify("这个压缩包读不了：" + error.message, false); return null; }
    finally { S.busy = false; render(); }
  }
  async function chooseImport(deps = {}) {
    const native = deps.native !== undefined ? deps.native : nativeApi();
    if (!native?.transfer_choose_import) { notify("这里打不开文件对话框：从下面“最近的导出包”里选一个", false); return null; }
    const chosen = await native.transfer_choose_import();
    if (!chosen || chosen.cancelled) return null;
    if (chosen.error) { notify("选文件没成：" + chosen.error, false); return null; }
    return inspectHandle(chosen.handle, deps);
  }
  async function requestImport(deps = {}) {
    const call = deps.api || api, ask = deps.confirm || confirmDialog;
    const block = importBlockReason(S);
    if (block) { notify(block, false); return null; }
    const picked = chosenItems(S.plan, S.choices);
    const raw = picked.filter(item => S.choices.get(item.id).mode === "raw").length;
    const quit = needsClaudeQuit(S.plan, S.choices);
    const ok = await ask({ title: `导入 ${picked.length} 条会话？`, ok: "开始导入", check: quit ? "我已经在 Claude 桌面版里按 ⌘Q 完全退出了" : "",
      body: `原样导入 <b>${raw}</b> 条，转成文字导入 <b>${picked.length - raw}</b> 条。<br>只新建，不覆盖这台电脑上已有的会话；每条都记了恢复日志，出错会自动撤回。<br>做完可以「撤销这次导入」（导入的会话进回收站）。` });
    if (!ok) return null;
    S.busy = true; render();
    try {
      const result = await call("/api/transfer/import", { method: "POST", body: importBody(S.plan, S.choices) });
      S.job = result.job; watchJob(result.job_id, deps);
      return result;
    } catch (error) { notify("导入没开始：" + error.message, false); return null; }
    finally { S.busy = false; render(); }
  }
  async function requestUndo(runId, deps = {}) {
    const call = deps.api || api, ask = deps.confirm || confirmDialog;
    const run = (S.runs || []).find(r => r.id === runId) || (S.importResult?.id === runId ? S.importResult : null);
    const claude = (run?.items || []).some(i => i.status === "imported" && i.local_tool === "claude");
    const ok = await ask({ title: "撤销这次导入？", ok: "撤销", check: claude ? "我已经在 Claude 桌面版里按 ⌘Q 完全退出了" : "",
      body: "这次导入进来的会话会移进<b>回收站</b>（可以从回收站恢复）；Claude 会话文件夹里带过来的工具输出等文件挪到灵桥的撤销区。<br>导入之后又被改过的会保留不动，结果里会列出来。" });
    if (!ok) return null;
    S.busy = true; render();
    try {
      const result = await call("/api/transfer/undo", { method: "POST", body: { run_id: runId, confirm: true } });
      const u = result.run.undone;
      notify(`撤销好了：${u.moved.length} 条进了回收站` + (u.kept.length ? `，${u.kept.length} 条没动` : ""), !u.kept.length);
      if (S.importResult?.id === runId) S.importResult = result.run;
      S.runs = null; refreshList();
      return result;
    } catch (error) { notify("撤销没做：" + error.message, false); return null; }
    finally { S.busy = false; render(); if (S.view === "import" && S.runs === null) loadRuns(); }
  }
  async function reveal(runId, deps = {}) {
    try { await (deps.api || api)("/api/transfer/reveal", { method: "POST", body: { run_id: runId } }); }
    catch (error) { notify(error.message, false); }
  }
  async function pollClaude() {
    clearTimeout(S.claudeTimer);
    if (S.view !== "import" || !S.plan) return;
    try {
      const claude = await api("/api/transfer/claude");
      const before = S.claude?.running; S.claude = claude;
      if (before !== claude.running) render();
    } catch { /* 下次再查 */ }
    S.claudeTimer = setTimeout(pollClaude, document.hidden ? 10000 : 3000);
  }

  /* ---------- 渲染 ---------- */
  function jobCard() {
    const job = S.job;
    if (!job || job.status !== "running") return "";
    return `<div class="tf-card tf-job"><div class="tf-job-text">${esc(progressText(job))}</div>
      <div class="tf-bar"><span style="width:${percent(job)}%"></span></div><div class="tf-dim">大会话要多等一会儿；关掉这个窗口也会在后台接着做，做完会提示。</div></div>`;
  }
  function exportResultCard(run) {
    if (!run) return "";
    const tools = TOOLS.filter(t => run.by_tool?.[t]).map(t => `${TOOL_NAMES[t]} ${run.by_tool[t]}`).join(" · ");
    return `<div class="tf-card tf-done"><div class="tf-head"><b>导出好了</b><span class="tf-dim">${esc(run.at)} · ${esc(run.machine)}</span></div>
      <div class="tf-line"><code>${esc(run.path)}</code>（${esc(fmtBytes(run.size))}）</div>
      <div class="tf-counts"><span class="tf-chip ok">${run.sessions} 条会话</span>${tools ? `<span class="tf-chip none">${esc(tools)}</span>` : ""}${run.skipped?.length ? `<span class="tf-chip warn">没导出 ${run.skipped.length} 条</span>` : ""}</div>
      ${run.secrets ? `<div class="tf-warn-text">里面有 ${run.secrets} 处像密钥的内容（在 ${run.secret_sessions} 条会话里）。这个压缩包只在自己的电脑之间传，别发给别人。</div>` : ""}
      ${run.skipped?.length ? `<details><summary>没导出的 ${run.skipped.length} 条</summary><ul class="tf-list">${run.skipped.map(x => `<li>${esc(TOOL_NAMES[x.tool] || x.tool)} · ${esc(x.title)}<span class="tf-dim"> · ${esc(x.reason)}</span></li>`).join("")}</ul></details>` : ""}
      <div class="tf-line">下一步：把这个文件拿到另一台电脑（隔空投送、U 盘、SSD 都行），在那台灵桥的会话列表上点「导入会话」选它。</div>
      <div class="tf-actions"><button class="btn tf-primary" data-act="reveal" data-run="${esc(run.id)}">在访达中显示</button><button class="btn" data-act="close">好</button></div></div>`;
  }
  function exportView() {
    const items = S.exportItems || [];
    const list = items.length ? `<ul class="tf-list">${items.slice(0, 8).map(row => `<li>${icon(row.tool)}${esc(row.title)}<span class="tf-dim"> · ${esc(fmtBytes(row.size))}</span></li>`).join("")}${items.length > 8 ? `<li class="tf-dim">…还有 ${items.length - 8} 条</li>` : ""}</ul>` : "";
    return `${S.exportResult ? "" : `<div class="tf-card"><div class="tf-head"><b>导出 ${items.length} 条会话</b><span class="tf-dim">打成一个 .zip，拿到另一台电脑的灵桥里导入</span></div>${list}</div>`}
      ${jobCard()}${exportResultCard(S.exportResult)}`;
  }
  function batchRow(row) {
    const key = keyOf(row), on = S.picked.has(key), off = !exportable(row);
    const tags = [row.mirror ? '<span class="tf-chip warn" title="这是这台上别的会话同步出来的副本">同步副本</span>' : "",
      row.kind === "subagent" ? '<span class="tf-chip none">子代理</span>' : "", row.archived ? '<span class="tf-chip none">已归档</span>' : "",
      off ? '<span class="tf-chip bad">缺文字记录</span>' : ""].join("");
    return `<label class="tf-row${on ? " on" : ""}${off ? " off" : ""}"><input type="checkbox" data-pick="${esc(key)}" ${on ? "checked" : ""} ${off ? "disabled" : ""}>
      <span class="tf-tool">${icon(row.tool)}${esc(TOOL_NAMES[row.tool])}</span>
      <span class="tf-title"><b>${esc(row.title)}</b><span class="tf-dir">${esc(row.dir)}</span></span>
      <span class="tf-tags">${tags}</span><span class="tf-size">${esc(fmtBytes(row.size))}</span><span class="tf-time">${esc(fmtTime(row.mtime * 1000))}</span></label>`;
  }
  function batchView() {
    const sum = selectionSummary(S.rows, S.picked);
    return `<div class="tf-dim tf-gap">这里列的是会话列表当前筛选出来的 ${S.rows.length} 条（换工具分组、时间、大小、搜索可以改范围）。</div>
      <div class="tf-selbar"><span>已选 <b>${sum.count}</b> 条${sum.count ? `，约 ${esc(fmtBytes(sum.bytes))}` : ""}</span>
        <button class="btn tf-mini" data-act="pick-all" ${S.rows.length ? "" : "disabled"}>全选</button>
        <button class="btn tf-mini" data-act="pick-none" ${sum.count ? "" : "disabled"}>清空</button>
        <span class="tf-spacer"></span>
        <button class="btn tf-primary" data-act="export-picked" ${sum.count && !S.busy && S.job?.status !== "running" ? "" : "disabled"}>导出为压缩包…</button>
        <div class="tf-hint">全选只选这台上自己的会话；「同步副本」是别的会话复制出来的，要的话单独勾。压缩包里是完整聊天记录，只在自己的电脑之间传。</div></div>
      ${S.rows.length ? `<div class="tf-rows">${S.rows.map(batchRow).join("")}</div>` : '<div class="tf-dim tf-pad">当前列表是空的</div>'}`;
  }
  function modeCell(item) {
    if (item.mode === "skip") return `<span class="tf-chip none">跳过</span><span class="tf-dim"> ${esc(item.skip_reason)}</span>`;
    const c = S.choices.get(item.id), value = c?.mode === "raw" ? "raw:" + item.tool : "text:" + (c?.target || "");
    return `<select data-mode="${item.id}" aria-label="导入方式">${modeOptions(item).map(o => `<option value="${esc(o.value)}"${value === o.value ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>
      ${!item.raw_ok && item.raw_reason ? `<div class="tf-dim">不能原样：${esc(item.raw_reason)}</div>` : ""}`;
  }
  function planRow(item) {
    const c = S.choices.get(item.id), skip = item.mode === "skip";
    const notes = (item.notes || []).map(n => `<div class="tf-note">${esc(n)}</div>`).join("");
    return `<div class="tf-prow${skip ? " off" : ""}"><input type="checkbox" data-take="${item.id}" ${c?.on ? "checked" : ""} ${skip ? "disabled" : ""} aria-label="导入这条">
      <span class="tf-tool">${icon(item.tool)}${esc(TOOL_NAMES[item.tool])}</span>
      <span class="tf-title"><b>${esc(item.title)}</b><span class="tf-dir">${esc(item.local_cwd)}</span>${notes}</span>
      <span class="tf-mode">${modeCell(item)}</span>
      <span class="tf-size">${esc(fmtBytes(item.size))}<span class="tf-dim"> · ${item.turns} 段对话</span></span></div>`;
  }
  function claudeBanner() {
    if (!S.plan || !needsClaudeQuit(S.plan, S.choices)) return "";
    const claude = S.claude || S.plan.claude;
    if (claude?.running === false) return '<div class="tf-banner ok"><span class="tf-dot"></span>Claude 桌面版已经退出，可以往它的侧栏里加条目了。</div>';
    if (claude?.running) return `<div class="tf-banner bad"><span class="tf-dot"></span>有会话要放进 Claude 桌面版侧栏：先在桌面版里按 ⌘Q 完全退出（它开着会把新条目覆盖掉）。在桌面版里跑着的 AI 对话也会一起停。</div>`;
    return `<div class="tf-banner warn"><span class="tf-dot"></span>没法确认 Claude 桌面版有没有退出${claude?.error ? "（" + esc(claude.error) + "）" : ""}，先不能写它的侧栏。</div>`;
  }
  function importResultCard(run) {
    if (!run) return "";
    const c = run.counts || {}, undone = run.undone;
    const groups = { imported: [], skipped: [], failed: [] };
    for (const item of run.items || []) (groups[item.status] || groups.failed).push(item);
    const line = item => `<li>${esc(TOOL_NAMES[item.tool])} · ${esc(item.title)}<span class="tf-dim"> · ${item.status === "imported" ? (item.mode === "raw" ? "原样导入" : `转文字 → ${esc(TOOL_NAMES[item.local_tool] || "")}`) : esc(item.reason || "")}</span>${(item.notes || []).map(n => `<div class="tf-note">${esc(n)}</div>`).join("")}</li>`;
    return `<div class="tf-card tf-done${undone ? " undone" : ""}"><div class="tf-head"><b>${undone ? "这次导入已撤销" : "导入好了"}</b><span class="tf-dim">${esc(run.at)} · 来自 ${esc(run.source?.machine || "")} · ${esc(run.zip)}</span></div>
      <div class="tf-counts"><span class="tf-chip ok">进来 ${c.imported || 0} 条</span><span class="tf-chip run">原样 ${c.raw || 0}</span><span class="tf-chip run">转文字 ${c.text || 0}</span>${c.skipped ? `<span class="tf-chip none">跳过 ${c.skipped}</span>` : ""}${c.failed ? `<span class="tf-chip bad">没成 ${c.failed}</span>` : ""}</div>
      ${groups.imported.length ? `<details open><summary>进来的 ${groups.imported.length} 条</summary><ul class="tf-list">${groups.imported.map(line).join("")}</ul></details>` : ""}
      ${groups.skipped.length ? `<details><summary>跳过的 ${groups.skipped.length} 条</summary><ul class="tf-list">${groups.skipped.map(line).join("")}</ul></details>` : ""}
      ${groups.failed.length ? `<details open><summary>没成的 ${groups.failed.length} 条</summary><ul class="tf-list">${groups.failed.map(line).join("")}</ul></details>` : ""}
      ${undone ? `<div class="tf-line">撤销：${undone.moved.length} 条进了回收站${undone.kept.length ? `，${undone.kept.length} 条没动` : ""}；会话文件夹里带过来的文件在 <code>${esc(undone.holding)}</code></div>`
        : `<div class="tf-line">下一步：Claude 桌面版重新打开后在侧栏能看到；Codex、ZCode、WorkBuddy 重启一下 App 再看列表。灵桥的会话列表已经刷新。</div>
           ${c.imported ? `<div class="tf-actions"><button class="btn" data-act="undo" data-run="${esc(run.id)}">撤销这次导入</button></div>` : ""}`}</div>`;
  }
  function runsList() {
    if (S.runs === null) return '<div class="tf-dim">正在读取…</div>';
    if (!S.runs.length) return '<div class="tf-dim">还没有导出或导入过。</div>';
    return `<div class="tf-runs">${S.runs.map(r => r.kind === "export"
      ? `<div class="tf-run"><span class="tf-chip none">导出</span><span class="tf-run-at">${esc(r.at)}</span><span class="tf-title"><b>${esc(r.name)}</b><span class="tf-dir">${r.sessions} 条 · ${esc(fmtBytes(r.size))}${r.secrets ? ` · 像密钥的 ${r.secrets} 处` : ""}</span></span>
         <span>${r.exists ? `<button class="btn tf-mini" data-act="reveal" data-run="${esc(r.id)}">在访达中显示</button>` : '<span class="tf-dim">文件已挪走</span>'}</span></div>`
      : `<div class="tf-run"><span class="tf-chip run">导入</span><span class="tf-run-at">${esc(r.at)}</span><span class="tf-title"><b>${esc(r.zip)}</b><span class="tf-dir">来自 ${esc(r.source?.machine || "")} · 进来 ${r.counts?.imported || 0} 条（原样 ${r.counts?.raw || 0}、转文字 ${r.counts?.text || 0}）${r.counts?.failed ? ` · 没成 ${r.counts.failed}` : ""}</span></span>
         <span>${r.undone ? '<span class="tf-chip none">已撤销</span>' : r.counts?.imported ? `<button class="btn tf-mini" data-act="undo" data-run="${esc(r.id)}">撤销</button>` : ""}</span></div>`).join("")}</div>`;
  }
  function importView() {
    const running = S.job?.status === "running";
    const candidates = S.candidates === null ? '<div class="tf-dim">正在找…</div>'
      : S.candidates.length ? `<div class="tf-cands">${S.candidates.map(c => `<div class="tf-cand"><span class="tf-title"><b>${esc(c.name)}</b><span class="tf-dir">${esc(c.dir)} · ${esc(c.at)} · ${esc(fmtBytes(c.size))} · ${c.sessions} 条 · 来自 ${esc(c.machine)}</span></span>
          <button class="btn tf-mini" data-act="inspect" data-handle="${esc(c.handle)}" ${S.busy || running ? "disabled" : ""}>预览</button></div>`).join("")}</div>`
      : '<div class="tf-dim">“下载”和“桌面”里没有灵桥导出的压缩包。用上面的按钮选一个。</div>';
    let plan = "";
    if (S.plan) {
      const p = S.plan, block = importBlockReason(S), picked = chosenItems(p, S.choices);
      plan = `<div class="tf-card"><div class="tf-head"><b>${esc(p.zip)}</b><span class="tf-dim">来自 ${esc(p.source?.machine || "?")}（灵桥 ${esc(p.source?.lingqiao || "?")}）· 导出于 ${esc(p.created_at)} · ${p.items.length} 条</span></div>
        ${p.same_machine ? '<div class="tf-warn-text">这是这台电脑自己导出的包：已经有的会话都会跳过。</div>' : ""}
        <div class="tf-counts"><span class="tf-chip ok">原样 ${p.counts.raw}</span><span class="tf-chip run">转文字 ${p.counts.text}</span><span class="tf-chip none">跳过 ${p.counts.skip}</span></div>
        <div class="tf-dim">能原样就原样（ID 不变，工具里看到的和那台一样）；表结构不同或这台没装那个工具的，转成文字对话导入。已经有的不覆盖。</div></div>
        ${claudeBanner()}
        <div class="tf-prows">${p.items.map(planRow).join("")}</div>
        <div class="tf-selbar"><span>要导入 <b>${picked.length}</b> 条</span><span class="tf-spacer"></span>
          <button class="btn" data-act="discard">换个压缩包</button>
          <button class="btn tf-primary" data-act="import" ${block ? "disabled" : ""} title="${esc(block)}">开始导入</button>
          <div class="tf-hint">${esc(block || "只新建、不覆盖；每条都能撤回。")}</div></div>`;
    }
    return `${jobCard()}${importResultCard(S.importResult)}
      ${S.plan ? plan : `<div class="tf-card"><div class="tf-head"><b>选一个压缩包</b><span class="tf-dim">另一台电脑的灵桥导出的 .zip</span></div>
        <div class="tf-actions"><button class="btn tf-primary" data-act="choose" ${S.busy || running ? "disabled" : ""}>选择压缩包…</button><button class="btn tf-mini" data-act="candidates">刷新列表</button></div>
        <div class="tf-sub">最近的导出包（下载、桌面里）</div>${candidates}</div>`}
      <details class="tf-card tf-history"${S.historyOpen ? " open" : ""}><summary>最近的导出和导入（可以撤销导入）</summary>${runsList()}</details>`;
  }
  const TITLES = { export: "导出会话", batch: "批量导出", import: "导入会话" };
  function render() {
    if (!S.mask || !S.view) return;
    const machine = S.status?.machine?.name;
    const body = S.view === "export" ? exportView() : S.view === "batch" ? batchView() : importView();
    const scroll = el(".tf-scroll")?.scrollTop || 0;
    S.mask.innerHTML = `<div class="tf tf-dialog" role="dialog" aria-modal="true" aria-label="${esc(TITLES[S.view])}">
      <div class="tf-dialog-head"><div><h3>${esc(TITLES[S.view])}</h3><p>在两台电脑的灵桥之间搬会话：<b>能原样就原样</b>放回去，放不回去的<b>转成文字对话</b>导入。只新建，不覆盖，可以撤销。${machine ? ` · 这台电脑：${esc(machine)}` : ""}</p></div>
        <button class="appearance-close" data-act="close" aria-label="关闭">×</button></div>
      <div class="tf-scroll">${body}</div><div class="modal-mask tf-confirm" id="tf-confirm"></div></div>`;
    const box = el(".tf-scroll"); if (box && scroll) box.scrollTop = scroll;
    bind();
  }
  function bind() {
    S.mask.querySelectorAll("[data-pick]").forEach(box => box.addEventListener("change", () => {
      if (box.checked) S.picked.add(box.dataset.pick); else S.picked.delete(box.dataset.pick);
      render();
    }));
    S.mask.querySelectorAll("[data-take]").forEach(box => box.addEventListener("change", () => {
      const c = S.choices.get(Number(box.dataset.take)); if (c) c.on = box.checked; render();
    }));
    S.mask.querySelectorAll("[data-mode]").forEach(sel => sel.addEventListener("change", () => {
      const c = S.choices.get(Number(sel.dataset.mode)); if (!c) return;
      const [mode, target] = sel.value.split(":"); c.mode = mode; c.target = target; render();
    }));
    S.mask.querySelector(".tf-history")?.addEventListener("toggle", event => { S.historyOpen = event.target.open; if (S.historyOpen && S.runs === null) loadRuns(); });
    S.mask.querySelectorAll("[data-act]").forEach(button => button.addEventListener("click", () => {
      const act = button.dataset.act;
      if (act === "close") close();
      else if (act === "pick-all") { S.picked = pickAll(S.rows, S.picked); render(); }
      else if (act === "pick-none") { S.picked.clear(); render(); }
      else if (act === "export-picked") requestExport(S.rows.filter(row => S.picked.has(keyOf(row))));
      else if (act === "reveal") reveal(button.dataset.run);
      else if (act === "choose") chooseImport();
      else if (act === "candidates") { S.candidates = null; render(); loadCandidates(); }
      else if (act === "inspect") inspectHandle(button.dataset.handle);
      else if (act === "discard") { if (S.plan) api("/api/transfer/discard", { method: "POST", body: { plan_id: S.plan.plan_id } }).catch(() => {}); S.plan = null; S.choices = new Map(); render(); }
      else if (act === "import") requestImport();
      else if (act === "undo") requestUndo(button.dataset.run);
    }));
  }
  function onKey(event) {
    if (event.key !== "Escape" || !S.view) return;
    event.preventDefault();
    if (S.confirm) S.confirm(false); else close();
  }
  function openView(view) {
    S.mask = S.mask || document.getElementById("transfer-mask");
    if (!S.mask) return;
    if (!S.view) document.addEventListener("keydown", onKey);
    S.view = view;
    S.mask.classList.add("show");
    render();
  }
  function close() {
    if (S.confirm) S.confirm(false);
    clearTimeout(S.claudeTimer);
    S.view = null;
    if (S.mask) { S.mask.classList.remove("show"); S.mask.innerHTML = ""; }
    document.removeEventListener("keydown", onKey);
  }

  window.LingqiaoTransfer = {
    exportSessions(rows) { if (!S.job || S.job.status !== "running") { S.exportResult = null; } return requestExport(rows); },
    openBatch(rows) {
      S.rows = (rows || []).slice();
      const keys = new Set(S.rows.map(keyOf));
      for (const key of [...S.picked]) if (!keys.has(key)) S.picked.delete(key);
      openView("batch");
      if (!S.status) loadStatus();
    },
    openImport() {
      openView("import");
      if (!S.status) loadStatus();
      if (S.candidates === null) loadCandidates();
      if (S.plan) pollClaude();
    },
    close,
    _t: { esc, api, keyOf, pickAll, selectionSummary, initialChoices, modeOptions, chosenItems, importBody, needsClaudeQuit, importBlockReason,
      progressText, percent, fmtBytes, fmtTime, requestExport, requestImport, requestUndo, chooseImport, inspectHandle, watchJob,
      exportResultCard, importResultCard, batchView, importView, exportView, runsList, state: S, render, openView, TOOL_NAMES },
  };
})();
