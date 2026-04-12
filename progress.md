# Progress Log

## Session: 2026-04-12

### Phase 1: Core Reconstruction
- **Status:** complete
- Actions taken:
  - 重建配置加载、CLI、runtime 目录和 SQLite schema。
  - 新增 `Workflow`，统一管理 `prepare-reference` 到 `export` 的阶段调用。
  - 恢复项目的可安装入口 `group-emotion-video`。

### Phase 2: Reference Preparation
- **Status:** complete
- Actions taken:
  - 实现 Excel 一次性解析并写出 `data/reference/query_seed_catalog.json`。
  - 实现 annotation schema 冻结并写出 `data/reference/annotation_domains.json`。
  - 新增 `configs/group_emotion_labels.seed.json` 作为 `group_emotion` 初始标签池。

### Phase 3: Acquisition / Preprocessing / Annotation
- **Status:** complete
- Actions taken:
  - 实现 B 站搜索、元信息 enrich、并行下载和拒绝记录。
  - 实现字幕聚合切分、固定窗口切分、L1/L2 过滤、关键帧抽取和 clip manifest。
  - 实现单模型标注、全局 LLM 并发闸门、值域约束校验和导出逻辑。

### Phase 4: Verification
- **Status:** complete
- Actions taken:
  - 新增 reference、validation、LLM gate、workflow 四组测试。
  - 执行 `python3 -m compileall src/group_emotion_video`。
  - 执行 `python3 -m pytest -q`。
  - 修复预处理线程池下 SQLite 连接线程安全问题。

### Phase 5: Documentation Sync
- **Status:** complete
- Actions taken:
  - 更新 `README.md`，补充运行方式、依赖和目录约定。
  - 重写 `docs/轻量化重构方案.md`，使其与当前实现一致。
  - 更新 `task_plan.md`、`findings.md`、`progress.md`。

### Phase 6: Online Smoke
- **Status:** complete
- Actions taken:
  - 新增隔离 profile `configs/profiles/online_smoke.yaml`，将在线 smoke 的 data/runtime/reference 分开。
  - 修复 `scripts/run_pipeline.py` 直接运行时找不到 `src` 包路径的问题。
  - 修复真实 Excel 目录行 `外部诱因空间 (X_ext)` 被错误写入 query seed 的问题。
  - 为 B 站查询新增 search normalization，优先展开括号内的具体场景词与事件词。
  - 基于真实 B 站搜索选择 `固定座位空间（教室/考场） 现场` 作为 smoke query，成功下载视频 `BV1ZGX3BtEwN`。
  - 预处理阶段补强情绪启发词，使“师生教室翻唱”类群体情绪场景不再被 `weak_emotion_signal` 误拒。
  - 首次在线标注在 `gemini-3.1-pro-preview` 上超时；切换到 `gemini-3.1-flash-lite-preview` 且使用文本优先后成功完成标注。
  - 成功导出 1 个 accepted clip 到 `runtime/online_smoke/exports/dataset_export_20260412_194157/`。

### Phase 7: Status Metrics & Estimation
- **Status:** complete
- Actions taken:
  - 为 `videos` / `annotations` 表补充下载、预处理、标注的开始时间、结束时间和耗时字段，并对已有 SQLite 自动迁移。
  - 扩展 `Workflow.status()`，输出已下载视频数、待预处理视频数、CLIP 总数、待标注 CLIP 数、DONE/Reject/failed 计数和阶段平均耗时。
  - 新增基于当前平均耗时、accepted clip 产出率和 worker 配置的 `projection_5d` 估算。
  - 更新离线 E2E 测试，覆盖新的 `status` 结构和关键统计字段。

### Phase 8: Continuous Service Mode
- **Status:** complete
- Actions taken:
  - 新增 CLI 子命令 `download-loop` 和 `pipeline-loop`，支持把下载与预处理/标注拆成两个常驻进程。
  - 下载阶段增加 `download_start / download_progress / download_finish` 日志，并在 SQLite 中先写入 `downloading` 状态。
  - 预处理与标注改为联动流水线：accepted clip 进入待标注队列，达到阈值或超时后派发并行标注。
  - 为 clips 增加创建时间、`annotating` 状态和队列年龄统计，便于做动态快照日志和批触发判断。
  - 新增完整 profile `configs/profiles/continuous_vllm_pipeline.yaml`，并重写 README 的服务模式文档。

## Test Results

| Test | Result |
|------|--------|
| `python3 -m compileall src/group_emotion_video` | passed |
| `python3 -m pytest -q` | `6 passed` |
| `python3 -m pytest -q` | `8 passed` |
| `python3 -m pytest -q` | `9 passed` |
| `python3 -m pytest -q tests/test_workflow.py` | passed |
| 在线 smoke | `prepare-reference -> seed-queries -> crawl -> preprocess -> annotate -> export` | passed |

## Error Log

| Timestamp | Error | Resolution |
|-----------|-------|------------|
| 2026-04-12 | `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread` | 将 SQLite 连接改为 `check_same_thread=False`，并加 `RLock` 与 WAL 模式 |
| 2026-04-12 | `scripts/run_pipeline.py` 直接运行时 `ModuleNotFoundError: group_emotion_video` | 在脚本入口显式注入 `src` 到 `sys.path` |
| 2026-04-12 | 真实 Excel 目录行被错误转成 query | 在 reference 准备阶段新增 catalog header 跳过规则 |
| 2026-04-12 | 真实 B 站搜索对抽象 query 召回差 | 增加 outbound search normalization |
| 2026-04-12 | 在线标注请求超时 | 切换到 `gemini-3.1-flash-lite-preview` 并将 smoke 改为文本优先 |

## Current Status

- 实现已落地。
- 离线测试已通过。
- 首轮真实联网 smoke 已跑通。
- `status` 已能直接作为 5 天产能估算入口。
- 服务模式已支持双进程常驻运行和动态快照日志。
- 下一步应扩大在线 smoke 样本数，并继续微调 query/search/filter 策略。
