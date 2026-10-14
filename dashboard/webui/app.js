const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

let token = localStorage.getItem("hx_token") || "";
let cache = new Map();
let detail = null;
let activeConfig = "";
let activeTab = "process";
let latestId = 0;
let latestRev = 0;
let loaded = false;
let lastState = {};
let itemTotal = 0;      // 库里的总条数（stats.item_count / 拍平的 item_count 都算）
let resultTotal = 0;    // 当前搜索词命中的条数
let channels = [];
let configurable = ["collect", "process"];

const hints = {
  PROXY_URL: "X 与网页来源访问代理地址，可留空。",
  COLLECT_INTERVAL_SECONDS: "X 每轮结束后的等待秒数，保留现有配置。",
  WEB_COLLECT_INTERVAL_SECONDS: "网页来源独立刷新间隔，默认 900 秒（15 分钟）。",
  SOURCES: "启用的采集器，逗号分隔：x,rss,snapi,sina；留空全部启用。修改后重启采集服务。",
  COOKIES_PATH: "X cookies 文件路径。",
  WATCH_ACCOUNT: "关注列表账号。",
  NEWS_ACCOUNTS: "直接扫描账号，逗号分隔。",
  SCAN_BATCH: "每轮扫描数量。",
  PAUSE_SECONDS: "账号间暂停秒数。",
  MODEL_PROTOCOL: "模型接口协议：openai=兼容 /chat/completions；responses=OpenAI Responses；anthropic=兼容 /messages。",
  MODEL_API_BASE: "模型 API 地址，写到版本号为止，例如 https://api.openai.com/v1、https://api.anthropic.com/v1、本机 http://127.0.0.1:11434/v1。没有默认服务商，必须自己填。",
  MODEL_NAME: "模型名称，例如 gpt-4o-mini、claude-3-5-sonnet-latest、qwen-max。",
  MODEL_API_KEY: "模型 API Key，不会回显。",
  MODEL_TIMEOUT: "单次请求超时秒数。",
  MODEL_MAX_TOKENS: "单次输出上限；0 = 不显式限制（交给服务端默认）。译文+标签是 JSON，上限太小会被截断。",
  AI_REPEAT_CONTEXT: "判定内容重复时，给模型对比的最近已入库标题条数；0 = 不给清单（只看文本里的旧闻线索）。",
  ANALYSIS_MAX_ATTEMPTS: "每次轮到一条内容时最多完整分析几次；仍失败就移到队尾，下次轮到继续，没有累计重试上限。",
  ANALYSIS_INLINE_RETRIES: "遇到限流/超时时，同一轮内当场重试几次。",
  ANALYSIS_RATE_LIMIT_WAIT: "限流/超时后等待多少秒再重试。",
  ANALYSIS_PAUSE_SECONDS: "每条内容处理完成后的等待秒数，0 表示不主动等待。",
};

const protocolLabels = {
  openai: "openai compatible（/chat/completions）",
  responses: "responses（OpenAI Responses API）",
  anthropic: "anthropic compatible（/messages）",
};

const SERVICE_LABELS = { collect: "采集", process: "处理" };
const ADAPTER_LABELS = { plugin: "插件", process: "托管进程" };

/* ── 统一的 {ok, data} / {ok:false, error} 信封 ──────────────────────────────
   所有 GET/POST 都带 X-HX-Token；401 时重新取一次 token 再打一遍（只重试一次）。 */
class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = Number(status || 0);
  }
}

function readToken(force = false) {
  if (!token || force) {
    token = prompt("请输入看板 token（启动看板时的终端会打印）") || "";
    localStorage.setItem("hx_token", token);
  }
  return token;
}

async function request(path, options = {}) {
  const method = options.method || "GET";
  const body = options.body === undefined ? null : options.body;
  const retry = options.retry !== false;
  const headers = { "X-HX-Token": readToken() };
  const init = { method, headers, cache: "no-store" };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  const response = await fetch(path, init);
  if (response.status === 401 && retry) {
    readToken(true);                                  // token 过期/不对：重输一次再试
    return request(path, { method, body, retry: false });
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!payload || typeof payload !== "object") {
    throw new ApiError(`接口没有返回 JSON（HTTP ${response.status}）`, response.status);
  }
  if (payload.ok !== true) {
    throw new ApiError(payload.error || `请求失败（HTTP ${response.status}）`, response.status);
  }
  return payload.data;
}

const apiGet = (path) => request(path);
const apiPost = (path, body) => request(path, { method: "POST", body: body || {} });

/* ── 小提示（和现有 tooltip 一个风格），不用 alert ── */
function toastHost() {
  // 模态 dialog 在 top layer：提示也得挂在里面，否则被盖住
  const host = document.querySelector("dialog[open]") || document.body;
  let box = host.querySelector(":scope > .toasts");
  if (!box) {
    box = document.createElement("div");
    box.className = "toasts";
    host.append(box);
  }
  return box;
}

function notify(message, kind = "error") {
  const text = String(message ?? "").trim();
  if (!text) return;
  const box = document.createElement("div");
  box.className = `toast toast-${kind}`;
  box.setAttribute("role", kind === "error" ? "alert" : "status");
  box.textContent = text;
  toastHost().append(box);
  setTimeout(() => box.remove(), kind === "error" ? 6000 : 2400);
}

function reportError(error, fallback = "操作失败") {
  notify(error && error.message ? error.message : fallback, "error");
}

/* 配置项标题：字段名 + 一个 ⓘ 图标；说明用浮层 tooltip（点开 / 再点或点别处关闭）
   注意别用 <label> 包这颗按钮：label 会把点击转发给包着的按钮，点字段名也会弹提示。 */
function fieldLabel(key, hint) {
  const hasHint = Boolean(hint);
  const label = ({WEB_COLLECT_INTERVAL_SECONDS: "网页刷新间隔（秒）", COLLECT_INTERVAL_SECONDS: "X 等待间隔（秒）", SOURCES: "启用来源"})[key] || key;
  return `
      <div class="field-head">
        <span class="field-name">${esc(label)}</span>
        ${hasHint ? `<button type="button" class="hint-toggle" data-tip="${esc(hint)}"
          aria-expanded="false" aria-label="${esc(key)}：${esc(hint)}">i</button>` : ""}
      </div>`;
}

/* ── 浮层 tooltip ──
   必须挂在"当前打开的 <dialog> 里面"：模态 dialog 在浏览器 top layer，挂到 <body> 上的元素
   哪怕 z-index 再大也会被它盖住。挂在 dialog 内、用 fixed 定位，
   再按 dialog 自己的边界翻转，就不会被 dialog 的 overflow:hidden 裁掉。 */
let hintTip = null;
let hintAnchor = null;

function hideHintTip() {
  if (hintTip) {
    hintTip.remove();
    hintTip = null;
  }
  if (hintAnchor) {
    hintAnchor.setAttribute("aria-expanded", "false");
    hintAnchor = null;
  }
}

function tipHost(anchor) {
  // 挂到"这个图标所在的那个 dialog"里：模态 dialog 在 top layer，挂 body 会被它盖住
  return anchor.closest("dialog") || document.body;
}

function placeHintTip() {
  if (!hintTip || !hintAnchor) return;
  const host = hintTip.parentElement;
  const rect = hintAnchor.getBoundingClientRect();
  const box = hintTip.getBoundingClientRect();
  const edge = host === document.body
    ? { top: 6, left: 6, right: window.innerWidth - 6, bottom: window.innerHeight - 6 }
    : host.getBoundingClientRect();

  let top = rect.bottom + 8;
  if (top + box.height > edge.bottom - 6) top = rect.top - box.height - 8;   // 下面放不下就翻到上面
  let left = rect.left;
  if (left + box.width > edge.right - 6) left = edge.right - box.width - 6;
  hintTip.style.top = `${Math.max(edge.top + 6, top)}px`;
  hintTip.style.left = `${Math.max(edge.left + 6, left)}px`;
}

function showHintTip(anchor) {
  const text = anchor.dataset.tip || "";
  hideHintTip();
  if (!text) return;
  const tip = document.createElement("div");
  tip.className = "float-tip";
  tip.textContent = text;
  tipHost(anchor).append(tip);
  hintTip = tip;
  hintAnchor = anchor;
  anchor.setAttribute("aria-expanded", "true");
  placeHintTip();
}

function fieldControl(field) {
  if (Array.isArray(field.choices) && field.choices.length) {
    const options = field.choices.map((choice) => {
      const label = protocolLabels[choice] || choice;
      const selected = String(choice) === String(field.value) ? " selected" : "";
      return `<option value="${esc(choice)}"${selected}>${esc(label)}</option>`;
    }).join("");
    return `<select data-key="${esc(field.key)}">${options}</select>`;
  }
  return `<input data-key="${esc(field.key)}"
        type="${field.secret ? "password" : "text"}"
        value="${field.secret ? "" : esc(field.value || "")}"
        placeholder="${field.secret ? "已配置则留空" : esc(field.value || "")}">`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

/* 翻译阶段存下的 AI 标签：ai_tags 是 JSON 数组字符串 */
function tagsOf(item) {
  const raw = item.ai_tags;
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw !== "string" || !raw.trim()) return [];
  try {
    const data = JSON.parse(raw);
    return Array.isArray(data) ? data.map(String) : [];
  } catch (error) {
    return [];
  }
}

/* importance：高/中/低 */
function importanceLabel(item) {
  const value = String(item.importance || "").trim();
  return ["高", "中", "低"].includes(value) ? `重要度 ${value}` : "";
}

/* 台账筛选 */
const FILTERS = {
  all: () => true,
  valid: (item) => item.stage === "processed" && item.decision === "发",
  invalid: (item) => item.stage === "processed" && item.decision === "丢",
  pending: (item) => item.stage !== "processed",
};
let itemFilter = "all";

