const navState = { catalog: null };

const elements = {
  phaseRoot: document.querySelector("#phase-root"),
  phaseSummary: document.querySelector("#phase-summary"),
  status: document.querySelector("#navigation-status"),
  reviewFields: document.querySelector("#review-fields"),
  reviewTarget: document.querySelector("#review-target"),
  consensusFields: document.querySelector("#consensus-fields"),
  singleConsensusFields: document.querySelector("#single-consensus-fields"),
  reviewCsv: document.querySelector("#review-csv"),
  reviewerACsv: document.querySelector("#reviewer-a-csv"),
  reviewerBCsv: document.querySelector("#reviewer-b-csv"),
  consensusCsv: document.querySelector("#consensus-csv"),
  singleConsensusCsv: document.querySelector("#single-consensus-csv"),
  labelsCsv: document.querySelector("#labels-csv"),
  openWorkspace: document.querySelector("#open-workspace-button"),
  refresh: document.querySelector("#refresh-navigation-button"),
  labelPreview: document.querySelector("#label-preview"),
  labelPreviewSummary: document.querySelector("#label-preview-summary"),
  inventory: document.querySelector("#csv-inventory"),
};

function selectedMode() {
  return document.querySelector('input[name="workspace-mode"]:checked').value;
}

function selectedReviewer() {
  return document.querySelector('input[name="reviewer-person"]:checked').value;
}

function reviewerForFile(name) {
  const normalized = name.toLowerCase().replace(/[\s_-]/g, "");
  if (normalized.startsWith("reviewera")) return "a";
  if (normalized.startsWith("reviewerb")) return "b";
  return "";
}

function setStatus(message, kind = "") {
  elements.status.textContent = message;
  elements.status.className = "save-status " + kind;
}

function makeOption(name, label = name) {
  const option = document.createElement("option");
  option.value = name;
  option.textContent = label;
  return option;
}

function setOptions(select, entries, preferred = "") {
  select.replaceChildren();
  if (!entries.length) {
    select.append(makeOption("", "没有可用文件"));
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const entry of entries) select.append(makeOption(entry.name, entry.displayName || entry.name));
  select.value = entries.some((entry) => entry.name === preferred) ? preferred : entries[0].name;
}

function displayKind(kind) {
  return {
    reviewer: "独立评审",
    consensus_output: "三文件共识输出",
    single_file_merge: "单文件共识",
    label_config: "label_set 配置",
    other: "其他 CSV",
    invalid: "无效 CSV",
  }[kind] || kind;
}

