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
  DEEPSEEK_KEY: "DeepSeek key，不会回显。",
  PROCESS_INTERVAL_SECONDS: "处理间隔秒数。",
  DISTRIBUTE_CHANNEL: "分发通道。",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
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
  element.innerHTML = `
      <div class="item-main">
        <b>${esc(item.account || "未知账号")} · ${esc(item.content_type || "内容")}</b>
        <div class="item-preview">${esc(item.zh || item.raw || "")}</div>
        <div class="meta item-meta">
          阶段：${esc(item.stage || item._stage || "未知")} ·
          分数：${esc(item.score ?? "—")}
        </div>
        ${sourceUrl(item) ? `来源：<a class="source-url" href="${esc(sourceUrl(item))}" target="_blank" rel="noopener noreferrer">${esc(sourceUrl(item))}</a>` : ""}
      </div>
      ${thumbnail}
    `;
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

/* 首屏/手动刷新：整表重建一次。返回的条目本身已是"新的在前"。 */
function replaceItems(items) {
  const box = $("#items");
  box.innerHTML = "";
  cache.clear();

  const fragment = document.createDocumentFragment();
  for (const item of items) {
    cache.set(keyOf(item), item);
    fragment.append(buildItem(item));
  }
  box.append(fragment);
}

/* 增量：只把新条目插到最前面，已存在的卡片完全不碰（图片因此不会重新请求）。 */
function prependItems(items) {
  const box = $("#items");
  const known = new Set([...box.children].map((element) => element.dataset.key));
  const anchor = box.firstElementChild;

  for (const item of items) {
    const key = keyOf(item);
    cache.set(key, item);
    if (known.has(key)) continue;
    known.add(key);
    const element = buildItem(item);
    if (anchor) box.insertBefore(element, anchor);
    else box.append(element);
  }
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
    if (element.dataset.key === key) element.innerHTML = buildItem(item).innerHTML;
  }
  cache.set(key, item);
}

async function loadAll() {
  try {
    const state = await (await fetch("/api/snapshot")).json();
    renderSummary(state);
    replaceItems(state.items || []);
    latestId = Math.max(latestId, Number(state.latest_id || 0));
    loaded = true;
  } catch (error) {
    $("#updated").textContent = "连接失败";
  }
}

/* 只拉 id 比本地游标更新的条目；没有新内容时一个字节的正文都不传。 */
async function pullNew() {
  if (!loaded) return;
  try {
    const state = await (await fetch(`/api/snapshot?since_id=${latestId}`)).json();
    renderSummary(state);
    const items = state.items || [];
    if (items.length) prependItems(items);
    const newest = items.reduce(
      (max, item) => Math.max(max, Number(item.id) || 0),
      Number(state.latest_id || 0),
    );
    latestId = Math.max(latestId, newest);
  } catch (error) {
    /* 保持现有列表不动，下个周期再试 */
  }
}

function openDetail(key) {
  const item = cache.get(key);
  if (!item) return;

  detail = item;
  $("#detail-title").textContent = `${item.account || "未知账号"} · ${item.content_type || "内容"}`;
  const meta = [
    formatDate(item.date || item.published_date),
    item.stage || item._stage,
    item.score !== undefined && item.score !== null ? `评分 ${item.score}` : "评分 —",
  ].filter(Boolean);
  $("#detail-meta").innerHTML = meta
    .map((value) => `<span>${esc(value)}</span>`)
    .join("");
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
      <label> X Cookies <small>可粘贴 Header String、JSON 或 Netscape cookies.txt</small></label>
      <textarea id="cookie-content" class="cookie-textarea" placeholder="直接粘贴 Cookie 内容">${esc(data.cookie_text || "")}</textarea>
      <small>保存后会写入 data/state/x_cookies.txt，并自动更新 COOKIES_PATH。</small>
      <input id="cookie-file" type="file" accept=".txt,.json,text/plain,application/json">
    </div>` : "";
  $("#config-fields").innerHTML = (data.fields || []).map((field) => `
    <div class="config-field">
      <label>
        ${esc(field.key)}
        <small>${esc(hints[field.key] || "")}</small>
      </label>
      <input data-key="${esc(field.key)}"
        type="${field.secret ? "password" : "text"}"
        value="${field.secret ? "" : esc(field.value || "")}"
        placeholder="${field.secret ? "已配置则留空" : esc(field.value || "")}">
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

loadAll();
const eventStream = new EventSource("/api/stream");
eventStream.onmessage = (event) => {
  const state = JSON.parse(event.data);
  renderSummary(state);
  if (Number(state.latest_id || 0) > latestId) pullNew();
};
