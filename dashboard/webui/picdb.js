const gallery = document.querySelector("#gallery");
const viewer = document.querySelector("#viewer");
const currentSlide = document.querySelector("#viewer-current-slide");
const nextSlide = document.querySelector("#viewer-next-slide");
const PAGE_SIZE = 48;
let token = localStorage.getItem("hx_token") || "";
let mode = "all";
let query = "";
let assets = [];
let total = 0;
let totalBytes = null;
let activeIndex = -1;
let busy = false;
let currentViewerImage = null;
let nextViewerImage = null;
let imageSwitching = false;
let viewerEpoch = 0;
let slideAnimations = [];
let closeViewerPromise = null;
let pendingGalleryRender = false;
const imageOrigins = new WeakMap();

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function getToken(force = false) {
  if (!token || force) {
    token = prompt("请输入看板 token（启动看板时的终端会打印）") || "";
    if (token) localStorage.setItem("hx_token", token);
  }
  return token;
}

async function api(path, options = {}, retry = true) {
  const headers = { "X-HX-Token": getToken() };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (response.status === 401 && retry) {
    getToken(true);
    return api(path, options, false);
  }
  const result = await response.json();
  if (!result || result.ok !== true) throw new Error(result?.error || `请求失败（${response.status}）`);
  return result.data;
}

function toast(message) {
  const box = document.querySelector("#toast");
  const host = viewer.open ? viewer : document.body;
  if (box.parentElement !== host) host.append(box);
  box.textContent = message;
  box.classList.add("show");
  setTimeout(() => box.classList.remove("show"), 2400);
}

function dateText(value) {
  if (!value) return "入库时间未知";
  return new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12: false });
}

function sizeText(value) {
  const bytes = Number(value || 0);
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

async function copyText(value, message) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
    } else {
      const field = document.createElement("textarea");
      field.value = value;
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    toast(message);
  } catch {
    toast("复制失败，请检查浏览器剪贴板权限");
  }
}

function render() {
  // The modal borrows the actual gallery <img> nodes. Keep their parents alive
  // until closeViewer() returns them, then perform the deferred gallery render.
  if (viewer.open) {
    pendingGalleryRender = true;
    return;
  }
  document.querySelector("#shown-count").textContent = `${assets.length} / ${total}`;
  document.querySelector("#total-size-value").textContent = Number.isFinite(totalBytes)
    ? (totalBytes / (1024 ** 3)).toFixed(2)
    : "—";
  document.querySelector("#empty").hidden = assets.length !== 0;
  const columnCount = window.matchMedia("(max-width: 440px)").matches ? 1
    : window.matchMedia("(max-width: 760px)").matches ? 2
      : window.matchMedia("(max-width: 1050px)").matches ? 3 : 4;
  const columns = Array.from({ length: columnCount }, () => ({ height: 0, cards: [] }));
  const columnGap = parseFloat(getComputedStyle(gallery).columnGap) || 16;
  const columnWidth = Math.max(1, (gallery.clientWidth - (columnCount - 1) * columnGap) / columnCount);
  const existingImages = new Map(Array.from(gallery.querySelectorAll("img[data-asset-id]"))
    .map((image) => [image.dataset.assetId, image]));
  const cards = assets.map((asset, index) => {
    const width = Number(asset.width) > 0 ? Number(asset.width) : 4;
    const height = Number(asset.height) > 0 ? Number(asset.height) : 3;
    return `<article class="image-card">
      <div class="image-frame" data-view="${index}" role="button" tabindex="0" aria-label="打开图片 ${esc(asset.id)}" style="aspect-ratio:${width} / ${height}">
        <img data-asset-id="${esc(asset.id)}" data-src="${esc(asset.url)}" loading="lazy" width="${width}" height="${height}" alt="图片资产 ${esc(asset.id)}">
        <button class="image-id hash-copy" type="button" data-copy-hash="${esc(asset.id)}" title="点击复制完整 hash">${esc(asset.id)}</button>
      </div>
      <div class="card-body">
        <div class="card-line"><strong>${Number(asset.width) || "?"} × ${Number(asset.height) || "?"}</strong><span class="card-date">${esc(dateText(asset.created_at))}</span></div>
        <div class="card-actions"><span class="card-size">${esc(sizeText(asset.byte_size))} · ${Number(asset.ref_count) || 0} 条新闻引用</span>
        <button class="blacklist ${asset.blacklisted ? "restore" : ""}" data-blacklist="${esc(asset.id)}">${asset.blacklisted ? "移出黑名单" : "加入黑名单"}</button></div>
      </div></article>`;
  });
  cards.forEach((markup, index) => {
    const asset = assets[index];
    const column = columns.reduce((shortest, candidate) => candidate.height < shortest.height ? candidate : shortest);
    const aspect = asset.width > 0 && asset.height > 0 ? asset.height / asset.width : 0.75;
    column.cards.push(markup);
    column.height += Math.min(2.2, Math.max(0.35, aspect)) * columnWidth + 118;
  });
  gallery.innerHTML = columns.map((column) => `<div class="gallery-column">${column.cards.join("")}</div>`).join("");
  gallery.querySelectorAll("img[data-asset-id]").forEach((freshImage) => {
    const existing = existingImages.get(freshImage.dataset.assetId);
    if (existing && existing.getAttribute("src") === freshImage.dataset.src) {
      freshImage.replaceWith(existing);
    } else {
      // Don't put src in the HTML string: the browser may start fetching it
      // before the just-rendered node is replaced by its existing counterpart.
      freshImage.src = freshImage.dataset.src;
      freshImage.removeAttribute("data-src");
    }
  });
  document.querySelector("#load-sentinel").textContent = assets.length < total ? "继续下滑自动加载…" : "";
  gallery.querySelectorAll("[data-view]").forEach((element) => {
    element.addEventListener("click", () => openViewer(Number(element.dataset.view)));
    element.addEventListener("keydown", (event) => {
      if (event.target.closest("[data-copy-hash]")) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openViewer(Number(element.dataset.view)); }
    });
  });
  gallery.querySelectorAll("[data-blacklist]").forEach((button) => {
    button.addEventListener("click", () => setBlacklist(button.dataset.blacklist, !button.classList.contains("restore")));
  });
  gallery.querySelectorAll("[data-copy-hash]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      copyText(button.dataset.copyHash, "已复制完整图片 hash");
    });
  });
}

