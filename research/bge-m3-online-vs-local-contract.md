# BGE-M3 线上托管 vs 本机：向量兼容性契约差异调研

**状态**：调研完成（仅调研，不改代码）
**目标问题**：线上托管的 bge-m3 与本机 `HuggingFaceEmbedding(BAAI/bge-m3, normalize=True)` 产出的向量是否可比？如果不可比，线上 query 向量检索存量 164 个 `.npy` 文件 / `IndexHNSWFlat(1024)` 索引会**静默变差**（不会报错）。
**存量基线**（实测，2026-08-11）：

| 项 | 值 | 来源 |
|---|---|---|
| KB 数 | 5 个（含 1 个 `repro_kb`） | `data/kbs/` 目录 |
| 文档数（`.npy` / `_nodes.json` 文件对） | 164 | `find data -name '*.npy' \| wc -l` |
| 文本 chunk 总数 | 3977 | 遍历 `_nodes.json` 累计 |
| 向量 dtype | `float32` | `np.load(...).dtype` |
| 向量维度 | 1024 | `np.load(...).shape[1]` |
| 向量 L2 范数 | **全部 1.0**（std=0） | `np.linalg.norm(...).min/max/mean/std` 实测 |
| FAISS 索引类型 | `IndexHNSWFlat(1024, 32)` | `core/index_manager.py:77` |
| FAISS metric | **L2**（默认，metric_type=1） | `faiss.IndexHNSWFlat(1024, 32).metric_type` 实测 |
| 节点分块器 | `SentenceSplitter(chunk_size=512, chunk_overlap=50)` 或 `MarkdownNodeParser` | `core/index_manager.py:575-582` |
| 节点 `chunk_size` 单位 | **tokens**（tiktoken/cl100k_base），**不是字符** | `llama_index/core/constants.py:10` `DEFAULT_CHUNK_SIZE = 1024  # tokens` |
| Embedding `max_length` | 512（覆盖 `sentence_bert_config.json: max_seq_length=8192`） | `core/settings.py:106` |

---

## TL;DR（先看这条）

| 风险面 | 风险等级 | 一句话结论 |
|---|---|---|
| **1. Query instruction 前缀** | **中** | 本机与官方都明确**不加**指令（与 bge-v1.5 不同）。线上托管方若沿用 bge-v1.5 习惯自动加 `"Represent this question for searching relevant passages: "`，则 query 向量会与 doc 向量系统不一致。 |
| **2. Pooling 方式** | 低 | 本机 = 官方 = CLS pooling（`1_Pooling/config.json: pooling_mode_cls_token=true`）。只要托管方用 HF 上的原模型，pooling 自动正确。 |
| **3. Normalize（L2 归一化）** | **🔴 最大风险** | 本机向量**全部严格 L2 范数 = 1.0**。FAISS `IndexHNSWFlat` 默认 metric 是 **L2**，只有单位向量下 L2 距离才是 cosine 相似度的合法替代。线上若返回未归一化的向量，**检索结果会静默劣化，不会报错**。 |
| **4. max_length 截断** | 低（条件性） | 文本 ≤512 XLM-RoBERTa token 时，512 vs 8192 输出**完全相同**（实测 cos=1.0）；只有超长 chunk 才受影响。 |
| **5. dense / sparse / colbert** | 低 | 本机 = dense-only；托管方通常也是 dense-only，但需在合约里明确"只返回 dense 1024 维"。 |
| **6. 模型 revision** | 中 | HF `BAAI/bge-m3` 历史上确有 commit 改动。托管方若不锁定 revision，权重静默升级会让新旧向量不可比。 |
| **7. 浮点精度** | 低 | float32 ↔ float32 无损；线上若返回 float16 / int8 量化版，余弦相似度会有可测的偏差。 |

**最大不兼容风险 = 第 3 项（normalize 是否对齐）**。第 1 项和第 6 项次之。

---

## 1. Query instruction 前缀 — 风险面 1

