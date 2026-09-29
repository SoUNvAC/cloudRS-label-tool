const state = {
  mode: "annotation",
  reviewerANamed: "Reviewer A",
  reviewerBNamed: "Reviewer B",
  onlyDisagreements: false,
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
  workspaceMode: document.querySelector("#workspace-mode"),
  saveStatus: document.querySelector("#save-status"),
  previous: document.querySelector("#previous-button"),
  next: document.querySelector("#next-button"),
  position: document.querySelector("#position"),
  tileId: document.querySelector("#tile-id"),
  completionSummary: document.querySelector("#completion-summary"),
  agreementQueueControls: document.querySelector("#agreement-queue-controls"),
  skipAgreements: document.querySelector("#skip-agreements-toggle"),
  agreementQueueHint: document.querySelector("#agreement-queue-hint"),
  reviewerComparison: document.querySelector("#reviewer-comparison"),
  reviewerAgreement: document.querySelector("#reviewer-agreement"),
  reviewerAName: document.querySelector("#reviewer-a-name"),
  reviewerBName: document.querySelector("#reviewer-b-name"),
  reviewerALabel: document.querySelector("#reviewer-a-label"),
  reviewerBLabel: document.querySelector("#reviewer-b-label"),
  reviewerANotes: document.querySelector("#reviewer-a-notes"),
  reviewerBNotes: document.querySelector("#reviewer-b-notes"),
  labelHeading: document.querySelector("#label-heading"),
  labelSubtitle: document.querySelector("#label-subtitle"),
  notesHeading: document.querySelector("#notes-heading"),
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

function visibleTileIndexes() {
  return state.tiles
    .map((tile, index) => ({ tile, index }))
    .filter(({ tile }) => !state.onlyDisagreements || !tile.reviewer_agreement_label_set)
    .map(({ index }) => index);
}

function renderSummary() {
  const completed = state.tiles.filter(isComplete).length;
  const visible = visibleTileIndexes();
  const visiblePosition = visible.indexOf(state.currentIndex);
  elements.completionSummary.textContent = `已标注 ${completed} / ${state.tiles.length}`;
  elements.position.textContent = state.onlyDisagreements
    ? `${Math.max(visiblePosition + 1, 0)} / ${visible.length} 待裁决`
    : `${state.currentIndex + 1} / ${state.tiles.length}`;
  elements.previous.disabled = visiblePosition <= 0;
  elements.next.disabled = visiblePosition < 0 || visiblePosition === visible.length - 1;
}

function displayReviewerValue(value, fallback = "（未填写）") {
  return value.trim() || fallback;
}

function normalizedLabelSet(value) {
  return [...new Set(selectedFromLabelSet(value))].sort().join("|");
}

function reviewerLabelsMatch(reviewerA, reviewerB) {
  return Boolean(reviewerA.trim()) && normalizedLabelSet(reviewerA) === normalizedLabelSet(reviewerB);
}

function renderReviewerComparison(tile, autoApplied = false) {
  const isMerge = state.mode === "merge";
  elements.reviewerComparison.hidden = !isMerge;
  elements.agreementQueueControls.hidden = !isMerge;
  elements.labelHeading.textContent = isMerge ? "最终共识 label_set" : "label_set";
  elements.labelSubtitle.textContent = isMerge ? "评审团裁决；只会写入最终共识文件" : "当前图的标签与备注";
  elements.notesHeading.textContent = isMerge ? "rule_or_counterexample" : "notes";
  elements.notes.placeholder = isMerge ? "填写裁决规则或反例；会自动保存" : "可选备注；会自动保存";
  if (!isMerge) return;

  const reviewerA = tile.reviewer_a;
  const reviewerB = tile.reviewer_b;
  elements.reviewerAName.textContent = `Reviewer A · ${state.reviewerANamed}`;
  elements.reviewerBName.textContent = `Reviewer B · ${state.reviewerBNamed}`;
  elements.reviewerALabel.textContent = displayReviewerValue(reviewerA.label_set);
  elements.reviewerBLabel.textContent = displayReviewerValue(reviewerB.label_set);
  elements.reviewerANotes.textContent = displayReviewerValue(reviewerA.notes, "（无 notes）");
  elements.reviewerBNotes.textContent = displayReviewerValue(reviewerB.notes, "（无 notes）");
  const agreed = Boolean(tile.reviewer_agreement_label_set);
  const rawAgreement = reviewerLabelsMatch(reviewerA.label_set, reviewerB.label_set);
  if (agreed) {
    elements.reviewerAgreement.textContent = autoApplied
      ? "A / B 的 label_set 一致，已自动带入最终标签"
      : "A / B 的 label_set 一致";
    elements.reviewerAgreement.className = "muted reviewer-match";
  } else if (rawAgreement) {
    elements.reviewerAgreement.textContent = "A / B 标签相同，但当前 label_set 配置不允许自动合并";
    elements.reviewerAgreement.className = "muted reviewer-mismatch";
  } else if (!reviewerA.label_set.trim() && !reviewerB.label_set.trim()) {
    elements.reviewerAgreement.textContent = "A / B 均未填写 label_set";
    elements.reviewerAgreement.className = "muted";
  } else {
    elements.reviewerAgreement.textContent = "A / B 的 label_set 不一致，需要裁决";
    elements.reviewerAgreement.className = "muted reviewer-mismatch";
  }
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

function autoApplyMatchingReviewerLabels(tile) {
  if (state.mode !== "merge" || tile.label_set.trim()) return false;
  const agreement = tile.reviewer_agreement_label_set || "";
  if (!agreement) return false;
  const proposed = selectedFromLabelSet(agreement);
  const configured = labelMap();
  if (!proposed.length || proposed.some((label) => !configured.has(label))) return false;
  if (proposed.length > 1 && !pairsAreAllowed(proposed[0], proposed[1])) return false;

  state.selectedLabels = proposed;
  state.multiEnabled = proposed.length > 1;
  return true;
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
  const visible = visibleTileIndexes();
  if (state.onlyDisagreements && !visible.includes(state.currentIndex)) {
    state.currentIndex = visible[0] ?? -1;
  }
  const tile = currentTile();
  if (!tile) {
    elements.image.removeAttribute("src");
    elements.image.hidden = true;
    elements.missingImage.hidden = false;
    elements.missingImage.textContent = "没有需要人工裁决的条目。";
    elements.tileId.textContent = "—";
    elements.notes.value = "";
    renderSummary();
    return;
  }
  state.selectedLabels = selectedFromLabelSet(tile.label_set);
  state.multiEnabled = state.selectedLabels.length > 1;
  const autoApplied = autoApplyMatchingReviewerLabels(tile);
  elements.tileId.textContent = tile.tile_id;
  elements.notes.value = tile.notes;
  elements.image.src = `/api/image/${encodeURIComponent(tile.tile_id)}?v=${Date.now()}`;
  elements.image.hidden = !tile.has_image;
  elements.missingImage.hidden = tile.has_image;
  elements.image.alt = `${tile.tile_id} 待标注图片`;
  resetToFit();
  renderReviewerComparison(tile, autoApplied);
  renderLabels();
  renderSummary();
  if (autoApplied) scheduleSave("auto_reviewer_agreement");
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
  if (!tile) return Promise.resolve();
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

function navigateVisible(offset) {
  const visible = visibleTileIndexes();
  const position = visible.indexOf(state.currentIndex);
  const destination = visible[position + offset];
  if (destination !== undefined) void navigate(destination);
}

function applyStatePayload(payload) {
  state.tiles = payload.tiles;
  state.labels = payload.labels;
  state.mode = payload.mode || "annotation";
  state.reviewerANamed = payload.reviewer_a_name || "Reviewer A";
  state.reviewerBNamed = payload.reviewer_b_name || "Reviewer B";
  elements.csvName.textContent = state.mode === "merge" ? `合并输出：${payload.csv_name}` : payload.csv_name;
  const workspaceMode = payload.workspace_mode || (state.mode === "merge" ? "consensus" : "review");
  const modeText = {
    review: `独立评审${payload.reviewer_role ? ` ${payload.reviewer_role}` : ""}`,
    consensus: "共识裁决（A/B/共识三文件）",
    single_file_consensus: "共识裁决（单文件）",
  }[workspaceMode] || "工作区";
  elements.workspaceMode.textContent = modeText;
}

async function autoMergeAndFilter() {
  const activeTileId = currentTile()?.tile_id;
  elements.skipAgreements.disabled = true;
  try {
    await state.saveChain;
    setStatus("正在合并 A/B 一致项…", "saving");
    const response = await fetch("/api/auto-merge-agreements", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "自动合并失败");
    const stateResponse = await fetch("/api/state", { cache: "no-store" });
    const payload = await stateResponse.json();
    if (!stateResponse.ok) throw new Error(payload.error || "刷新合并队列失败");
    applyStatePayload(payload);
    state.onlyDisagreements = true;
    const retainedIndex = state.tiles.findIndex((tile) => tile.tile_id === activeTileId);
    state.currentIndex = retainedIndex >= 0 ? retainedIndex : 0;
    renderTile();
    elements.agreementQueueHint.textContent = `已自动写入 ${result.saved} 条；${result.already_final} 条已有最终裁决。当前仅显示 ${result.manual} 条待裁决项。`;
    setStatus("A/B 一致项已合并，正在查看待裁决队列", "saved");
  } catch (error) {
    state.onlyDisagreements = false;
    elements.skipAgreements.checked = false;
    setStatus(`自动合并失败：${error.message}`, "error");
  } finally {
    elements.skipAgreements.disabled = false;
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
  const visible = visibleTileIndexes();
  const startPosition = visible.indexOf(state.currentIndex);
  for (let offset = 1; offset <= visible.length; offset += 1) {
    const index = visible[(startPosition + offset) % visible.length];
    if (!isComplete(state.tiles[index])) return index;
  }
  return -1;
}

function bindViewerInteractions() {
  let pointer = null;
  const touchPointers = new Map();
  let pinch = null;
  let spacePressed = false;
  const clampScale = (scale) => Math.min(8, Math.max(0.2, scale));
  const touchDistance = () => {
    const [first, second] = [...touchPointers.values()];
    return Math.hypot(second.x - first.x, second.y - first.y);
  };
  const touchMidpoint = () => {
    const [first, second] = [...touchPointers.values()];
    return { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 };
  };
  const beginPinch = () => {
    if (touchPointers.size < 2) return;
    pinch = {
      distance: Math.max(touchDistance(), 1),
      midpoint: touchMidpoint(),
      transform: { ...state.transform },
    };
    pointer = null;
    elements.viewport.classList.add("dragging");
  };
  const refreshPanCursor = () => {
    elements.viewport.classList.toggle("pan-ready", spacePressed);
  };
  elements.viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  elements.viewport.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "touch") {
      event.preventDefault();
      touchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      elements.viewport.setPointerCapture(event.pointerId);
      if (touchPointers.size >= 2) {
        beginPinch();
      } else {
        pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
        elements.viewport.classList.add("dragging");
      }
      return;
    }
    const wantsPan = event.pointerType === "touch" || event.button === 1 || (event.button === 0 && spacePressed);
    if (!wantsPan) return;
    event.preventDefault();
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
    elements.viewport.setPointerCapture(event.pointerId);
    elements.viewport.classList.add("dragging");
  });
  elements.viewport.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch" && touchPointers.has(event.pointerId)) {
      event.preventDefault();
      touchPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (touchPointers.size >= 2) {
        if (!pinch) beginPinch();
        const midpoint = touchMidpoint();
        state.transform.scale = clampScale(pinch.transform.scale * touchDistance() / pinch.distance);
        state.transform.x = pinch.transform.x + midpoint.x - pinch.midpoint.x;
        state.transform.y = pinch.transform.y + midpoint.y - pinch.midpoint.y;
        renderTransform();
      } else if (pointer && event.pointerId === pointer.id) {
        state.transform.x += event.clientX - pointer.x;
        state.transform.y += event.clientY - pointer.y;
        pointer.x = event.clientX;
        pointer.y = event.clientY;
        renderTransform();
      }
      return;
    }
    if (!pointer || event.pointerId !== pointer.id) return;
    state.transform.x += event.clientX - pointer.x;
    state.transform.y += event.clientY - pointer.y;
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    renderTransform();
  });
  const endDrag = (event) => {
    if (event?.pointerType === "touch") {
      touchPointers.delete(event.pointerId);
      pinch = null;
      if (touchPointers.size === 1) {
        const [id, point] = touchPointers.entries().next().value;
        pointer = { id, ...point };
      } else {
        pointer = null;
        elements.viewport.classList.remove("dragging");
      }
      return;
    }
    pointer = null;
    elements.viewport.classList.remove("dragging");
  };
  elements.viewport.addEventListener("pointerup", endDrag);
  elements.viewport.addEventListener("pointercancel", endDrag);
  window.addEventListener("keydown", (event) => {
    if (event.code !== "Space" || event.target.matches("textarea, input, button")) return;
    spacePressed = true;
    event.preventDefault();
    refreshPanCursor();
  });
  window.addEventListener("keyup", (event) => {
    if (event.code !== "Space") return;
    spacePressed = false;
    refreshPanCursor();
  });
  window.addEventListener("blur", () => {
    spacePressed = false;
    refreshPanCursor();
    endDrag();
    touchPointers.clear();
    pinch = null;
  });
}

