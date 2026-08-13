# Online Embedding / Rerank Provider Survey — `raawaa/tech-doc-audit`

> **Scope**: 把本机 `BAAI/bge-m3` embedding + `BAAI/bge-reranker-v2-m3` rerank 替换为线上 API
> 的服务商横评。机器在中国大陆、出网走代理(项目里已有多处绕过逻辑),所以只考虑**国内
> 直连可用**的服务商,海外服务商(Jina / Voyage / Cohere / OpenAI)不进入对比表。
>
> **抓取日期**:2026-08-12
> **存量**:157 篇 KB 文档的 FAISS `IndexHNSWFlat(1024)` 向量(已用本地 bge-m3 算好)。
> **迁移强偏好**:如果线上能拿到**同一个** `BAAI/bge-m3` + `BAAI/bge-reranker-v2-m3`,
> 现有向量**理论上可以原样复用**,迁移成本趋近于零。**这是本报告的头号问题。**
>
> **当前本机配置**(代码锚点):
> - embedding `BAAI/bge-m3`(`llama_index.embeddings.huggingface.HuggingFaceEmbedding`,
>   `normalize=True, max_length=512, embed_batch_size=8`,1024 维) — `core/settings.py:91-104`
> - rerank `BAAI/bge-reranker-v2-m3`(`sentence_transformers.CrossEncoder`) — `core/settings.py:126-171`
> - FAISS `IndexHNSWFlat(1024, 32)` — `core/index_manager.py:67-80`

---

## TL;DR

- **同时提供 `BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3` 的国内服务商,只有一家:`SiliconFlow(硅基流动)`**。
  它对这两个模型有免费版(0 元)与 Pro 版(`¥0.07 / 1M tokens`,input 计费)两档,接口
  schema 与 OpenAI / Cohere / Jina 各有差别,**不是 OpenAI 兼容**——embedding 端走自家
  `/v1/embeddings`,rerank 端走 `/v1/rerank`(`RerankClassicRequest`)。
- **推荐 SiliconFlow**(理由见 §6)。次优:`Aliyun 百炼 DashScope`(自研 text-embedding-v4 +
  qwen3-rerank,**不**托管 bge,需要重新向量化)。
- **最大风险**:SiliconFlow 是国内唯一托管 bge-m3 全系列的厂商,但它**不是 OpenAI 兼容**;
  `llama-index-embeddings-openai` 不能直接用,需要写一个 `SiliconFlowEmbedding` 适配器或
  直接调 `httpx`。rerank 接口也要单独封装,没有现成的 LlamaIndex `BaseRerank` 适配器。
- **次大风险**:即使挑了托管 bge-m3 的厂商,**向量归一化**策略需对齐。FAISS `IndexHNSWFlat`
  当前存的是 bge-m3 `normalize=True` 输出;若线上接口默认不归一化(或不保证归一化),需要客户端
  自己做 L2-normalize 后再写库,否则检索精度会掉——本次调研**未在官方文档逐家核实归一化默认行为**,
  见 §6 的 T3 落地项。
- **完全不要选**:`百度千帆`(只有 bge-large-zh/en,**没有 m3**、**没有 bge-reranker-v2-m3**);
  智谱 / 阿里 / 腾讯 / 字节自研路线均**不托管 bge**,换模型等于重建索引。

---

## 1. 同时托管 bge-m3 + bge-reranker-v2-m3 的服务商(收敛清单)

> 这是本项目的头号问题。

| 服务商 | bge-m3 | bge-reranker-v2-m3 | 备注 |
|---|---|---|---|
| **SiliconFlow(硅基流动)** | **是** — `BAAI/bge-m3` | **是** — `BAAI/bge-reranker-v2-m3` | 模型 ID 与 HuggingFace 完全一致;有标准版(免费)与 `Pro/BAAI/bge-m3`(¥0.07 / 1M tokens) |
| 阿里云百炼 DashScope | 否 | 否 | 嵌入用 `text-embedding-v4`,重排用 `qwen3-rerank`(2026-08-12 当前上架模型,旧的 `gte-rerank-v2` 似已淡出) |
| 智谱 BigModel | 否 | 否 | 嵌入用 `Embedding-3`/`Embedding-2`,无 rerank |
| 百度千帆 | 否(bge-large-zh/en) | 否(bce-reranker-base / Qwen3-Reranker) | 不托管 m3 系列 |
| 火山方舟 Volcengine | 否 | 否 | 嵌入 `doubao-embedding`(1024/2048 维),重排走方舟自有模型 |
| 腾讯混元 | 否 | 否 | 嵌入 `hunyuan-embedding`(1024 维),无独立 rerank API |