async function load(reset = true) {
  if (busy) return;
  busy = true;
  let loaded = false;
  const status = document.querySelector("#status");
  status.textContent = "正在读取图片库…";
  if (!reset) document.querySelector("#load-sentinel").textContent = "正在加载更多…";
  if (reset) assets = [];
  try {
    const params = new URLSearchParams({ offset: String(assets.length), limit: String(PAGE_SIZE), mode, q: query });
    const data = await api(`/api/picdb/images?${params}`);
    total = data.total;
    totalBytes = data.total_bytes == null ? null : Number(data.total_bytes);
    assets = reset ? data.assets : assets.concat(data.assets);
    status.textContent = mode === "all" ? "显示全部有效图片，包含黑名单资产；新入库图片排在最前。" : "这些图片及其后续匹配图不会分发。";
    render();
    loaded = true;
  } catch (error) {
    status.textContent = error.message;
    toast(error.message);
  } finally {
    busy = false;
    if (loaded && assets.length < total) {
      const sentinel = document.querySelector("#load-sentinel");
      loadObserver.unobserve(sentinel);
      loadObserver.observe(sentinel);
    }
  }
}

async function setBlacklist(assetId, blacklisted) {
  try {
    await api("/api/picdb/blacklist", { method: "POST", body: JSON.stringify({ asset_id: assetId, blacklisted }) });
    toast(blacklisted ? "已拉黑；后续匹配图片将不再入库或分发" : "已恢复；后续匹配图片可正常入库和分发");
    if (viewer.open) await closeViewer();
    await load(true);
  } catch (error) {
    toast(error.message);
  }
}

function openViewer(index) {
  activeIndex = index;
  currentViewerImage = takeGalleryImage(assets[index], currentSlide);
  if (!currentViewerImage) {
    toast("找不到列表中的图片节点，请刷新图片库后重试");
    return;
  }
  updateViewer();
  viewer.showModal();
  requestAnimationFrame(() => viewer.classList.add("visible"));
}

