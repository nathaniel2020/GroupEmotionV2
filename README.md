# 群体情感视频轻量化流水线

当前仓库已经按 `013.02` 方案落地为一条轻量但完整的生产链路：

`prepare-reference -> seed-queries -> crawl -> preprocess -> annotate -> export`

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
- 若启用 LLM：设置环境变量 `YUNWU_API_KEY` 或对应 `llm.api_key_env`

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

烟雾测试 profile 在 [configs/profiles/gemini_yunwu_smoke.yaml](configs/profiles/gemini_yunwu_smoke.yaml)。

## 运行

先准备 reference：

```bash
python scripts/run_pipeline.py prepare-reference
```

然后按阶段运行：

```bash
python scripts/run_pipeline.py seed-queries
python scripts/run_pipeline.py crawl
python scripts/run_pipeline.py preprocess
python scripts/run_pipeline.py annotate
python scripts/run_pipeline.py export
python scripts/run_pipeline.py status
```

也可以一次串起来：

```bash
python scripts/run_pipeline.py run --steps prepare-reference,seed-queries,crawl,preprocess,annotate,export,status
```

叠加 profile：

```bash
python scripts/run_pipeline.py --config configs/profiles/gemini_yunwu_smoke.yaml run --steps prepare-reference,seed-queries,crawl,preprocess,annotate
```

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
- 标注字段值域做了严格校验，accepted 样本必须通过 schema 检查。
- 测试已覆盖 reference 生成、值域校验、LLM 并发闸门、离线 E2E。

当前未覆盖：

- 线上真实 B 站抓取与真实模型调用的集成冒烟尚未在本轮执行。
- `ffmpeg` 缺失时，`clip_export_mode=ffmpeg` 无法工作；测试里使用的是 `copy` 模式。
