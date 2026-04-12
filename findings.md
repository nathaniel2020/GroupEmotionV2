# Findings & Decisions

## Implemented Findings

- 当前主链路已恢复为完整流程，不再是旧版仅标注链路。
- Excel 参考源已收敛为一次性解析，运行期只消费本地 `query_seed_catalog.json`。
- 标注 schema 已冻结为本地 `annotation_domains.json`，并单独补入 `group_emotion` 标签池。
- B 站采集采用“搜索串行、enrich 并行、下载并行”的轻量并发模型。
- clip 预处理后，raw source video 会被清理；rejected clip 的视频和派生工件不会保留。
- 标注输出已经与 `013.01` 的核心 runtime 形态兼容，并额外做了严格值域约束。
- `status` 已扩展为可直接读吞吐的总览接口，会返回视频 / clip / 标注计数、阶段平均耗时，以及基于当前均值和 worker 的 5 天产能估算。
- 在线运行模式已切成两个常驻命令：`download-loop` 专管下载，`pipeline-loop` 负责预处理和批量标注联动。
- `download-loop` 现在支持 query 自动补种和自动回填，不再要求队列耗尽后手动执行 `seed-queries`。

## Code-Level Decisions

| Decision | Rationale |
|----------|-----------|
| 用单个 `runtime/index.sqlite` 管索引和状态 | 简化实现，避免多库管理成本 |
| 用 JSON 工件保留元信息、切分、拒绝原因和标注结果 | 保持样本级可溯源 |
| LLM 并发通过全局 `BoundedSemaphore` 控制 | 防止不同 stage 各自失控并发 |
| `openai`、`requests`、`yt-dlp` 均做懒加载 | 离线测试无需真实联网依赖 |
| `clip_export_mode` 支持 `copy` 和 `ffmpeg` | 便于测试和生产两种运行模式 |
| SQLite 改为 `check_same_thread=False + RLock + WAL` | 修复预处理线程池下的并发访问问题 |
| 下载 / 预处理 / 标注耗时直接写入 SQLite 主表 | 让 `status` 无需扫日志或工件时间戳就能聚合真实吞吐 |
| 不做单一总进度条，改做阶段快照日志 | 因为总视频数和总 clip 数在动态变化，单百分比会误导 |
| query 调度采用 `run_count + cooldown` 的 recycle 机制 | 避免 query 一次跑完后只能靠人工重新补种 |

## Validation Findings

- `prepare-reference` 可稳定生成 query seed catalog 和 annotation domains。
- 值域校验可拦截 enum 越界、数值越界和条件必填缺失。
- LLM 并发闸门能限制真实 in-flight 请求数。
- 离线 E2E 已验证从 seed row 到 accepted clip 导出的一条完整链路。
- 在线 smoke 已验证从真实 B 站搜索、下载、切分、过滤、在线标注到导出的完整闭环。
- 真实 Excel 中存在 `外部诱因空间 (X_ext)` 这类目录行，必须在 reference 准备阶段显式跳过。
- 直接把抽象场景名原样送到 B 站搜索，召回质量很差；需要做确定性的 search normalization，优先展开括号中的具体场景词和事件词。
- 原始 L2 `weak_emotion_signal` 启发词过窄，像“师生教室翻唱”这类明显群体情绪场景会被误拒；补入 `翻唱/合唱/演唱` 后通过。
- `gemini-3.1-pro-preview + 图片输入` 在这个 smoke clip 上超时；`gemini-3.1-flash-lite-preview + 文本优先` 能在可接受时间内跑通。
- 当前 `build_query_seed_catalog()` 仍会为每个 seed 固定生成 `scene_text` 和 `scene_text + 现场` 两类宽 query；对很多 seed 来说，这两类 query 缺少事件约束，容易把召回拉向泛场景视频。
- 当前 `_window_text()` 在字幕存在时只使用字幕文本，不会把 `title/description/tags/scene_text/trigger_text` 一并纳入 L2 判断；真实视频若字幕是对白型文本，就很容易在 `_l2_filter()` 中被 `weak_group_signal` / `weak_emotion_signal` 直接拒掉。
- 当前 `_l2_filter()` 是硬规则：group hint 至少命中一个、emotion hint 至少命中一个，否则直接 rejected；`use_llm_filter=false` 时没有灰区放行或二次复核路径，因此线上很容易出现 accepted 为 0 的极端结果。
- 已修正 `build_search_query()`：当 query 是纯 `scene_text` 时，若该 seed 有 trigger，则会自动把 trigger 约束一并带入搜索词，避免“只搜教室/礼堂/操场”这类宽召回。
- 已修正 `_l2_filter()`：字幕存在时不再只看字幕，而是把字幕与视频元信息联合判断；对白型字幕不会再把本来明显带有群体/情绪信号的样本整批误拒。
- 已扩展 `ClipRepository.summary()`：`status["clips"]` 现在会直接返回 `top_rejection_reasons`，便于在线判断拒绝是否主要集中在 `weak_group_signal` / `weak_emotion_signal`。
- 真实线上统计显示 `weak_emotion_signal` 仍然远高于其他 reject 原因，说明主要瓶颈已经从“字幕/元信息只看一边”收敛为“词表覆盖不足”。
- 已继续扩展 L2 群体/情绪词表，补入更接近真实 query 和 B 站标题的事件词，如 `表扬/荣誉/获奖/淘汰/拒稿/录取/冲突/争吵/庆祝/鼓掌` 等。
- 已扩展 `AnnotationRepository.summary()`：`status["annotations"]` 现在会返回 `top_quality_flags`，便于直接判断 `failed` 是死在 `schema_invalid_enum`、`low_confidence` 还是其他校验。
- 已确认 `VideoRepository.summary()` 里 `downloading` 统计条件此前写错成了 `download_status='downloaded'`；实际下载中记录写的是 `download_status='downloading'`，这会导致 `download-loop` 日志长期显示 `downloading: 0`。
- 已把 `download-loop` 快照改成下载侧专用视图，不再混入 `clips/annotations` 与预处理/标注均时；同时为 `crawl()` 增加单 query 摘要日志，直接区分“真下载慢”和“重复命中旧视频导致空转”。

## Known Gaps

- `group_emotion` 的冻结标签池目前是工程种子版，后续仍可能根据首批数据继续收敛。
- 当前只在线跑通了 1 个 `done` 样本，样本规模还不足以评估整体召回和标注稳定性。