**结论**:**只有 SiliconFlow 同时托管**。其余 5 家均需替换为自研模型,等价于**全量重新向量化
157 篇 KB**(预估百万级 token)。

---

## 2. 横向对比表

> 列含义:
> 1. **是否托管 bge-m3** + 模型 ID;
> 2. **是否托管 bge-reranker-v2-m3** + 模型 ID;
> 3. 若不托管,自家嵌入/重排模型、**输出维度**、是否支持指定维度;
> 4. **价格**(embedding ¥ / 1M tokens;rerank 计价方式);
> 5. **限流**(RPM/TPM/并发);免费额度;
> 6. **单次请求上限**(文本条数、每条最大 token、总 token 上限);
> 7. **OpenAI 兼容性**(embedding 是否能直接挂 `OpenAIEmbedding` base_url;rerank schema);
> 8. **准入门槛**;
> 9. **稳定性信号**(SLA / 状态页 / 已知模型下架历史)。
> **币种**:除特别注明外,国内服务商均为 CNY(元);抓取日期 2026-08-12。

| # | 服务商 | 1. bge-m3? | 2. bge-reranker-v2-m3? | 3. 自家嵌入(维度) | 4. 价格 | 5. 限流 | 6. 单次请求上限 | 7. OpenAI 兼容 | 8. 准入 | 9. 稳定性 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **SiliconFlow 硅基流动** | ✅ `BAAI/bge-m3` | ✅ `BAAI/bge-reranker-v2-m3` | — | 嵌入:`BAAI/bge-m3` 标准版 ¥0 / 1M(input/output/cached 全部免费);`Pro/BAAI/bge-m3` ¥0.07 / 1M(input only)。rerank:`BAAI/bge-reranker-v2-m3` 标准版免费;`Pro/...` ¥0.07 / 1M | 限流按账户、按模型分档。向量模型 RPM 范围 2000–10000,TPM 500K–10M;rerank RPM 2000、TPM 500K(典型档位);免费版固定额度,需**实名认证**才能用免费档;按月消费分档升级 | 嵌入:`POST /v1/embeddings`,接受 `input` 为 string 或 list<string>;max input 长度由模型决定,bge-m3 上限 8192 tokens。rerank:`POST /v1/rerank`,`documents` 是 string 或 array(无显式 maxItems,但受 runtime 限制);`max_chunks_per_doc` 默认 1024,`overlap_tokens` 0–80(均仅 bge-reranker-v2-m3 / bce-reranker / Qwen3-Reranker 支持) | 嵌入:有 OpenAI 兼容模式的 `/v1/embeddings` 端点,但**请求 schema 是 SiliconFlow 自家**(缺 `dimensions` 字段,只有 `model / input / encoding_format`),不能直接挂 `OpenAIEmbedding`。rerank:私有 schema(`RerankClassicRequest`),不是 OpenAI 风格 | 个人可开,需实名(免费模型);支持支付宝/微信;企业可走商务 | 自称"企业级 SLA 保障";未公布公开 status page;**已知**:2024–2026 期间个别 Pro 模型做过计费调整;模型下架历史**未在官方文档核实** |
| 2 | **Aliyun 百炼 DashScope** | ❌ | ❌ | `text-embedding-v4`(默认 1024 维,也支持 2048 维;旧版 `text-embedding-v3` 仍在;`tongyi-embedding-vision-plus` 多模态)。重排:`qwen3-rerank`(2026-08-12 上架;旧的 `gte-rerank-v2` / `gte-rerank` / `qwen3-vl-rerank` 等 SDK 文档仍有) | 嵌入 v4 文档页显示 0.5 / 0.1 / 0.05 元 / 1M 三档(分别对应 2048 / 1024 / 512 维,**未在官方文档逐档核实**,见来源 4);rerank 价格**未在官方文档核实**(官方控制台 2026-08-12 JS 渲染,WebFetch 取不到) | **查不到**(百炼控制台动态渲染,官方模型页未公开 RPM/TPM 数字;SDK 文档未列) | 嵌入:`TextEmbedding.call(model, input, dimension=1024, text_type="document")`,支持 `string / list[str] / file`;**维度可指定**(64 / 128 / 256 / 512 / 1024 / 1536 / 1792 / 2048,SDK 默认 1024)。rerank:`TextReRank.call(model, query, documents, top_n, return_documents)`,无显式 maxItems | 嵌入:**提供 OpenAI 兼容模式** base URL `https://[{WorkspaceId}].cn-beijing.maas.aliyuncs.com/compatible-mode/v1`;`text-embedding-v3` 兼容模式下 `dimensions` 参数**不生效**(官方"与 OpenAI 的差异"明确)。rerank:私有 schema,不是 OpenAI 风格 | 个人实名可开;阿里云账号实名(个人身份证或企业营业执照);支持支付宝/对公 | 有阿里云大 SLA 体系;**模型变更频繁**:2026-08-01 起 `dashscope.console.aliyun.com` 域名下线,统一并入 `bailian.console.aliyun.com`,迁移风险可控 |
| 3 | **Zhipu BigModel 智谱** | ❌ | ❌ | `Embedding-3`(默认 1024 维,支持 256 / 1024 / 2048 维自定义)、`Embedding-2`(1024 维)。**无 rerank 模型** | **未在官方文档核实**(open.bigmodel.cn/pricing 页 2026-08-12 取不到完整表格);Batch API 价格为标准 API 的 50% | **未在官方文档核实**;Batch 队列有上限:Embedding-2 / Embedding-3 各 2,000,000 请求;每 Batch 文件最多 10,000 请求 | 嵌入:OpenAI 兼容 base URL 形式,**未在官方文档核实具体 URL**;模型 ID `embedding-2` / `embedding-3`。**无 rerank API** | 个人实名认证(中国大陆身份证)可开,认证后送 5,000,000 免费 tokens(Batch API 适用;实时 API 免费额度**未在官方文档核实**) | 智谱整体运营稳定;无公开 status page;无已知重大下架 |
| 4 | **百度千帆 Qianfan** | ❌(只有 bge-large-zh / bge-large-en,无 bge-m3) | ❌(只有 bce-reranker-base / Qwen3-Reranker 0.6B / 4B / 8B) | `Embedding-V1`、`bge-large-zh`、`bge-large-en`、`tao-8k`、`Qwen3-Embedding-0.6B/4B/8B`(维度**未在官方文档逐个核实**,Qwen3 embedding 系列应支持 Matryoshka)。重排:`bce-reranker-base`、`Qwen3-Reranker-0.6B/4B/8B` | 嵌入 / 重排统一 ¥0.5 / 1M tokens(0.0005 元 / 千 tokens);Qwen3-Reranker ¥0.8 / 1M | **未在官方文档核实** | 嵌入:OpenAI 兼容 SDK 文档(`/doc/qianfan/s/Hmh4suq26`)存在,具体 base URL **未在官方文档核实**。重排:OpenAI 兼容 SDK 同样存在 | 个人实名认证可开;**实名认证后送 ¥20 代金券**;按 token 计费 | 百度云 SLA 体系;无公开 status page;**已知**:2026-08-12 之前 bge-large-zh / bge-large-en 仍在线;Qwen3-Reranker 系 2026 年新上 |
| 5 | **火山方舟 Volcengine** | ❌ | ❌ | `doubao-embedding`(支持 1024 / 2048 维)、`doubao-embedding-vision`(多模态)。重排:**未在官方文档核实**(方舟的 "向量化"、"模型列表" 文档为动态渲染,WebFetch 2026-08-12 取不到具体模型清单) | 嵌入:文档页显示 ¥0.5 / 1M 和 ¥0.3 / 1M 两档(分别对应 2048 / 1024 维,**未在官方文档核实**,见来源 5)。重排价格**未在官方文档核实** | **未在官方文档核实**;官方 CLI `arkcli pricing models --modality Embedding` 可查询具体档位 | 嵌入:Ark base URL `https://ark.cn-beijing.volces.com/api/v3`(OpenAI 兼容);**维度可指定**(`dimensions` 参数,**未在官方文档核实**)。重排:**未在官方文档核实** | 嵌入:OpenAI 兼容(`volcenginesdkarkruntime`,与 `openai-python` 兼容)。重排:**未在官方文档核实** | 个人实名可开(火山引擎账号);企业资质可选;新用户有代金券活动 | 火山方舟 SLA 体系;有状态页(私有);**已知**:模型列表动态调整频繁 |
| 6 | **腾讯混元 Hunyuan** | ❌ | ❌ | `hunyuan-embedding`(1024 维,**固定**,不支持 `dimensions` 参数)。**无独立 rerank API**(只有 chat 侧的文本生成) | **未在官方文档核实**(定价文档 `cloud.tencent.com/document/product/1729/97731` 提到 hunyuan-TurboS 为 ¥0.8 / 1M input,¥2 / 1M output;embedding 价格**未在公开页核实**);**新用户免费额度:1,000,000 tokens(1 年有效),与 Hunyuan-lite 共用池**;Hunyuan-lite 永久免费 | 默认接口请求频率限制:**5 次/秒**(embedding `GetEmbedding`);Token 计算接口 20 次/秒 | 嵌入:`Input`(单条 string,总长 ≤ 1024 tokens,超长截断)或 `InputList`(数组,**总长度 ≤ 50**);OpenAI 兼容 base URL `https://api.hunyuan.cloud.tencent.com/v1`,`model` 字段固定为 `hunyuan-embedding`,`dimensions` 字段**不支持**(固定 1024) | 嵌入:OpenAI 兼容(但 `dimensions` 参数被忽略,只能拿 1024 维);**注意**:OpenAI 客户端传 `dimensions` 不会报错,但会被服务端丢弃,**不能换 dim**。重排:无 API | 个人腾讯云实名(身份证)可开;支持微信/对公 | 腾讯云 SLA 体系;**已知**:2023-09-01 至今 API 版本稳定 |

