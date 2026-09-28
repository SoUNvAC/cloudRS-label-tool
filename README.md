# CloudRS 本地图片标注工具

这是一个仅监听本机地址（`127.0.0.1`）的 Python Web 标注工具。它把一个 CSV 交付文件绑定到一次运行中，显示 `panels/<tile_id>.png`，并在每次编辑和切换图片时使用原子写入保存。

## 启动

需要 Python 3.10 或更新版本；无需安装第三方依赖。在本目录运行：

```powershell
python app.py --csv reviewer_A_main.csv --open-browser
```

校准和另一位 reviewer 应在独立运行中指定各自的文件：

```powershell
python app.py --csv reviewer_A_calibration.csv --open-browser
python app.py --csv reviewer_B_main.csv --open-browser
```

终端会显示浏览器地址。按 `Ctrl+C` 停止服务。请勿同时在两个工具实例中编辑同一个 CSV。

## 合并两位 reviewer 的校准结果

评审团使用合并模式读取两份独立的 reviewer 结果，并只写入最终共识文件：

```powershell
python app.py --merge --reviewer-a reviewer_A_calibration.csv --reviewer-b reviewer_B_calibration.csv --consensus calibration_consensus.csv --open-browser
```

页面中央仍为当前 `tile_id` 的 PNG。右栏先以只读卡片展示 Reviewer A 与 Reviewer B 的 `label_set` 和 notes，并明确提示两人的标签是否一致；评审团再填写最终 `agreed_label_set` 和 `rule_or_counterexample`。

- 只会更新 `calibration_consensus.csv`，两份 reviewer CSV 在合并模式下绝不会被写入。
- 两份 reviewer CSV 与 consensus CSV 的 `tile_id`、行数和顺序必须完全一致；不一致时工具拒绝启动或保存，避免错误合并。
- 每次加载当前图片时，若 A/B 的非空标签集合一致且最终共识仍为空，会自动勾选并保存该标签；已有最终共识绝不自动覆盖。
- 最终共识沿用原有自动保存、切图前强制保存、原子替换、快照和审计日志机制。

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