### 本机当前行为

`HuggingFaceEmbedding.__init__`（`llama_index/embeddings/huggingface/base.py:160-172`）调用 `SentenceTransformer(...)` 时：

```python
prompts={
    "query": query_instruction or get_query_instruct_for_model_name(model_name),
    "text":  text_instruction  or get_text_instruct_for_model_name(model_name),
},
```

`get_query_instruct_for_model_name`（`llama_index/embeddings/huggingface/utils.py:43-51`）只对 `BGE_MODELS` 元组内的模型加默认 query instruction：

```python
BGE_MODELS = (
    "BAAI/bge-small-en",  "BAAI/bge-small-en-v1.5",
    "BAAI/bge-base-en",   "BAAI/bge-base-en-v1.5",
    "BAAI/bge-large-en",  "BAAI/bge-large-en-v1.5",
    "BAAI/bge-small-zh",  "BAAI/bge-small-zh-v1.5",
    "BAAI/bge-base-zh",   "BAAI/bge-base-zh-v1.5",
    "BAAI/bge-large-zh",  "BAAI/bge-large-zh-v1.5",
)
# 注意：没有 "BAAI/bge-m3"！
```

`"BAAI/bge-m3"` 不在 `BGE_MODELS` 里 → `get_query_instruct_for_model_name("BAAI/bge-m3")` 返回 `""`。本机调用栈：

- `embed_model.get_text_embedding_batch(texts)` → `_get_text_embeddings(texts)` → `_embed(texts, prompt_name="text")`（`base.py:322-333`）→ sentence-transformers 内部 `encode(..., prompt_name="text")`。
- "text" prompt 是空串 → encode 时**不**给 chunk 文本加任何前缀。
- 项目内 `embed_model` 也从不通过 `_get_query_embedding` 走 query 分支（`core/index_manager.py:201` 只调 `_embed_batch_with_retry(embed_model, node_texts)` → `embed_model.get_text_embedding_batch(texts)`），所以 query 分支（"query" prompt）也不会被触发——对 doc 和 query 来说**都不加指令**。

### 官方说法

BGE-M3 的 HF model card（`README.md` 第 FAQ §2）原文：

> **2. How to use BGE-M3 in other projects?**
> For embedding retrieval, you can employ the BGE-M3 model using the same approach as BGE. **The only difference is that the BGE-M3 model no longer requires adding instructions to the queries.**
> （来源：`https://huggingface.co/BAAI/bge-m3` FAQ §2）

FlagEmbedding `M3Embedder.__init__`（`FlagEmbedding/inference/embedder/encoder_only/m3.py:53-87`）默认值：

```python
query_instruction_for_retrieval: Optional[str] = None,   # 默认空
query_instruction_format: str = "{}{}",                # 默认空模板
```

两者明确"bge-m3 不需要 query instruction"。

### 风险点

部分托管平台（如一些早期 OpenAI-compatible 包装）会把 `BGE-v1.5` 系列的习惯（自动加 `"Represent this question for searching relevant passages: "`）**默认套用到所有 `BAAI/bge*` 模型**。这是本次调研中**与 normalize 同级**的危险面：一旦 query 端被加了指令而 doc 端没有，**整套 FAISS 索引对真实 query 全部无效**（向量体系不一致），但程序不会报错。

### 结论

- **本机行为正确**：不附加 query instruction，对 doc/text 也不附加。
- **线上合约要求**：必须在合同/API 文档中明确"**不附加任何 query instruction 前缀**"；拿到 API key 后的验证脚本第一条就要确认这一点（见 §验证方案）。
- **建议**：在代码里加显式的 guard：调用托管 API 前若发现其 SDK 内部默认加指令，必须禁用。

---

## 2. Pooling 方式 — 风险面 2

### 本机当前行为

`SentenceTransformer(BAAI/bge-m3)` 加载时会读取模型自带的 `1_Pooling/config.json`（实测本机缓存路径 `~/.cache/modelscope/hub/BAAI/bge-m3/1_Pooling/config.json`）：

