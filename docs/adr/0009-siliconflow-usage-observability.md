# SiliconFlow 用量可观测性:累加 prompt_tokens + run 末打印

`#146` 在 grill 完 ADR-0008(限流防御策略)后,追问"何时加 SF 用量 telemetry"。经 grilling 锁定:免费档现状下,加最小可观测(白拿 baseline),撞墙 / 升 Pro / 第一次账单来时已有数据。一条决策:

- **`_embed_with_siliconflow` 累加 `resp.usage.prompt_tokens` 到线程本地 metrics**(`core/metrics.py` 新模块,镜像 `core.degradation` 的 thread-local 模式),`scripts/eval_qa_drift.py` run 末打印本次总 token。代码增量 ~10 行 + 1 个测试。

## 接口契约

```python
# core/metrics.py(thread-local,与 core.degradation 同源范式)
def record_embedding_tokens(n: int) -> None: ...
def get_embedding_tokens_total() -> int: ...
def reset_embedding_tokens_total() -> None: ...
```

调用点: `_embed_with_siliconflow` 在 `client.embeddings.create(...)` 返回后读 `resp.usage.prompt_tokens`,调 `record_embedding_tokens(n)`。失败 / `usage` 为 `None` 静默跳过(不影响主路径)。

`scripts/eval_qa_drift.py` 在退出前打印 `total_prompt_tokens=N`。

## 为什么值得记 ADR

下一位读者会"修"出两个相反的设计:
- 看到 `_embed_with_siliconflow` 没用 `resp.usage` 字段,会"清理掉" —— 免费档是白拿 baseline 的机会,扔了等于丢未来决策数据
- 想直接接到 Prometheus / OpenTelemetry —— 与本仓当前无可观测性栈的状态不匹配(`core.degradation` 是唯一 telemetry 设施);先扩 thread-local,真有需求再升级

## 取舍

免费档 `BAAI/bge-m3` 标准版 = ¥0/1M tokens(`research/online-embedding-rerank-providers.md §4`),目前零成本。但 baseline 在不在决定撞墙 / 升 Pro 时能不能立刻回答"上月用了多少"。10 行代码换一份永久 baseline,可接受。

## 关联

- 父图 `#138`(已 close)、实施 `#144`(已 close)、跟进 `#145`(降级语义,ADR-0007)、本 ADR-0009
- 防撞策略见 ADR-0008
- `core/metrics.py` 与 `core/degradation.py` 同源 thread-local 模式