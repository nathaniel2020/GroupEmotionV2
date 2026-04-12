# 群体情感视频轻量化流水线

当前仓库已经按 `013.02` 方案落地为一条轻量但完整的生产链路：

`prepare-reference -> seed-queries -> crawl -> preprocess -> annotate -> export`

当前推荐的在线运行模式是两进程：

- `download-loop`：单独负责 query 消费、B 站检索、enrich、下载，并持续打印下载日志
- `pipeline-loop`：负责“预处理 -> accepted clip 队列 -> 并行标注”的联动流水线

核心约束：

- 运行期只读取本地 reference JSON，不再重复解析 Excel。
- B 站采集支持并行 enrich 和并行下载。
- LLM 调用支持全局并发闸门，所有 stage 共用同一上限。
- 标注输出兼容 `013.01` 形态：`agent_outputs`、`final_annotation`、`field_confidence`、`quality_flags`。
- raw source video 只做临时文件，处理完成后删除；accepted clip 保留，rejected clip 删除。

## 环境要求

- Python `>=3.11`
- `ffmpeg`
- 网络可访问 B 站和所配置的大模型服务
- 若启用 LLM：设置环境变量 `YUNWU_API_KEY`、`OPENAI_API_KEY` 或对应 `llm.api_key_env`

安装：

```bash
pip install -e .
```

## 配置

基础配置在 [configs/base.yaml](configs/base.yaml)。

默认包含：

- Excel 种子来源：`原子情感因素.xlsx`
- schema 来源：`013.01 video/doc/标注字段.xlsx`
- B 站下载并发：`crawl.download.workers`
- 预处理并发：`preprocessing.workers`
- clip 标注并发：`annotation.clip_workers`
- 全局 LLM 请求并发：`llm.parallel.max_inflight_requests`
- 服务循环参数：`service.download_loop.*`、`service.pipeline_loop.*`
- query 自动补种 / 回填参数：`service.download_loop.auto_seed_if_empty`、`auto_refill_done_queries`、`query_recycle_cooldown_sec`、`max_runs_per_query`

可用 profile：

- 烟雾测试：[configs/profiles/gemini_yunwu_smoke.yaml](configs/profiles/gemini_yunwu_smoke.yaml)
- 隔离在线 smoke：[configs/profiles/online_smoke.yaml](configs/profiles/online_smoke.yaml)
- 双进程常驻运行完整示例：[configs/profiles/continuous_vllm_pipeline.yaml](configs/profiles/continuous_vllm_pipeline.yaml)

## 初始化

先准备 reference：

```bash
python scripts/run_pipeline.py prepare-reference
```

如果配置里的源文件路径不对，也可以手动指定 `prepare-reference` 的输入/输出路径：

```bash
python scripts/run_pipeline.py prepare-reference \
  --excel-path /path/to/原子情感因素.xlsx \
  --sheet-name 情感因素 \
  --schema-source-path /path/to/标注字段.xlsx \
  --label-seed-path configs/group_emotion_labels.seed.json \
  --query-seed-catalog-path data/reference/query_seed_catalog.json \
  --annotation-domains-path data/reference/annotation_domains.json
```

如果你想手动初始化 query，可以执行：

```bash
python scripts/run_pipeline.py seed-queries
```

但在当前默认配置下，`download-loop` 启动时已经支持：

- 当 `queries` 表为空时自动 `seed-queries`
- 当 `pending/retry` 用完时，自动把满足冷却条件的 `done` query 回收成新的 `pending`

## 本地视频入库

如果你已经有一批本地视频，不想再走 `crawl/download`，可以直接把视频注册进 `runtime/.../index.sqlite`，后续就能直接调用 `preprocess` / `pipeline-loop`。

如果来源是 `013.01` 的完整 runtime，优先使用下面这个专用命令，而不是通用的 `register-local-videos`。

### 导入 `013.01` runtime

专用导入命令：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  import-01301-runtime \
  --legacy-runtime-root /mnt/data_disk/home/liuhaoyu/GroupEmotion/runtime