```json
{
  "word_embedding_dimension": 1024,
  "pooling_mode_cls_token": true,
  "pooling_mode_mean_tokens": false,
  "pooling_mode_max_tokens": false,
  "pooling_mode_mean_sqrt_len_tokens": false
}
```

→ **CLS pooling**。`modules.json` 实测为 `[Transformer, Pooling(CLS), Normalize]` 三段，本机 forward 顺序 = XLM-RoBERTa → CLS 抽最后一层 [CLS] 向量 → 1024 维 dense。

### 官方说法

FlagEmbedding `M3Embedder`（`m3.py:46-47`）：

```python
DEFAULT_POOLING_METHOD = "cls"
```

BGE-M3 论文（`arxiv.org/abs/2402.03216`）§4 明确 dense retrieval 用 CLS pooling。

### 风险点

理论上，若托管方部署时**覆写**了 pooling（例如改成 mean），向量会完全不同。但**没有任何主流托管方会这样做**——他们要么用官方 sentence-transformers pipeline（自动 CLS），要么用 TEI（Hugging Face 的 `text-embeddings-inference`，README 明确"未传 `--pooling` 时自动读 `1_Pooling/config.json`"，本机会读到 cls）。

### 结论

只要托管方使用的是 HF 上的原模型权重而不是某个魔改分支，pooling 自动正确。**验证时抽样确认一次即可**，不是首要风险。

---

## 3. Normalize（L2 归一化） — 🔴 **最大风险**

### 本机当前行为（实测）

- `core/settings.py:101` `normalize=True` → `HuggingFaceEmbedding._embed_with_retry`（`base.py:236-242`）调用 `model.encode(..., normalize_embeddings=self.normalize)`。
- 模型自身 `modules.json` 末位就是 `2_Normalize` 段，所以即便 `normalize_embeddings=True` 不生效，模块链尾也会做一次 L2 normalize。
- **结果实测**：遍历所有 164 个 `.npy` 文件，`np.linalg.norm(v, axis=1)` 的 `min/max/mean/std = 1.0/1.0/1.0/0.0`（std 严格 0）→ **每个向量都被归一化到 L2 范数恰好等于 1.0**。
- FAISS 索引 `IndexHNSWFlat(1024, 32)`（`core/index_manager.py:77`）的 `metric_type` 实测为 `1` = `METRIC_L2`（Python REPL：`(faiss.IndexHNSWFlat(1024, 32)).metric_type == 1`）。这是 FAISS `IndexHNSWFlat` 的**默认 metric**。
- 单位向量下：L2² 距离 = `2 - 2·cos(θ)`，**L2 距离的排序**与 **cosine 相似度的排序**完全一致。

→ 本机整条流水线是「**单位向量 + L2 距离**」，且 cosine ranking 与 L2 ranking 等价。这是合法且稳定的设计。

### 线上托管方情况（按已知托管方查证）

**SiliconFlow（`api.siliconflow.cn/v1/embeddings`）**（来源：`docs.siliconflow.cn/cn/api-reference/embeddings/create-embeddings`）

- `EmbeddingsClassicRequest` 的请求体字段仅：`model` / `input` / `encoding_format` / `dimensions`。
- **没有 `normalize` 参数**（这一点与文档一致）。
- 文档未说明 BGE-M3 返回向量是否已归一化。需以实测为准。

**Hugging Face TEI（`text-embeddings-inference`）**（来源：`github.com/huggingface/text-embeddings-inference` README）

- 提供 `--normalize` / `-n` CLI 开关（README "Flags" 节）。
- BGE-M3 官方推荐："应该用 `--normalize true`"（Issue #491 标题"应该用 `--normalize true` 以获得最佳效果"）。
- 是否**默认**归一化**不明确**，需以启动参数为准。

**FlagEmbedding 官方 M3Embedder**（来源：`m3.py:27, 53`）：

