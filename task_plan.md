# Task Plan: 013.02 轻量化重构实施

## Goal
实现一条轻量但完整的 `query -> B站采集 -> 并行下载 -> 切分/过滤 -> 并行 LLM 标注 -> 导出` 流水线，并将外部参考数据冻结为本地 JSON。

## Current Phase
Phase 10

## Phases

### Phase 1: Architecture Skeleton
- [x] 恢复 `src/group_emotion_video` 主模块结构
- [x] 建立 CLI、配置加载、runtime 目录和 SQLite 索引
- [x] 明确 `prepare-reference` 到 `export` 的工作流入口
- **Status:** complete

### Phase 2: Reference & Schema
- [x] 解析 Excel 生成 `query_seed_catalog.json`
- [x] 冻结 annotation domains 到 `annotation_domains.json`
- [x] 合并本地 `group_emotion` 标签种子
- **Status:** complete

### Phase 3: Acquisition & Preprocessing
- [x] 实现 B 站搜索、enrich、并行下载
- [x] 实现字幕切分 / 固定窗口切分
- [x] 实现 L1/L2 过滤和文件清理策略
- **Status:** complete

### Phase 4: Annotation & Export
- [x] 实现全局 LLM 并发闸门
- [x] 实现 `013.01` 兼容标注输出与值域校验
- [x] 实现 accepted clip 导出
- **Status:** complete

### Phase 5: Verification & Docs
- [x] 补充单元测试和离线 E2E 测试
- [x] 修复 SQLite 线程安全问题
- [x] 更新 README、实现文档和进度文件
- **Status:** complete

### Phase 7: Status Metrics & Throughput Estimation
- [x] 为下载 / 预处理 / 标注补充时延落库字段
- [x] 扩展 `status` 输出视频、clip、标注和平均耗时统计
- [x] 基于当前均值和 worker 配置增加 5 天产能估算
- **Status:** complete

### Phase 8: Continuous Service Mode
- [x] 新增 `download-loop` 独立常驻下载命令
- [x] 新增 `pipeline-loop` 预处理/标注联动流水线
- [x] 补充完整常驻运行 profile 与 README
- **Status:** complete

### Phase 9: Query Auto Refill
- [x] 为 query 增加 `run_count`，支持限制最大重跑轮次
- [x] 让 `download-loop` 在空库时自动 seed，在 `pending` 用完时自动 recycle `done`
- [x] 更新 README 与测试覆盖自动补种 / 自动回填
- **Status:** complete

### Phase 10: Retrieval / Filter Diagnostics
- [x] 检查 query 生成与 search normalization 的真实形态
- [x] 检查 preprocess L1/L2 reject 条件和 accepted 写库逻辑
- [x] 根据当前诊断落地 query / filter / status 的低风险修正
- **Status:** complete

## Key Decisions
| Decision | Rationale |
|----------|-----------|
| reference 只准备一次，运行期不再读 Excel | 避免重复 IO 和外部依赖 |
| 单 SQLite + JSON 工件 | 足够轻，且可溯源 |
| raw video 临时保留，视频处理后删除 | 降低存储压力 |
| 下载和 LLM 并发都配置化 | 便于控制吞吐和成本 |
| accepted 样本必须严格过 schema | 保持和 `013.01` 可兼容、可导出 |

## Remaining Work
- 扩大真实网络环境下的 B 站抓取样本，验证字幕获取稳定性
- 在真实 Yunwu/Gemini 配置上继续做多样本联网冒烟
- 根据首轮数据分布微调 `group_emotion` label catalog 与过滤阈值
- 清理或归档 smoke 运行中的失败工件历史
- 让真实在线运行积累更多耗时样本，提升 5 天产能估算的稳定性
- 在真实 VLLM 环境下验证 `annotation_trigger_size=100` 的批触发吞吐与延迟权衡
- 根据真实数据分布调优 `query_recycle_cooldown_sec` 和 `max_runs_per_query`
- 基于真实 reject reason 分布调优 query 生成策略和 L2 过滤强度
- 基于 `status["annotations"]["top_quality_flags"]` 调整 schema 提示词或 `group_emotion` 标签池，解决 accepted clip 全部 `failed` 的问题