```

默认行为：

- 从 `/mnt/.../runtime/artifacts/raw_videos/bilibili` 扫描原始视频
- 显示终端进度条
- 按 `move` 语义把视频转移到当前 `013.02` runtime 的 `tmp/videos/...`
- 优先尝试读取 `--legacy-runtime-root` 下的 `db/keyword_index.db` 和 `db/video_meta.db`
- 恢复 `bilibili` / `BVID` / `url` / `title` / `query` 关联等元信息
- 在当前 `runtime/.../index.sqlite` 中写成与 `013.02` 自己下载产物同一套状态机：`download_status='downloaded'`

常用参数：

- `--raw-root`：如果原始视频不在默认位置，手动指定扫描根目录
- `--limit N`：先做小批量试跑
- `--replace-existing`：覆盖已导入记录
- `--no-progress`：关闭进度条

导入后直接处理：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  pipeline-loop
```

说明：

- 这里不需要 `--tag 群体 --tag emotion`。
- 那两个 `--tag` 只是给通用本地导入器补文本提示，帮助无字幕视频通过预处理的弱规则过滤，不属于规范导入流程。
- 例如当你传入 `--legacy-runtime-root /mnt/data_disk/home/liuhaoyu/GroupEmotion/runtime` 时，导入器读取的是：
  `/mnt/data_disk/home/liuhaoyu/GroupEmotion/runtime/db/keyword_index.db`
  和
  `/mnt/data_disk/home/liuhaoyu/GroupEmotion/runtime/db/video_meta.db`
- 如果源目录和当前 runtime 不在同一个文件系统，底层无法做原子 `rename`；`move` 语义会退化成“复制成功后删除源文件”，这是操作系统限制，不是导入器绕过了 `move`。

推荐先准备 reference，这样命令会优先用 `query_seed_catalog.json` 去匹配场景目录名：

```bash
python scripts/run_pipeline.py prepare-reference
```

如果你的目录组织类似 `013.01` 风格，例如：

```text
/path/to/local_batch/
  教室/
    xxx.mp4
    yyy.mp4
  演唱会现场/
    zzz.mp4
```

可以直接执行：

```bash
python scripts/run_pipeline.py register-local-videos \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  --input-root /path/to/local_batch
```

这个命令会：

- 递归扫描视频文件
- 尝试用一级子目录名匹配 reference 里的 `scene_text`
- 把原视频复制到当前 runtime 的 `tmp/videos/local_import/...`
- 在 `runtime/.../index.sqlite` 的 `queries` / `videos` 表里补齐记录
- 把这些视频标记为 `downloaded`，供 `preprocess` 或 `pipeline-loop` 继续消费

如果你的文件都在一个平铺目录下，没有场景子目录，可以手动指定：

```bash
python scripts/run_pipeline.py register-local-videos \
  --input-root /path/to/flat_videos \
  --scene-text "教室" \
  --trigger-text "公开正面能力认可" \
  --tag 群体 \
  --tag emotion
```

可选参数：

- `--transfer-mode copy|hardlink|symlink|move`：默认 `copy`。建议保留默认值，避免后续预处理删除原始库里的文件。
- `--replace-existing`：如果同一批本地视频之前已经注册过，用它覆盖旧记录并重新 staging。
- `--default-duration-sec`：`ffprobe` 拿不到时用这个兜底。
- `--tag ...`：会写入视频 tags。对没有字幕、文件名也缺少语义信息的本地视频，建议显式加上如 `群体`、`emotion` 这类提示词，避免预处理阶段被弱信号过滤。

入库后可以直接跑：

```bash
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml preprocess
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml annotate
```