function matchesFilter(item) {
  return (FILTERS[itemFilter] || FILTERS.all)(item);
}

function applyFilter() {
  for (const element of [...$("#items").children]) {
    const item = cache.get(element.dataset.key);
    element.hidden = item ? !matchesFilter(item) : false;
  }
  for (const button of document.querySelectorAll("[data-filter]")) {
    button.classList.toggle("active", button.dataset.filter === itemFilter);
  }
}

function chip(text, className = "") {
  return `<span class="chip ${className}">${esc(text)}</span>`;
}

/* AI 完整分析的价值分 */
function scoreOf(item) {
  const score = item.ai_score;
  return score === null || score === undefined || score === "" || Number.isNaN(Number(score))
    ? null : { value: Number(score), label: "AI 分" };
}

/* 一条内容的判定/分数/重要度/标签小圆片 */
function chipRow(item) {
  const parts = [chip(item.stage === "processed" ? `判定：${item.decision}` : "待分析")];
  const score = scoreOf(item);
  if (score) parts.push(chip(`${score.label} ${score.value}`, "chip-score"));
  const importance = importanceLabel(item);
  if (importance) parts.push(chip(importance, "chip-importance"));
  tagsOf(item).forEach((tag) => parts.push(chip(tag)));
  return parts.join("");
}

function imageOf(item) {
  if (Array.isArray(item.images)) return item.images[0] || "";
  return item.image || "";
}

function imageList(item) {
  return Array.isArray(item.images) ? item.images.filter(Boolean) : (item.image ? [item.image] : []);
}

function sourceUrl(item) {
  return item.ori_url || "";
}

function formatDate(value) {
  const text = String(value || "");
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : text;
}

/* 时间戳可能是 "2026-09-21T10:00:00" 也可能是 Unix 秒 */
function formatTime(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  if (/^\d{9,}$/.test(text)) {
    const date = new Date(Number(text) * 1000);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleString("zh-CN", { hour12: false });
    }
  }
  return formatDate(value) || "—";
}

function imageUrl(path) {
  const normalized = String(path || "").replace(/\\/g, "/");
  const marker = "data/images/";
  const cut = normalized.lastIndexOf(marker);
  const relative = (cut >= 0 ? normalized.slice(cut + marker.length) : normalized).replace(/^\/+/, "");
  return "/api/image/" + relative.split("/").filter(Boolean).map(encodeURIComponent).join("/");
}

function keyOf(item) {
  return String(item.id || item.origin_id || item.tweet_id || item.ori_url || item.raw);
}

function buildItem(item) {
  const image = imageOf(item);
  const thumbnail = image
    ? `<img class="thumb" src="${imageUrl(image)}" alt="内容图片" loading="lazy" decoding="async">`
    : "";

  const element = document.createElement("article");
  element.className = "item";
  element.dataset.key = keyOf(item);
  const title = String(item.ai_title || "").trim();
  const chipHtml = chipRow(item);
  element.innerHTML = `
      <div class="item-main">
        <b>${esc(item.account || "未知账号")} · ${esc(item.content_type || "内容")}</b>
        ${title ? `<div class="item-title">${esc(title)}</div>` : ""}
        ${item.invalid_reason ? `<div class="invalid-reason">无效原因：${esc(item.invalid_reason)}</div>` : ""}
        <div class="item-preview">${esc(item.zh || item.raw || "")}</div>
        ${chipHtml ? `<div class="chips">${chipHtml}</div>` : ""}
        <div class="item-provenance">
          <div><span>来源：</span>${sourceUrl(item) ? `<a class="source-url" href="${esc(sourceUrl(item))}" target="_blank" rel="noopener noreferrer" title="${esc(sourceUrl(item))}">${esc(sourceUrl(item))}</a>` : "<em>—</em>"}</div>
          <div><span>信息发布时间：</span><time>${esc(formatTime(item.ts || item.date))}</time></div>
          <div><span>信息获取时间：</span><time>${esc(formatTime(item.collected_at))}</time></div>
        </div>
      </div>
      ${thumbnail}
    `;
  element.hidden = !matchesFilter(item);
  return element;
}

/* ── 状态：/api/state、/api/snapshot、SSE 都是同一份 data ── */
/* 新协议把统计放在 data.stats 里，老形状是平铺在 data 上；两边都认，省得对齐出错。 */
function statValue(data, key, fallback = 0) {
  if (data && data.stats && data.stats[key] !== undefined && data.stats[key] !== null) {
    return data.stats[key];
  }
  if (data && data[key] !== undefined && data[key] !== null) return data[key];
  return fallback;
}

function mergeState(data) {
  for (const [key, value] of Object.entries(data)) {
    if (value !== undefined) lastState[key] = value;
  }
}

function setConnection(ok) {
  const conn = $("#conn");
  if (!conn) return;
  conn.textContent = ok ? "实时连接正常" : "连接中断";
  conn.className = `conn ${ok ? "conn-on" : "conn-off"}`;
}

/* 一次 state/snapshot 响应：能认出来的字段都刷一遍（缺字段就不动，别把界面清空） */
function applyState(data) {
  if (!data || typeof data !== "object") return;
  mergeState(data);
  if (data.collection) renderCollection(data.collection);

  if (data.services) renderServices(data.services);
  if (data.process_progress) renderProcessProgress(data.process_progress);
  if (data.configurable !== undefined || data.configurable_services !== undefined) {
    const value = data.configurable !== undefined ? data.configurable : data.configurable_services;
    // 正常是服务名数组；万一是布尔（"后端能不能配"）就别把它当数组用
    if (Array.isArray(value)) configurable = value;
    else configurable = value ? ["collect", "process"] : [];
    renderConfigButtons();
  }
  if (Array.isArray(data.channels)) {
    channels = data.channels;
    renderChannelBrief();
    if (activeTab === "distribute") renderChannels();
  }
  // /api/state 把计数放在 stats 里，/api/snapshot 是拍平在顶层的；取到哪个算哪个
  const incomingTotal = statValue(data, "item_count", null);
  const incomingPending = statValue(data, "pending_count", null);
  if (incomingTotal !== null) itemTotal = Number(incomingTotal);
  if (incomingTotal !== null || incomingPending !== null) renderCounters(data);
  if (data.env_ready !== undefined) renderEnv(data.env_ready);
  renderTimestamp(data.ts);
}

function renderCollection(data) {
  const count = $("#collection-count"), sources = $("#collection-sources"), warnings = $("#collection-warnings");
  if (!count || !sources || !warnings) return;
  const rows = Array.isArray(data.sources) ? data.sources : [];
  const num = value => Math.max(0, Number(value) || 0);
  const cellNum = value => value == null ? "—" : num(value);
  const running = rows.filter(row => row.state === "running").length;
  const issues = rows.filter(row => ["warning", "error", "interrupted"].includes(row.state)).length;
  const total = rows.reduce((sum, row) => sum + num(row.stored_count), 0);
  count.textContent = `${rows.length} 个来源 · 采集中 ${running} · 异常 ${issues} · 库内 ${rows.every(row => row.stored_count != null) ? total : "—"} 条`;
  const labels = { never: "尚未采集", unrecorded: "状态待确认", running: "采集中", ok: "正常", warning: "有告警", error: "采集失败", interrupted: "已中断" };
  sources.innerHTML = `<table class="collection-table"><caption class="sr-only">网页来源采集状态与数量</caption><thead><tr>
    <th scope="col">来源</th><th scope="col">状态</th><th scope="col" class="collection-number">本轮入库</th>
    <th scope="col" class="collection-number">库内总数</th><th scope="col" class="collection-number">待采集</th>
    <th scope="col" class="collection-number">失败待重试</th><th scope="col">最近采集</th></tr></thead><tbody>${rows.map(row => {
      const state = Object.hasOwn(labels, row.state) ? row.state : "never";
      const error = row.last_error;
      const errorText = error ? [error.error_type, error.detail].filter(Boolean).join("：") : "";
      const note = state === "interrupted" ? "上次采集未完成，可重新启动采集服务" : state === "unrecorded" ? `已有历史采集数据，未记录完整轮次状态${errorText ? `；最近告警：${errorText}` : ""}` : errorText;
      const recent = num(row.last_run) ? new Date(num(row.last_run) * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
      const title = /^https?:\/\//i.test(row.url || "")
        ? `<a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">${esc(row.label || row.source)}</a>` : esc(row.label || row.source);
      return `<tr data-source="${esc(row.source)}"><th scope="row"><span class="collection-type">${esc(row.type || "网页")}</span>${title}</th>
        <td><span class="collection-status collection-status--${state}">${labels[state]}</span>${error && state === "unrecorded" ? `<span class="collection-status collection-status--warning">有告警</span>` : ""}${note ? `<small class="collection-error" title="${esc(note)}">${esc(note)}</small>` : ""}</td>
        <td class="collection-number collection-added">${cellNum(row.collected)}</td><td class="collection-number">${cellNum(row.stored_count)}</td>
        <td class="collection-number">${cellNum(row.pending_count)}</td><td class="collection-number${num(row.failed_count) ? " collection-failed" : ""}">${cellNum(row.failed_count)}</td>
        <td class="collection-time">${esc(recent)}</td></tr>`;
    }).join("")}</tbody></table>`;
  warnings.innerHTML = (Array.isArray(data.warnings) ? data.warnings : []).map(row => {
    const url = String(row.url || ""), link = /^https?:\/\//i.test(url)
      ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>` : esc(url);
    return `<div class="collection-warning"><b>WARNING · ${esc(row.source)} · ${esc(row.error_type)}</b><p>${link}</p><p>${esc(row.detail)} · ${esc(row.stage)}</p></div>`;
  }).join("") || "暂无采集 WARNING";
}
function renderServices(services) {
  for (const name of ["collect", "process"]) {
    const service = services[name] || {};
    const status = $(`#${name}-state`);
    const button = $(`[data-service="${name}"]`);

    if (status) {
      status.textContent = (service.state || (service.running ? "运行中" : "已停止")) +
        (service.pid ? ` · PID ${service.pid}` : "");
      status.className = service.running ? "running" : "stopped";
    }
    if (button) button.textContent = service.running ? "停止" : "启动";
  }
}