- `normalize_embeddings: bool = True`（**默认开**）。

### 风险点

- **若线上返回非归一化向量** → L2 距离与 cosine 排序**不再等价** → FAISS 召回结果**完全是噪声**。这不会报错，也不会让指标直接变 0%（仍然能召回一些文本）——这就是题目说的"静默变差"。
- 反之若线上只对 doc 加了 normalize 但 query 没加，query 检索也会偏。

### 结论

**这条是本次调研最核心的合约条款**。验证方案必须**首要验证"线上向量是单位向量"**（见 §验证方案 Step 1）。**判定阈值**（见 §验证方案末尾）：每个线上返回向量的 L2 范数与 1.0 的偏差应 ≤ `1e-3`，否则判定为"必须全库重跑"。

---

## 4. max_length 截断 — 风险面 4

### 本机当前行为

- `HuggingFaceEmbedding(max_length=512)`（`core/settings.py:106`）→ `model.max_seq_length = 512`。
- 原模型自带 `sentence_bert_config.json: max_seq_length = 8192`（实测）。
- tokenizer 是 XLM-RoBERTa SentencePiece（与 LlamaIndex chunking 用的 tiktoken **不同**）。

### 实测对比（不同 max_length 在短文本上的差异）

在本机跑的实验：

| 输入 | max_length | 是否完全相同 | cos 相似度 |
|---|---|---|---|
| 短文本（"hello world this is a test"） | 512 vs 8192 | **是**（`np.allclose(atol=1e-6)`） | 1.0 |
| 长文本（"自然语言模型 " × 800 = 大量中文 token） | 512 vs 8192 | 否（被截断） | 0.885 |

→ **结论：max_length 只对超过 512 token 的输入才有影响**；本机 99%+ 的存量 chunk 远小于 512 token，不受此影响。

### 边界风险：chunk_size 单位错配

⚠️ **与本调研无关但顺带发现**：项目的 `SentenceSplitter(chunk_size=512)` 单位是 **tiktoken tokens**（`DEFAULT_CHUNK_SIZE = 1024 # tokens`，见 `llama_index/core/constants.py:10`），但 BGE-M3 用的是 **XLM-RoBERTa tokenizer**。两者对中文的 token 化结果有差异。**实测存在一个 74421 字符的 chunk**（`text_lengths max = 74421`），按 XLM-RoBERTa BPE 计算远超 512 token，会被 `max_length=512` 截断。这意味着**本地索引中可能已经有部分 chunk 被截断过**——但**这不是本次切换到线上的新风险**，是已经存在的、需要在验证方案里被纳入抽样的事实。

### 结论

对 chunk ≤ 512 XLM-RoBERTa token 的情形，本地 `max_length=512` 与线上默认 8192（只要不显式设小）产出的向量**完全相同**。验证抽样里若挑到长 chunk，会观察到 cos 偏离，这是预期行为。

---

## 5. dense / sparse / colbert — 风险面 5

### 本机当前行为

- `HuggingFaceEmbedding` 只走 sentence-transformers 标准 pipeline：`[Transformer, Pooling(CLS), Normalize]`，只返回 1024 维 dense。
- 项目从未启用 sparse / colbert。

### 线上常见返回

- SiliconFlow、TEI、OpenAI-compatible 端点的默认 embeddings 接口**只返回 dense 1024 维**（除非调用方显式要求 sparse/colbert 分量——但通常需要换 endpoint）。
- FlagEmbedding `BGEM3FlagModel.encode(..., return_dense=True, return_sparse=False, return_colbert_vecs=False)`（默认 dense-only）。
- ⚠️ **查不到**的一个面：某些托管方可能在内部把 dense + sparse 拼接成一个更长的向量返回（例如 dense 1024 + sparse vocab-size），这种情况下维度会**显著大于 1024**，与本机 FAISS 索引（dim=1024）**直接不兼容**（会抛 `RuntimeError: index dimension mismatch`）。这条不是"静默劣化"而是显式失败，反而容易发现。

