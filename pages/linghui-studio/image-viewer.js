// Originals are requested only after an explicit click, through the authenticated
// AstrBot bridge. Keep them out of the paginated thumbnail caches.
export function createImageViewer({ loadImage, downloadImage }) {
  const dialog = document.getElementById("image-viewer");
  const viewport = document.getElementById("image-viewer-viewport");
  const image = document.getElementById("image-viewer-image");
  const title = document.getElementById("image-viewer-title");
  const info = document.getElementById("image-viewer-info");
  const notice = document.getElementById("image-viewer-notice");
  const message = document.getElementById("image-viewer-message");
  const hint = document.getElementById("image-viewer-hint");
  const scaleLabel = document.getElementById("image-viewer-scale");
  const retry = dialog.querySelector('[data-viewer-action="retry"]');
  const download = dialog.querySelector('[data-viewer-action="download"]');
  const zoomButtons = dialog.querySelectorAll("[data-viewer-zoom]");
  let requestToken = 0;
  let current = null;
  let objectUrl = "";
  let width = 0;
  let height = 0;
  let zoom = 1;
  let fit = true;
  let drag = null;
  let opener = null;
  const defaultHint = "双击切换适应 / 100% · 放大后可拖动";

  function clearImage() {
    image.hidden = true;
    image.removeAttribute("src");
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = "";
    width = height = 0;
    drag = null;
    viewport.classList.remove("dragging");
    viewport.scrollLeft = viewport.scrollTop = 0;
    scaleLabel.textContent = "—";
    zoomButtons.forEach((button) => { button.disabled = true; });
  }

  function setZoom(value, fitted = false) {
    if (!width || !height || image.hidden) return;
    const marginX = Math.max(16, (viewport.clientWidth - width * zoom) / 2);
    const marginY = Math.max(16, (viewport.clientHeight - height * zoom) / 2);
    const centerX = (viewport.scrollLeft + viewport.clientWidth / 2 - marginX) / (width * zoom);
    const centerY = (viewport.scrollTop + viewport.clientHeight / 2 - marginY) / (height * zoom);
    fit = fitted;
    zoom = fitted
      ? Math.max(0.001, Math.min(1, (viewport.clientWidth - 32) / width, (viewport.clientHeight - 32) / height))
      : Math.max(0.05, Math.min(4, value));
    image.style.width = `${Math.max(1, Math.round(width * zoom))}px`;
    image.style.height = `${Math.max(1, Math.round(height * zoom))}px`;
    scaleLabel.textContent = `${Math.round(zoom * 100)}%`;
    dialog.querySelector('[data-viewer-action="fit"]').setAttribute("aria-pressed", String(fit));
    dialog.querySelector('[data-viewer-action="actual"]').setAttribute("aria-pressed", String(!fit && zoom === 1));
    viewport.scrollLeft = fitted ? 0 : Math.max(0, centerX * width * zoom + Math.max(16, (viewport.clientWidth - width * zoom) / 2) - viewport.clientWidth / 2);
    viewport.scrollTop = fitted ? 0 : Math.max(0, centerY * height * zoom + Math.max(16, (viewport.clientHeight - height * zoom) / 2) - viewport.clientHeight / 2);
  }

  function fail(text) {
    info.textContent = "大图暂不可用";
    message.textContent = text || "加载失败，请重试或下载原图查看。";
    notice.hidden = false;
    retry.hidden = false;
  }

  async function open(id, sourceIndex = 0) {
    if (!id) return;
    const token = ++requestToken;
    clearImage();
    current = { id, sourceIndex };
    title.textContent = sourceIndex > 0 ? `输入原图 ${sourceIndex}` : "生成结果";
    info.textContent = "正在读取缓存原图…";
    image.alt = sourceIndex > 0 ? `本次请求的输入原图 ${sourceIndex}` : "生成结果大图";
    notice.hidden = false;
    retry.hidden = true;
    download.disabled = false;
    hint.textContent = defaultHint;
    message.textContent = "正在加载大图…";
    viewport.setAttribute("aria-busy", "true");
    if (!dialog.open) {
      opener = document.activeElement;
      dialog.showModal();
    }
    let pendingUrl = "";
    try {
      const response = await loadImage(id, sourceIndex);
      if (token !== requestToken || !dialog.open) return;
      if (!response?.success) throw new Error(response?.message || "原图缓存不可用，请刷新记录后重试。");
      const match = /^data:image\/(png|jpeg|webp|gif|bmp);base64,([A-Za-z0-9+/=\s]+)$/.exec(response.image || "");
      if (!match) throw new Error("图片格式无法预览，请下载原图查看。");
      const bytes = Uint8Array.from(atob(match[2]), (char) => char.charCodeAt(0));
      pendingUrl = URL.createObjectURL(new Blob([bytes], { type: `image/${match[1]}` }));
      // Decode off-DOM: an earlier request must never replace a newer image.
      const decoded = new Image();
      decoded.src = pendingUrl;
      await decoded.decode();
      if (token !== requestToken || !dialog.open) return;
      width = decoded.naturalWidth;
      height = decoded.naturalHeight;
      objectUrl = pendingUrl;
      pendingUrl = "";
      image.src = objectUrl;
      image.hidden = false;
      notice.hidden = true;
      zoomButtons.forEach((button) => { button.disabled = false; });
      const originalSize = `${response.original_width || width} × ${response.original_height || height}`;
      info.textContent = response.preview_only
        ? `高清预览 ${width} × ${height} · 原始尺寸 ${originalSize} · 下载保留原文件`
        : `原图 ${originalSize} · 原始文件`;
      zoom = 1;
      setZoom(1, true);
    } catch (error) {
      if (token === requestToken && dialog.open) fail(error?.message);
    } finally {
      if (pendingUrl) URL.revokeObjectURL(pendingUrl);
      if (token === requestToken) viewport.setAttribute("aria-busy", "false");
    }
  }

  dialog.addEventListener("close", () => {
    // A queued close event must not clear a rapidly reopened dialog.
    if (dialog.open) return;
    ++requestToken;
    current = null;
    clearImage();
    viewport.setAttribute("aria-busy", "false");
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    opener = null;
  });

  dialog.addEventListener("click", async (event) => {
    const action = event.target.closest("[data-viewer-action]")?.dataset.viewerAction;
    if (action === "close") dialog.close();
    if (action === "fit") setZoom(1, true);
    if (action === "actual") setZoom(1);
    if (action === "in") setZoom(zoom * 1.25);
    if (action === "out") setZoom(zoom / 1.25);
    if (action === "retry" && current) void open(current.id, current.sourceIndex);
    if (action === "download" && current && !download.disabled) {
      const token = requestToken;
      download.disabled = true;
      hint.textContent = "正在准备原图下载…";
      try {
        await downloadImage(current.id, current.sourceIndex);
        if (token === requestToken) hint.textContent = "已请求下载原图";
      } catch (error) {
        if (token === requestToken) hint.textContent = error?.message || "下载失败，请稍后重试。";
      } finally {
        if (token === requestToken) download.disabled = false;
      }
    }
  });

  dialog.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (["+", "=", "-", "0", "1"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "+" || event.key === "=") setZoom(zoom * 1.25);
      if (event.key === "-") setZoom(zoom / 1.25);
      if (event.key === "0") setZoom(1, true);
      if (event.key === "1") setZoom(1);
    }
  });
  viewport.addEventListener("dblclick", () => setZoom(1, !fit));
  viewport.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.button !== 0 || image.hidden) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("dragging");
    event.preventDefault();
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    viewport.scrollLeft = drag.left - event.clientX + drag.x;
    viewport.scrollTop = drag.top - event.clientY + drag.y;
  });
  const endDrag = () => { drag = null; viewport.classList.remove("dragging"); };
  viewport.addEventListener("pointerup", endDrag);
  viewport.addEventListener("pointercancel", endDrag);
  viewport.addEventListener("lostpointercapture", endDrag);
  new ResizeObserver(() => { if (dialog.open && fit) setZoom(1, true); }).observe(viewport);
  return { open };
}