function renderProcessProgress(progress) {
  const currentLine = $("#process-progress-current");
  const remaining = $("#process-remaining");
  const countButton = $("#process-backlog-open");
  if (!currentLine || !remaining || !countButton) return;
  const current = progress.current;
  if (progress.running && current) {
    const title = current.title || String(current.raw || "").replace(/\s+/g, " ").slice(0, 64) || "正在整理内容";
    const position = progress.total ? ` · ${progress.index}/${progress.total}` : "";
    currentLine.textContent = `${progress.phase || "正在处理"}：#${current.id} @${current.account || "未知账号"} · ${title}${position}`;
  } else if (progress.running) {
    currentLine.textContent = progress.phase || "正在准备处理任务";
  } else {
    currentLine.textContent = progress.phase === "已停止" ? "处理服务尚未运行" : (progress.phase || "等待处理");
  }
  remaining.textContent = String(progress.remaining_count || 0);
  const stuck = Number(progress.stuck_count || 0);
  countButton.title = "点击查看所有待处理内容；失败项持续轮转重试";
  countButton.classList.toggle("has-stuck", stuck > 0);
  countButton.setAttribute("aria-label", `剩余 ${progress.remaining_count || 0} 条，点击查看列表，失败项持续轮转重试`);
}

function renderProcessBacklog(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  const list = $("#process-backlog-list");
  const summary = $("#process-backlog-summary");
  summary.textContent = `剩余 ${data?.remaining_count || 0} 条 · 失败项移到队尾，持续循环重试`;
  if (!items.length) {
    list.innerHTML = '<p class="process-backlog-empty">当前没有待处理内容。</p>';
    return;
  }
  list.innerHTML = items.map((item) => {
    const title = item.title || String(item.raw || "").replace(/\s+/g, " ").slice(0, 100) || "（无正文）";
    const missing = item.missing?.length ? `缺少：${item.missing.join("、")}` : "译文待完成";
    const status = item.error ? "失败待轮转重试" : "等待处理";
    const origin = item.origin_id ? ` · X:${item.origin_id}` : "";
    return `<article class="process-backlog-item ${item.stuck ? "is-stuck" : ""}">
      <div class="process-backlog-item__head"><strong>#${esc(item.id)} · ${esc(title)}</strong><span>${esc(status)}</span></div>
      <div class="process-backlog-item__meta">@${esc(item.account)} · ${esc(item.phase)} · ${esc(item.stage || "")} · ${esc(missing)} · 累计尝试 ${esc(item.attempts)} 次 · 每次轮到最多 ${esc(item.max_attempts)} 次${esc(origin)}</div>
      ${item.error ? `<p class="process-backlog-item__error">${esc(item.error)}</p>` : ""}
    </article>`;
  }).join("");
}

async function openProcessBacklog() {
  const dialog = $("#process-backlog-dialog");
  const list = $("#process-backlog-list");
  list.innerHTML = '<p class="process-backlog-empty">正在读取待处理列表…</p>';
  dialog.showModal();
  try {
    renderProcessBacklog(await apiGet("/api/process/backlog"));
  } catch (error) {
    list.innerHTML = `<p class="process-backlog-empty">读取失败：${esc(error.message || "请稍后重试")}</p>`;
  }
}

function renderConfigButtons() {
  const list = Array.isArray(configurable) ? configurable : [];
  for (const button of $$("[data-config]")) {
    const name = button.dataset.config;
    const can = list.includes(name === "ai" ? "process" : name);
    button.hidden = !can;
    button.disabled = !can;
  }
}

function renderCounters(data) {
  const pending = Number(statValue(data, "pending_count", 0));
  const count = $("#count");
  if (count) count.textContent = itemTotal;
  const pendingEl = $("#pending");
  if (pendingEl) pendingEl.textContent = pending;
  updateListStatus();
}

function renderEnv(ready) {
  const element = $("#env");
  if (!element) return;
  element.textContent = ready ? "已就绪" : "未就绪";
  element.className = ready ? "running" : "stopped";
}

function renderTimestamp(ts) {
  const updated = $("#updated");
  if (!updated) return;
  const date = ts ? new Date(Number(ts) * 1000) : new Date();
  const stamp = Number.isNaN(date.getTime()) ? new Date() : date;
  updated.textContent = `更新于 ${stamp.toLocaleTimeString("zh-CN", { hour12: false })}`;
}

/* ── 渠道（运行摘要里的小字 + 分发 tab 的卡片） ── */
/* 服务端给的是真布尔，但 int 0/1、"0" 也一起认，别把停用的渠道当成启用 */
function channelEnabled(channel) {
  const value = channel.enabled;
  if (value === undefined || value === null) return true;
  if (value === false || value === 0 || value === "0" || value === "false") return false;
  return true;
}

function channelConnected(channel) {
  return channelEnabled(channel) && Boolean(channel.connected);
}

function channelStatus(channel) {
  if (!channelEnabled(channel)) return { cls: "off", text: "已停用" };
  if (channel.connected) return { cls: "on", text: "已连接" };
  return { cls: "down", text: "未连接" };
}

/* 插件渠道 = 启用/停用推送；托管进程渠道 = 真的启停进程 */
function channelAction(channel) {
  const enabled = channelEnabled(channel);
  const isPlugin = channel.adapter === "plugin";
  if (isPlugin) {
    return { text: enabled ? "停用推送" : "启用推送", cls: enabled ? "secondary" : "" };
  }
  return { text: enabled ? "停止" : "启动", cls: enabled ? "secondary" : "" };
}

function renderChannelBrief() {
  const online = channels.filter(channelConnected).length;
  const onlineEl = $("#channel-online");
  if (onlineEl) onlineEl.textContent = `${online} / ${channels.length}`;
  const totalEl = $("#channel-total");
  if (totalEl) totalEl.textContent = channels.length;
  const stateEl = $("#summary-state");
  if (stateEl) {
    stateEl.textContent = channels.length
      ? `${channels.length} 个渠道 · ${online} 个在线`
      : "还没有登记分发渠道";
  }

  const box = $("#channel-brief");
  if (!box) return;
  if (!channels.length) {
    box.innerHTML = `<p class="channel-brief__empty">没有登记任何分发渠道。</p>`;
    return;
  }
  box.innerHTML = channels.map((channel) => {
    const status = channelStatus(channel);
    const pending = Number(channel.pending || 0);
    return `<div class="channel-brief__row">
        <i class="dot dot-${status.cls}"></i>
        <span class="channel-brief__name">${esc(channel.label || channel.id)}</span>
        <span class="channel-brief__meta">${esc(status.text)}${pending ? ` · 待发 ${pending}` : ""}</span>
      </div>`;
  }).join("");
}

function targetHtml(target) {
  const label = target.label || target.kind || target.source || "—";
  const pending = Number(target.pending || 0);
  return `<li><span class="target-label">${esc(label)}</span>
    <span class="target-pending${pending ? " has" : ""}">待发 ${pending}</span></li>`;
}

function channelCard(channel) {
  const status = channelStatus(channel);
  const action = channelAction(channel);
  const targets = Array.isArray(channel.targets) ? channel.targets : [];
  // 备注优先用 note（渠道自报的 status.note）；没有就退回服务端给的 status 文案
  let note = String(channel.note || "").trim();
  const extra = String(channel.status || "").trim();
  if (!note && extra && extra !== status.text) note = extra;
  return `
    <article class="channel-card">
      <div class="channel-head">
        <div class="channel-head__text">
          <h3>${esc(channel.label || channel.id)}</h3>
          <div class="channel-sub"><code>${esc(channel.id)}</code>
            <span class="chip">${esc(ADAPTER_LABELS[channel.adapter] || channel.adapter || "未知类型")}</span>
            <span class="badge badge-${status.cls}">${esc(status.text)}</span>
          </div>
        </div>
        <div class="channel-actions">
          ${String(channel.id).toLowerCase() === "alison" ? `<button type="button" class="secondary" data-channel-config="alison">配置</button>` : ""}
          <button class="${action.cls}" data-channel-toggle="${esc(channel.id)}">${esc(action.text)}</button>
        </div>
      </div>
      <div class="channel-metrics">
        <div><span>待分发</span><b>${Number(channel.pending || 0)}</b></div>
        <div><span>累计发送</span><b>${Number(channel.sent_total || 0)}</b></div>
        <div><span>最近活动</span><b>${esc(formatTime(channel.last_seen))}</b></div>
      </div>
      <ul class="channel-targets">
        ${targets.length ? targets.map(targetHtml).join("") : `<li class="target-empty">还没有注册目标</li>`}
      </ul>
      ${note ? `<p class="channel-note">备注：${esc(note)}</p>` : ""}
    </article>`;
}

function renderChannels() {
  const box = $("#channels");
  if (!box) return;
  if (!channels.length) {
    box.innerHTML = `<p class="empty-card">还没有任何分发渠道。插件类渠道连上服务端、或看板把托管进程拉起来之后，就会出现在这里。</p>`;
    return;
  }
  box.innerHTML = channels.map(channelCard).join("");
}

async function refreshChannels(silent = false) {
  try {
    const data = await apiGet("/api/channels");
    channels = Array.isArray(data?.channels) ? data.channels : [];
    renderChannelBrief();
    renderChannels();
  } catch (error) {
    if (!silent) notify(`渠道列表读取失败：${error.message}`, "error");
  }
}

