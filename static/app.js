const state = {
  tiles: [],
  labels: [],
  currentIndex: 0,
  selectedLabels: [],
  multiEnabled: false,
  transform: { scale: 1, x: 0, y: 0 },
  saveChain: Promise.resolve(),
  notesTimer: null,
};

const elements = {
  csvName: document.querySelector("#csv-name"),
  saveStatus: document.querySelector("#save-status"),
  previous: document.querySelector("#previous-button"),
  next: document.querySelector("#next-button"),
  position: document.querySelector("#position"),
  tileId: document.querySelector("#tile-id"),
  completionSummary: document.querySelector("#completion-summary"),
  viewport: document.querySelector("#image-viewport"),
  image: document.querySelector("#annotation-image"),
  missingImage: document.querySelector("#missing-image"),
  zoomIn: document.querySelector("#zoom-in-button"),
  zoomOut: document.querySelector("#zoom-out-button"),
  fit: document.querySelector("#fit-button"),
  actualSize: document.querySelector("#actual-size-button"),
  reloadLabels: document.querySelector("#reload-labels-button"),
  multiToggle: document.querySelector("#multi-select-toggle"),
  multiHint: document.querySelector("#multi-select-hint"),
  labelOptions: document.querySelector("#label-options"),
  labelWarning: document.querySelector("#label-warning"),
  clearLabels: document.querySelector("#clear-labels-button"),
  notes: document.querySelector("#notes"),
  save: document.querySelector("#save-button"),
  nextUnfinished: document.querySelector("#next-unfinished-button"),
};

function currentTile() {
  return state.tiles[state.currentIndex];
}

function labelMap() {
  return new Map(state.labels.map((definition) => [definition.label, definition]));
}

function setStatus(message, kind = "") {
  elements.saveStatus.textContent = message;
  elements.saveStatus.className = `save-status ${kind}`;
}

function isComplete(tile) {
  return Boolean(tile.label_set.trim());
}

function renderSummary() {
  const completed = state.tiles.filter(isComplete).length;
  elements.completionSummary.textContent = `已标注 ${completed} / ${state.tiles.length}`;
  elements.position.textContent = `${state.currentIndex + 1} / ${state.tiles.length}`;
  elements.previous.disabled = state.currentIndex === 0;
  elements.next.disabled = state.currentIndex === state.tiles.length - 1;
}

function renderTransform() {
  const { scale, x, y } = state.transform;
  elements.image.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
}

function resetToFit() {
  state.transform = { scale: 1, x: 0, y: 0 };
  renderTransform();
}

function zoom(factor) {
  state.transform.scale = Math.min(8, Math.max(0.2, state.transform.scale * factor));
  renderTransform();
}

function selectedFromLabelSet(value) {
  return value.split("|").map((item) => item.trim()).filter(Boolean);
}

function showLabelWarning(message) {
  elements.labelWarning.hidden = !message;
  elements.labelWarning.textContent = message || "";
}

function renderLabels() {
  const configured = labelMap();
  const unknown = state.selectedLabels.filter((label) => !configured.has(label));
  showLabelWarning(
    unknown.length
      ? `当前 CSV 含已不在 label_set.csv 中的标签：${unknown.join("、")}。请重新选择后才能保存。`
      : ""
  );
  elements.labelOptions.replaceChildren();
  for (const definition of state.labels) {
    const card = document.createElement("label");
    card.className = `label-card${state.selectedLabels.includes(definition.label) ? " checked" : ""}`;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedLabels.includes(definition.label);
    checkbox.addEventListener("change", () => chooseLabel(definition.label, checkbox.checked));
    const content = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = definition.label;
    content.append(title);
    if (definition.description) {
      const description = document.createElement("small");
      description.textContent = definition.description;
      content.append(description);
    }
    card.append(checkbox, content);
    elements.labelOptions.append(card);
  }
  elements.multiToggle.checked = state.multiEnabled;
  elements.multiHint.textContent = state.multiEnabled
    ? "已启用双标签：最多两个，且必须是 label_set.csv 明确允许的组合。"
    : "默认单选。启用后仅可选择配置允许的两个语义标签。";
}

function pairsAreAllowed(firstName, secondName) {
  const labels = labelMap();
  const first = labels.get(firstName);
  const second = labels.get(secondName);
  if (!first || !second || first.exclusive || second.exclusive || !first.multi_selectable || !second.multi_selectable) return false;
  return (!first.allowed_with.length || first.allowed_with.includes(secondName))
    && (!second.allowed_with.length || second.allowed_with.includes(firstName));
}

