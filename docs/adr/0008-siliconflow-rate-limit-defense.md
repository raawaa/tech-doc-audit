# SiliconFlow 限流防御策略:不预防性加节流、撞墙再启 revisit

`#138`/`#144` 把 embedding + rerank 从本机搬到 SiliconFlow 线上 API 后,`#146` 追问批量建库与在线并发下的限流策略(RPM/TPM/EMBED_BATCH_SIZE)。经 grilling 锁定两条决策,构成防撞墙契约:

1. **删除孤儿 `EMBED_BATCH_SIZE = 32` 常量** —— `core/siliconflow_client.py:64` 硬编码 `EMBED_BATCH_SIZE = 32` 但**无任何函数引用**,per-doc 整 chunk list 已经是一次 HTTP 请求(限流按 HTTP 请求数 + token 数计,SF 不限 list 长度只限总 token),无需切子批。改 `_embed_with_siliconflow` 的 docstring 明文"per-doc 整 list 单请求、不切子批"。
2. **不加主动节流**(单 KB worker pool 调整 / 跨 KB 信号量 / 跨进程 semaphore **均不加**)。理由:`services/bulk_reparse_service.py:62` `DEFAULT_CONCURRENCY = 4` × N 进程 ≤ T3 §5.1 实测 50 并发短字符串无 429 的范围;`research/t3-siliconflow-probe-results.md` 报告账户阈值"≥ 1000 RPM(本机没撞到上限)"。撞墙兜底已有两层 —— `_embed_batch_with_retry` tenacity 3 次 2s→30s 指数退避(`core/index_manager.py:298`)+ OpenAI SDK 内置 `max_retries=2` 尊重 `retry-after`。

## 触发 ADR-0008-revisit 的条件

任何一条满足即重开本 ADR 讨论加节流:

- T3-style 重测在 ≥ N 并发下撞 429(N 待定,默认 ≥ 50)
- 线上某账号稳态观察到连续 429(retry 层吃不完,真报错上浮)
- `bulk_reparse` 跑量 ≥ 2 个进程并行跑不同 KB,被官方限流档位截停

revise 时倾向:进程级 `BoundedSemaphore`(文件锁 / `fcntl.flock`),env `SF_MAX_CONCURRENCY` 控制 N。**当前不写、撞墙再写**。

## 为什么值得记 ADR

下一位读者会"修"出三个相反的设计:
- 看到 `EMBED_BATCH_SIZE = 32` 孤儿会"接进去"切子批 —— 25 chunks/doc 切 32-batch = 切 1 批(等于啥也没干),徒增复杂度
- 看到 `bulk_reparse_service` 默认 4 路并发 + 无跨 KB 锁,会"加防御" —— 没数据点支持"现在就要防"
- 看到 retry 层,会再加一层"主动限流" —— `_embed_batch_with_retry` + SDK 内置重试已两层,再加一层是重复

三条都是刻意的。

## 取舍

`bulk_reparse` 单 KB 4 路并发 × 157 docs ≈ 157 个 SF 请求在 ~30s 内打完(实测 _embed_with_siliconflow 稳态 ~80ms,4 路并发 wall-time ~6s),稳态 < 30 RPM,远低于账户 1000 RPM 阈值。可接受 —— 撞墙概率极低,撞墙后 retry 层兜底,真扛不住时按触发条件开 ADR-0008-revisit。

## 关联

- 父图 `#138`(已 close)、实施 `#144`(已 close)、跟进 `#145`(降级语义,ADR-0007)、本 ADR-0008
- 用量可观测性见 ADR-0009