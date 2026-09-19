const $ = (selector) => document.querySelector(selector);

let token = localStorage.getItem("hx_token") || "";
let cache = new Map();
let detail = null;
let activeConfig = "";
let latestId = 0;
let loaded = false;
let lastState = {};

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
  DISTRIBUTE_CHANNEL: "分发通道。",
};

const protocolLabels = {
  openai: "openai compatible（/chat/completions）",
  responses: "responses（OpenAI Responses API）",
  anthropic: "anthropic compatible（/messages）",
};

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
   哪怕 z-index 再大也会被它盖住（这就是刚才提示看不见的原因）。挂在 dialog 内、用 fixed 定位，
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
  if (state === "green") return "三绿";
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

async function auth() {
  if (!token) {
    token = prompt("请输入看板 token") || "";
    localStorage.setItem("hx_token", token);
  }
  return {
    "Content-Type": "application/json",
    "X-HX-Token": token,
  };
}

function imageOf(item) {
  if (Array.isArray(item.images)) return item.images[0] || "";
  return item.image || "";
}

function imageList(item) {
  return Array.isArray(item.images) ? item.images.filter(Boolean) : (item.image ? [item.image] : []);
}

function sourceUrl(item) {
  return item.ori_url || item.url || "";
}

function formatDate(value) {
  const text = String(value || "");
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : text;
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

function renderSummary(state) {
  lastState = state;

  for (const name of ["collect", "process", "distribute"]) {
    const service = state.services?.[name] || {};
    const status = $(`#${name}-state`);
    const button = $(`[data-service="${name}"]`);

    if (status) {
      status.textContent = (service.state || "已停止") +
        (service.pid ? ` · PID ${service.pid}` : "");
      status.className = service.running ? "running" : "stopped";
    }
    if (button) button.textContent = service.running ? "停止" : "启动";
  }

  $("#count").textContent = state.item_count || 0;
  $("#pending").textContent = state.pending_count || 0;
  $("#env").textContent = state.env_ready ? "已配置" : "未配置";
  $("#updated").textContent = new Date().toLocaleTimeString();
}

/* ── 列表：默认 20 条，滚到还剩 10 条时再加载 20 条 ── */
const PAGE_SIZE = 20;        // 首屏与每次加载的条数
const LOAD_AHEAD = 10;       // 视口下方还剩这么多条目就开始加载下一页
const RECONCILE_MS = 15000;  // 定时全量核对（防止 SSE 丢事件导致漏内容）
const MAX_WINDOW = 300;      // 定时核对一次最多比对多少条，避免大库拖慢前端

let shownCount = 0;          // 当前列表里"应该显示"的条数
let hasMore = false;
let latestRev = 0;
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
  const total = Number(lastState.item_count || 0);
  status.textContent = `已显示 ${shownCount} / 共 ${total} 条`;
  const more = $("#load-more");
  if (more) more.hidden = !hasMore;
}

async function loadAll() {
  try {
    const state = await (await fetch(`/api/snapshot?offset=0&limit=${PAGE_SIZE}`)).json();
    renderSummary(state);
    replaceItems(state.items || []);
    latestId = Math.max(latestId, Number(state.latest_id || 0));
    latestRev = Math.max(latestRev, Number(state.latest_rev || 0));
    shownCount = (state.items || []).length;
    hasMore = Boolean(state.has_more);
    loaded = true;
    updateListStatus();
  } catch (error) {
    $("#updated").textContent = "连接失败";
  }
}

/* 滚动到底部附近（还剩 LOAD_AHEAD 条）就再要一页。 */
async function loadMore() {
  if (!loaded || !hasMore || loadingMore) return;
  loadingMore = true;
  const more = $("#load-more");
  if (more) more.textContent = "加载中…";
  try {
    const state = await (await fetch(`/api/items?offset=${shownCount}&limit=${PAGE_SIZE}`)).json();
    const items = state.items || [];
    appendItems(items);
    shownCount += items.length;
    hasMore = items.length >= PAGE_SIZE;
    applyFilter();
  } catch (error) {
    /* 下次滚动再试 */
  } finally {
    loadingMore = false;
    if (more) more.textContent = "加载更多";
    updateListStatus();
  }
}

/* 视口下方还剩几条？剩 ≤ LOAD_AHEAD 就补一页。 */
function maybeLoadMore() {
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
    const state = await (await fetch(
      `/api/snapshot?since_id=${latestId}&since_rev=${latestRev}`)).json();
    renderSummary(state);
    const items = state.items || [];
    const touched = items.length ? upsertItems(items) : 0;
    latestId = Math.max(latestId, Number(state.latest_id || 0));
    latestRev = Math.max(latestRev, Number(state.latest_rev || 0));
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
    const state = await (await fetch(`/api/snapshot?offset=0&limit=${limit}`)).json();
    renderSummary(state);
    const items = state.items || [];
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
    latestId = Math.max(latestId, Number(state.latest_id || 0));
    latestRev = Math.max(latestRev, Number(state.latest_rev || 0));
    hasMore = shownCount < Number(state.item_count || 0);
    updateListStatus();
  } catch (error) {
    /* 下一轮再试 */
  }
}