---

## 3. 关键 schema 摘录(SiliconFlow 详,其他略)

### 3.1 SiliconFlow embedding(POST /v1/embeddings)

来源:SiliconFlow 官方文档 `docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings`
(经 context7 缓存,URL: `https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings/api-reference/embeddings/create-embeddings`)

**Request**:
```http
POST https://api.siliconflow.cn/v1/embeddings
Authorization: Bearer <token>
Content-Type: application/json

{
  "model": "BAAI/bge-m3",
  "input": "...",
  "encoding_format": "float"
}
```

**Response**:
```json
{
  "model": "BAAI/bge-m3",
  "data": [{"object": "embedding", "embedding": [0.0123, ...], "index": 0}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12}
}
```

**注意**:**没有 `dimensions` 字段**(bge-m3 默认就是 1024 维,客户端不可调)。
**与 OpenAI `/v1/embeddings` 的差异**:`encoding_format` 用 `float` / `base64`;没有
`user` / `dimensions` / `encoding_format` 的 OpenAI 完整三件套;但 `model + input + encoding_format`
子集与 OpenAI 完全兼容,所以 `OpenAIEmbedding(api_key=..., base_url="https://api.siliconflow.cn/v1",
model="BAAI/bge-m3")` **可能可用**,前提是项目不依赖 `dimensions` 参数——本项目存的就是 1024 维,
正好对齐。**待 T3 实测确认**(`OpenAIEmbedding` 在 SiliconFlow base_url 下能否成功调通)。