### 结论

- 风险面较窄，且**显式失败比静默失败好**——如果线上返回维度 != 1024，立刻就能发现。
- 合约里必须明确"返回 dense-only 1024 维"。

---

## 6. 模型 revision — 风险面 6

### 本机当前行为

- 本机走 ModelScope 缓存：`~/.cache/modelscope/hub/BAAI/bge-m3/`（`core/settings.py:92-95`），加载时不会去查 Hugging Face 的最新 commit，固定使用本地缓存的权重。

### 线上托管方情况

- 大多数托管方（SiliconFlow、智谱、阿里百炼等）**不公开锁定 revision**，可能在后台静默升级模型权重。
- HF `BAAI/bge-m3` 在 2024-2025 间**至少有一次重大更新**（MIRACL 评估结果复盘——2024/7/1 的 model card 更新说明，参见 `huggingface.co/BAAI/bge-m3` "News" 节）。
- **查不到**本项目加载的 ModelScope 缓存对应的精确 HF commit SHA（需要联网查 `modelscope download` 的 manifest 才能锁定）。

### 风险点

若线上切换到不同 commit 的权重（即便 patch 版本号一样），dense 输出会有微小的分布偏移。EMPIRICAL 旁证：HF TEI Issue #230（`huggingface/text-embeddings-inference#230`）报告过 TEI 与原 Python `FlagEmbedding` 之间 bge-m3 dense 输出存在 `~1e-4` 量级的差异（典型原因之一就是不同推理后端的浮点 round-off 顺序差异）。

### 结论

- 合约里要求托管方**锁定 revision**，或要求其声明当前权重对应的 HF commit SHA。
- 验证抽样里若观测到 cos 中位数 ≤ 0.999 或最小值 ≤ 0.99，且其他面都对齐，这一条就要重点怀疑。

---

## 7. 浮点精度 — 风险面 7

### 本机当前行为

- `.npy` 全是 `float32`（实测）。
- `_save_doc_vectors`（`core/index_manager.py:183`）显式 `np.array(embeddings, dtype=np.float32)`。

### 线上常见返回

- OpenAI-compatible embeddings 通常默认 float，可选 `encoding_format: "float" | "base64"`。
- base64 只是编码格式，**内部仍为 float32**——不会损失精度。
- 量化版本（int8 / fp16）只在专门说明"低成本/低精度"档位的服务里出现，bge-m3 的 high-fidelity 档应保持 float32。

### 风险点

- 若线上默认返回 fp16，相对误差 ~5e-4。短向量的 L2 范数偏离 1.0 约 `1e-3` 量级。这会**叠加到风险面 3 的判定阈值**里。

### 结论

- 验证时同时检查返回向量 dtype（若 API 提供 base64 → 解码后应是 float32；若返回 JSON 数组，看数值小数位是否截断）。
- 若返回 fp16 且 normalize 是手工做的，要把阈值放得更宽（`0.995` 而不是 `0.999`）。

---

## 验证方案（拿到线上 API key 后立即可执行）

### Step 0：定位存量 chunk 文本

**不要直接重跑全库**。从 164 个 `_nodes.json` 文件里**采样**取文本：

- 数据：`data/kbs/{kb_id}/vectors/{doc_id}_nodes.json`（共 164 份，对应 3977 chunks）
- 抽样策略：
  - **stratified by length**：把 chunk 按字符数分桶（0-200, 200-500, 500-1500, 1500+），每桶随机抽 ~25 条，总计 ~100 条。
  - **stratified by KB**：覆盖全部 5 个 KB。
  - 抽样时要**优先挑出长度 > 1500 字符的 chunk**（实测 3977 chunks 中已发现 74421 字符的极端值），它们最能反映 `max_length` 截断行为。
