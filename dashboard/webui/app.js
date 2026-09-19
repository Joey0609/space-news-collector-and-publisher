const $ = (selector) => document.querySelector(selector);

let token = localStorage.getItem("hx_token") || "";
let cache = new Map();
let detail = null;
let activeConfig = "";

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

function imageUrl(path) {
  const clean = String(path || "")
    .replace(/^.*[\\/](data[\\/]images[\\/]?)/, "")
    .replace(/^data[\\/]images[\\/]?/, "");
  return "/api/image/" + encodeURIComponent(clean).replace(/%2F/g, "/");
}

function render(snapshot) {
  for (const name of ["collect", "process", "distribute"]) {
    const service = snapshot.services?.[name] || {};
    const state = $(`#${name}-state`);
    const button = $(`[data-service="${name}"]`);

    if (state) {
      state.textContent = (service.state || "已停止") +
        (service.pid ? ` · PID ${service.pid}` : "");
      state.className = service.running ? "running" : "stopped";
    }
    if (button) button.textContent = service.running ? "停止" : "启动";
  }

  $("#count").textContent = snapshot.item_count || 0;
  $("#pending").textContent = snapshot.pending_count || 0;
  $("#env").textContent = snapshot.env_ready ? "已配置" : "未配置";
  $("#updated").textContent = new Date().toLocaleTimeString();

  const next = new Map(
    (snapshot.items || []).map((item) => [
      String(item.id || item.origin_id || item.tweet_id || item.ori_url || item.raw),
      item,
    ]),
  );

  for (const [key, value] of next) cache.set(key, value);

  const box = $("#items");
  const existing = new Map(
    [...box.children].map((element) => [element.dataset.key, element]),
  );

  for (const [key, item] of next) {
    let element = existing.get(key);
    const image = imageOf(item);
    const thumbnail = image
      ? `<img class="thumb" src="${imageUrl(image)}" alt="内容图片">`
      : "";

    const html = `
      <div class="item-main">
        <b>${esc(item.account || "未知账号")} · ${esc(item.content_type || "内容")}</b>
        <div>${esc(item.zh || item.raw || "")}</div>
        <div class="meta">
          阶段：${esc(item.stage || "未知")} ·
          分数：${esc(item.score ?? "—")} ·
          来源：${esc(item.ori_url || "")}
        </div>
      </div>
      ${thumbnail}
      <button class="detail-btn" data-detail="${esc(key)}">详情</button>
    `;

    if (!element) {
      element = document.createElement("article");
      element.className = "item new-item";
      element.dataset.key = key;
      element.innerHTML = html;
      box.prepend(element);
      setTimeout(() => element.classList.remove("new-item"), 500);
    } else if (element.innerHTML !== html) {
      element.innerHTML = html;
    }
  }

  for (const element of box.children) {
    if (!next.has(element.dataset.key)) element.remove();
  }
}

async function snapshot() {
  try {
    const response = await fetch("/api/snapshot");
    render(await response.json());
  } catch (error) {
    $("#updated").textContent = "连接失败";
  }
}

function openDetail(key) {
  const item = cache.get(key);
  if (!item) return;

  detail = item;
  $("#detail-title").textContent = `${item.account || "内容"} · ${item.content_type || ""}`;
  $("#detail-edit").value = item.zh || "";
  showLanguage("zh");

  $("#detail-images").innerHTML = (Array.isArray(item.images) ? item.images : [])
    .map((path) => `<img src="${imageUrl(path)}" alt="内容图片">`)
    .join("") || '<span class="meta">无图片</span>';

  $("#detail-dialog").showModal();
}

function showLanguage(language) {
  if (detail) $("#detail-body").textContent = detail[language] || "暂无内容";
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
  $("#detail-dialog").close();
  await snapshot();
}

async function toggleService(name) {
  const current = await (await fetch("/api/snapshot")).json();
  const response = await fetch("/api/service/toggle", {
    method: "POST",
    headers: await auth(),
    body: JSON.stringify({
      service: name,
      enabled: !current.services?.[name]?.running,
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
  const detailButton = event.target.closest("[data-detail]");
  if (detailButton) openDetail(detailButton.dataset.detail);

  const languageButton = event.target.closest("[data-lang]");
  if (languageButton) showLanguage(languageButton.dataset.lang);

  const serviceButton = event.target.closest("[data-service]");
  if (serviceButton) toggleService(serviceButton.dataset.service);

  const configButton = event.target.closest("[data-config]");
  if (configButton) openConfig(configButton.dataset.config);

  if (event.target.id === "detail-close") $("#detail-dialog").close();
  if (event.target.id === "detail-save") saveDetail();
  if (event.target.id === "config-close" || event.target.id === "config-cancel") $("#config-dialog").close();
  if (event.target.id === "config-save") saveConfig();
});

snapshot();
const eventStream = new EventSource("/api/stream");
eventStream.onmessage = (event) => render(JSON.parse(event.data));