### 3.2 SiliconFlow rerank(POST /v1/rerank)

来源:SiliconFlow 官方文档 `docs.siliconflow.cn/cn/api-reference/rerank/create-rerank`
(2026-08-12 通过 WebFetch 抓取,response header 含 `x-siliconcloud-trace-id`)

**Request**(经典文本重排):
```http
POST https://api.siliconflow.cn/v1/rerank
Authorization: Bearer $SILICONFLOW_API_KEY
Content-Type: application/json

{
  "model": "BAAI/bge-reranker-v2-m3",
  "query": "...",
  "documents": ["...", "..."],
  "return_documents": true,
  "top_n": 4,
  "max_chunks_per_doc": 1024,
  "overlap_tokens": 0
}
```

字段:
- `model`(string, required)
- `query`(string, required, ≥1 char)
- `documents`(string 或 array, required, ≥1 item)
- `instruction`(optional,**仅** Qwen3-Reranker 支持)
- `top_n`(int, optional, ≥1)
- `return_documents`(bool, default `false`)
- `max_chunks_per_doc`(int, default 1024, ≥1)— **仅** bge-reranker-v2-m3 / bce-reranker / Qwen3-Reranker 支持
- `overlap_tokens`(int, 0–80)— 同上支持范围

**Response**:
```json
{
  "id": "rerank-...",
  "results": [
    {"index": 1, "document": {"text": "..."}, "relevance_score": 0.85}
  ],
  "meta": {"tokens": {...}, "billed_units": {...}}
}
```

结果按 `relevance_score` 降序,score ∈ [0, 1]。`document` 字段仅在 `return_documents=true` 时回传。

**与 OpenAI rerank 接口的对比**:OpenAI **没有**标准 rerank 接口(OpenAI 至今未上线 rerank
endpoint)。SiliconFlow / Cohere / Jina / Voyage 都是各自私有的 schema。SiliconFlow 的 schema
与 Cohere `co.rerank(...)` 接口**不完全兼容**——字段名接近但不一致(`return_documents` vs
`return_documents`,`top_n` 一致,但 SiliconFlow 多 `max_chunks_per_doc / overlap_tokens`)。