async function toggleChannel(id) {
  const channel = channels.find((entry) => String(entry.id) === String(id));
  if (!channel) return;
  const enabled = channelEnabled(channel);
  try {
    await apiPost("/api/channels/toggle", { channel: id, enabled: !enabled });
    notify(`${channel.label || id}：${enabled ? "已停用" : "已启用"}`, "ok");
    await refreshChannels();
    loadPushes();
  } catch (error) {
    reportError(error, "渠道操作失败");
  }
}

/* ── 最近分发记录 ── */
function pushTitle(push) {
  const title = String(push.title || push.ai_title || "").trim();
  if (title) return title;
  const zh = String(push.zh || "").trim().replace(/\s+/g, " ");
  if (zh) return zh.length > 40 ? `${zh.slice(0, 40)}…` : zh;
  return push.item_id !== undefined && push.item_id !== null ? `#${push.item_id}` : "—";
}

function renderPushes(pushes, errorText = "") {
  const body = $("#pushes-body");
  if (!body) return;
  if (errorText) {
    body.innerHTML = `<tr><td colspan="5" class="pushes-empty">${esc(errorText)}</td></tr>`;
    return;
  }
  if (!pushes.length) {
    body.innerHTML = `<tr><td colspan="5" class="pushes-empty">还没有分发记录。</td></tr>`;
    return;
  }
  body.innerHTML = pushes.map((push) => {
    const target = push.label || push.kind || push.source || "—";
    const status = push.status === "success" ? "成功" : push.status === "sent_unrecorded" ? "已送达，待记账" : "失败";
    return `<tr>
      <td class="mono">${esc(formatTime(push.sent_at))}</td>
      <td>${esc(push.channel || "—")}</td>
      <td>${esc(target)}</td>
      <td><span class="push-status ${push.status === "success" ? "is-success" : "is-failed"}">${esc(status)}</span>${push.detail ? `<small class="push-detail">${esc(push.detail)}</small>` : ""}</td>
      <td>${esc(pushTitle(push))}</td>
    </tr>`;
  }).join("");
}

async function loadPushes(silent = false) {
  try {
    const data = await apiGet("/api/pushes?limit=20");
    renderPushes(Array.isArray(data?.pushes) ? data.pushes : []);
  } catch (error) {
    // 定时轮询失败就静默跳过，保留表里已有的记录，不刷一屏红字
    if (!silent) renderPushes([], `分发记录读取失败：${error.message}`);
  }
}

/* ── tab 切换：只切视图，不重新加载整页 ── */
function switchTab(tab) {
  activeTab = tab === "distribute" ? "distribute" : "process";
  for (const button of $$("[data-tab]")) {
    const active = button.dataset.tab === activeTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  const processView = $("#view-process");
  const distributeView = $("#view-distribute");
  if (processView) processView.hidden = activeTab !== "process";
  if (distributeView) distributeView.hidden = activeTab !== "distribute";

  if (activeTab === "distribute") {
    renderChannels();
    refreshChannels();
    loadPushes();
  } else {
    maybeLoadMore();
  }
}

/* ── 列表：默认 20 条，滚到还剩 10 条时再加载 20 条 ── */
const PAGE_SIZE = 20;        // 首屏与每次加载的条数
const LOAD_AHEAD = 10;       // 视口下方还剩这么多条目就开始加载下一页
const RECONCILE_MS = 15000;  // 定时全量核对（防止 SSE 丢事件导致漏内容）
const PUSHES_MS = 10000;     // 分发 tab 打开时刷新最近分发记录的间隔
const MAX_WINDOW = 300;      // 定时核对一次最多比对多少条，避免大库拖慢前端

let shownCount = 0;          // 当前列表里"应该显示"的条数
let hasMore = false;
let loadingMore = false;
let listRequestVersion = 0;

function listQuery() {
  return {
    q: String($("#item-search")?.value || "").trim(),
    sort: String($("#item-sort")?.value || "published"),
  };
}

function listUrl(path, params = {}) {
  const query = new URLSearchParams({ ...listQuery(), ...params });
  return `${path}?${query}`;
}

function elementOf(key) {
  for (const element of $("#items").children) {
    if (element.dataset.key === key) return element;
  }
  return null;
}

/* 首屏/顺序变化时整表重建，返回的条目本身已是"新的在前"。 */
function replaceItems(items) {
  const box = $("#items");
  const scrollTop = window.scrollY;
  box.innerHTML = "";
  cache.clear();

  const fragment = document.createDocumentFragment();
  for (const item of items) {
    cache.set(keyOf(item), item);
    fragment.append(buildItem(item));
  }
  box.append(fragment);
  window.scrollTo({ top: scrollTop });
  applyFilter();
}

/* 追加下一页（滚动加载更多）。 */
function appendItems(items) {
  const box = $("#items");
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const key = keyOf(item);
    if (elementOf(key)) continue;
    cache.set(key, item);
    fragment.append(buildItem(item));
  }
  box.append(fragment);
}

/* 实时补丁：新条目插到最前，被更新过的（完整分析刚提交）就地刷新。 */
function upsertItems(items) {
  const box = $("#items");
  const added = [];
  let updated = 0;
  for (const item of items) {
    const key = keyOf(item);
    const element = elementOf(key);
    const previous = cache.get(key);
    if (!element) {
      added.push(item);
      continue;
    }
    if (!previous || Number(item.rev || 0) !== Number(previous.rev || 0)
        || JSON.stringify(item) !== JSON.stringify(previous)) {
      refreshItem(item);
      updated += 1;
    }
  }
  added.sort((a, b) => Number(a.id || 0) - Number(b.id || 0));   // 小的先插，最大的落在最上面
  for (const item of added) {
    cache.set(keyOf(item), item);
    const element = buildItem(item);
    box.insertBefore(element, box.firstElementChild);
    shownCount += 1;
  }
  if (added.length || updated) {
    applyFilter();
    updateListStatus();
  }
  return added.length + updated;
}

function removeItem(key) {
  for (const element of [...$("#items").children]) {
    if (element.dataset.key === key) element.remove();
  }
  cache.delete(key);
}

function refreshItem(item) {
  const key = keyOf(item);
  for (const element of [...$("#items").children]) {
    if (element.dataset.key === key) {
      element.innerHTML = buildItem(item).innerHTML;
      element.hidden = !matchesFilter(item);
    }
  }
  cache.set(key, item);
}

function updateListStatus() {
  const status = $("#list-status");
  if (!status) return;
  status.textContent = `已显示 ${shownCount} / 共 ${resultTotal} 条`;
  const more = $("#load-more");
  if (more) more.hidden = !hasMore;
}

function refreshListQuery() {
  listRequestVersion += 1;
  loaded = false;
  shownCount = 0;
  resultTotal = 0;
  hasMore = false;
  cache.clear();
  $("#items").replaceChildren();
  updateListStatus();
  loadAll();
}

let itemSearchTimer = 0;
$("#item-search")?.addEventListener("input", () => {
  clearTimeout(itemSearchTimer);
  itemSearchTimer = setTimeout(refreshListQuery, 300);
});
$("#item-sort")?.addEventListener("change", refreshListQuery);

async function loadAll() {
  const requestVersion = ++listRequestVersion;
  try {
    const data = await apiGet(listUrl("/api/snapshot", { offset: 0, limit: PAGE_SIZE }));
    if (requestVersion !== listRequestVersion) return;
    applyState(data);
    replaceItems(data.items || []);
    latestId = Math.max(latestId, Number(statValue(data, "latest_id", 0)));
    latestRev = Math.max(latestRev, Number(statValue(data, "latest_rev", 0)));
    shownCount = (data.items || []).length;
    resultTotal = Number(data.result_total ?? itemTotal);
    hasMore = data.has_more !== undefined ? Boolean(data.has_more) : shownCount < resultTotal;
    loaded = true;
    updateListStatus();
    setConnection(true);
  } catch (error) {
    setConnection(false);
    reportError(error, "首屏加载失败");
  }
}

/* 滚动到底部附近（还剩 LOAD_AHEAD 条）就再要一页。 */
async function loadMore() {
  if (!loaded || !hasMore || loadingMore) return;
  loadingMore = true;
  const requestVersion = listRequestVersion;
  const more = $("#load-more");
  if (more) more.textContent = "加载中…";
  try {
    const data = await apiGet(listUrl("/api/items", { offset: shownCount, limit: PAGE_SIZE }));
    if (requestVersion !== listRequestVersion) return;
    const items = data.items || [];
    appendItems(items);
    shownCount += items.length;
    resultTotal = data.total !== undefined && data.total !== null ? Number(data.total) : resultTotal;
    hasMore = items.length >= PAGE_SIZE && shownCount < resultTotal;
    applyFilter();
  } catch (error) {
    notify(`加载更多失败：${error.message}`, "error");
  } finally {
    loadingMore = false;
    if (more) more.textContent = "加载更多";
    updateListStatus();
  }
}

/* 视口下方还剩几条？剩 ≤ LOAD_AHEAD 就补一页。 */
function maybeLoadMore() {
  if (activeTab !== "process") return;   // 分发 tab 下列表是隐藏的，量出来的位置没意义
  if (!loaded || !hasMore || loadingMore) return;
  const viewport = window.innerHeight;
  let below = 0;
  for (const element of $("#items").children) {
    if (element.hidden) continue;
    if (element.getBoundingClientRect().top > viewport) below += 1;
    if (below > LOAD_AHEAD) return;
  }
  if (below <= LOAD_AHEAD) loadMore();
}