function renderInventory(entries) {
  elements.inventory.replaceChildren();
  for (const entry of entries) {
    const row = document.createElement("tr");
    for (const value of [entry.name, displayKind(entry.kind), String(entry.rows), entry.columns.join(", ")]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    elements.inventory.append(row);
  }
}

function renderCatalog(catalog) {
  navState.catalog = catalog;
  elements.phaseRoot.textContent = catalog.data_dir;
  elements.phaseSummary.textContent = String(catalog.csv_files.length) + " 个 CSV · " + String(catalog.panel_count) + " 张 PNG（panels/）";
  const reviewers = catalog.csv_files.filter((entry) => entry.kind === "reviewer");
  const consensus = catalog.csv_files.filter((entry) => entry.kind === "consensus_output");
  const single = catalog.csv_files.filter((entry) => entry.kind === "single_file_merge");
  updateReviewCsv();
  const aReviewers = reviewers.filter((entry) => /reviewer[_-]?a/i.test(entry.name));
  const bReviewers = reviewers.filter((entry) => /reviewer[_-]?b/i.test(entry.name));
  setOptions(elements.reviewerACsv, aReviewers.length ? aReviewers : reviewers, reviewers[0] && reviewers[0].name);
  setOptions(elements.reviewerBCsv, bReviewers.length ? bReviewers : reviewers, reviewers[1] && reviewers[1].name);
  setOptions(elements.consensusCsv, consensus);
  setOptions(elements.singleConsensusCsv, single);
  setOptions(elements.labelsCsv, catalog.label_files.map((name) => ({ name })), catalog.default_labels);
  renderInventory(catalog.csv_files);
  updateModeFields();
  void refreshLabelPreview();
}

function updateReviewCsv() {
  const entries = (navState.catalog && navState.catalog.csv_files || []);
  const reviewers = entries.filter((entry) => entry.kind === "reviewer");
  const person = selectedReviewer();
  const selectedBefore = elements.reviewCsv.value;
  const matchingFiles = [
    ...reviewers.filter((entry) => reviewerForFile(entry.name) === person),
    ...entries.filter((entry) => entry.kind === "other").map((entry) => ({
      ...entry,
      displayName: entry.name + "（其他 CSV）",
    })),
  ];
  setOptions(elements.reviewCsv, matchingFiles, selectedBefore);
  const role = person.toUpperCase();
  const selectedEntry = entries.find((entry) => entry.name === elements.reviewCsv.value);
  elements.reviewTarget.textContent = elements.reviewCsv.value
    ? selectedEntry?.kind === "other"
      ? "当前将编辑：" + elements.reviewCsv.value + "（其他 CSV；进入前会校验可标注列）"
      : "当前将编辑：评审人 " + role + " · " + elements.reviewCsv.value
    : "未找到评审人 " + role + " 的工作 CSV";
}

function updateModeFields() {
  const mode = selectedMode();
  elements.reviewFields.hidden = mode !== "review";
  elements.consensusFields.hidden = mode !== "consensus";
  elements.singleConsensusFields.hidden = mode !== "single_file_consensus";
  if (mode === "review") updateReviewCsv();
}

async function refreshLabelPreview() {
  const name = elements.labelsCsv.value;
  if (!name) {
    elements.labelPreview.replaceChildren();
    elements.labelPreviewSummary.textContent = "未找到 label_set CSV";
    return;
  }
  try {
    const response = await fetch("/api/labels-preview?labels=" + encodeURIComponent(name), { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "读取 label_set 失败");
    elements.labelPreview.replaceChildren();
    for (const definition of payload.labels) {
      const card = document.createElement("article");
      card.className = "label-preview-item";
      const title = document.createElement("strong");
      title.textContent = definition.label;
      const description = document.createElement("span");
      description.textContent = definition.description || "（无说明）";
      card.append(title, description);
      elements.labelPreview.append(card);
    }
    elements.labelPreviewSummary.textContent = payload.name + " · " + payload.labels.length + " 个标签";
  } catch (error) {
    elements.labelPreviewSummary.textContent = "预览失败：" + error.message;
  }
}

async function openWorkspace() {
  const mode = selectedMode();
  const payload = { mode, labels: elements.labelsCsv.value };
  if (mode === "review") payload.csv = elements.reviewCsv.value;
  if (mode === "consensus") {
    payload.reviewer_a = elements.reviewerACsv.value;
    payload.reviewer_b = elements.reviewerBCsv.value;
    payload.consensus = elements.consensusCsv.value;
  }
  if (mode === "single_file_consensus") payload.csv = elements.singleConsensusCsv.value;
  elements.openWorkspace.disabled = true;
  try {
    setStatus("正在验证并打开工作区…", "saving");
    const response = await fetch("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "无法打开工作区");
    window.location.assign("/workspace");
  } catch (error) {
    setStatus("无法打开：" + error.message, "error");
    elements.openWorkspace.disabled = false;
  }
}

async function loadNavigation() {
  elements.refresh.disabled = true;
  try {
    const response = await fetch("/api/navigation", { cache: "no-store" });
    const catalog = await response.json();
    if (!response.ok) throw new Error(catalog.error || "读取 phase 目录失败");
    renderCatalog(catalog);
    setStatus("请选择模式与文件", "saved");
  } catch (error) {
    setStatus("加载失败：" + error.message, "error");
  } finally {
    elements.refresh.disabled = false;
  }
}

for (const modeInput of document.querySelectorAll('input[name="workspace-mode"]')) {
  modeInput.addEventListener("change", updateModeFields);
}
for (const reviewerInput of document.querySelectorAll('input[name="reviewer-person"]')) {
  reviewerInput.addEventListener("change", updateReviewCsv);
}
elements.reviewCsv.addEventListener("change", updateReviewCsv);
elements.labelsCsv.addEventListener("change", () => void refreshLabelPreview());
elements.openWorkspace.addEventListener("click", () => void openWorkspace());
elements.refresh.addEventListener("click", () => void loadNavigation());
void loadNavigation();
