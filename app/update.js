/* 检查更新：灵桥和 plugins/ 里的插件是从 GitHub 克隆的文件夹时，看看有没有新版本，一键更新。
   更新时后台先跑一遍测试，没通过就退回原来的版本；通过了再请用户重启灵桥。
   接口都带 X-Bridge-Token 头。点开侧栏底部的「检查更新」时才加载。 */
(function () {
  "use strict";
  const S = { mask: null, status: null, busy: false, asking: false, job: null, timer: null, onStatus: null, error: "", restarting: false };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fmtTime(seconds) {
    if (!seconds) return "还没检查过";
    const d = new Date(seconds * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  async function api(url, { method = "GET", body, timeoutMs = 150000 } = {}) {
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
  function updatable(repo) { return Boolean(repo?.git && repo.behind > 0 && !repo.reason); }
  function expectOf(repos) {
    const out = {};
    for (const repo of repos || []) if (updatable(repo) && repo.remote_head) out[repo.key] = repo.remote_head;
    return out;
  }
  function running(job) { return job?.status === "running"; }
  function canApply(state) {
    return !state.busy && !running(state.job) && !(state.job?.status === "done" && state.job?.restart) &&
      Object.keys(expectOf(state.status?.repos)).length > 0;
  }
  function repoState(repo) {
    if (!repo.git) return { cls: "", text: repo.reason || "没法自动更新" };
    if (updatable(repo)) return { cls: "new", text: `有新版本${repo.remote_version && repo.remote_version !== repo.version ? " " + repo.remote_version : ""}（${repo.behind} 处改动）` };
    if (repo.reason) return { cls: "warn", text: repo.reason };
    return { cls: "", text: "已经是最新的" };
  }
  function repoHtml(repo) {
    const state = repoState(repo);
    const changes = updatable(repo) && repo.changes?.length
      ? `<ul class="upd-changes">${repo.changes.slice(0, 10).map(line => `<li>${esc(line)}</li>`).join("")}${repo.changes.length > 10 ? `<li>……还有 ${repo.changes.length - 10} 处</li>` : ""}</ul>` : "";
    return `<div class="upd-repo"><div class="upd-repo-head"><b>${esc(repo.name)}</b><span class="upd-ver">${esc(repo.version ? "v" + repo.version : "")}</span>` +
      `<span class="upd-state ${state.cls}">${esc(state.text)}</span></div>${changes}</div>`;
  }
  function jobHtml(job) {
    if (!job) return "";
    const tail = job.tail?.length ? `<pre>${esc(job.tail.join("\n"))}</pre>` : "";
    if (running(job)) return `<div class="upd-job">${esc(job.step || "正在更新…")}<br>期间别关灵桥；测试大约要一分钟。</div>`;
    if (job.status === "done") return `<div class="upd-job ok">${esc(job.step || "更新好了")}。${job.restart ? "点「现在重启」用上新版本。" : ""}</div>`;
    return `<div class="upd-job bad">${esc(job.error || "没更新成功")}${job.rolled_back ? "（已经退回原来的版本，灵桥照常能用）" : ""}${tail}</div>`;
  }

  /* ---------- 弹窗 ---------- */
  function ensureMask() {
    if (S.mask) return S.mask;
    const mask = document.createElement("div");
    mask.className = "modal-mask upd-mask"; mask.id = "update-mask";
    mask.addEventListener("click", event => { if (event.target === mask && !S.busy && !running(S.job)) close(); });
    document.body.appendChild(mask);
    S.mask = mask;
    return mask;
  }
  function render() {
    const mask = ensureMask();
    const status = S.status;
    let body;
    if (!status) body = `<div class="loading">${S.busy ? "正在连 GitHub 看有没有新版本…" : "正在读取…"}</div>`;
    else if (!status.git) body = `<div class="upd-note warn">这台 Mac 没装 git，没法自动更新。装好 Xcode 命令行工具（终端里运行 xcode-select --install）后再试。</div>`;
    else body = status.repos.map(repoHtml).join("") +
      `<div class="upd-note">上次检查：${esc(fmtTime(status.checked_at))}${status.auto_check ? " · 每天自动看一次" : " · 自动检查已关"}。会话、设置这些数据不会被更新动到。</div>`;
    const ask = S.asking ? `<div class="m-warn">更新时会先跑一遍测试（大约一分钟），没通过就自动退回原来的版本。确定现在更新？</div>` : "";
    const restartReady = S.job?.status === "done" && S.job?.restart;
    const actions = S.restarting ? `<span class="upd-note">正在重启灵桥…</span>`
      : S.asking ? `<button class="btn" data-act="cancel">先不更新</button><button class="btn primary" data-act="confirm">确定更新</button>`
      : `<button class="btn" data-act="close" ${running(S.job) ? "disabled" : ""}>关闭</button>` +
        `<button class="btn" data-act="check" ${S.busy || running(S.job) ? "disabled" : ""}>${S.busy ? "正在检查…" : "重新检查"}</button>` +
        (restartReady ? `<button class="btn primary" data-act="restart">现在重启</button>`
          : `<button class="btn primary" data-act="apply" ${canApply(S) ? "" : "disabled"}>更新</button>`);
    mask.innerHTML = `<div class="modal upd-modal" role="dialog" aria-modal="true" aria-labelledby="upd-title"><h3 id="upd-title">检查更新</h3>` +
      `<div class="m-body">${body}${jobHtml(S.job)}${S.error ? `<div class="upd-error">${esc(S.error)}</div>` : ""}${ask}</div>` +
      `<div class="m-actions">${actions}</div></div>`;
    mask.querySelectorAll("[data-act]").forEach(button => button.addEventListener("click", () => act(button.dataset.act)));
    mask.classList.add("show");
  }
  function act(name) {
    if (name === "close") close();
    else if (name === "check") refresh(true);
    else if (name === "apply") { S.asking = true; render(); }
    else if (name === "cancel") { S.asking = false; render(); }
    else if (name === "confirm") apply();
    else if (name === "restart") restart();
  }
  async function refresh(fromGitHub) {
    S.busy = true; S.error = ""; render();
    try {
      S.status = await api(fromGitHub ? "/api/update/check" : "/api/update/status", fromGitHub ? { method: "POST", body: {} } : {});
      if (S.status.job) S.job = S.status.job;
      S.onStatus?.(S.status);
    } catch (error) { S.error = (fromGitHub ? "检查失败：" : "读取失败：") + error.message; }
    finally { S.busy = false; render(); if (running(S.job)) poll(); }
  }
  async function apply() {
    S.asking = false; S.error = "";
    const expect = expectOf(S.status?.repos);
    try {
      const result = await api("/api/update/apply", { method: "POST", body: { confirm: true, expect } });
      S.job = result.job; render(); poll();
    } catch (error) { S.error = "没开始更新：" + error.message; render(); }
  }
  function poll() {
    clearTimeout(S.timer);
    S.timer = setTimeout(async () => {
      try {
        const result = await api("/api/update/job");
        S.job = result.job;
      } catch (error) { S.error = "查不到更新进度：" + error.message; }
      render();
      if (running(S.job)) poll();
      else if (S.job?.status === "failed") refresh(false);
    }, 1000);
  }
  async function restart() {
    S.restarting = true; render();
    try { await api("/api/update/restart", { method: "POST", body: { confirm: true } }); }
    catch (error) { S.restarting = false; S.error = "重启失败：" + error.message + "（可以手动退出再打开灵桥）"; render(); }
  }
  function open(options = {}) {
    S.onStatus = options.onStatus || null; S.asking = false; S.error = ""; S.restarting = false;
    render();
    refresh(!running(S.job));
  }
  function close() {
    clearTimeout(S.timer);
    if (S.mask) S.mask.classList.remove("show");
  }

  window.LingqiaoUpdate = { open, close, _t: { updatable, expectOf, canApply, repoState, repoHtml, jobHtml, act, state: S } };
})();