或者直接让常驻流水线接管：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  pipeline-loop
```

## 单阶段运行

按阶段手动执行：

```bash
python scripts/run_pipeline.py crawl
python scripts/run_pipeline.py preprocess
python scripts/run_pipeline.py annotate
python scripts/run_pipeline.py export
python scripts/run_pipeline.py status
```

`status` 会输出 query / video / clip / annotation 的计数、下载/预处理/标注平均耗时，以及基于当前均值和 worker 配置的 `projection_5d` 估算。

也可以一次串行执行：

```bash
python scripts/run_pipeline.py run --steps prepare-reference,seed-queries,crawl,preprocess,annotate,export,status
```

叠加 profile：

```bash
python scripts/run_pipeline.py --config configs/profiles/gemini_yunwu_smoke.yaml run --steps prepare-reference,seed-queries,crawl,preprocess,annotate
```

## 双进程服务模式

推荐开两个终端。

终端 1，下载进程：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  download-loop
```

终端 2，预处理 + 标注流水线：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  pipeline-loop
```

`download-loop` 的特点：

- 下载是单独进程，适合长期运行
- 如果 reference JSON 不存在，会先自动执行一轮 `prepare-reference`
- 每个视频会打印 `download_start / download_progress / download_finish`
- 循环快照里会看到 `downloading / downloaded / pending_preprocess`
- 当 `queries` 表为空时可自动补种
- 当 `pending` 用完时可自动 recycle 已完成 query，不需要手动重新 `seed-queries`
- `max_queries_per_cycle: 0` 表示每轮尽可能多抓，不人为限制 query 批次

如果两台机器不能通信，但你希望它们尽量多抓视频，同时避免重复跑同一批 query，应该直接做静态 query 分片：

```yaml
crawl:
  query_shard:
    count: 2
    index: 0