/* 实时补丁：SSE 说"有新条目或老条目被改过"时拉一次。 */
async function pullChanges() {
  if (!loaded) return 0;
  const query = listQuery();
  if (query.q || query.sort !== "published") return reconcile();
  try {
    const data = await apiGet(`/api/snapshot?since_id=${latestId}&since_rev=${latestRev}`);
    applyState(data);
    resultTotal = Number(data.result_total ?? itemTotal);
    const items = data.items || [];
    const touched = items.length ? upsertItems(items) : 0;
    latestId = Math.max(latestId, Number(statValue(data, "latest_id", 0)));
    latestRev = Math.max(latestRev, Number(statValue(data, "latest_rev", 0)));
    updateListStatus();
    return touched;
  } catch (error) {
    return 0;   // 保持现有列表不动，下个周期再试
  }
}

/* 定时全量核对"最新 ~ 当前显示的这一段"：补漏、刷新、删掉服务端已不存在的。
   只靠 SSE/增量补丁有可能漏（浏览器休眠、连接断过），这一段负责兜底。 */
async function reconcile() {
  if (!loaded) return;
  const limit = Math.min(Math.max(shownCount, PAGE_SIZE), MAX_WINDOW);
  try {
    const data = await apiGet(listUrl("/api/snapshot", { offset: 0, limit }));
    applyState(data);
    resultTotal = Number(data.result_total ?? itemTotal);
    const items = data.items || [];
    const keys = items.map(keyOf);
    const domKeys = [...$("#items").children].map((element) => element.dataset.key);

    const sameOrder = domKeys.length === keys.length && domKeys.every((key, i) => key === keys[i]);
    if (keys.length && !sameOrder) {
      replaceItems(items);            // 集合或顺序变了（有删除/新条目穿插）：按服务端顺序重建
      shownCount = Math.max(shownCount, items.length);
    } else {
      let changed = 0;
      for (const item of items) {
        const previous = cache.get(keyOf(item));
        if (!previous || Number(item.rev || 0) !== Number(previous.rev || 0)
            || JSON.stringify(item) !== JSON.stringify(previous)) {
          refreshItem(item);
          changed += 1;
        }
      }
      if (changed) applyFilter();
    }
    for (const element of [...$("#items").children]) {
      if (!keys.includes(element.dataset.key)) removeItem(element.dataset.key);
    }
    latestId = Math.max(latestId, Number(statValue(data, "latest_id", 0)));
    latestRev = Math.max(latestRev, Number(statValue(data, "latest_rev", 0)));
    hasMore = shownCount < resultTotal;
    updateListStatus();
  } catch (error) {
    /* 下一轮再试 */
  }
}

/* ── 详情 dialog ── */
function openDetail(key) {
  const item = cache.get(key);
  if (!item) return;

  detail = item;
  const title = String(item.ai_title || "").trim();
  $("#detail-title").textContent = title
    ? `${title} · ${item.account || "未知账号"}`
    : `${item.account || "未知账号"} · ${item.content_type || "内容"}`;
  const meta = [
    formatDate(item.date),
    item.stage || item._stage,
    scoreOf(item) ? `${scoreOf(item).label} ${scoreOf(item).value}` : "分数 —",

    item.decision,
    item.invalid_reason ? `无效原因：${item.invalid_reason}` : "",
    importanceLabel(item),
    ...tagsOf(item),
  ].filter(Boolean);
  $("#detail-meta").innerHTML = meta
    .map((value) => `<span>${esc(value)}</span>`)
    .join("");
  const summary = String(item.ai_summary || "").trim();
  const summaryBox = $("#detail-summary");
  summaryBox.textContent = summary ? `AI 要点：${summary}` : "";
  summaryBox.hidden = !summary;

  // 引用（quote）：被引用那条的正文/译文 + 链接。有引用的条目由 AI 与主帖一起分析，
  // 所以这里也标一句，能看到"为什么这条没被判成评价"。
  const quotedBox = $("#detail-quoted");
  const quotedRaw = String(item.quoted_raw || "").trim();
  const quotedZh = String(item.quoted_zh || "").trim();
  if (quotedRaw || quotedZh) {
    const who = String(item.quoted_account || "").trim();
    const url = String(item.quoted_url || "").trim();
    const head = `引用了 @${who || "?"}`;
    const link = url ? ` ${url}` : "";
    quotedBox.textContent = `${head}${link}\n${quotedZh || quotedRaw}`;
    quotedBox.hidden = false;
  } else {
    quotedBox.textContent = "";
    quotedBox.hidden = true;
  }
  $("#detail-edit").value = item.zh || "";
  $("#detail-fields").textContent = JSON.stringify(item, null, 2);
  showLanguage("raw");

  const images = imageList(item);
  $("#detail-image-section").hidden = images.length === 0;
  $("#detail-images").innerHTML = images
    .map((path) => `<figure><img src="${imageUrl(path)}" alt="内容图片"><figcaption>${esc(path)}</figcaption></figure>`)
    .join("");

  $("#detail-dialog").querySelector(".detail-scroll").scrollTop = 0;
  markDialogClean($("#detail-dialog"));
  $("#detail-dialog").showModal();
}

function showLanguage(language) {
  if (!detail) return;
  const isFields = language === "fields";
  const isTranslation = language === "zh";
  $("#detail-body").textContent = isFields
    ? ""
    : (isTranslation ? (detail.zh || "暂无译文") : (detail.raw || "暂无原文"));
  $("#detail-body").hidden = isFields;
  $("#detail-fields").hidden = !isFields;
  $("#translation-editor").hidden = !isTranslation;
  document.querySelectorAll("[data-lang]").forEach((button) => {
    button.classList.toggle("active", button.dataset.lang === language);
  });
}

async function saveDetail() {
  if (!detail) return;
  const zh = $("#detail-edit").value;
  try {
    await apiPost("/api/item/update", { id: detail.id, zh });
  } catch (error) {
    return reportError(error, "保存失败");
  }
  detail.zh = zh;
  detail.rev = Number(detail.rev || 0) + 1;   // 本地也 +1，定时核对时对得上
  refreshItem(detail);
  notify("译文已保存", "ok");
  $("#detail-dialog").close();
}

async function deleteDetail() {
  if (!detail) return;
  if (detail.id === undefined || detail.id === null) {
    return notify("这条内容没有有效数据库 ID，无法删除", "error");
  }
  if (!confirm(`确定删除「${detail.account || "这条推文"}」吗？删除后不可恢复。`)) return;

  try {
    await apiPost("/api/item/delete", { id: detail.id });
  } catch (error) {
    return reportError(error, "删除失败");
  }
  const key = keyOf(detail);
  detail = null;
  removeItem(key);
  $("#detail-dialog").close();
}

async function toggleService(name) {
  const running = Boolean(lastState.services?.[name]?.running);
  try {
    await apiPost("/api/service/toggle", { service: name, enabled: !running });
    notify(`${SERVICE_LABELS[name] || name}服务：${running ? "已停止" : "已启动"}`, "ok");
  } catch (error) {
    reportError(error, "操作失败");
  }
}

/* ── 配置 dialog ── */
/* 新接口是 {fields, env}；老接口字段直接带 value，还有 cookie_text。两边都认。 */
function configFieldList(data) {
  const env = (data && typeof data.env === "object" && data.env) ? data.env : {};
  const raw = Array.isArray(data?.fields) ? data.fields : [];
  const list = raw.map((field) => {
    if (typeof field === "string") {
      return { key: field, value: env[field] ?? "", secret: false, choices: [], hint: "" };
    }
    const key = String(field.key || field.name || "");
    const value = field.value !== undefined && field.value !== null
      ? field.value
      : (env[key] ?? "");
    return {
      key,
      value,
      secret: Boolean(field.secret),
      choices: Array.isArray(field.choices) ? field.choices : [],
      hint: String(field.hint || field.tip || field.help || ""),
    };
  }).filter((field) => field.key);

  if (list.length) return list;
  return Object.keys(env).map((key) => ({
    key, value: env[key], secret: false, choices: [], hint: "",
  }));
}

function renderConfigFields(data) {
  const fields = configFieldList(data);
  const cookieField = activeConfig === "collect" ? `
    <div class="config-field cookie-field">
      ${fieldLabel("X Cookies", "可粘贴 Header String、JSON 或 Netscape cookies.txt；保存后会写入 data/state/x_cookies.txt，并自动更新 COOKIES_PATH。")}
      <textarea id="cookie-content" class="cookie-textarea" placeholder="直接粘贴 Cookie 内容">${esc(data?.cookie_text || "")}</textarea>
      <div class="cookie-tools">
        <input id="cookie-file" type="file" accept=".txt,.json,text/plain,application/json">
      </div>
    </div>` : "";

  $("#config-fields").innerHTML = fields.map((field) => `
    <div class="config-field">
      ${fieldLabel(activeConfig === "process" ? PROCESS_LABELS[field.key] || field.key : field.key, hints[field.key] || field.hint || "")}
      ${fieldControl(field)}
    </div>
  `).join("") + cookieField;

  const fileInput = $("#cookie-file");
  if (fileInput) {
    fileInput.onchange = (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => { $("#cookie-content").value = reader.result; };
      reader.readAsText(file, "utf-8");
    };
  }
}

async function openConfig(name) {
  if (name === "ai") return openAIConfig();
  activeConfig = name;
  $("#config-title").textContent = name === "process" ? "处理配置" : `${SERVICE_LABELS[name] || name}服务配置`;
  try {
    const data = await apiGet(`/api/config/${encodeURIComponent(name)}`);
    renderConfigFields(data || {});
    markDialogClean($("#config-dialog"));
  } catch (error) {
    return reportError(error, "配置读取失败");
  }
  $("#config-dialog").showModal();
}

async function saveConfig() {
  if (!activeConfig) return;
  const values = {};
  document.querySelectorAll("#config-fields [data-key]").forEach((field) => {
    if (field.value.trim() || field.dataset.key === "SOURCES") values[field.dataset.key] = field.value.trim();
  });
  const cookieContent = $("#cookie-content")?.value.trim() || "";

  try {
    await apiPost("/api/config", { service: activeConfig, values });
    if (activeConfig === "collect" && cookieContent) {
      await apiPost("/api/cookies/import", { content: cookieContent });
    }
  } catch (error) {
    return reportError(error, "配置保存失败");
  }
  notify("配置已保存", "ok");
  $("#config-dialog").close();
}