function bindControls() {
  elements.previous.addEventListener("click", () => navigateVisible(-1));
  elements.next.addEventListener("click", () => navigateVisible(1));
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
  elements.skipAgreements.addEventListener("change", () => {
    if (elements.skipAgreements.checked) {
      void autoMergeAndFilter();
    } else {
      state.onlyDisagreements = false;
      elements.agreementQueueHint.textContent = "勾选后会将空的最终共识行批量填入 A/B 一致标签；已有裁决不会覆盖。";
      if (state.currentIndex < 0) state.currentIndex = 0;
      renderTile();
    }
  });
  window.addEventListener("keydown", (event) => {
    if (event.target.matches("textarea, input")) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); navigateVisible(-1); }
    if (event.key === "ArrowRight") { event.preventDefault(); navigateVisible(1); }
    if (event.key === "+" || event.key === "=") { event.preventDefault(); zoom(1.25); }
    if (event.key === "-") { event.preventDefault(); zoom(1 / 1.25); }
    if (event.key.toLowerCase() === "f") { event.preventDefault(); resetToFit(); }
  });
  window.addEventListener("beforeunload", () => {
    if (currentTile()) void saveCurrent("page_unload");
  });
}

async function initialize() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "读取数据失败");
    applyStatePayload(payload);
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
