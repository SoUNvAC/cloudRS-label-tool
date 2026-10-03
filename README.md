# CloudRS 本地图片标注工具

这是一个仅监听本机地址（`127.0.0.1`）的 Python Web 标注工具。它把一个 CSV 交付文件绑定到一次运行中，显示 `panels/<tile_id>.png`，并在每次编辑和切换图片时使用原子写入保存。

## 导航式启动（推荐）

需要 Python 3.10 或更新版本；无需安装第三方依赖。一个完整 Phase 建议放在独立目录：

```text
phase-xxx/
  panels/
  label_set.csv
  reviewer_A_calibration.csv
  reviewer_B_calibration.csv
  calibration_consensus.csv
  reviewer_A_confirmation.csv
  reviewer_B_confirmation.csv
```

启动时只指定该 Phase 目录：

```powershell
python app.py --data-dir D:\path\to\phase-xxx --open-browser
```

首页会显示 CSV 清单、记录数、PNG 数量和 `label_set` 预览。选择模式和 CSV 后才进入工作区：

- **独立评审**：选择一份 reviewer CSV，完成 calibration 或 confirmation；每次只打开一个人的一份文件。
- **手动选择其他 CSV**：在独立评审中选择此项，可从识别为“其他 CSV”的文件里手动打开；进入前会校验其包含 `tile_id`、`label_set`、`notes` 三列。
- **共识裁决（三文件）**：选择 A calibration、B calibration、`calibration_consensus.csv`，完成共同裁决。
- **共识裁决（单文件）**：选择同时含 A/B/final 字段的单文件。

PNG 始终通过 `panels/<tile_id>.png` 查找，因此 CSV 内的编号可跳跃。终端会显示浏览器地址；按 `Ctrl+C` 停止服务。

## 命令行直达工作区（兼容保留）

如果不需要导航页，仍可直接进入一份工作文件：

```powershell
python app.py --csv reviewer_A_calibration.csv --open-browser
python app.py --merge --reviewer-a reviewer_A_calibration.csv --reviewer-b reviewer_B_calibration.csv --consensus calibration_consensus.csv --open-browser
python app.py --merge-csv merge_adjudication.csv --open-browser
```

页面中央仍为当前 `tile_id` 的 PNG。右栏先以只读卡片展示 Reviewer A 与 Reviewer B 的 `label_set` 和 notes，并明确提示两人的标签是否一致；评审团再填写最终 `agreed_label_set` 和 `rule_or_counterexample`。

- 只会更新 `calibration_consensus.csv`，两份 reviewer CSV 在合并模式下绝不会被写入。
- 两份 reviewer CSV 与 consensus CSV 的 `tile_id`、行数和顺序必须完全一致；不一致时工具拒绝启动或保存，避免错误合并。
- 每次加载当前图片时，若 A/B 的非空标签集合一致且最终共识仍为空，会自动勾选并保存该标签；已有最终共识绝不自动覆盖。
- 勾选“自动合并 A/B 一致项，只看待裁决项”后，会一次性填入所有可安全自动合并的空共识行，并将浏览、上一张/下一张和“下一张未完成”限制在 A/B 不一致的人工队列。取消勾选只恢复查看全部，不会回滚已写入的共识。
- 最终共识沿用原有自动保存、切图前强制保存、原子替换、快照和审计日志机制。

## 单文件 A/B 合并裁决

如果 A、B 结果与最终裁决都在同一份 CSV，使用：

```powershell
python app.py --merge-csv merge_adjudication.csv --open-browser
```

文件必须包含以下表头；可以保留其他列，工具不会删除它们：

```csv
tile_id,reviewer_A_label_set,reviewer_B_label_set,final_label_set,rationale
```

页面会读取两列 reviewer 标签，并且只编辑 `final_label_set` 与 `rationale`。PNG 始终按 `panels/<tile_id>.png` 定位，因此 CSV 中 `tile_id` 的编号可以跳跃、不连续，也不要求与目录中的全部图片一一覆盖。

## 标注与保存

- 鼠标滚轮缩放；按住空格再左键拖动，或用鼠标中键拖动平移图片。工具栏也提供适应窗口和 100%。
- `←` / `→` 切图时，当前行会先强制保存；保存失败就不会切图。
- 标签或 notes 修改后自动保存，也可点击“立即保存”。每次写入先生成 `.annotation_history/` 中的原始快照，再以临时文件加原子替换方式更新 CSV；审计记录保存在同一目录的 `audit.jsonl`。
- CSV 中的额外列、行顺序和 `tile_id` 会保留。空标签代表“未完成”，交付前请用“下一张未完成”检查。

## 更新 `label_set.csv`

网页内的“更新 label_set”会重新读取本地文件，不会修改已有标注。配置列为：

```csv
label,description,multi_selectable,exclusive,allowed_with
thin_cloud,半透明云,true,false,thick_cloud|haze_cirrus
```

- `label` 是要写入 `label_set` 的值，必填且唯一。
- `description` 显示在网页右栏，可为空。
- `multi_selectable` 控制该标签能否参与双标签，默认 `true`。
- `exclusive=true` 的标签必须单独使用。
- `allowed_with` 用 `|` 分隔可组合的其他标签；留空表示允许与所有非独占、可复选标签组合。双向配置必须都允许才可选。

当前模板依据 `PHASE61D2_REVIEW_MANUAL.md` 配置了 `clear`、`thin_cloud`、`thick_cloud`、`cloud_shadow`、`terrain_water_shadow`、`haze_cirrus` 等标签及保守的双标签组合。若作业词表变动，先编辑并保存 `label_set.csv`，再点击网页“更新 label_set”。

## 验证

```powershell
python -m unittest discover -s tests -v
```
