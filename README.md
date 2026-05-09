# 群体情感视频轻量化流水线

当前仓库按 `013.02` 方案落地为轻量但完整的生产链路。推荐运行方式是“双进程服务模式”：

1. 先执行一次 `prepare-reference`
2. 终端 1 常驻运行 `download-loop`
3. 终端 2 常驻运行 `pipeline-loop`
4. 需要交付数据集时再执行 `export`

`download-loop` 负责 query 消费、B 站检索、enrich、下载；`pipeline-loop` 负责“预处理 -> accepted clip 队列 -> 并行标注”的联动流水线。

核心约束：

- 运行期只读取本地 reference JSON，不再重复解析 Excel。
- B 站采集支持并行 enrich 和并行下载。
- LLM 调用支持全局并发闸门，所有 stage 共用同一上限。
- 标注输出兼容 `013.01` 形态：`agent_outputs`、`final_annotation`、`field_confidence`、`quality_flags`；标注执行层支持候选标注与可选 Judge 裁决。
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
- 候选标注与裁决：`annotation.candidate_models`、`annotation.judge.enabled`
- 候选模型默认继承 `llm.*`；需要单独服务配置时，在候选项或 `annotation.judge` 里覆盖 `model/base_url/api_key_env/timeout_sec/temperature/max_images_per_prompt`
- 全局 LLM 请求并发：`llm.parallel.max_inflight_requests`
- 服务循环参数：`service.download_loop.*`、`service.pipeline_loop.*`
- query 自动补种 / 回填参数：`service.download_loop.auto_seed_if_empty`、`auto_refill_done_queries`、`query_recycle_cooldown_sec`、`max_runs_per_query`

候选模型不再按 `agent` 拆分；每个候选模型都会独立完成完整标注。最简写法是直接列模型名：

```yaml
annotation:
  candidate_models:
    - gemma-4-26b-a4b-it
    - qwen3.5-35b
    - internvl3_5-30b-a3b-hf
```

如果不同模型跑在不同 OpenAI-compatible 服务上，写成对象并覆盖连接参数：

```yaml
annotation:
  candidate_models:
    - name: gemma
      model: gemma-4-26b-a4b-it
      base_url: http://127.0.0.1:8101/v1
      api_key_env: OPENAI_API_KEY
    - name: qwen
      model: qwen3.5-35b
      base_url: http://127.0.0.1:8102/v1
      api_key_env: OPENAI_API_KEY
  judge:
    enabled: true
    model: qwen3.5-35b
    base_url: http://127.0.0.1:8102/v1
```

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

## 双进程服务模式

这是当前推荐的主运行方式。开两个终端，并统一使用 [configs/profiles/continuous_vllm_pipeline.yaml](configs/profiles/continuous_vllm_pipeline.yaml)。

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

通常不需要手动跑 `crawl`、`preprocess`、`annotate`。`download-loop` 会持续补 query、抓候选、下载视频；`pipeline-loop` 会持续消费已下载视频，完成切片、过滤和并行标注。

`download-loop` 的特点：

- 如果 reference JSON 不存在，会先自动执行一轮 `prepare-reference`
- 每个视频会打印 `download_start / download_progress / download_finish`
- 循环快照里会看到 `downloading / downloaded / pending_preprocess`
- 当 `queries` 表为空时可自动补种
- 当 `pending` 用完时可自动 recycle 已完成 query
- `max_queries_per_cycle: 0` 表示每轮尽可能多抓，不人为限制 query 批次

`pipeline-loop` 的特点：

- 如果 reference JSON 不存在，会先自动执行一轮 `prepare-reference`
- 预处理和标注是联动流水线，不是两个割裂阶段
- accepted clip 会先进入待标注队列
- 达到 `annotation_trigger_size` 或等待超过 `annotation_trigger_timeout_sec` 后触发并行标注
- 待标注队列达到 `queue_max_clips` 时会对预处理形成反压

这里不做“总百分比进度条”，因为总视频数和总 clip 数都在动态变化。更合适的是动态快照日志。

多机运行时，如果机器之间不能通信但要避免重复跑同一批 query，可以做静态 query 分片：

```yaml
crawl:
  query_shard:
    count: 2
    index: 0
```

两台机器分别配置：

- 机器 A：`count: 2, index: 0`
- 机器 B：`count: 2, index: 1`

如果是 `N` 台机器，就设 `count: N`，然后每台机器各自使用唯一的 `index`。

## 辅助命令

常规生产运行以 `download-loop` + `pipeline-loop` 为准。下面这些命令主要用于查看状态、导出和人工复核：

