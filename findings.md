# Findings

## 参考项目提炼

- 主链路已经扩展到 `scene -> query -> crawl -> video -> preprocess -> clip -> annotate -> lineage/export`，适合长期建设，不适合作为当前重构起点。
- 参考项目的 `llm.py` 已经处理了不少 OpenAI-compatible 细节，但视频输入模式较多，失败面也更广。
- 参考项目的 `logging_utils.py` 只有基础文件日志，尚不足以支撑“失败请求回放”。
- `annotation/service.py` 表明最终质量控制已经有 `quality_flags` 和失败落盘思路，这部分值得保留并进一步前移。

## 本项目的最小策略

- 收缩输入单位：先统一成 `sample.json`，避免采集和预处理成为当前阻塞。
- 收缩调用形态：优先关键帧 + transcript，减少直接视频上传导致的协议和体积问题。
- 扩展日志粒度：把 run、event、llm request、llm response、llm error 拆开记录。
- 保留质量闭环：要求字段级证据、字段级置信度、低置信回流和人工抽检。