function openDetail(key) {
  const item = cache.get(key);
  if (!item) return;

  detail = item;
  const title = String(item.ai_title || "").trim();
  $("#detail-title").textContent = title
    ? `${title} · ${item.account || "未知账号"}`
    : `${item.account || "未知账号"} · ${item.content_type || "内容"}`;
  const meta = [
    formatDate(item.date || item.published_date),
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

  const response = await fetch("/api/item/update", {
    method: "POST",
    headers: await auth(),
    body: JSON.stringify({ id: detail.id, zh: $("#detail-edit").value }),
  });

  if (!response.ok) return alert("保存失败");
  detail.zh = $("#detail-edit").value;
  refreshItem(detail);
  $("#detail-dialog").close();
}

async function deleteDetail() {
  if (!detail?.id) return alert("这条内容没有有效数据库 ID");
  if (!confirm(`确定删除「${detail.account || "这条推文"}」吗？删除后不可恢复。`)) return;

  const response = await fetch("/api/item/delete", {
    method: "POST",
    headers: await auth(),
    body: JSON.stringify({ id: detail.id }),
  });
  if (!response.ok) return alert("删除失败，请检查看板权限或服务日志");
  const key = keyOf(detail);
  detail = null;
  removeItem(key);
  $("#detail-dialog").close();
}

async function toggleService(name) {
  const response = await fetch("/api/service/toggle", {
    method: "POST",
    headers: await auth(),
    body: JSON.stringify({
      service: name,
      enabled: !lastState.services?.[name]?.running,
    }),
  });
  if (!response.ok) alert("操作失败");
}

async function openConfig(name) {
  activeConfig = name;
  const response = await fetch(`/api/service/config/${name}`, { headers: await auth() });
  if (!response.ok) return alert("配置读取失败");

  const data = await response.json();
  $("#config-title").textContent = `${name === "collect" ? "采集" : name === "process" ? "处理" : "分发"}服务配置`;
  const cookieField = name === "collect" ? `
    <div class="config-field cookie-field">
      ${fieldLabel("X Cookies", "可粘贴 Header String、JSON 或 Netscape cookies.txt；保存后会写入 data/state/x_cookies.txt，并自动更新 COOKIES_PATH。")}
      <textarea id="cookie-content" class="cookie-textarea" placeholder="直接粘贴 Cookie 内容">${esc(data.cookie_text || "")}</textarea>
      <input id="cookie-file" type="file" accept=".txt,.json,text/plain,application/json">
    </div>` : "";
  $("#config-fields").innerHTML = (data.fields || []).map((field) => `
    <div class="config-field">
      ${fieldLabel(field.key, hints[field.key] || "")}
      ${fieldControl(field)}
    </div>
  `).join("") + cookieField;

  if (name === "collect") {
    $("#cookie-file").onchange = (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => { $("#cookie-content").value = reader.result; };
      reader.readAsText(file, "utf-8");
    };
  }

  $("#config-dialog").showModal();
}

async function saveConfig() {
  const values = {};
  document.querySelectorAll("#config-fields [data-key]").forEach((field) => {
    if (field.value.trim()) values[field.dataset.key] = field.value.trim();
  });

  const response = await fetch("/api/service/config", {
    method: "POST",
    headers: await auth(),
    body: JSON.stringify({ service: activeConfig, values }),
  });

  if (!response.ok) return alert("配置保存失败");
  if (activeConfig === "collect") {
    const content = $("#cookie-content")?.value.trim();
    if (content) {
      const cookieResponse = await fetch("/api/cookies/import", {
        method: "POST",
        headers: await auth(),
        body: JSON.stringify({ content }),
      });
      if (!cookieResponse.ok) return alert("Cookie 导入失败，请检查格式");
    }
  }
  $("#config-dialog").close();
}

document.addEventListener("click", (event) => {
  const detailDialog = $("#detail-dialog");
  const detailButton = event.target.closest("[data-detail]");
  if (detailButton) openDetail(detailButton.dataset.detail);

  const item = event.target.closest(".item[data-key]");
  if (item && !event.target.closest("a, button")) openDetail(item.dataset.key);

  const sourceLink = event.target.closest(".source-url");
  if (sourceLink) event.stopPropagation();

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

loadAll();
const eventStream = new EventSource("/api/stream");
eventStream.onmessage = (event) => {
  const state = JSON.parse(event.data);
  renderSummary(state);
  // 采集/翻译写库都会让 latest_rev / latest_id 变大 → 立刻拉增量补丁（约 1 秒内上屏）
  if (Number(state.latest_rev || 0) > latestRev || Number(state.latest_id || 0) > latestId) {
    pullChanges();
  }
  updateListStatus();
};
/* SSE 丢了事件也不怕：定时把"最新 ~ 当前显示的这一段"重新核对一遍 */
setInterval(reconcile, RECONCILE_MS);
window.addEventListener("scroll", maybeLoadMore, { passive: true });
window.addEventListener("resize", maybeLoadMore);
document.addEventListener("click", (event) => {
  if (event.target.id === "load-more") loadMore();
});
