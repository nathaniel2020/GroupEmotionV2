# 3 台机器最简运行说明

适用场景：

- 3 台机器都跑这个仓库
- 每台机器各自有自己的 `runtime/continuous_pipeline`
- 每台机器都通过 shard 只处理自己的 1/3 数据

## 1. 每台机器都先做一次安装

```bash
cd /path/to/013.02\ video
pip install -e .
```

需要提前保证：

- Python `>=3.11`
- 已安装 `ffmpeg`
- 本机能访问 B 站
- 本机能访问配置里的 LLM 服务

当前主 profile 是：

- `configs/profiles/continuous_vllm_pipeline.yaml`

已经准备好的 3 份分片 overlay 是：

- 机器 0：`configs/profiles/shards/continuous_vllm_machine_0.yaml`
- 机器 1：`configs/profiles/shards/continuous_vllm_machine_1.yaml`
- 机器 2：`configs/profiles/shards/continuous_vllm_machine_2.yaml`

如果需要带登录态抓取，还要先确认 cookie 配置。

当前 `configs/base.yaml` 默认不绑定固定 cookie 文件路径。你可以按机器实际情况选择是否带 cookie。

推荐优先级：

- 已登录桌面浏览器：`sources.bilibili.cookies_from_browser`
- 无桌面服务器：`sources.bilibili.cookie_file` 或环境变量 `BILIBILI_COOKIE_FILE`
- 临时测试：环境变量 `BILIBILI_COOKIE`
- 完全无 cookie：保持这些配置为空，不导出环境变量

方式 1：直接从浏览器读取 cookie

```yaml
sources:
  bilibili:
    cookies_from_browser: chrome
```

如果要显式指定 profile 目录：

```yaml
sources:
  bilibili:
    cookies_from_browser: chrome:~/.config/google-chrome
```

方式 2：提供现成的 `cookies.txt`

```yaml
sources:
  bilibili:
    cookie_file: /path/to/bilibili_cookies.txt
```

方式 3：用环境变量指定 `cookie.txt` 路径

```bash
export BILIBILI_COOKIE_FILE=/path/to/bilibili_cookies.txt
```

下载阶段会直接把这个文件交给 `yt-dlp`。

方式 4：用环境变量临时注入整串 cookie

```bash
export BILIBILI_COOKIE='你的完整 cookie'
```

不要把明文 cookie 提交进 Git。

如果下载日志里出现：

```text
ERROR: [BiliBili] ... No video formats found!
```

优先按下面顺序排查：

1. 在出错机器上确认 `yt-dlp` 版本，不要只看本地开发机。

```bash
python -m yt_dlp --version
```

如果版本明显偏旧，先升级：

```bash
python -m pip install -U yt-dlp
```

2. 确认这台机器真的带上了可用登录态。三机环境里最常见的问题不是“代码没配 cookie”，而是某一台机器实际读不到 cookie，最后退化成无 cookie 下载。
3. 桌面机器优先用 `sources.bilibili.cookies_from_browser`；无桌面浏览器的服务器优先用 `sources.bilibili.cookie_file` 或 `BILIBILI_COOKIE_FILE`，不要指望 `cookies_from_browser` 在纯服务器环境稳定工作。
4. 用同一个账号在浏览器里手动打开对应视频；如果浏览器里这个账号都看不了，流水线也拿不到可下载格式。

这类报错通常不是“网络超时”，而是“页面打开了，但 B 站没有向当前会话暴露可播放流”。

## 2. 每台机器准备 reference

如果 3 台机器上都已经有这两个源文件：

- `../原子情感因素.xlsx`
- `../标注字段.xlsx`

直接执行：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  --config configs/profiles/shards/continuous_vllm_machine_0.yaml \
  prepare-reference
```

把上面命令里的 shard 配置换成对应机器自己的那一份即可。

如果这两个源文件不在默认位置，就手动指定路径：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  --config configs/profiles/shards/continuous_vllm_machine_0.yaml \
  prepare-reference \
  --excel-path /path/to/原子情感因素.xlsx \
  --sheet-name 情感因素 \
  --schema-source-path /path/to/标注字段.xlsx
```

## 3. 每台机器开 2 个终端

终端 1：下载

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  --config configs/profiles/shards/continuous_vllm_machine_0.yaml \
  download-loop
```

终端 2：预处理 + 标注

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  --config configs/profiles/shards/continuous_vllm_machine_0.yaml \
  pipeline-loop
```

机器 1 和机器 2 只需要把 shard 配置分别换成：

- `configs/profiles/shards/continuous_vllm_machine_1.yaml`
- `configs/profiles/shards/continuous_vllm_machine_2.yaml`

## 4. 如果是导入 013.01 runtime

执行：

```bash
python scripts/run_pipeline.py \
  --config configs/profiles/continuous_vllm_pipeline.yaml \
  --config configs/profiles/shards/continuous_vllm_machine_0.yaml \
  import-01301-runtime \
  --legacy-runtime-root /path/to/013.01/runtime
```

导入完成后，再开 `pipeline-loop` 即可。

导入器会优先读取下面两个旧库文件：

- `db/keyword_index.db`
- `db/video_meta.db`

它们的作用是补充旧系统里的 `query`、`title`、`url`、`scene` 等元信息。

不是强制必须有，但没有时导入信息会更弱，只能更多依赖目录名和文件名兜底。

## 5. 那两个导入文件要不要放进项目并提交 GitHub

不建议。

原因很直接：

- 它们是运行期数据，不是源码
- 一般体积不小，还会持续变化
- 里面可能带历史元信息，不适合直接进仓库
- 当前仓库约定本来就不提交 `runtime/`、`data/reference/*.json` 和本地运行产物

更合适的做法是：

- 把这两个文件保留在项目外部目录
- 运行时通过 `--legacy-runtime-root` 指向它们
- 如果只是为了复现导入逻辑，单独准备一小份脱敏测试样例放到 `tests/fixtures/`，不要把真实生产库提交上来