---

## 4. SiliconFlow 限流与档位详解

来源:SiliconFlow 官方文档 `docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation`
(经 context7 缓存)。

- **限流维度**:RPM / RPH / RPD / TPM / TPD;每模型独立档位,账户级别共享。
- **典型档位**(官方文档示例,**未给具体模型数字**):
  - **语言模型(chat)**:RPM 1000–10000,TPM 50K–5M
  - **向量(embedding)**:RPM 2000–10000,TPM 500K–10M
  - **重排(rerank)**:RPM 2000,TPM 500K
  - **生图**:IPM 2 / IPD 400
- **Free vs Paid**:
  - **Free 版模型**(即 `BAAI/bge-m3` / `BAAI/bge-reranker-v2-m3` 标准版):固定额度,**需实名认证
    后才能用**;费用 0 元。
  - **Paid 版模型**(前缀 `Pro/`,如 `Pro/BAAI/bge-m3`):按用量计费,¥0.07 / 1M tokens;
    限流按"用量等级"分档,**等级依据月消费金额**。
- **触发逻辑**:RPM / TPM 任一超额即 429(RateLimitExceeded)。例:RPM 20、TPM 200K 时,
  1 分钟内发 20 次 × 100 tokens 的请求 → 触发 RPM 限流,即便 TPM 还没到 200K。
- **如何查自己账户的具体档位**:`https://cloud.siliconflow.cn/me/models`(控制台 Model Marketplace,
  列出每个模型的 RPM / TPM 实时值)。
- **Batch**:与在线限流独立,文件大小上限 1GB,**不占用在线 RPM/TPM**。
- **升级**:商务对接,即时生效。

---

## 5. 字段对齐与本项目约束的差距分析

| 字段 | 项目当前(本地 bge-m3) | SiliconFlow `BAAI/bge-m3` | 评估 |
|---|---|---|---|
| 输出维度 | 1024 | 1024 | **完全对齐**——FAISS `IndexHNSWFlat(1024)` **可原样复用** |
| 归一化 | `normalize=True` | **未在官方文档核实默认是否归一化**,但 bge-m3 模型本身在 HF 上 `encode(normalize_embeddings=True)` 是常见做法 | **T3 必须实测**:线上返回的向量是否已 L2-normalize;若否,客户端要 `normalize_l2()` 再写库 |
| max length | 512(`HuggingFaceEmbedding(max_length=512)`) | bge-m3 在 SiliconFlow 上限 8192 tokens | 512 是本地显存约束,线上可放宽;**但分块策略要不要改需要 T3 决策**——切小 chunk 会拉低召回,切大会被线上截断 |
| embed_batch_size | 8 | 无显式 batch_size 字段;`input` 接受 list,按 list 大小决定批大小 | 客户端控制,可保持 8 |
| rerank query 长度 | CrossEncoder 默认 512 | **未在官方文档核实**,但 bge-reranker-v2-m3 上限 512 tokens | 一致 |
| rerank documents | CrossEncoder 默认 512 | `max_chunks_per_doc=1024` 默认开启,文档超过会自动 chunk+overlap | **比本地更智能**——长文档不需要客户端先切 |

---

## 6. 推荐与风险

### 6.1 推荐:SiliconFlow(硅基流动)

**核心理由**:
1. **唯一同时托管 `BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3` 的国内服务商**(§1)。
   模型 ID 与 HuggingFace 完全一致,意味着:
   - **157 篇 KB 的现有 FAISS 向量在归一化策略对齐的前提下可直接复用**,零迁移成本。
   - rerank 评分分布与本地一致,后续调阈值(`LLM_PROMPT_FULL_THRESHOLD` 等)经验可平滑迁移。
2. **免费版可用**:`BAAI/bge-m3` 标准版与 `BAAI/bge-reranker-v2-m3` 标准版均 ¥0;实名认证即可,
   个人身份证即可办,无须预充值。
3. **OpenAI 兼容度足够**:embedding 端点的 `model + input + encoding_format` 子集与 OpenAI
   `/v1/embeddings` 完全一致,`OpenAIEmbedding(base_url="https://api.siliconflow.cn/v1",
   api_key=..., model="BAAI/bge-m3")` 大概率无需写适配器就能跑(待 T3 实测)。
4. **Rerank 接口 schema 完整、文档清晰**,有 `max_chunks_per_doc` / `overlap_tokens` 处理长文档,
   比本地的 `CrossEncoder` 更省心。
5. **价格低**:Pro 版 ¥0.07 / 1M tokens;按 157 篇 KB 的总 token 量级(预估几千万 token),
   总费用在 5 元以内。免费档完全够用。

