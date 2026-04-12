# Group Emotion Video Lite

这是 `013.01 video` 的轻量化重构起点。目标不是立刻复刻原项目的全链路能力，而是先把最容易失控的部分收紧：

- 流程先收缩成 `sample -> annotate -> review -> export`
- 标注质量先依靠“证据约束 + 低置信回流 + 人工抽检”保障
- 多模态大模型调用先统一到一个 OpenAI-compatible 客户端
- 日志先做到“每次运行、每次请求、每次失败”都能回放

## 为什么先这样做

参考项目已经验证了完整流程的方向，但当前复杂度偏高：

- 自动 query 调度、采集、预处理、标注、lineage 同时推进，排障面太大
- 大模型调用失败时缺少稳定的请求/响应留档，问题定位效率低
- 质量控制点分散在多个阶段，导致改一处要牵动整条链路

因此这个版本优先做一条更短、可审计、可恢复的最小主链路。

## 简化后的主流程

1. 准备一个 `sample.json`
   包含 `sample_id`、候选关键帧、可选 transcript、可选远程视频 URL。
2. 运行一次标注
   模型必须输出结构化 JSON，并给出字段级证据与置信度。
3. 进入复核队列
   当低置信、字段冲突、证据不足时，自动进入 `needs_review`。
4. 导出
   仅导出 `approved` 样本，附带日志索引与原始响应路径。

## 目录

```text
configs/         基础配置
docs/            重构与方法文档
prompts/         Prompt 模板
scripts/         命令行入口
src/             轻量代码骨架
tests/           后续补测试
data/            原始/中间/处理后数据目录
runtime/         运行期产物
```

## 快速开始

安装依赖：

```bash
python3 -m pip install -e .
```

查看流程说明：

```bash
python3 scripts/run_pipeline.py plan
```

查看示例标注请求：

```bash
python3 scripts/run_pipeline.py annotate \
  --sample data/interim/example_sample.json \
  --dry-run
```

## 关键约束

- 默认不把本地原视频直接塞给模型；优先发关键帧和 transcript，降低多模态失败率。
- 每次模型调用都会生成单独的 `request.json`、`response.json` 和 `events.jsonl` 记录。
- 质量控制优先做减法：先用一个主模型 + 一个裁决规则，而不是多智能体并发堆叠。

详细设计见 [docs/轻量化重构方案.md](/Users/aidan/Home/Code/010-019论文项目/013-群体情感/013.02%20video/docs/轻量化重构方案.md) 和 [docs/日志与多模态调用规范.md](/Users/aidan/Home/Code/010-019论文项目/013-群体情感/013.02%20video/docs/日志与多模态调用规范.md)。