function chooseLabel(label, checked) {
  const definition = labelMap().get(label);
  if (!definition) return;
  if (!checked) {
    state.selectedLabels = state.selectedLabels.filter((item) => item !== label);
  } else if (!state.multiEnabled) {
    state.selectedLabels = [label];
  } else if (definition.exclusive || !definition.multi_selectable) {
    state.selectedLabels = [label];
    state.multiEnabled = false;
    showLabelWarning(`“${label}”是独占标签，已自动关闭双标签。`);
  } else if (state.selectedLabels.length === 0) {
    state.selectedLabels = [label];
  } else if (state.selectedLabels.length === 1) {
    if (!pairsAreAllowed(state.selectedLabels[0], label)) {
      showLabelWarning(`配置不允许组合：${state.selectedLabels[0]} | ${label}`);
    } else {
      state.selectedLabels.push(label);
      showLabelWarning("");
    }
  } else {
    showLabelWarning("双标签模式最多选择两个标签。请先取消其中一个。");
  }
  renderLabels();
  scheduleSave("label_change");
}

function renderTile() {
  const tile = currentTile();
  state.selectedLabels = selectedFromLabelSet(tile.label_set);
  state.multiEnabled = state.selectedLabels.length > 1;
  elements.tileId.textContent = tile.tile_id;
  elements.notes.value = tile.notes;
  elements.image.src = `/api/image/${encodeURIComponent(tile.tile_id)}?v=${Date.now()}`;
  elements.image.hidden = !tile.has_image;
  elements.missingImage.hidden = tile.has_image;
  elements.image.alt = `${tile.tile_id} 待标注图片`;
  resetToFit();
  renderLabels();
  renderSummary();
}

