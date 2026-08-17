# 线上 embedding 失败降级语义:无自动兜底、分层重试、无死信

`#138/#144` 把 embedding + rerank 从本机搬到 SiliconFlow 线上 API 后,`#145` 追问线上失败(429/5xx/连接错误)时的降级语义。经 grilling 锁定五条决策,构成一个整体契约:

1. **无自动兜底**——embedding 失败即抛,不自动切回本机 bge-m3、不做双供应商回退。运维级兜底 = 重启切 `EMBED_PROVIDER=local`(存量向量本机编码可复用)。理由:跨供应商向量漂移会污染共享 FAISS 索引(见 `cross-provider-drift` 研究),可用性收益盖不过检索语义被静默破坏的风险;且 local 兜底需常驻 torch,与"上云去 GPU"初衷相悖。
2. **错误类别分层单主**——HTTP 层错误(429/408/409/5xx)只由 OpenAI SDK 内置重试负责(`max_retries=2`,尊重 `retry-after` + jitter);连接层错误(`APIConnectionError`/`APITimeoutError`)只由调用方 tenacity 负责,且仅批量路径(3 次,2s→30s 指数退避);查询路径零附加重试(用户在等,长退避体感即挂死)。不可重试错误(模型缺失 / ValueError)**不进入任何重试**。
3. **无死信存储**——批量建库单稿 embedding 失败不中止整批(每稿隔离):该稿记 `failed`、原因进既有批量报表 failed 明细,其余稿继续;追跑走既有 reparse 通道(OCR 缓存命中零配额)。dead-letter 暂存只服务于"失败件重放",是为一类低概率事件另起一套存储+重放机制,拒绝。
4. **rerank 保持现状**——零重试 + 失败回原排序(不写索引、漂移安全,失败只是少个精排)。

**为什么值得记 ADR**:下一位读者会"修"出三个相反的设计——看到本机 bge-m3 模型与 `_gpu_inference_lock` 都还留着,会接线自动回退;看到 SDK 重试之上还有 tenacity,会砍掉"看起来重复"的重试层;看到批量建库一次失败全员中止,会写一个死信队列。三条都是刻意的。

**取舍**:SF 长时间故障时查询路径全停,恢复依赖人工切 provider。可接受——实测 RPM 宽松(账户限额 ≥1000、稳态 RPM < 1),rerank 已优雅降级,免费档足够支撑正常用量。