function closeViewer() {
  if (!viewer.open) return Promise.resolve();
  if (viewer.classList.contains("closing")) return closeViewerPromise || Promise.resolve();
  viewerEpoch += 1;
  slideAnimations.forEach((animation) => animation.cancel());
  slideAnimations = [];
  viewer.classList.add("closing");
  viewer.classList.remove("visible");
  closeViewerPromise = new Promise((resolve) => {
    setTimeout(() => {
      if (viewer.open) viewer.close();
      restoreViewerImages();
      viewer.classList.remove("closing");
      const box = document.querySelector("#toast");
      if (box.parentElement !== document.body) document.body.append(box);
      if (pendingGalleryRender) {
        pendingGalleryRender = false;
        render();
      }
      closeViewerPromise = null;
      resolve();
    }, 190);
  });
  return closeViewerPromise;
}

function takeGalleryImage(asset, slide) {
  const image = Array.from(gallery.querySelectorAll("img[data-asset-id]"))
    .find((candidate) => candidate.dataset.assetId === asset?.id);
  if (!image) return null;
  imageOrigins.set(image, {
    parent: image.parentElement,
    nextSibling: image.nextSibling,
    loading: image.loading,
  });
  image.loading = "eager";
  image.classList.add("viewer-slide-image");
  slide.replaceChildren(image);
  return image;
}

function restoreGalleryImage(image) {
  if (!image) return;
  const origin = imageOrigins.get(image);
  if (!origin) return;
  image.classList.remove("viewer-slide-image");
  image.loading = origin.loading;
  if (origin.parent.isConnected) {
    const before = origin.nextSibling?.parentNode === origin.parent ? origin.nextSibling : null;
    origin.parent.insertBefore(image, before);
  }
  imageOrigins.delete(image);
}

function restoreViewerImages() {
  restoreGalleryImage(currentViewerImage);
  restoreGalleryImage(nextViewerImage);
  currentViewerImage = null;
  nextViewerImage = null;
  currentSlide.replaceChildren();
  nextSlide.replaceChildren();
  currentSlide.style.transform = "translateX(0)";
  nextSlide.style.transform = "translateX(100%)";
  nextSlide.style.visibility = "hidden";
}

function updateViewer() {
  const asset = assets[activeIndex];
  if (!asset) return;
  document.querySelector("#viewer-title").textContent = `图片资产 ${asset.id}`;
  document.querySelector("#viewer-date").textContent = `入库时间：${dateText(asset.created_at)}`;
  document.querySelector("#viewer-dimensions").textContent = `分辨率：${asset.width || "未知"} × ${asset.height || "未知"}`;
  document.querySelector("#viewer-size").textContent = `文件大小：${sizeText(asset.byte_size)}`;
  document.querySelector("#viewer-refs").textContent = `新闻引用：${asset.ref_count} 条 · 版本 v${asset.version}`;
  const path = document.querySelector("#viewer-path");
  const filePath = asset.path || `data/images/library/${asset.id}.img`;
  path.textContent = filePath;
  path.title = filePath;
  path.onclick = () => copyText(filePath, "已复制图片文件路径");
  const news = document.querySelector("#viewer-news");
  const titles = Array.isArray(asset.news_titles) ? asset.news_titles : [];
  news.innerHTML = titles.length
    ? titles.map((title) => `<li>${esc(title)}</li>`).join("")
    : '<li class="news-empty">暂无新闻标题</li>';
  const button = document.querySelector("#viewer-blacklist");
  button.hidden = false;
  button.textContent = asset.blacklisted ? "移出黑名单" : "加入黑名单";
  button.classList.toggle("restore", Boolean(asset.blacklisted));
  button.onclick = () => setBlacklist(asset.id, !asset.blacklisted);
}