/* AI 接入点：编辑对象与使用对象独立；只有左侧“使用此接入点”才提交切换。 */
const PROCESS_LABELS = {
  AI_REPEAT_CONTEXT: "去重参考条数", ANALYSIS_MAX_ATTEMPTS: "单条每轮尝试次数",
  ANALYSIS_INLINE_RETRIES: "单次请求重试次数", ANALYSIS_RATE_LIMIT_WAIT: "限流等待（秒）",
  ANALYSIS_PAUSE_SECONDS: "处理间隔（秒）",
};
const AI_LABELS = {
  name: "接入点名称", MODEL_PROTOCOL: "接口协议", MODEL_API_BASE: "API 地址",
  MODEL_NAME: "模型名称", MODEL_API_KEY: "API 密钥", MODEL_TIMEOUT: "请求超时（秒）",
  MODEL_MAX_TOKENS: "最大输出 Token",
};
let aiSettings = null;
let aiSelected = "";
let aiBusy = false;

function captureAIFields() {
  const row = aiSettings?.endpoints.find(item => item.id === aiSelected);
  if (!row) return;
  $$("#ai-fields [data-key]").forEach(input => { row[input.dataset.key] = input.value; });
}

function renderAIConfig() {
  const row = aiSettings.endpoints.find(item => item.id === aiSelected);
  $("#ai-count").textContent = aiSettings.endpoints.length;
  $("#ai-list").innerHTML = aiSettings.endpoints.map(item => `
    <div class="ai-card ${item.id === aiSelected ? "selected" : ""}">
      <button type="button" class="ai-select" data-ai-select="${esc(item.id)}" aria-pressed="${item.id === aiSelected}">
        <span class="ai-card-title">${esc(item.name || "未命名接入点")}</span>
        ${item.id === aiSettings.active_id ? '<span class="ai-active">当前使用</span>' : ""}
        <span class="ai-card-model">${esc(item.MODEL_NAME || "待配置")}</span>
      </button>
      <button type="button" class="secondary ai-use" data-ai-use="${esc(item.id)}" ${item.id === aiSettings.active_id ? "disabled" : ""}>
        ${item.id === aiSettings.active_id ? "正在使用" : "使用此接入点"}
      </button>
    </div>`).join("");
  $("#ai-fields").innerHTML = Object.entries(AI_LABELS).map(([key, label]) => `
    <div class="config-field ${["MODEL_API_BASE", "MODEL_NAME", "MODEL_API_KEY"].includes(key) ? "ai-wide" : ""}">
      ${fieldLabel(label, hints[key] || "")}
      ${fieldControl({key, value: row[key] || "", secret: key === "MODEL_API_KEY", choices: key === "MODEL_PROTOCOL" ? ["openai", "responses", "anthropic"] : []})}
      ${key === "MODEL_API_KEY" ? `<small>${row.key_configured ? "已配置，留空则保留原密钥" : "请填写此接入点的 API 密钥"}</small>` : ""}
    </div>`).join("");
  // 未保存的密钥只保存在当前编辑会话，切换卡片后也不能丢失。
  $("#ai-fields [data-key='MODEL_API_KEY']").value = row.MODEL_API_KEY || "";
  $("#ai-delete").disabled = row.id === aiSettings.active_id || aiSettings.endpoints.length === 1;
  const active = aiSettings.endpoints.find(item => item.id === aiSettings.active_id);
  $("#ai-current").textContent = `当前使用：${active?.name || ""}`;
}

async function openAIConfig() {
  try { aiSettings = await apiGet("/api/config/ai"); }
  catch (error) { return reportError(error, "AI 配置读取失败"); }
  aiSelected = aiSettings.active_id;
  renderAIConfig();
  markDialogClean($("#ai-dialog"));
  $("#ai-dialog").showModal();
}

function addAIEndpoint(copy = false) {
  if (aiBusy) return;
  captureAIFields();
  const source = aiSettings.endpoints.find(item => item.id === aiSelected);
  const row = copy ? {...source, name: `${source.name} 副本`, copy_from: source.copy_from || source.id} : {
    name: "新接入点", MODEL_PROTOCOL: "openai", MODEL_API_BASE: "", MODEL_NAME: "",
    MODEL_API_KEY: "", MODEL_TIMEOUT: "90", MODEL_MAX_TOKENS: "0", key_configured: false,
  };
  row.id = crypto.randomUUID();
  aiSettings.endpoints.push(row);
  aiSelected = row.id;
  renderAIConfig();
  $("#ai-fields [data-key='name']").focus();
}

async function saveAIConfig(useId = "") {
  if (aiBusy) return;
  captureAIFields();
  aiBusy = true;
  const controls = $$("#ai-dialog button, #ai-dialog input, #ai-dialog select");
  const disabled = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  try {
    aiSettings = await apiPost(useId ? "/api/config/ai/use" : "/api/config/ai", {
      endpoints: aiSettings.endpoints, version: aiSettings.version, ...(useId ? {id: useId} : {}),
    });
    renderAIConfig();
    markDialogClean($("#ai-dialog"));
    if (useId) notify("已切换使用的接入点，下一次 AI 请求生效", "ok");
    else { notify("AI 配置已保存", "ok"); $("#ai-dialog").close(); }
  } catch (error) { reportError(error, useId ? "接入点切换失败" : "AI 配置保存失败"); }
  finally {
    aiBusy = false;
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
    // 使用成功后编辑卡片可能已成为当前项，不能恢复成切换前的删除按钮状态。
    $("#ai-delete").disabled = aiSelected === aiSettings.active_id || aiSettings.endpoints.length === 1;
  }
}

/* ── 艾莉森 QQ Bot：独立配置弹窗；刷新时绝不重绘正在编辑的表单 ── */
const ALISON_KEYS = ["APPID", "SECRET", "SANDBOX", "ALISON_WITH_IMAGE", "ALISON_DISCOVER_GROUPS"];
let alisonSession = 0;
let alisonPoll = null;
let alisonBusy = false;
let alisonLoading = false;
const alisonGroupBusy = new Set();

function alisonBool(value) {
  return value === true || value === 1 || value === "1" || String(value).toLowerCase() === "true";
}

function alisonStatus(id, text, error = false) {
  const element = $(id);
  element.textContent = text;
  element.classList.toggle("alison-error", error);
}

function alisonError(error, fallback) {
  // A server error might echo request bodies (including SECRET). Never display it here.
  alisonStatus("#alison-load-state", fallback, true);
}

function fillAlisonFields(fields) {
  const byKey = new Map((Array.isArray(fields) ? fields : []).map(field => [field.key, field]));
  const form = $("#alison-form");
  for (const key of ALISON_KEYS) {
    const control = form.elements.namedItem(key);
    const field = byKey.get(key);
    if (!control) continue;
    const value = field?.value ?? "";
    if (control.type === "checkbox") control.checked = alisonBool(value);
    else if (key === "SANDBOX") control.value = alisonBool(value) ? "true" : "false";
    else control.value = String(value);
  }
  const sandbox = byKey.get("SANDBOX");
  const select = $("#alison-sandbox");
  // Use the backend's canonical value when it offers explicit choices.
  if (Array.isArray(sandbox?.choices) && sandbox.choices.length) {
    select.innerHTML = sandbox.choices.map(choice => {
      const value = String(choice);
      const label = alisonBool(value) ? "沙箱环境" : (value === "false" || value === "0" ? "正式环境" : value);
      return `<option value="${esc(value)}">${esc(label)}</option>`;
    }).join("");
    select.value = String(sandbox.value ?? "");
    if (select.selectedIndex < 0) select.value = alisonBool(sandbox.value) ? "true" : "false";
  } else {
    select.innerHTML = '<option value="false">正式环境</option><option value="true">沙箱环境</option>';
    select.value = alisonBool(sandbox?.value) ? "true" : "false";
  }
}

function alisonTags(value) {
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  if (!value) return [];
  if (typeof value === "string") {
    try { const parsed = JSON.parse(value); if (Array.isArray(parsed)) return parsed.map(String).filter(Boolean); }
    catch (_) { /* comma-delimited text is also accepted */ }
    return value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
  }
  return [];
}

function alisonGroupCard(group) {
  const id = String(group.group_openid ?? "");
  const member = alisonBool(group.is_member);
  const memberLabel = !member ? "已退群" : (!group.last_checked && !group.joined_at ? "已登记 · 待核实" : "已加入");
  const enabled = alisonBool(group.push_enabled);
  const tags = alisonTags(group.group_tags);
  const name = String(group.group_name || "群资料查询中…");
  const memberCount = group.group_member_num === null || group.group_member_num === undefined || group.group_member_num === ""
    ? "查询中…" : String(group.group_member_num);
  return `<article class="alison-group-card">
    <div class="alison-group-top"><div class="alison-group-identity"><h4>${esc(name)}</h4></div>
      <label class="alison-group-toggle"><span>推送${enabled ? "已开启" : "已关闭"}</span><input type="checkbox" role="switch" data-alison-group="${esc(id)}" ${enabled ? "checked" : ""} ${alisonGroupBusy.has(id) ? "disabled" : ""} aria-label="${esc(name)} 推送" /></label></div>
    <div class="alison-group-info">${group.group_finger_memo ? `<p>${esc(group.group_finger_memo)}</p>` : ""}
      <div class="alison-group-tags">${group.group_class_text ? `<span>${esc(group.group_class_text)}</span>` : ""}${tags.map(tag => `<span>${esc(tag)}</span>`).join("")}</div>
      <div class="alison-group-meta"><span>人数 ${esc(memberCount)}</span><span>成员状态：${memberLabel}</span><span>加入时间：${esc(formatTime(group.joined_at))}</span><span>上次检查：${esc(formatTime(group.last_checked))}</span></div>
      ${group.last_error ? `<p class="alison-group-error">检查错误：${esc(group.last_error)}</p>` : ""}</div>
    </article>`;
}