```

两台机器分别配置：

- 机器 A：`count: 2, index: 0`
- 机器 B：`count: 2, index: 1`

当前默认连续运行 profile 只开 `query_shard`，不再默认启用 `video_shard`。

- `query_shard`：把 query 队列拆开，避免两台机器重复跑同一批 query，同时尽量扩大整体 query 覆盖面
- `video_shard`：如果你更在意跨机去重而不是拉高下载量，可以手动重新打开；代价是很多 query 命中会被直接裁掉，单机 `queued_for_download` 会明显下降

这样两边都会基于同一份 `query_seed_catalog.json` 自动补种，但每台机器只会导入和消费自己那一片 `query_id`。这种配置更适合“先尽量多下载，再靠后续过滤挑样本”的场景。

如果是 3 台机器，就设 `count: 3`，然后三台机器的 `index` 分别为 `0`、`1`、`2`。

`pipeline-loop` 的特点：

- 如果 reference JSON 不存在，会先自动执行一轮 `prepare-reference`
- 预处理和标注不是两个完全割裂的阶段，而是联动流水线
- accepted clip 会先进入待标注队列
- 当待标注 clip 达到 `annotation_trigger_size`，或最老待标注 clip 等待超过 `annotation_trigger_timeout_sec`，就会触发并行标注
- 如果待标注队列达到 `queue_max_clips`，会对预处理形成反压，避免 clip 无限堆积

这里不做“总百分比进度条”，因为总视频数和总 clip 数都在动态变化。更合适的是动态快照日志。

## 关键配置

下载侧：

- `crawl.download.workers`：下载并发
- `crawl.download.max_inflight_per_host`：单 host 并发上限
- `crawl.retry.max_attempts`：B 站 412 / 429 / timeout 等瞬时错误的最大尝试次数
- `crawl.retry.base_sleep_sec` / `max_sleep_sec` / `backoff_factor` / `jitter_sec`：失败后的退避 sleep 策略
- `sources.bilibili.cookie_env`：从环境变量读取 B 站 cookie，默认读取 `BILIBILI_COOKIE`
- `service.download_loop.max_queries_per_cycle`：每个 loop 最多消费多少 query
- `service.download_loop.max_queries_per_cycle: 0`：表示不限制，当前能跑多少就跑多少
- `service.download_loop.auto_seed_if_empty`：空库时自动 `seed-queries`
- `service.download_loop.auto_refill_done_queries`：`pending/retry` 用完后是否自动回填已完成 query
- `service.download_loop.refill_batch_size`：每次回填多少条 query
- `service.download_loop.refill_batch_size: 0`：表示本轮尽可能多回填
- `service.download_loop.query_recycle_cooldown_sec`：query 重新进入队列前的冷却时间
- `service.download_loop.max_runs_per_query`：每条 query 最多允许跑多少轮，`0` 表示无限制
- `service.download_loop.poll_interval_sec`
- `service.download_loop.idle_sleep_sec`

流水线侧：

- `preprocessing.workers`：并行预处理视频数
- `annotation.clip_workers`：并行标注 clip 数
- `llm.parallel.max_inflight_requests`：并行发给 VLLM / OpenAI-compatible 服务的请求上限
- `service.pipeline_loop.preprocess_claim_limit`：每轮 claim 多少视频进入预处理
- `service.pipeline_loop.annotation_trigger_size`：待标注 clip 达到多少开始派发
- `service.pipeline_loop.annotation_batch_size`：每次派发多少 clip 给标注 worker
- `service.pipeline_loop.annotation_trigger_timeout_sec`：待标注队列没攒够也最多等多久
- `service.pipeline_loop.queue_max_clips`：待标注队列上限，超过后会暂停继续 claim 新视频
- `service.pipeline_loop.preprocess_claim_limit: 0`：表示只要有空闲预处理 worker 就持续吃视频
- `service.pipeline_loop.annotation_trigger_size: 0`：表示只要有 accepted clip 就立即派发标注
- `service.pipeline_loop.annotation_batch_size: 0`：表示每轮尽量把空闲标注 worker 填满
- `service.pipeline_loop.queue_max_clips: 0`：表示不设待标注队列上限

当前完整示例 profile 默认就是“download 尽可能多抓 + pipeline 只要有视频/clip就处理 + VLLM 并行请求”的配置。

如果 B 站开始返回 `HTTP Error 412: Precondition Failed`，优先做两件事：

- 降低 `crawl.download.max_inflight_per_host`
- 增大 `crawl.retry.base_sleep_sec`

如果需要带登录态抓取，不要把明文 cookie 提交进 Git。当前代码会自动读取环境变量：

```bash
export BILIBILI_COOKIE='你的完整 cookie'
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml download-loop
```

## `status` 解读

`python scripts/run_pipeline.py status` 会返回：

- `queries`：`pending / retry / done`
- `videos`：`downloading / downloaded / pending_preprocess / preprocessing / processed`
- `clips`：`accepted / rejected / pending_annotation / annotating`
- `annotations`：`done / rejected / failed / completed`
- `average_durations_sec`：下载、预处理、标注平均耗时
- `projection_5d`：基于当前均值、accepted clip 产出率和 done rate 的 5 天估算

如果库里样本还很少，`projection_5d` 会比较抖；先积累几轮真实运行数据再看更有意义。

## 目录约定

- `data/reference/`
  - `query_seed_catalog.json`
  - `annotation_domains.json`
- `runtime/index.sqlite`
- `runtime/artifacts/videos/`
- `runtime/artifacts/clips/`
- `runtime/artifacts/annotations/`
- `runtime/artifacts/rejections/`
- `runtime/exports/`

## 当前实现范围

- 已实现离线可测的 reference 准备、query 入库、B 站候选采集、并行下载、切分/过滤、并行标注、导出。
- 已实现双 loop 服务模式：`download-loop` 和 `pipeline-loop`。
- 标注字段值域做了严格校验，accepted 样本必须通过 schema 检查。
- 测试已覆盖 reference 生成、值域校验、LLM 并发闸门、离线 E2E、服务模式 loop。
- 已完成一轮真实在线 smoke：B 站抓取、下载、切分、在线标注、导出都已跑通。

当前未覆盖：

- 多 query、多视频规模下的在线稳定性和吞吐压测尚未完成。
- `ffmpeg` 缺失时，`clip_export_mode=ffmpeg` 无法工作；测试里使用的是 `copy` 模式。

## 仓库清洁约定

- `runtime/`、`data/reference/*.json`、缓存目录和本地 smoke 运行产物都不应提交。
- 需要复现 smoke 时，直接使用 profile 重新跑，不保留历史运行目录。