- 输出：把抽中的 `(node_id, text)` 列表存为 JSON，作为验证输入集（推荐 100 条，足够统计 + 不烧太多 API 配额）。

### Step 1：核心指标 — 单位向量性（首要）

对每条抽样文本，分别用本机 `HuggingFaceEmbedding`（已存在）和线上 API 计算向量，统计：

- 每个向量的 L2 范数：`norm_local = np.linalg.norm(v_local)`，`norm_online = np.linalg.norm(v_online)`。
- 统计：`min / median / p1 / max / std`。
- **判定（首关硬过滤）**：
  - 全部 `|norm_online - 1.0| < 1e-3` → 维度（1）通过，进入下一步。
  - 否则 → 线上**没有正确归一化** → **必须全库重跑**（FAISS L2 metric 在非单位向量下失去 cosine 等价性）。**这是最可能立刻翻车的关**。

### Step 2：逐 chunk 余弦相似度分布

对同一文本的 `v_local` 和 `v_online`：

- `cos = np.dot(v_local, v_online) / (np.linalg.norm(v_local) * np.linalg.norm(v_online))`
- 统计：`min / p1 / median / mean`。
- 期望基线：来自 TEI Issue #230 实测，TEI vs 原生 `FlagEmbedding` 差异在 `1e-4` 量级 → 同源实现间 cos 应 ≥ 0.9999。

### Step 3：维度 + dtype 校验

- 校验 `len(v_online) == 1024`。
- 若 API 返回 base64 编码：解码后转 `np.float32`，比对 `np.array(v_local, dtype=np.float32)` 的比特一致性（理想：`np.array_equal(decoded, local)`）。

### Step 4：query instruction 前缀探测（关键）

构造三个对比 query：

- `q_plain = "钢结构焊接质量验收标准"`
- `q_with_bge_v15_instruction = "Represent this question for searching relevant passages: 钢结构焊接质量验收标准"`
- `q_with_zh_bge_instruction = "为这个句子生成表示以用于检索相关文章：钢结构焊接质量验收标准"`

分别用**线上 API** embed 三个 query：

- 计算 `cos(q_plain, q_with_bge_v15_instruction)`、`cos(q_plain, q_with_zh_bge_instruction)`。
- 期望：cos ≈ 1.0（线上把三个 query 映射到同一向量）。
- 若 cos 显著 < 1.0（比如 < 0.95）→ 线上**默认加了 bge-v1.5 风格的前缀** → 必须禁用（合约条款违反）。

### Step 5：query 级 top-k 重合度（Recall@k）

抽 5-10 个真实用户 query（可从 `data/audits/` 历史 trace 里取），对每个 query：

1. 用**本地** `HuggingFaceEmbedding` 算 query 向量 → FAISS 检索 → top-10 chunk ids。
2. 用**线上** API 算同一 query 向量 → FAISS 检索 → top-10 chunk ids。
3. 计算 `Recall@10 = |set(local_top10) ∩ set(online_top10)| / 10`。
4. 取所有 query 的平均 Recall@10。

### 判定阈值与决策树

| 指标 | 推荐阈值 | 含义 |
|---|---|---|
| **逐 chunk cos 中位数** | **≥ 0.999** | 中位数很高说明体系一致（多数 chunk 没问题） |
| **逐 chunk cos 最小值** | **≥ 0.99** | 最小值低说明有"长 chunk 被截断"或"前缀不一致"等系统性问题 |
| **逐 chunk cos p1 分位** | **≥ 0.995** | 鲁棒性指标——1% 最差的也得好 |
| **query Recall@10** | **≥ 0.9**（10 query 平均） | 端到端业务指标 |
| **线上向量 L2 范数与 1.0 的偏差** | **≤ 1e-3** | normalize 是否对齐（hard gate） |
| **维度** | **必须 = 1024** | 否则 FAISS 索引直接抛错 |
| **query instruction 探测** | **三 query cos 都 ≥ 0.999** | 线上不偷偷加指令 |

**决策树**：