function renderAlisonGroups(groups) {
  const list = Array.isArray(groups) ? groups : [];
  $("#alison-group-count").textContent = `(${list.length})`;
  $("#alison-groups-list").innerHTML = list.length
    ? list.map(alisonGroupCard).join("")
    : '<p class="alison-empty">还没有已知群。等待入群事件自动发现，或在下方填写 group_openid 手动添加。</p>';
}

function renderAlisonRefresh(refresh) {
  const state = refresh || {};
  const running = alisonBool(state.running);
  const progress = $("#alison-progress");
  const errorCount = Number(state.error || 0);
  const total = Number(state.total || 0);
  const done = Number(state.done || 0);
  progress.hidden = !running && !total && !errorCount;
  progress.classList.toggle("alison-error", Boolean(errorCount));
  progress.textContent = `${running ? "正在刷新" : "刷新完成"}：${done} / ${total}${errorCount ? ` · 错误 ${errorCount}` : ""}${state.message ? ` · ${state.message}` : ""}`;
  $("#alison-refresh").disabled = running || alisonBusy;
  if (running) scheduleAlisonPoll();
  else stopAlisonPoll();
}

function stopAlisonPoll() {
  if (alisonPoll) clearTimeout(alisonPoll);
  alisonPoll = null;
}
function scheduleAlisonPoll() {
  if (alisonPoll || !$("#alison-dialog").open) return;
  const session = alisonSession;
  alisonPoll = setTimeout(async () => {
    alisonPoll = null;
    if (session !== alisonSession || !$("#alison-dialog").open) return;
    try {
      const data = await apiGet("/api/config/alison");
      if (session !== alisonSession || !$("#alison-dialog").open) return;
      // Poll only the directory and progress. Form controls retain unsaved edits.
      renderAlisonGroups(data?.groups);
      renderAlisonRefresh(data?.refresh);
    } catch (_) {
      if (session !== alisonSession || !$("#alison-dialog").open) return;
      alisonStatus("#alison-load-state", "群信息轮询失败，稍后重试。", true);
      scheduleAlisonPoll();
    }
  }, 2000);
}

async function openAlison() {
  const dialog = $("#alison-dialog");
  if (dialog.open) return;
  const session = ++alisonSession;
  alisonLoading = true;
  $("#alison-content").hidden = true;
  alisonStatus("#alison-load-state", "正在读取配置…");
  alisonStatus("#alison-save-state", "");
  alisonStatus("#alison-add-state", "");
  dialog.showModal();
  $("#alison-close").focus();
  try {
    const data = await apiGet("/api/config/alison");
    if (session !== alisonSession || !dialog.open) return;
    fillAlisonFields(data?.fields);
    $("#alison-group-openid").value = "";
    markDialogClean(dialog);
    renderAlisonGroups(data?.groups);
    $("#alison-content").hidden = false;
    alisonStatus("#alison-load-state", "");
    renderAlisonRefresh(data?.refresh);
    const hasCredentials = ["APPID", "SECRET"].every(key =>
      String((data?.fields || []).find(field => field.key === key)?.value || "").trim());
    const missingInfo = (data?.groups || []).some(group => group.is_member && !group.last_checked &&
      (!group.group_name || group.group_member_num === null || group.group_member_num === undefined));
    if (hasCredentials && missingInfo && !data?.refresh?.running) {
      const started = await apiPost("/api/config/alison/groups/refresh", { missing_only: true });
      if (session === alisonSession && dialog.open) renderAlisonRefresh(started?.refresh);
    }
  } catch (_) {
    if (session === alisonSession && dialog.open) alisonError(null, "配置读取失败，请关闭后重试。");
  } finally { if (session === alisonSession) alisonLoading = false; }
}

function closeAlison() {
  const dialog = $("#alison-dialog");
  if (!dialog.open || dialog.classList.contains("is-closing")) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    dialog.close();
    return;
  }
  dialog.classList.add("is-closing");
  window.setTimeout(() => {
    dialog.classList.remove("is-closing");
    if (dialog.open) dialog.close();
  }, 160);
}
$("#alison-dialog").addEventListener("close", () => {
  $("#alison-dialog").classList.remove("is-closing");
  ++alisonSession;
  stopAlisonPoll();
});
async function saveAlisonSettings() {
  if (alisonBusy || alisonLoading) return false;
  const form = $("#alison-form");
  const values = {};
  for (const key of ALISON_KEYS) {
    const control = form.elements.namedItem(key);
    values[key] = control.type === "checkbox" ? control.checked : control.value.trim();
  }
  const session = alisonSession;
  alisonBusy = true;
  $("#alison-save").disabled = true;
  alisonStatus("#alison-save-state", "正在保存…");
  try {
    await apiPost("/api/config/alison", { values });
    if (session === alisonSession) {
      alisonStatus("#alison-save-state", "配置已保存");
      markDialogPartClean($("#alison-dialog"), "settings");
    }
    return true;
  } catch (_) {
    if (session === alisonSession) alisonStatus("#alison-save-state", "保存失败，请重试。", true);
    return false;
  } finally {
    alisonBusy = false;
    $("#alison-save").disabled = false;
  }
}
$("#alison-form").addEventListener("submit", event => {
  event.preventDefault(); saveDialogWithLock($("#alison-dialog"), saveAlisonSettings);
});

async function saveAlisonGroup() {
  const input = $("#alison-group-openid");
  const group_openid = input.value.trim();
  if (!group_openid || $("#alison-add").disabled) return false;
  const session = alisonSession;
  $("#alison-add").disabled = true;
  alisonStatus("#alison-add-state", "正在添加…");
  try {
    const data = await apiPost("/api/config/alison/groups/add", { group_openid });
    if (session !== alisonSession) return;
    const existing = Array.from($("#alison-groups-list").querySelectorAll("[data-alison-group]"))
      .map(toggle => toggle.closest(".alison-group-card"));
    if (data?.group) {
      const duplicate = existing.some(card => card.querySelector("[data-alison-group]")?.dataset.alisonGroup === group_openid);
      if (!duplicate) {
        if (!existing.length) $("#alison-groups-list").innerHTML = "";
        $("#alison-groups-list").insertAdjacentHTML("afterbegin", alisonGroupCard(data.group));
        $("#alison-group-count").textContent = `(${existing.length + 1})`;
      }
    }
    input.value = "";
    markDialogPartClean($("#alison-dialog"), "group");
    alisonStatus("#alison-add-state", "群已添加");
    return true;
  } catch (_) {
    if (session === alisonSession) alisonStatus("#alison-add-state", "添加失败，请检查 OpenID 后重试。", true);
    return false;
  } finally { $("#alison-add").disabled = false; }
}
$("#alison-add-form").addEventListener("submit", event => {
  event.preventDefault(); saveDialogWithLock($("#alison-dialog"), saveAlisonGroup);
});

$("#alison-groups-list").addEventListener("change", async event => {
  const control = event.target.closest("[data-alison-group]");
  if (!control) return;
  const group_openid = control.dataset.alisonGroup;
  const enabled = control.checked;
  const session = alisonSession;
  alisonGroupBusy.add(group_openid);
  control.disabled = true;
  try {
    const data = await apiPost("/api/config/alison/groups/toggle", { group_openid, enabled });
    if (session !== alisonSession) return;
    control.closest(".alison-group-card").outerHTML = alisonGroupCard(data?.group || { group_openid, push_enabled: enabled });
  } catch (_) {
    if (session === alisonSession) {
      control.checked = !enabled;
      alisonStatus("#alison-add-state", "群推送设置失败，请重试。", true);
    }
  } finally {
    alisonGroupBusy.delete(group_openid);
    const current = Array.from($("#alison-groups-list").querySelectorAll("[data-alison-group]"))
      .find(toggle => toggle.dataset.alisonGroup === group_openid);
    if (current) current.disabled = false;
  }
});

$("#alison-refresh").addEventListener("click", async () => {
  if (alisonBusy) return;
  const session = alisonSession;
  alisonBusy = true;
  $("#alison-refresh").disabled = true;
  alisonStatus("#alison-load-state", "正在启动群信息刷新…");
  try {
    const data = await apiPost("/api/config/alison/groups/refresh", {});
    if (session !== alisonSession) return;
    alisonStatus("#alison-load-state", "");
    renderAlisonRefresh(data?.refresh);
  } catch (_) {
    if (session === alisonSession) alisonStatus("#alison-load-state", "群信息刷新失败，请重试。", true);
  } finally {
    alisonBusy = false;
    if (session === alisonSession && !alisonPoll) $("#alison-refresh").disabled = false;
  }
});

/* 所有弹窗统一关闭入口：无改动直接关闭，有改动可保存、放弃或继续编辑。 */
const dialogBaselines = new WeakMap();
let pendingDialogClose = null;
let closeSaveBusy = false;

function dialogSnapshot(dialog) {
  if (dialog.id === "ai-dialog") {
    captureAIFields();
    return {endpoints: aiSettings?.endpoints || [], active: aiSettings?.active_id};
  }
  if (dialog.id === "config-dialog") return {
    fields: $$("#config-fields [data-key]").map(control => [control.dataset.key, control.value]),
    cookies: $("#cookie-content")?.value || "",
  };
  if (dialog.id === "detail-dialog") return {translation: $("#detail-edit").value};
  if (dialog.id === "alison-dialog") return {
    settings: ALISON_KEYS.map(key => {
      const input = $("#alison-form").elements.namedItem(key);
      return [key, input.type === "checkbox" ? input.checked : input.value];
    }), group: $("#alison-group-openid").value,
  };
  return {};
}