**次优替代:Aliyun 百炼**(`text-embedding-v4` + `qwen3-rerank`)
- 优点:OpenAI 兼容 base URL 更"标准"(`maas.aliyuncs.com/compatible-mode/v1`);阿里云 SLA 强;
  有 2048 维选项。
- 缺点:**不托管 bge**——切换后**全量重新向量化 157 篇 KB**;模型 ID 不同,**FAISS 索引要重建**
  (`IndexHNSWFlat(1024)` 可保留但所有向量要重新写入);旧 rerank 分数分布完全失效,阈值需重调。

### 6.2 风险与缓解

| # | 风险 | 严重度 | 缓解措施 |
|---|---|---|---|
| 1 | **SiliconFlow 不是 OpenAI 严格兼容**——`OpenAIEmbedding` 在 SiliconFlow base_url 下能否成功调用、T3 必须实测(`dimensions` 字段不存在,但本项目不需要);若失败,写一个 `SiliconFlowEmbedding(BaseEmbedding)` 适配器,30 行内 | 中 | 写一个本地 mock adapter,T3 用 `tests/` 里的现有 `_inject_block_range` 链路先打桩 |
| 2 | **归一化默认行为未在官方文档核实**——bge-m3 模型本身通常 `normalize=True`,但线上 API 不一定后处理;若 FAISS 存的是未归一化向量,**余弦相似度检索会全部失效**(FAISS `IndexHNSWFlat` 默认用 L2 距离) | 高 | T3 第一步:取 5 篇已知 KB 文档,跑线上 embedding,跟本地 embedding 做 `np.allclose(..., atol=1e-3)`;若差 1 个 norm 数量级,客户端 `normalize_l2()` 再写库 |
| 3 | **SiliconFlow 单一供应商风险**——bge-m3 / bge-reranker-v2-m3 在国内别无分店;SiliconFlow 故障 / 涨价 / 下架 = 项目断粮 | 中 | .env 里同时配 SiliconFlow + Aliyun 双 base_url,主用 SiliconFlow,失败自动回退 Aliyun 重新向量化(已知迁移成本高,故仅作兜底,不主动切换) |
| 4 | **Rerank 接口无 LlamaIndex 适配器**——`llama-index-core` 的 `BaseRerank` 没有现成的 SiliconFlow / Cohere / Jina 实现(只有 Cohere);需要写 `SiliconFlowRerank` 适配器 | 中 | 复用 `services/agent_tools.py` 已有的 `CrossEncoder` 调用点结构,封装 `class SiliconFlowRerank(BaseRerank): postprocess_query()` |
| 5 | **限流档位不透明**——免费版 RPM / TPM 没有公开数字,只能登录后查 Model Marketplace;按本项目用量(每审核任务调 embedding 几百次、调 rerank 几十次),免费档应该够用,但**未在官方文档核实** | 低 | .env 留 `EMBEDDING_RPM=2`、`EMBEDDING_TPM=20000`、`RERANK_RPM=2`,客户端做令牌桶,触发限流时指数退避 |
| 6 | **模型下架历史**——2024–2026 期间 SiliconFlow 偶尔调整 Pro 模型计费;`BAAI/bge-m3` / `BAAI/bge-reranker-v2-m3` 本身是 BAAI 开源权重,只要 SiliconFlow 不主动下线就一直可用 | 低 | 锁版本:`EMBEDDING_MODEL=BAAI/bge-m3`、`RERANKER_MODEL=BAAI/bge-reranker-v2-m3` 写死;监控日志抓 `model_not_found` 错误 |
| 7 | **代理干扰 httpx**——项目已有 `core/settings.py` 多处绕过逻辑;SiliconFlow 用 `https://api.siliconflow.cn/v1/embeddings`,需先在本地用 curl 实测代理下能否直连;若仍需走代理,`httpx.AsyncClient(proxy=...)` 与现有 DeepSeek client 共用一份代理配置即可 | 中 | T3 验证步骤第一条:裸 `curl https://api.siliconflow.cn/v1/models -H "Authorization: Bearer $KEY"` |

### 6.3 落地建议(给 T3 / T4)

1. **T3 适配器**:
   - `core/embedding_siliconflow.py`:`class SiliconFlowEmbedding(BaseEmbedding)`——30 行,基于
     `httpx.AsyncClient` 调 `/v1/embeddings`,返回 `List[float]`,客户端 `normalize_l2()`。
   - `core/rerank_siliconflow.py`:`class SiliconFlowRerank(BaseRerank)`——50 行,调 `/v1/rerank`,
     把 `relevance_score` 转成 LlamaIndex 期望的 `NodeWithScore`。
