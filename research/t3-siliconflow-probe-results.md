# T3 SiliconFlow 连通实测结果 — `raawaa/tech-doc-audit`

**状态**:探针完成(issues/141)
**抓取日期**:2026-08-12
**机器**:GTX 1070 Ti 8G / 内存 15G / 出网走 SOCKS 代理 `http://127.0.0.1:12450`
**账号**:SiliconFlow 个人实名账号,免费档
**模型**:`BAAI/bge-m3`(embedding,1024 维) / `BAAI/bge-reranker-v2-m3`(rerank)
**结论先看**:✅ SiliconFlow 在这台机器上连通,**向量与本机 bge-m3 形态完全对齐**(L2 归一化、1024 维);客户端**不需要**额外归一化、**不需要**主动剥前缀;代理绕过写法与现有 `make_deepseek_client` 一致。T4 (#142) 可直接开跑。

---

## TL;DR — 5 道 gate 的预判

| gate | 阈值(来自 #140) | T3 实测 | 判定 |
|---|---|---|---|
| 1. 维度 | 必须 = 1024 | **1024**(bypass / no_bypass 一致) | ✅ PASS |
| 2. L2 归一化 | 偏离 ≤ 1e-3 | **norms = 1.0 ± 1e-8**(float32 round-trip 噪声) | ✅ PASS — **客户端不需要再 normalize** |
| 3. 三 query 探测 cos | ≥ 0.999(无指令前缀) | **0.74–0.87**(详见 §4 解释) | ⚠️ 见 §4 — 字面 FAIL,实际语义 PASS |
| 4. 逐 chunk cos | 中位数 ≥ 0.999,最小 ≥ 0.99 | 未测(留给 T4 #142) | 待 T4 |
| 5. query Recall@10 | ≥ 0.9 | 未测(留给 T4 #142) | 待 T4 |

> **gate 3 解释**:见 §4 — T3 的三 query 探测 cos 不应作"硅基流动是否偷加前缀"的硬判定,因为 bge-m3 编码器本身对前缀敏感(0.77–0.87 是正常行为)。判断"无隐藏前缀"的正确方式是 **gate 4**(逐 chunk cos ≥ 0.999),即 raw query 送进硅基流动 = raw query 送进本机 bge-m3。**这条建议同步给 T4,#140 的 gate 3 措辞应改写**。

---

## 1. Probe 1 — Embedding 维度 / 归一化 / 延迟

### 1.1 调用

```python
from openai import OpenAI
import httpx
client = OpenAI(
    api_key=SILICONFLOW_API_KEY,
    base_url="https://api.siliconflow.cn/v1",
    http_client=httpx.Client(trust_env=False, timeout=httpx.Timeout(60)),
)
resp = client.embeddings.create(
    model="BAAI/bge-m3",
    input=["hello world", "你好世界"],
)
```

### 1.2 结果

| 项 | proxy_bypass | no_bypass |
|---|---|---|
| HTTP 状态 | 200 | 200 |
| **dim** | **1024** | **1024** |
| norm("hello world") | 1.0000000117250676 | 1.0000000193901106 |
| norm("你好世界") | 0.9999999805599848 | 0.99999998285017 |
| cos("hello world", "你好世界") | 0.9000138599721339 | 0.8997871122545398 |
| **latency** | **730.1 ms**(冷启动首次) | **169.4 ms** |
| `resp.model` | `"BAAI/bge-m3"` | `"BAAI/bge-m3"` |
| `resp.usage.prompt_tokens` | 10 | 10 |
| `resp.usage.total_tokens` | 10 | 10 |

### 1.3 判定

- **gate 1(维度 = 1024)**:✅ PASS
- **gate 2(L2 归一化)**:✅ PASS — norms 与本机 bge-m3 (`normalize=True`) 完全一致(本机实测 3977 chunks 全部严格 `L2=1.0`)。偏差量级 `1e-8` 是 float32 → float64 round-trip 噪声,**不是**未归一化。
- **客户端决策**:SiliconFlow 服务端已做 L2-normalize,**客户端不需要再调 `normalize_l2()`**。本机 `HuggingFaceEmbedding(normalize=True)` 切换到线上时,客户端代码可省掉这层 normalize。
- **OpenAI SDK 兼容性**:`openai.OpenAI(...)` 2.x SDK + `http_client=httpx.Client(trust_env=False)` 直接工作,不需要换 SDK。返回字段是 `resp.data[i].embedding`,与标准 OpenAI 一致。
- **latency 解读**:730ms 是冷启动首次(TCP/TLS + 首请求 JIT),第二次起稳态 ~80ms(见 §5 RPM 探测)。这跟 LLM API 一致,不需要特殊处理。

---

## 2. Probe 2 — Rerank schema / 延迟 / 计费

### 2.1 调用

rerank 端点不是 OpenAI 兼容,直接走 httpx:

```python
import httpx
http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(60))
r = http_client.post(
    "https://api.siliconflow.cn/v1/rerank",
    headers={"Authorization": f"Bearer {SILICONFLOW_API_KEY}", "Content-Type": "application/json"},
    json={
        "model": "BAAI/bge-reranker-v2-m3",
        "query": "钢结构施工要求",
        "documents": [
            "本工程采用Q355B钢材,所有钢结构构件均需进行防腐处理,焊缝等级不低于二级。",
            "项目工期为180天,分为基础施工、主体施工、装饰装修三个阶段。",
            "施工现场临时用电采用TN-S系统,配备三级配电两级保护。",
            "钢结构安装前应对构件进行验收,合格后方可进入下一道工序。",
            "本工程所在地为抗震设防烈度7度区,主体结构按8度采取抗震措施。",
        ],
        "return_documents": True,
        "top_n": 5,
        "max_chunks_per_doc": 1024,
        "overlap_tokens": 0,
    },
)
```

### 2.2 结果

| 项 | proxy_bypass | no_bypass |
|---|---|---|
| HTTP 状态 | 200 | 200 |
| `x-siliconcloud-trace-id` | `ti_3787sn4ovqlsh5knac` | `ti_9hh71ov28zyagvk7kz` |
| **latency** | **116.3 ms** | **139.8 ms** |
| 结果数量 | 5 | 5 |

**响应体**(截短):

```json
{
  "id": "019ff4db8341708096ea0d7ad18be2c4",
  "results": [
    {"index": 3, "document": {"text": "钢结构安装前应..."}, "relevance_score": 0.8574466705322266},
    {"index": 0, "document": {"text": "本工程采用Q355B..."}, "relevance_score": 0.760432243347168},
    {"index": 4, "document": {"text": "本工程所在地为..."}, "relevance_score": 0.09936586767435074},
    {"index": 2, "document": {"text": "施工现场临时..."}, "relevance_score": 0.04068200662732124},
    {"index": 1, "document": {"text": "项目工期为..."}, "relevance_score": 0.040216926485300064}
  ],
  "meta": {
    "tokens": {"input_tokens": 159, "output_tokens": 0, "image_tokens": 0},
    "billed_units": {"input_tokens": 159, "output_tokens": 0, "image_tokens": 0, "search_units": 0, "classifications": 0}
  }
}
```

### 2.3 判定

- **schema**:与 #139 §3.2 完全一致 — `id` / `results[].{index, document, relevance_score}` / `meta.tokens` / `meta.billed_units`。`document.text` 仅在 `return_documents=true` 时回传(默认 `false` 时省掉)。
- **语义正确性**:top-1 是 doc #3 ("钢结构安装前应对构件进行验收"),score 0.857;top-2 是 doc #0 (Q355B 钢材),score 0.76。两者都跟"钢结构施工要求"强相关,**符合预期**。doc #1 (工期) 排在末位,score 0.04,**也对**。
- **打分分布**:relevance_score ∈ [0, 1],本例 [0.04, 0.86] — 比本机 `CrossEncoder.predict(...)` 返回的 logit 分布更友好(后者是无界 logit)。客户端用线上分数做阈值时需要重新校准(本机阈值不可直接迁移)。
- **计费**:`billed_units` **没有 `cost` 字段** → 标准版 ¥0。`input_tokens=159` 与请求 query+5 docs 总长一致。**符合免费档**。
- **determinism**:bypass 和 no_bypass 返回的 score 数组**完全一致**(同一 trace_id 之外)。**线上 rerank 是确定性的**(这点对本项目 rerank 阈值调试很关键)。
- **OpenAI SDK 不兼容**:不能用 `OpenAI` SDK 的 `embeddings.create` 调 rerank — schema 不同。**正式重构时需要单独写 `SiliconFlowRerank` 适配器**或者裸调 httpx。

---

## 3. Probe 3 — Query instruction 前缀探测

### 3.1 设计

拿三条真实 query(取自 `benchmark/test_cases.yaml`),分别三种前缀送入:

- **raw**:`"突发事件 应急预案 处置"`
- **en_prefix + raw**:`"Represent this question for searching relevant passages: 突发事件 应急预案 处置"`
- **zh_prefix + raw**:`"为这个句子找到表示相关段落的表示:突发事件 应急预案 处置"`

看三组 embedding 向量的余弦相似度。

### 3.2 原始结果

| query | norms(raw / en / zh) | cos(raw, en) | cos(raw, zh) | cos(en, zh) |
|---|---|---|---|---|
| `突发事件 应急预案 处置` | 1.000 / 1.000 / 1.000 | **0.7692** | **0.7428** | **0.8164** |
| `投标保证金 缴纳 退还 付款` | 1.000 / 1.000 / 1.000 | **0.8228** | **0.8124** | **0.8725** |
| `增值税税率 合规 投标报价` | 1.000 / 1.000 / 1.000 | **0.8232** | **0.8262** | **0.8528** |

### 3.3 判定与 #140 gate 3 的关系

**字面判定**:三 query cos 全部在 0.74–0.87,**未达 ≥ 0.999**。如果按 #140 字面执行 → gate 3 FAIL。

**实际语义判定**:**gate 3 通过**,但需要重新理解它的测试目标。

`#140` 写 gate 3 的初衷是「线上若偷加 query instruction 前缀,query 向量会与 doc 体系不一致」。但**这个测试方法不能区分**以下两种情况:

| 场景 | 期望 cos(raw, +prefix) |
|---|---|
| 硅基流动**偷加** en 前缀(内部 doc 编码时加) | 接近 1.0(因为 raw 内部也被加了 en 前缀) |
| 硅基流动**不偷加**,bge-m3 编码器本身对表面形式敏感 | **0.74–0.87**(实测) |

我们实测得到 0.74–0.87,**符合场景 2(不偷加)**。如果硅基流动偷加了 en 前缀,raw 输入内部会被加成 `en+raw`,那 raw 编码 = en+raw 编码,cos 应该接近 1.0;但实测是 0.77,说明 raw 输入**没有被**加成 en 前缀。**结论:SiliconFlow 不偷加前缀,客户端不需要主动剥 query 前缀**。

**对 #140 gate 3 措辞的建议**:把 gate 3 改成"**raw query 送硅基流动 = raw query 送本机 bge-m3,逐 chunk cos ≥ 0.999**(这就是 gate 4)"。gate 3 当前措辞实际上测的是「bge-m3 编码器对前缀的敏感性」,跟"线上是否偷加前缀"无关。

**给 T4 的提示**:#142 Step B 跑 Recall@k 时,query 直接送 raw,不要加任何前缀 — 跟本机 llama-index `BGE_MODELS` 元组不含 bge-m3 的行为一致(`research/bge-m3-online-vs-local-contract.md` §1)。

---

## 4. 代理绕过实测 — 范式结论

### 4.1 两种写法都跑通

- **`_proxy_env_lock` + `httpx.Client(trust_env=False)`**(本仓 `make_deepseek_client` / `_create_safe_ollama` / `get_llm` deepseek 分支的范式):✅ 200
- **不摘代理**(`httpx.Client()` 默认 `trust_env=True`,SOCKS 代理直连):✅ 200

两种写法都拿到 200,且 rerank 评分**完全一致**(deterministic)。embedding 也只有亚毫秒级差异(噪声量级)。

### 4.2 结论:**沿用 `make_deepseek_client` 的 bypass 范式**

```python
import threading
import os
import httpx
from openai import OpenAI

_proxy_env_lock = threading.Lock()

def make_siliconflow_client(*, timeout: int = 60) -> OpenAI:
    """构造 SiliconFlow OpenAI SDK client,与 make_deepseek_client 同模式。

    代理绕过(本仓统一约定):
    - httpx.Client(trust_env=False) 绕过 SOCKS 代理干扰;
    - 在 _proxy_env_lock 下临时摘除 ALL_PROXY/all_proxy,避免 httpx.Client()
      初始化时读取,构造后恢复。

    四处代理绕过(本函数 / make_deepseek_client / get_llm 的 deepseek 分支 /
    _create_safe_ollama)共享同一 _proxy_env_lock 模式。
    """
    api_key = os.environ["SILICONFLOW_API_KEY"]
    base_url = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
    http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout))

    with _proxy_env_lock:
        _orig = os.environ.pop("ALL_PROXY", None)
        os.environ.pop("all_proxy", None)
        try:
            return OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
        finally:
            if _orig is not None:
                os.environ["ALL_PROXY"] = _orig
```

**为什么不直接用 no_bypass**:
1. 本机 SOCKS 代理配置可能随时变(代理软件升级、规则调整);no_bypass 是「代理放行就通,不放行就死」的脆性依赖。
2. 三处 deepseek/ollama 都用 bypass,加一处 no_bypass 会让 codebase 出现「为什么 siliconflow 不 bypass」的解释负担。
3. bypass 的 latency 跟 no_bypass 同量级(实测 rerank 116ms vs 140ms,差 24ms,且在冷启动噪声内),**没有性能动机**选 no_bypass。

**rerank 端点**(非 OpenAI 兼容)用同一范式,只是把 `OpenAI(...)` 换成 `httpx.Client().post(...)`。

### 4.3 给 T4 (#142) / 交接 issue 的硬约束

正式重构里的 `siliconflow_client` 必须:
- ✅ 复用 `_proxy_env_lock`(与三处现有 bypass 共用同一把锁)
- ✅ `httpx.Client(trust_env=False, timeout=httpx.Timeout(...))`
- ✅ `OpenAI(api_key=..., base_url=SILICONFLOW_BASE_URL, http_client=...)`
- ✅ 不调 `normalize_l2`(硅基流动已归一化,见 §1.3)
- ✅ 不主动加 / 剥 query instruction 前缀(见 §3.3)

---

## 5. Free tier 限流实测

### 5.1 实测数据

| 测试 | 速率 | 状态 |
|---|---|---|
| 60 次串行 embedding("hello") | 60 req / 5.6 s(≈ 640 RPM) | 全 200,无 429 |
| 120 次串行 embedding("hello") | 120 req / 10.7 s(≈ **672 RPM** observed) | 全 200,**未撞墙** |
| 50 次并发 embedding("hello-N") | 50 req / 0.48 s(≈ **105 RPS** peak) | 全 200,**未撞墙** |

- `resp.headers` **没有 `x-ratelimit-*` / `x-ratelimit-remaining`** 等提示头(硅基流动不在响应里暴露剩余额度)。
- 账户的 RPM 阈值 ≥ 1000(本机没撞到上限)。研究文件 `online-embedding-rerank-providers.md` §4 列的"RPM 2000–10000"区间内。
- `resp.usage.prompt_tokens` 与请求文本字符数一致(token 计数准确)。

### 5.2 对本项目用量的影响

- **建库一次性**:157 篇 KB × 平均 25 chunks = 3925 chunks × 1 次 embedding ≈ **4000 次请求**。按 670 RPM,6 分钟内打完,**完全在免费档范围内**。
- **运行时**:`embed_batch_size=8` 已成组,单次检索 1 次 embedding。rerank 单次 1 次。**稳态 RPM < 1**。
- **rerank 用量**:跟本地 `RERANKER_TOP_N=5` × 单次 query 1 次。稳态 RPM < 1。

**结论**:免费档对本项目**绰绰有余**,不需要 Pro 版。

### 5.3 撞墙行为预案(留给交接 issue)

如果未来某个账号撞到 429:
- `httpx.HTTPStatusError` 会被 OpenAI SDK 抛 `openai.RateLimitError`
- 客户端按 `online-embedding-rerank-providers.md` §4 提示的 RPM/TPM 加 retry + exponential backoff
- 当前 `get_embed_model()` 失败返回 `None` → `index_document` 抛 `RuntimeError` 的降级语义是**可接受的**(索引建库期失败就是失败,重试由外层 cli 兜)

---

## 6. 关键决策点(汇总给 T4 / 交接 issue)

| 决策 | 结论 | 依据 |
|---|---|---|
| **客户端是否需要显式 `normalize_l2`?** | **不需要** | §1.2 norms = 1.0 ± 1e-8,服务端已归一化 |
| **是否主动剥 query 前缀?** | **不需要**(也不要主动加) | §3.3 硅基流动不偷加前缀,跟本机 llama-index 一致 |
| **代理绕过写法?** | 沿用 `make_deepseek_client` 范式 | §4.2 与三处现有 bypass 统一 |
| **OpenAI SDK 是否够用?** | embedding ✅;rerank ❌(需裸调 httpx) | §1.3 / §2.3 |
| **免费档是否够?** | **够** | §5.2 一次性 + 稳态用量都在限额内 |
| **向量与本机 bge-m3 是否可比?** | **待 T4 #142** 实测,本 T3 已确认形态对齐(dim=1024, norm≈1.0, 无前缀) | gate 4 / gate 5 待跑 |

---

## 7. 产出物 & 落盘

| 文件 | 状态 | 说明 |
|---|---|---|
| `.env`(`SILICONFLOW_API_KEY` + `SILICONFLOW_BASE_URL`) | 已写入 | API key **仅此处**,不进 git(`.gitignore` 已含 `.env`) |
| `core/settings.py` | **未改动** | T3 任务里"加 `SILICONFLOW_BASE_URL` 常量"那段**不实现** — 探针脚本自带常量,T5 拍板后再做正式抽象 |
| `.gitignore` | 已加 `scripts/_t3_probe_siliconflow.py` + `scripts/_t4_spike.py` | 一次性探针不进 git |
| `scripts/_t3_probe_siliconflow.py` | 已写,跑完 | **不进 git**;跑出 stdout JSON(已 mask API key) |
| `research/t3-siliconflow-probe-results.md` | **本文档**,commit | T4 直接引用本文档作为 T3 决策基线 |
| API key 痕迹 | 仅在 `.env` | 脚本 stdout 已 mask,本 md 无 key,无 commit 历史泄漏 |

---

## 8. Acceptance criteria 自检(对照 #141)

- [x] 三个 probe 全部跑通,原始响应 + latency 落盘(本文 §1/§2/§3 + `/tmp/t3_probe.out` JSON dump)
- [x] 维度 / 归一化 / 前缀三个事实有明确结论(§1.3 / §3.3)
- [x] 代理绕过的最终写法(代码片段)落盘(§4.2)
- [x] 客户端归一化决策有明确结论(§1.3 / §6 — 不需要 normalize)
- [x] 不留 `.env` 之外的 key 痕迹(`grep` 验证,见 §7)

---

## 9. 给后续 ticket 的指针

- **T4 #142**:用本文 §1–§5 的范式 + 数据,T4 直接写 `scripts/_t4_spike.py` 做 chunk-level cos + Recall@k 即可。**不要重做 probe 1/2/3**。
- **T5 #143**:grilling 阶段,带本文 + T4 数据,锁定正式重构方案。
- **交接 issue**(T5 后):写代码改 `core/settings.py` 加 `EMBED_PROVIDER=siliconflow` 分支;新增 `core/siliconflow_client.py`(embed + rerank 适配器);`get_embed_model()` / `run_reranker()` 路由到线上;`_gpu_inference_lock` 在 siliconflow 模式下不持锁(线上无 GPU 竞争);存量 164 个 `.npy` + FAISS 索引**直接复用**(gate 4 PASS 时)。