async function postSave(snapshot) {
  setStatus("正在安全保存…", "saving");
  const response = await fetch("/api/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(snapshot),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "服务器拒绝保存");
  const tile = state.tiles.find((item) => item.tile_id === payload.tile_id);
  if (tile) {
    tile.label_set = payload.label_set;
    tile.notes = payload.notes;
  }
  renderSummary();
  setStatus(`已保存 ${new Date(payload.saved_at).toLocaleTimeString()}`, "saved");
  return payload;
}

function saveCurrent(reason = "manual") {
  const tile = currentTile();
  const snapshot = {
    tile_id: tile.tile_id,
    labels: [...state.selectedLabels],
    notes: elements.notes.value,
    reason,
  };
  const task = state.saveChain.then(() => postSave(snapshot));
  state.saveChain = task.catch(() => undefined);
  task.catch((error) => setStatus(`保存失败：${error.message}`, "error"));
  return task;
}

function scheduleSave(reason) {
  void saveCurrent(reason);
}

async function navigate(index) {
  if (index < 0 || index >= state.tiles.length || index === state.currentIndex) return;
  if (state.notesTimer) {
    window.clearTimeout(state.notesTimer);
    state.notesTimer = null;
  }
  try {
    await saveCurrent("navigate");
    state.currentIndex = index;
    sessionStorage.setItem(`cloudrs-index:${elements.csvName.textContent}`, String(index));
    renderTile();
  } catch {
    // postSave already reports the precise error; navigation deliberately stops.
  }
}

async function reloadLabels() {
  elements.reloadLabels.disabled = true;
  try {
    const response = await fetch("/api/labels", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "读取 label_set.csv 失败");
    state.labels = payload.labels;
    renderLabels();
    setStatus("label_set.csv 已更新；当前标注未被改动", "saved");
  } catch (error) {
    setStatus(`更新 label_set 失败：${error.message}`, "error");
  } finally {
    elements.reloadLabels.disabled = false;
  }
}

function nextUnfinishedIndex() {
  for (let offset = 1; offset <= state.tiles.length; offset += 1) {
    const index = (state.currentIndex + offset) % state.tiles.length;
    if (!isComplete(state.tiles[index])) return index;
  }
  return -1;
}

function bindViewerInteractions() {
  let pointer = null;
  elements.viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  elements.viewport.addEventListener("pointerdown", (event) => {
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
    elements.viewport.setPointerCapture(event.pointerId);
    elements.viewport.classList.add("dragging");
  });
  elements.viewport.addEventListener("pointermove", (event) => {
    if (!pointer || event.pointerId !== pointer.id) return;
    state.transform.x += event.clientX - pointer.x;
    state.transform.y += event.clientY - pointer.y;
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    renderTransform();
  });
  const endDrag = () => {
    pointer = null;
    elements.viewport.classList.remove("dragging");
  };
  elements.viewport.addEventListener("pointerup", endDrag);
  elements.viewport.addEventListener("pointercancel", endDrag);
}

function bindControls() {
  elements.previous.addEventListener("click", () => navigate(state.currentIndex - 1));
  elements.next.addEventListener("click", () => navigate(state.currentIndex + 1));
  elements.zoomIn.addEventListener("click", () => zoom(1.25));
  elements.zoomOut.addEventListener("click", () => zoom(1 / 1.25));
  elements.fit.addEventListener("click", resetToFit);
  elements.actualSize.addEventListener("click", () => {
    const renderedWidth = elements.image.clientWidth;
    const nativeWidth = elements.image.naturalWidth;
    // At "fit", CSS constrains the image to the viewport.  Convert that fitted
    // size into a transform that displays one image pixel per CSS pixel.
    const scale = renderedWidth && nativeWidth ? nativeWidth / renderedWidth : 1;
    state.transform = { scale: Math.min(8, Math.max(0.2, scale)), x: 0, y: 0 };
    renderTransform();
  });
  elements.multiToggle.addEventListener("change", () => {
    const map = labelMap();
    const selectedDefinition = map.get(state.selectedLabels[0]);
    if (elements.multiToggle.checked && selectedDefinition && (selectedDefinition.exclusive || !selectedDefinition.multi_selectable)) {
      state.multiEnabled = false;
      showLabelWarning(`“${selectedDefinition.label}”不能使用双标签。`);
    } else if (!elements.multiToggle.checked && state.selectedLabels.length > 1) {
      state.multiEnabled = true;
      showLabelWarning("当前已有两个标签；请先取消一个标签，再关闭双标签。");
    } else {
      state.multiEnabled = elements.multiToggle.checked;
      showLabelWarning("");
    }
    renderLabels();
  });
  elements.clearLabels.addEventListener("click", () => {
    state.selectedLabels = [];
    showLabelWarning("");
    renderLabels();
    scheduleSave("clear_labels");
  });
  elements.notes.addEventListener("input", () => {
    if (state.notesTimer) window.clearTimeout(state.notesTimer);
    state.notesTimer = window.setTimeout(() => {
      state.notesTimer = null;
      scheduleSave("notes_change");
    }, 500);
  });
  elements.save.addEventListener("click", async () => {
    try { await saveCurrent("manual"); } catch { /* UI status is set by saveCurrent. */ }
  });
  elements.nextUnfinished.addEventListener("click", () => {
    const index = nextUnfinishedIndex();
    if (index === -1) {
      setStatus("所有图片都已有标签。", "saved");
    } else {
      navigate(index);
    }
  });
  elements.reloadLabels.addEventListener("click", reloadLabels);
  window.addEventListener("keydown", (event) => {
    if (event.target.matches("textarea, input")) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); navigate(state.currentIndex - 1); }
    if (event.key === "ArrowRight") { event.preventDefault(); navigate(state.currentIndex + 1); }
    if (event.key === "+" || event.key === "=") { event.preventDefault(); zoom(1.25); }
    if (event.key === "-") { event.preventDefault(); zoom(1 / 1.25); }
    if (event.key.toLowerCase() === "f") { event.preventDefault(); resetToFit(); }
  });
  window.addEventListener("beforeunload", () => {
    if (state.tiles.length) void saveCurrent("page_unload");
  });
}

async function initialize() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "读取数据失败");
    state.tiles = payload.tiles;
    state.labels = payload.labels;
    elements.csvName.textContent = payload.csv_name;
    const savedIndex = Number.parseInt(sessionStorage.getItem(`cloudrs-index:${payload.csv_name}`), 10);
    if (Number.isInteger(savedIndex) && savedIndex >= 0 && savedIndex < state.tiles.length) state.currentIndex = savedIndex;
    renderTile();
    bindControls();
    bindViewerInteractions();
    setStatus("已加载；编辑会自动保存", "saved");
  } catch (error) {
    setStatus(`无法启动：${error.message}`, "error");
  }
}

void initialize();