function markDialogClean(dialog) { dialogBaselines.set(dialog, JSON.stringify(dialogSnapshot(dialog))); }
function markDialogPartClean(dialog, key) {
  const before = JSON.parse(dialogBaselines.get(dialog) || "{}");
  before[key] = dialogSnapshot(dialog)[key];
  dialogBaselines.set(dialog, JSON.stringify(before));
}
function dialogIsDirty(dialog) {
  return dialogBaselines.has(dialog) && dialogBaselines.get(dialog) !== JSON.stringify(dialogSnapshot(dialog));
}
function dialogIsBusy(dialog) {
  return dialog.dataset.saving === "1" || (dialog.id === "ai-dialog" && aiBusy) ||
    (dialog.id === "alison-dialog" && (alisonBusy || alisonGroupBusy.size || $("#alison-add").disabled));
}
function finishDialogClose(dialog) {
  hideHintTip();
  if (dialog.id === "alison-dialog") closeAlison();
  else dialog.close();
}
function requestDialogClose(dialog) {
  if (!dialog.open || dialogIsBusy(dialog) || closeSaveBusy) return;
  if (dialog.id === "unsaved-dialog") {
    pendingDialogClose = null;
    dialog.close();
    return;
  }
  if (!dialogIsDirty(dialog)) return finishDialogClose(dialog);
  if ($("#unsaved-dialog").open) return;
  pendingDialogClose = dialog;
  $("#unsaved-message").textContent = "此弹窗有未保存的修改，是否保存后关闭？";
  $("#unsaved-dialog").showModal();
  $("#unsaved-cancel").focus();
}

async function saveDialogWithLock(dialog, action) {
  if (dialogIsBusy(dialog)) return false;
  const controls = Array.from(dialog.querySelectorAll("input, select, textarea, button"));
  const disabled = controls.map(control => control.disabled);
  // 设置锁但不禁用提交按钮：Alison 的既有保存函数会检查按钮是否可用。
  dialog.dataset.saving = "1";
  controls.filter(control => control.matches("input,select,textarea")).forEach(control => { control.disabled = true; });
  try { return await action(); }
  finally { delete dialog.dataset.saving; controls.forEach((control, i) => { control.disabled = disabled[i]; }); }
}

async function savePendingDialog() {
  const dialog = pendingDialogClose;
  if (!dialog || closeSaveBusy || dialogIsBusy(dialog)) return;
  closeSaveBusy = true;
  $$("#unsaved-dialog button").forEach(button => { button.disabled = true; });
  try {
    await saveDialogWithLock(dialog, async () => {
      if (dialog.id === "ai-dialog") return saveAIConfig();
      if (dialog.id === "config-dialog") return saveConfig();
      if (dialog.id === "detail-dialog") return saveDetail();
      if (dialog.id === "alison-dialog") {
        const before = JSON.parse(dialogBaselines.get(dialog) || "{}");
        const current = dialogSnapshot(dialog);
        if (JSON.stringify(before.settings) !== JSON.stringify(current.settings) && !await saveAlisonSettings()) return;
        if (current.group.trim() && !await saveAlisonGroup()) return;
        if (!dialogIsDirty(dialog)) finishDialogClose(dialog);
      }
    });
    if (!dialog.open || !dialogIsDirty(dialog)) {
      $("#unsaved-dialog").close();
      pendingDialogClose = null;
    } else $("#unsaved-message").textContent = "保存未完成，请检查配置后重试，或返回继续编辑。";
  } finally {
    closeSaveBusy = false;
    $$("#unsaved-dialog button").forEach(button => { button.disabled = false; });
  }
}

for (const dialog of $$("dialog")) {
  const outside = event => {
    const rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  };
  dialog.addEventListener("pointerdown", event => {
    dialog.dataset.backdropPress = event.target === dialog && outside(event) ? "1" : "0";
  });
  dialog.addEventListener("click", event => {
    const pressed = dialog.dataset.backdropPress === "1";
    delete dialog.dataset.backdropPress;
    // 在弹窗内开始拖选文字、结束于遮罩时不能触发关闭。
    if (pressed && event.target === dialog && outside(event)) requestDialogClose(dialog);
  });
  dialog.addEventListener("cancel", event => { event.preventDefault(); requestDialogClose(dialog); });
}
$("#unsaved-cancel").addEventListener("click", () => requestDialogClose($("#unsaved-dialog")));
$("#unsaved-discard").addEventListener("click", () => {
  if (closeSaveBusy) return;
  const dialog = pendingDialogClose;
  pendingDialogClose = null;
  $("#unsaved-dialog").close();
  if (dialog) finishDialogClose(dialog);
});
$("#unsaved-save").addEventListener("click", savePendingDialog);

/* ── 事件委托 ── */
document.addEventListener("click", (event) => {
  const detailDialog = $("#detail-dialog");
  const detailButton = event.target.closest("[data-detail]");
  if (detailButton) openDetail(detailButton.dataset.detail);

  const item = event.target.closest(".item[data-key]");
  if (item && !event.target.closest("a, button")) openDetail(item.dataset.key);

  const sourceLink = event.target.closest(".source-url");
  if (sourceLink) event.stopPropagation();

  const tabButton = event.target.closest("[data-tab]");
  if (tabButton) switchTab(tabButton.dataset.tab);

  const channelConfigButton = event.target.closest("[data-channel-config='alison']");
  if (channelConfigButton) openAlison();
  if (event.target.id === "alison-close") requestDialogClose($("#alison-dialog"));

  const channelButton = event.target.closest("[data-channel-toggle]");
  if (channelButton) toggleChannel(channelButton.getAttribute("data-channel-toggle"));

  const languageButton = event.target.closest("[data-lang]");
  if (languageButton) showLanguage(languageButton.dataset.lang);

  const serviceButton = event.target.closest("[data-service]");
  if (serviceButton) toggleService(serviceButton.dataset.service);

  const configButton = event.target.closest("[data-config]");
  if (configButton) openConfig(configButton.dataset.config);
  const aiSelect = event.target.closest("[data-ai-select]");
  if (aiSelect && !aiBusy) { captureAIFields(); aiSelected = aiSelect.dataset.aiSelect; renderAIConfig(); }
  const aiUse = event.target.closest("[data-ai-use]");
  if (aiUse && !aiUse.disabled) saveAIConfig(aiUse.dataset.aiUse);
  if (event.target.id === "ai-add") addAIEndpoint();
  if (event.target.id === "ai-copy") addAIEndpoint(true);
  if (event.target.id === "ai-delete" && !aiBusy && !event.target.disabled) {
    aiSettings.endpoints = aiSettings.endpoints.filter(item => item.id !== aiSelected);
    aiSelected = aiSettings.active_id;
    renderAIConfig();
  }
  if (event.target.id === "ai-save") saveAIConfig();
  if (["ai-close", "ai-cancel"].includes(event.target.id)) requestDialogClose($("#ai-dialog"));

  const filterButton = event.target.closest("[data-filter]");
  if (filterButton) {
    itemFilter = filterButton.dataset.filter;
    applyFilter();
  }

  // 配置项说明：点 ⓘ 开 / 再点关 / 点旁边任意位置也关
  const hintButton = event.target.closest(".hint-toggle");
  if (hintButton) {
    if (hintAnchor === hintButton) hideHintTip();
    else showHintTip(hintButton);
  } else {
    hideHintTip();
  }

  if (event.target.id === "detail-close") requestDialogClose(detailDialog);
  if (event.target.id === "process-backlog-open") openProcessBacklog();
  if (event.target.id === "process-backlog-close") $("#process-backlog-dialog").close();
  if (event.target.id === "detail-save") saveDialogWithLock(detailDialog, saveDetail);
  if (event.target.id === "detail-delete" && !dialogIsBusy(detailDialog)) deleteDetail();
  if (event.target.id === "config-close" || event.target.id === "config-cancel") requestDialogClose($("#config-dialog"));
  if (event.target.id === "config-save") saveDialogWithLock($("#config-dialog"), saveConfig);
  if (event.target.id === "load-more") loadMore();
  if (event.target.id === "pushes-refresh") loadPushes();

});

/* 说明浮层：滚动/改窗口大小跟着重新定位，Esc 或点别处关闭 */
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideHintTip();
});
window.addEventListener("scroll", placeHintTip, true);
window.addEventListener("resize", placeHintTip);

/* ── 启动 ── */
loadAll();

const eventStream = new EventSource("/api/stream");
function onStreamState(event) {
  let state = null;
  try {
    state = JSON.parse(event.data);
  } catch (error) {
    return;
  }
  applyState(state);
  // 采集/翻译写库都会让 latest_rev / latest_id 变大 → 立刻拉增量补丁（约 1 秒内上屏）
  if (Number(statValue(state, "latest_rev", 0)) > latestRev
      || Number(statValue(state, "latest_id", 0)) > latestId) {
    pullChanges();
  }
  updateListStatus();
}
eventStream.addEventListener("state", onStreamState);   // 协议是 event: state
eventStream.onmessage = onStreamState;                  // 万一没带 event 名也能收
eventStream.onopen = () => setConnection(true);
eventStream.onerror = () => setConnection(false);

/* SSE 丢了事件也不怕：定时把"最新 ~ 当前显示的这一段"重新核对一遍 */
setInterval(reconcile, RECONCILE_MS);
/* 分发 tab 打开时，定时刷新渠道状态与最近分发记录 */
setInterval(() => {
  if (activeTab !== "distribute") return;
  refreshChannels(true);
  loadPushes(true);
}, PUSHES_MS);

window.addEventListener("scroll", maybeLoadMore, { passive: true });
window.addEventListener("resize", maybeLoadMore);
