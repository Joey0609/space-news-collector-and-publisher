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
let channels = [];
let configurable = ["collect", "process"];

const hints = {
  PROXY_URL: "X 访问代理地址，可留空。",
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
  AI_REPEAT_CONTEXT: "判「不重复」那盏灯时，给模型对比的最近已入库标题条数；0 = 不给清单（只看文本里的旧闻线索）。",
  TRANSLATE_MAX_ATTEMPTS: "永久失败的单条最多重试次数（每轮自动补翻未翻译/失败的条目）。",
  TRANSLATE_INLINE_RETRIES: "遇到限流/超时时，同一轮内当场重试几次。",
  TRANSLATE_RATE_LIMIT_WAIT: "限流/超时后等待多少秒再重试。",
  PROCESS_INTERVAL_SECONDS: "处理间隔秒数。",
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
  return `
      <div class="field-head">
        <span class="field-name">${esc(key)}</span>
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

/* is_news：1=新闻 0=非新闻 空=模型没判出来 */
function newsLabel(item) {
  if (item.is_news === 0 || item.is_news === "0") return "非新闻";
  if (item.is_news === 1 || item.is_news === "1") return "新闻";
  return "";
}

/* importance：高/中/低 */
function importanceLabel(item) {
  const value = String(item.importance || "").trim();
  return ["高", "中", "低"].includes(value) ? `重要度 ${value}` : "";
}

/* 四盏灯：是新闻 / 不重复 / 非评价 / 非敏感政治。1=绿 0=红 空=灰（还没点灯） */
const LIGHTS = [
  { key: "is_news", label: "是新闻", on: "是航天新闻/公告/任务进展", off: "闲聊、日常、广告、抽奖、无实质信息" },
  { key: "not_repeat", label: "不重复", on: "不是重复内容", off: "旧闻回顾，或与已入库清单里某条是同一件事" },
  { key: "not_comment", label: "非评价", on: "它自己就是内容（原发/官方/独立消息）", off: "对另一条新闻的吐槽、跟评、转发加感想" },
  { key: "not_sensitive", label: "非敏感政治", on: "不涉及敏感政治与外交", off: "涉政外交、战争军演、政客表态等红线" },
];

function lightValue(item, key) {
  const raw = item[key];
  if (raw === true || raw === 1 || raw === "1") return true;
  if (raw === false || raw === 0 || raw === "0") return false;
  return null;
}

function lightStateOf(item) {
  const values = LIGHTS.map((light) => lightValue(item, light.key));
  if (values.some((value) => value === false)) return "red";
  if (values.every((value) => value === true)) return "green";
  return "unknown";
}

/* 悬停立刻显示的提示文案（原生 title 有一秒延迟，所以自己画 tooltip） */
function lightTip(light, value) {
  if (value === true) return `${light.label}：绿 ✓ ${light.on}`;
  if (value === false) return `${light.label}：红 ✕ ${light.off}`;
  return `${light.label}：灰 — 还没点灯（判定时按老规则走）`;
}

function lightsHtml(item) {
  return LIGHTS.map((light) => {
    const value = lightValue(item, light.key);
    const cls = value === true ? "on" : value === false ? "off" : "unknown";
    const tip = lightTip(light, value);
    return `<i class="light light-${cls}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"></i>`;
  }).join("");
}

function lightText(item) {
  const values = LIGHTS.map((light) => lightValue(item, light.key));
  if (values.every((value) => value === null)) return "";
  const state = lightStateOf(item);
  if (state === "green") return "全绿";
  if (state === "red") {
    const reds = LIGHTS.filter((light) => lightValue(item, light.key) === false)
      .map((light) => light.label);
    return `红灯：${reds.join("、")}`;
  }
  return "未点齐";
}

/* 台账筛选 */
const FILTERS = {
  all: () => true,
  green: (item) => lightStateOf(item) === "green",
  red: (item) => lightStateOf(item) === "red",
  unknown: (item) => lightStateOf(item) === "unknown",
  send: (item) => item.decision === "发",
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

/* 分数：优先显示 AI 价值分（0~100），没有就退回规则加权分 */
function scoreOf(item) {
  const ai = item.ai_score;
  if (ai !== null && ai !== undefined && ai !== "" && !Number.isNaN(Number(ai))) {
    return { value: Number(ai), label: "AI 分" };
  }
  const fallback = (item.rules_score !== null && item.rules_score !== undefined
      && item.rules_score !== "") ? item.rules_score : item.score;
  if (fallback === null || fallback === undefined || fallback === ""
      || Number.isNaN(Number(fallback))) {
    return null;
  }
  return { value: Number(fallback), label: "规则分" };
}

/* 一条内容的判定/分数/重要度/标签小圆片 */
function chipRow(item) {
  const parts = [];
  const news = newsLabel(item);
  if (news) parts.push(chip(news, news === "非新闻" ? "chip-warn" : "chip-news"));
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
  const summary = String(item.ai_summary || "").trim();
  const chipHtml = chipRow(item);
  const lightHtml = lightsHtml(item);
  element.innerHTML = `
      <div class="item-main">
        <b><span class="lights">${lightHtml}</span> ${esc(item.account || "未知账号")} · ${esc(item.content_type || "内容")}</b>
        ${title ? `<div class="item-title">${esc(title)}</div>` : ""}
        <div class="item-preview">${esc(summary || item.zh || item.raw || "")}</div>
        ${chipHtml ? `<div class="chips">${chipHtml}</div>` : ""}
        ${sourceUrl(item) ? `来源：<a class="source-url" href="${esc(sourceUrl(item))}" target="_blank" rel="noopener noreferrer">${esc(sourceUrl(item))}</a>` : ""}
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

  if (data.services) renderServices(data.services);
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

function renderConfigButtons() {
  const list = Array.isArray(configurable) ? configurable : [];
  for (const button of $$("[data-config]")) {
    const name = button.dataset.config;
    const can = list.includes(name);
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
    body.innerHTML = `<tr><td colspan="4" class="pushes-empty">${esc(errorText)}</td></tr>`;
    return;
  }
  if (!pushes.length) {
    body.innerHTML = `<tr><td colspan="4" class="pushes-empty">还没有分发记录。</td></tr>`;
    return;
  }
  body.innerHTML = pushes.map((push) => {
    const target = push.label || push.kind || push.source || "—";
    return `<tr>
      <td class="mono">${esc(formatTime(push.sent_at))}</td>
      <td>${esc(push.channel || "—")}</td>
      <td>${esc(target)}</td>
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

/* 实时补丁：新条目插到最前，被更新过的（译文/四盏灯/判定刚写好）就地刷新。 */
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
  status.textContent = `已显示 ${shownCount} / 共 ${itemTotal} 条`;
  const more = $("#load-more");
  if (more) more.hidden = !hasMore;
}

async function loadAll() {
  try {
    const data = await apiGet(`/api/snapshot?offset=0&limit=${PAGE_SIZE}`);
    applyState(data);
    replaceItems(data.items || []);
    latestId = Math.max(latestId, Number(statValue(data, "latest_id", 0)));
    latestRev = Math.max(latestRev, Number(statValue(data, "latest_rev", 0)));
    shownCount = (data.items || []).length;
    hasMore = data.has_more !== undefined ? Boolean(data.has_more) : shownCount < itemTotal;
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
  const more = $("#load-more");
  if (more) more.textContent = "加载中…";
  try {
    const data = await apiGet(`/api/items?offset=${shownCount}&limit=${PAGE_SIZE}`);
    const items = data.items || [];
    appendItems(items);
    shownCount += items.length;
    const total = data.total !== undefined && data.total !== null
      ? Number(data.total)
      : itemTotal;
    hasMore = items.length >= PAGE_SIZE && shownCount < total;
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
  try {
    const data = await apiGet(`/api/snapshot?since_id=${latestId}&since_rev=${latestRev}`);
    applyState(data);
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
    const data = await apiGet(`/api/snapshot?offset=0&limit=${limit}`);
    applyState(data);
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
    hasMore = shownCount < itemTotal;
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
    item.rules_score !== null && item.rules_score !== undefined ? `规则分 ${item.rules_score}` : "",
    item.decision,
    item.reason,
    lightText(item),
    newsLabel(item),
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

  // 引用（quote）：被引用那条的正文/译文 + 链接。有引用的条目第 3 盏灯会被强制放行，
  // 所以这里也标一句，能看到"为什么这条没被判成评价"。
  const quotedBox = $("#detail-quoted");
  const quotedRaw = String(item.quoted_raw || "").trim();
  const quotedZh = String(item.quoted_zh || "").trim();
  if (quotedRaw || quotedZh) {
    const who = String(item.quoted_account || "").trim();
    const url = String(item.quoted_url || "").trim();
    const head = `引用了 @${who || "?"}（第 3 盏灯自动放行）`;
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
      ${fieldLabel(field.key, hints[field.key] || field.hint || "")}
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
  activeConfig = name;
  $("#config-title").textContent = `${SERVICE_LABELS[name] || name}服务配置`;
  try {
    const data = await apiGet(`/api/config/${encodeURIComponent(name)}`);
    renderConfigFields(data || {});
  } catch (error) {
    return reportError(error, "配置读取失败");
  }
  $("#config-dialog").showModal();
}

async function saveConfig() {
  if (!activeConfig) return;
  const values = {};
  document.querySelectorAll("#config-fields [data-key]").forEach((field) => {
    if (field.value.trim()) values[field.dataset.key] = field.value.trim();
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

/* ── 艾莉森 QQ Bot：独立配置弹窗；刷新时绝不重绘正在编辑的表单 ── */
const ALISON_KEYS = ["APPID", "SECRET", "SANDBOX", "ALISON_WITH_IMAGE", "ALISON_DISCOVER_GROUPS"];
const ALISON_NOTE = "群消息会自动登记群。未 @ 的普通消息需在每个 QQ 群的机器人设置开启「获取群内全部消息」并确认具备全量事件权限；未开启时仍可由入群或 @ 消息发现。";
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
  return `<article class="alison-group-card">
    <div class="alison-group-top"><div class="alison-group-identity"><h4>${esc(group.group_name || "未命名群")}</h4><code>${esc(id)}</code></div>
      <label class="alison-group-toggle"><span>推送${enabled ? "已开启" : "已关闭"}</span><input type="checkbox" role="switch" data-alison-group="${esc(id)}" ${enabled ? "checked" : ""} ${alisonGroupBusy.has(id) ? "disabled" : ""} aria-label="${esc(group.group_name || id)} 推送" /></label></div>
    <div class="alison-group-info">${group.group_finger_memo ? `<p>${esc(group.group_finger_memo)}</p>` : ""}
      <div class="alison-group-tags">${group.group_class_text ? `<span>${esc(group.group_class_text)}</span>` : ""}${tags.map(tag => `<span>${esc(tag)}</span>`).join("")}</div>
      <div class="alison-group-meta"><span>人数 ${group.group_member_num === null || group.group_member_num === undefined || group.group_member_num === "" ? "—" : esc(group.group_member_num)}</span><span>成员状态：${memberLabel}</span><span>加入时间：${esc(formatTime(group.joined_at))}</span><span>上次检查：${esc(formatTime(group.last_checked))}</span></div>
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
    renderAlisonGroups(data?.groups);
    $("#alison-discovery-note").textContent = data?.discovery_note || ALISON_NOTE;
    $("#alison-content").hidden = false;
    alisonStatus("#alison-load-state", "");
    renderAlisonRefresh(data?.refresh);
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
$("#alison-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeAlison();
});
$("#alison-dialog").addEventListener("close", () => {
  $("#alison-dialog").classList.remove("is-closing");
  ++alisonSession;
  stopAlisonPoll();
});
$("#alison-dialog").addEventListener("mousedown", event => {
  if (event.target === event.currentTarget) event.currentTarget.dataset.backdropPress = "1";
});
$("#alison-dialog").addEventListener("click", event => {
  const dialog = event.currentTarget;
  if (event.target === dialog && dialog.dataset.backdropPress === "1") closeAlison();
  delete dialog.dataset.backdropPress;
});

$("#alison-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (alisonBusy || alisonLoading) return;
  const form = event.currentTarget;
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
    if (session === alisonSession) alisonStatus("#alison-save-state", "配置已保存");
  } catch (_) {
    if (session === alisonSession) alisonStatus("#alison-save-state", "保存失败，请重试。", true);
  } finally {
    alisonBusy = false;
    $("#alison-save").disabled = false;
  }
});

$("#alison-add-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#alison-group-openid");
  const group_openid = input.value.trim();
  if (!group_openid || $("#alison-add").disabled) return;
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
    alisonStatus("#alison-add-state", "群已添加");
  } catch (_) {
    if (session === alisonSession) alisonStatus("#alison-add-state", "添加失败，请检查 OpenID 后重试。", true);
  } finally { $("#alison-add").disabled = false; }
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
  if (event.target.id === "alison-close") closeAlison();

  const channelButton = event.target.closest("[data-channel-toggle]");
  if (channelButton) toggleChannel(channelButton.getAttribute("data-channel-toggle"));

  const languageButton = event.target.closest("[data-lang]");
  if (languageButton) showLanguage(languageButton.dataset.lang);

  const serviceButton = event.target.closest("[data-service]");
  if (serviceButton) toggleService(serviceButton.dataset.service);

  const configButton = event.target.closest("[data-config]");
  if (configButton) openConfig(configButton.dataset.config);

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

  if (event.target.id === "detail-close") detailDialog.close();
  if (event.target.id === "detail-save") saveDetail();
  if (event.target.id === "detail-delete") deleteDetail();
  if (event.target.id === "config-close" || event.target.id === "config-cancel") $("#config-dialog").close();
  if (event.target.id === "config-save") saveConfig();
  if (event.target.id === "load-more") loadMore();
  if (event.target.id === "pushes-refresh") loadPushes();

  // Click on the backdrop (the <dialog> element itself is the outside area)
  // closes the read-only detail view. Tracked from mouseup so that a text
  // selection drag started inside the panel cannot close it by accident.
  if (event.target === detailDialog && detailDialog.dataset.backdropPress === "1") {
    detailDialog.close();
  }
  delete detailDialog.dataset.backdropPress;
});

document.addEventListener("mousedown", (event) => {
  const detailDialog = $("#detail-dialog");
  if (event.target === detailDialog) detailDialog.dataset.backdropPress = "1";
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