2. **.env 新增**:
   ```
   SILICONFLOW_API_KEY=sk-...
   SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
   EMBEDDING_PROVIDER=siliconflow      # siliconflow | local
   RERANKER_PROVIDER=siliconflow       # siliconflow | local
   ```
   `EMBEDDING_PROVIDER` / `RERANKER_PROVIDER` 双开关,**默认走线上**,`=local` 保留本机 fallback
   供单机开发用。
3. **不重建索引**:157 篇 KB 的现有 FAISS `IndexHNSWFlat(1024)` **保持原状**——前提是 §6.2 #2
   归一化对齐通过实测。
4. **rerank 阈值重调**(可选):本地 `CrossEncoder` 与线上 API 的 `relevance_score` 分布可能不同;
   若 QA 体验掉,先看 trace 里 `flag_issue` 前的 `search_kb` 召回分布。

### 6.4 明确**不推荐**的方案

- **百度千帆**:只有 bge-large-zh / bge-large-en,**无 bge-m3**——切过去等于换掉整个嵌入模型,
  重建索引。
- **智谱 BigModel**:**没有 rerank API**——rerank 这条腿断了。
- **腾讯混元**:只有 1024 维固定、`dimensions` 参数被服务端忽略;**无 rerank**。
- **火山方舟**:模型清单文档 JS 渲染,2026-08-12 抓不到完整价格 / 限流 / 模型 ID 表;**且不托管 bge**。
- **OpenAI / Jina / Voyage / Cohere**:境外,被代理干扰;**且不托管 bge**(OpenAI / Cohere / Jina 都有
  自研模型,但与 bge-m3 输出空间不同,换模型需重建索引)。

---

## 7. 附录:来源

### 一手来源(本次实际抓取)

1. SiliconFlow 定价页 — `https://siliconflow.cn/pricing`(2026-08-12 WebFetch)。
   - 关键确认:`BAAI/bge-m3` 标准版 ¥0、`Pro/BAAI/bge-m3` ¥0.07/1M;`BAAI/bge-reranker-v2-m3` 同。
2. SiliconFlow rerank API schema — `https://docs.siliconflow.cn/cn/api-reference/rerank/create-rerank`
   (2026-08-12 WebFetch)。
3. SiliconFlow embedding API schema — 经 context7 缓存,
   原始 URL: `https://docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings/api-reference/embeddings/create-embeddings`。
4. SiliconFlow 限流体系 — `https://docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation`
   (经 context7 缓存)。
5. DashScope `TextEmbedding` / `TextReRank` SDK 文档 — `https://github.com/dashscope/dashscope-sdk-python/blob/main/README.md`
   与 `_autodocs/6-reranking.md`(经 context7 缓存,2026-08-12)。
6. Aliyun Bailian "Choose a Model" 当前上架模型 — 经 WebFetch `help.aliyun.com/document_detail/2580028.html`(2026-08-12)。
   - 关键确认:`text-embedding-v4`、`tongyi-embedding-vision-plus`、`qwen3-rerank`(2026-08-12 当前);
     OpenAI 兼容 base URL 形式。
7. Aliyun Bailian embedding 定价 — 经 WebFetch(2026-08-12,页内显示 0.5 / 0.1 / 0.05 元三档);
   **逐档对应维度未在抓取内容中明确**(标"未在官方文档核实")。
8. 智谱 BigModel Batch API / Batch 价格 — `https://docs.bigmodel.cn/cn/faq/batch-api-issues`(经 context7 缓存)。
   - 关键确认:实时 API 价格未在抓取内容中明确;Batch 价格为标准 API 的 50%;实名认证后送 5,000,000
     Batch 免费 tokens;Embedding-2 / Embedding-3 Batch 队列上限 2,000,000 / Batch 文件 10,000。
9. 百度千帆价格表 — `https://cloud.baidu.com/doc/qianfan/s/wmh4sv6ya`(2026-08-12 WebFetch)。
   - 关键确认:所有嵌入 ¥0.5/1M,Qwen3-Reranker ¥0.8/1M;**bge-m3 不在列**,只有 bge-large-zh/en。
10. 百度千帆新用户免费额度 — `https://cloud.baidu.com/doc/qianfan/s/Imi2rpirg`(2026-08-12 WebFetch)。
    - 关键确认:17 个模型各有 1,000,000 free tokens、3 个月有效;含 bge-large-zh,**不含 bge-m3**;
      政策更新日期 2025-11-17(2025-10-24 起生效)。