```bash
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml status
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml dataset-stats
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml export
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml export --limit 1000
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml review-list --status pending --limit 20
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml review-complete --review-uid review_xxx --annotation-json /path/to/reviewed_annotation.json --reviewer aidan
```

`status` 会输出 query / video / clip / annotation 的计数、下载/预处理/标注平均耗时，以及基于当前均值和 worker 配置的 `projection_5d` 估算。

`dataset-stats` 会输出场景、平台、触发事件、群体行为、群体情绪、clip 时长、置信度、质量标记、字段覆盖率，以及候选标注 / Judge / 人审 / lineage 的审计计数。

`review-list` / `review-complete` 用于处理 `human_reviews` 待办。若 profile 设置 `annotation.export_requires_review_clear=true`，`export` 会跳过仍处于 `review_required=true` 的样本，直到复核完成。

`export` 会在 `runtime/exports/dataset_export_<timestamp>/` 下生成交付目录，根目录只包含：

- `clips/`
- `annotations/`
- `README.md`

如果要临时排查某个阶段，可以手动执行单阶段命令：

```bash
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml crawl
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml preprocess
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml annotate
```

## 关键配置

下载侧：

- `crawl.download.workers`：下载并发
- `crawl.download.max_inflight_per_host`：单 host 并发上限
- `crawl.retry.max_attempts`：B 站 412 / 429 / timeout 等瞬时错误的最大尝试次数
- `crawl.retry.base_sleep_sec` / `max_sleep_sec` / `backoff_factor` / `jitter_sec`：失败后的退避 sleep 策略
- `sources.bilibili.cookie_env`：从环境变量读取 B 站 cookie，默认读取 `BILIBILI_COOKIE`
- `sources.bilibili.cookie_file`：直接把现成的 `cookies.txt` 路径交给 `yt-dlp`
- `sources.bilibili.cookie_file_env`：从环境变量读取 `cookie_file` 路径，默认读取 `BILIBILI_COOKIE_FILE`
- `sources.bilibili.cookies_from_browser`：直接让 `yt-dlp` 从浏览器读取 cookie，格式与 `--cookies-from-browser` 一致，如 `chrome`、`chrome:/path/to/profile`
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

如果需要带登录态抓取，不要把明文 cookie 提交进 Git。临时注入整串 cookie 仍然支持：

```bash
export BILIBILI_COOKIE='你的完整 cookie'
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml download-loop
```

程序内部会把这串 cookie 注入 `requests` 的 cookie jar，并为 `yt-dlp` 生成临时 `cookiefile`，不再把 cookie 作为普通请求头透传。

如果你已经有现成的 `cookie.txt` / `cookies.txt`，现在可以直接给路径，不再做额外转换：

```yaml
sources:
  bilibili:
    cookie_file: /path/to/bilibili_cookies.txt
```

或者直接用环境变量指定路径：

```bash
export BILIBILI_COOKIE_FILE=/path/to/bilibili_cookies.txt
python scripts/run_pipeline.py --config configs/profiles/continuous_vllm_pipeline.yaml download-loop
```

下载阶段会把这个文件路径原样交给 `yt-dlp`。`requests` 查询阶段如果需要复用 cookie，会单独从文件里解析出 B 站相关条目注入 session。

如果你就是想无 cookie 下载，保持 `cookie_file` 为空，并且不要导出 `BILIBILI_COOKIE_FILE` / `BILIBILI_COOKIE` 即可。

如果你本机已经登录过 Chrome / Chromium，更推荐直接在配置里写：

```yaml
sources:
  bilibili:
    cookies_from_browser: chrome
```

或者显式指定 profile 目录：

```yaml
sources:
  bilibili:
    cookies_from_browser: chrome:~/.config/google-chrome
```

优先级如下：

- 配了 `sources.bilibili.cookies_from_browser`：优先用浏览器 cookie
- 否则如果配了 `sources.bilibili.cookie_file` 或导出了 `BILIBILI_COOKIE_FILE`：直接用现成 `cookies.txt`
- 否则回退到 `BILIBILI_COOKIE` / 自动生成的临时 `cookiefile`
- 否则无 cookie 下载

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

标注审计信息写入 `runtime/index.sqlite` 的 `annotation_candidates`、`judge_decisions`、`human_reviews` 和 `lineage_edges`。这些表不进入默认交付目录，但可通过 `dataset-stats` 和数据库抽查支撑内部复核。

导出的数据集目录不会复制 runtime 原始工件；它只保留最终切片、精简后的标注 JSON 和导出说明 README。

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