async function stepViewer(delta) {
  if (!assets.length || imageSwitching || !currentViewerImage) return;
  clearTimeout(wheelTimer);
  wheelTimer = null;
  wheelDelta = 0;
  imageSwitching = true;
  const epoch = viewerEpoch;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const nextIndex = (activeIndex + delta + assets.length) % assets.length;
  const incomingAsset = assets[nextIndex];
  const outgoingX = delta > 0 ? "-100%" : "100%";
  const incomingX = delta > 0 ? "100%" : "-100%";
  try {
    nextViewerImage = takeGalleryImage(incomingAsset, nextSlide);
    if (!nextViewerImage) return;
    if (!nextViewerImage.complete || !nextViewerImage.naturalWidth) await nextViewerImage.decode();
    if (!viewer.open || epoch !== viewerEpoch) {
      restoreGalleryImage(nextViewerImage);
      nextViewerImage = null;
      nextSlide.replaceChildren();
      return;
    }
    if (reducedMotion) {
      restoreGalleryImage(currentViewerImage);
      currentViewerImage = nextViewerImage;
      nextViewerImage = null;
      currentSlide.replaceChildren(currentViewerImage);
      nextSlide.replaceChildren();
      activeIndex = nextIndex;
      updateViewer();
    } else {
      nextSlide.style.visibility = "visible";
      nextSlide.style.transform = `translateX(${incomingX})`;
      currentSlide.style.transform = "translateX(0)";
      const slideOut = currentSlide.animate([
        { transform: "translateX(0)" },
        { transform: `translateX(${outgoingX})` },
      ], { duration: 260, easing: "cubic-bezier(.22,.7,.25,1)", fill: "forwards" });
      const slideIn = nextSlide.animate([
        { transform: `translateX(${incomingX})` },
        { transform: "translateX(0)" },
      ], { duration: 260, easing: "cubic-bezier(.22,.7,.25,1)", fill: "forwards" });
      slideAnimations = [slideOut, slideIn];
      await Promise.all([slideOut.finished, slideIn.finished]);
      if (!viewer.open || epoch !== viewerEpoch) return;
      restoreGalleryImage(currentViewerImage);
      currentViewerImage = nextViewerImage;
      nextViewerImage = null;
      currentSlide.replaceChildren(currentViewerImage);
      nextSlide.replaceChildren();
      slideOut.cancel();
      slideIn.cancel();
      slideAnimations = [];
      currentSlide.style.transform = "translateX(0)";
      nextSlide.style.transform = `translateX(${incomingX})`;
      nextSlide.style.visibility = "hidden";
      activeIndex = nextIndex;
      updateViewer();
    }
  } catch {
    // 下一张图片解码失败或弹窗被关闭时，保留当前图片并归还列表节点。
    if (nextViewerImage) restoreGalleryImage(nextViewerImage);
    nextViewerImage = null;
    nextSlide.replaceChildren();
    nextSlide.style.visibility = "hidden";
    nextSlide.style.transform = "translateX(100%)";
  } finally {
    imageSwitching = false;
  }
}

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  mode = button.dataset.mode;
  document.querySelectorAll("[data-mode]").forEach((tab) => tab.classList.toggle("active", tab === button));
  load(true);
}));
document.querySelector("#search").addEventListener("input", (event) => {
  query = event.target.value.trim();
  load(true);
});
const loadObserver = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting) && !busy && assets.length < total) load(false);
}, { rootMargin: "700px 0px" });
loadObserver.observe(document.querySelector("#load-sentinel"));
document.querySelector("#viewer-close").addEventListener("click", closeViewer);
viewer.addEventListener("click", (event) => {
  if (event.target === viewer) closeViewer();
});
viewer.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeViewer();
});
document.querySelector("#previous").addEventListener("click", (event) => {
  event.stopPropagation();
  stepViewer(-1);
});
document.querySelector("#next").addEventListener("click", (event) => {
  event.stopPropagation();
  stepViewer(1);
});
const viewerImagePanel = document.querySelector(".viewer-image");
let wheelDelta = 0;
let wheelTimer = null;
viewerImagePanel.addEventListener("wheel", (event) => {
  if (!viewer.open) return;
  event.preventDefault();
  wheelDelta += event.deltaY;
  clearTimeout(wheelTimer);
  wheelTimer = setTimeout(() => {
    const amount = wheelDelta;
    const delta = Math.sign(amount);
    wheelDelta = 0;
    wheelTimer = null;
    if (Math.abs(amount) >= 24 && delta) stepViewer(delta);
  }, 100);
}, { passive: false });
document.addEventListener("keydown", (event) => {
  if (!viewer.open) return;
  if (event.key === "ArrowLeft") stepViewer(-1);
  if (event.key === "ArrowRight") stepViewer(1);
});

load();
let lastColumnCount = 0;
window.addEventListener("resize", () => {
  const columnCount = window.matchMedia("(max-width: 440px)").matches ? 1
    : window.matchMedia("(max-width: 760px)").matches ? 2
      : window.matchMedia("(max-width: 1050px)").matches ? 3 : 4;
  if (columnCount !== lastColumnCount) {
    lastColumnCount = columnCount;
    if (viewer.open) pendingGalleryRender = true;
    else render();
  }
});