11. 火山方舟 CLI pricing / 模型元数据 — `https://github.com/volcengine/ark-cli`
    (经 context7 缓存,2026-08-12)。
    - 关键确认:`arkcli pricing models --modality Embedding` 可查向量定价;方舟 base URL
      `https://ark.cn-beijing.volces.com/api/v3`,OpenAI 兼容。
12. 火山方舟"向量化"文档页 — 经 WebFetch `www.volcengine.com/docs/82379/1409291`(2026-08-12,页内容
    大半 JS 渲染,只读到 doubao-embedding 1024 / 2048 维、¥0.5/¥0.3 两档价格片段)。
13. 腾讯混元 `GetEmbedding` API — `https://cloud.tencent.com/document/product/1729/102832`
    (经 context7 缓存,2026-08-12)。
    - 关键确认:`hunyuan-embedding` 1024 维固定;默认 5 次/秒;`Input` ≤ 1024 tokens,`InputList`
      数组 ≤ 50 条。
14. 腾讯混元 OpenAI 兼容 embedding — `https://cloud.tencent.com/document/product/1729/111007`
    (经 context7 缓存,2026-08-12)。
    - 关键确认:OpenAI 兼容 base URL `https://api.hunyuan.cloud.tencent.com/v1`,`model` 字段
      固定为 `hunyuan-embedding`,`dimensions` 不支持。
15. 腾讯混元免费额度 — `https://cloud.tencent.com/document/product/1729/97731`(经 context7 缓存)。
    - 关键确认:首次开通送 1,000,000 tokens / 1 年;Hunyuan-lite 永久免费;`hunyuan-TurboS`
      价格 ¥0.8 / 1M input,¥2 / 1M output。

### 二手 / 未在官方文档核实(标"未在官方文档核实"或"查不到")

- SiliconFlow bge-m3 / bge-reranker-v2-m3 是否默认对输出做 L2-normalize — **未在官方文档核实**。
- SiliconFlow 公开 SLA / 状态页 — **未在官方文档核实**(官方仅提"企业级 SLA 保障")。
- SiliconFlow 模型下架历史 — **未在官方文档核实**。
- Aliyun DashScope text-embedding-v4 0.5/0.1/0.05 元分别对应哪个维度 — **未在官方文档核实**。
- Aliyun DashScope qwen3-rerank 价格 — **未在官方文档核实**。
- Aliyun DashScope RPM / TPM 数字 — **查不到**(控制台动态渲染)。
- 智谱 BigModel 实时 embedding API 价格 — **未在官方文档核实**(定价页 2026-08-12 JS 渲染)。
- 智谱 BigModel OpenAI 兼容 base URL 具体值 — **未在官方文档核实**。
- 智谱 BigModel 实时 API(非 Batch)免费额度 — **未在官方文档核实**。
- 百度千帆 embedding / rerank 模型的输出维度(除 bge-large-zh 已知 1024 维) — **未在官方文档
  逐个核实**(Qwen3-Embedding 应支持 Matryoshka,**未在官方文档核实**)。
- 百度千帆 OpenAI 兼容 base URL — **未在官方文档核实**(SDK 文档路径存在 `/doc/qianfan/s/Hmh4suq26`
  但具体 URL 未抓取到)。
- 火山方舟 doubao-embedding / Qwen3-Reranker 的精确价格 / 维度 / RPM / TPM — **未在官方文档
  完整核实**(动态加载)。
- 火山方舟 rerank 模型清单 — **未在官方文档核实**。
- 腾讯混元 hunyuan-embedding 单价 — **未在官方文档核实**(官方只给了 hunyuan-TurboS
  ¥0.8/¥2 的 chat 价格,**embedding 价格文档未读到**)。

### 项目内代码锚点(只读,不动)

- `core/settings.py:91-104` — `HuggingFaceEmbedding(model_name="BAAI/bge-m3", normalize=True,
  embed_batch_size=int(os.environ.get("EMBED_BATCH_SIZE", "8")), max_length=512)`。
- `core/settings.py:126-171` — `RERANKER_MODEL=os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")`,
  `CrossEncoder(model_name, max_length=512)`。
- `core/index_manager.py:67-80` — `_create_index(dim: int = 1024)` + `faiss.IndexHNSWFlat(dim, 32)`,
  `efConstruction=200`、`efSearch=64`。
- `services/vector_search.py:1-10` — FAISS + bge-m3 检索入口。
- `.env.example:28-34` — 国内镜像下载 bge 模型指引。

— research-agent, 2026-08-12