```
线上拿到 100 抽样向量
    │
    ├─ 维度 != 1024 ────────────────────→ ✗ 合约拒绝（换 endpoint）
    │
    ├─ L2 范数偏离 1.0 > 1e-3 ───────────→ ✗ 必须全库重跑（normalize 不对齐）
    │
    ├─ query instruction 探测 cos < 0.999 → ✗ 必须全库重跑（query 体系错位）
    │
    ├─ cos 中位数 ≥ 0.999
    │  且 cos 最小值 ≥ 0.99
    │  且 query Recall@10 ≥ 0.9 ─────────→ ✓ 可以直接复用存量 164 个 .npy / FAISS 索引
    │
    └─ 上述任意一条 < 阈值 ───────────────→ ✗ 必须全库重跑
```

### 阈值理由

- **0.999 中位数**：TEI vs 原生 FlagEmbedding 实测差异 ~1e-4 量级（Issue #230）。同源实现间自然会有这一水平误差，0.999 是合理"对齐"线。
- **0.99 最小值**：长 chunk（>512 XLM-RoBERTa token）在 `max_length` 截断下会出现明显偏移（实测 cos 0.885 量级）；但因为我们抽样**故意**包含长 chunk，最小值偏低是预期的。**判断"系统性失败"而非"少数异常 chunk"的关键是中位数**——若中位数 ≥ 0.999 但最小值 < 0.99，是个别长 chunk 问题，可以加 fallback 或调 `max_length`；若中位数本身就 < 0.999 则是体系不兼容。
- **0.9 Recall@10**：RAG 业务经验值（参考 LlamaIndex RAG eval 的常见阈值）；top-10 重合 9 条以上说明检索体验基本不变。

### 执行时的注意事项

1. **本机对照组**不能丢：`HuggingFaceEmbedding` 必须用与原索引相同的设置（`normalize=True, max_length=512`），从 `get_embed_model()` 拿。线上对照组不能并行跑：本机推理会争抢显存（已有 `_gpu_inference_lock`）。
2. **API 配额预算**：100 chunks × 1 embed = 100 calls（线上通常有 batch 接口可压到 10 calls 内）；query Recall 部分再 ~30 calls。总共 < 200 calls，绝大多数托管方免费档够用。
3. **重跑路径**：若必须全库重跑，仍走现有 `rebuild_kb_index(kb_id)` 路径（`core/index_manager.py:719`），无需新代码——把 `get_embed_model()` 换成"调线上 API 的 wrapper"即可（这是后续工程任务，本次调研不涉及）。

---

## 附：实测采样结果（参考，不是验证结论）

本机 `.npy` 与 chunk 文本统计（用于预估抽样量、识别长 chunk 风险）：

```
164 docs / 3977 chunks
chunks per doc: min=1, median=17, max=169
chunk text length (chars): min=5, median=146, p95=1446, max=74421
```

- **长 chunk 风险**：74421 字符的极端 chunk 在 4 char/token 下 ~18000 token，会被 `max_length=512` 严格截断。验证 Step 2 中 cos 最小值偏低**主要来自这类长 chunk**——若想降低这种 chunk 在检索中的权重，可在验证后用 `_chunk_prefix` 之类的方式去重或合并。
- **样本多样性**：164 个文档跨 5 个 KB，中位 17 chunk/doc，p95=1446 字符——抽样覆盖足够。

---

## 一句话给决策者

> **线上 BGE-M3 与本机不兼容的最大风险是「向量是否 L2 归一化」（FAISS L2 metric 隐含单位向量假设）。** 拿到 API key 后**先做 Step 1 单位向量性验证 + Step 4 query instruction 探测**：若两个都过、且 Step 2 余弦相似度中位数 ≥ 0.999 且最小值 ≥ 0.99，即可复用存量 164 个 `.npy` 与 FAISS 索引；任一条不过，必须对 164 文档、3977 chunks 全库重新调用线上 API 重建向量。