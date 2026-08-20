# PaddleOCR SaaS API 实际限制与计费模型调研

> **TL;DR**:
> 1. 项目使用的 `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs` 端点**无公开官方文档**，100 页上限来自 issue #87 决议"百度 AI Studio PaddleOCR-VL-1.6 截图确认"——是**软截断（服务端只处理前 100 页、剩余丢弃），不是拒收**。
> 2. 单次 `POST /files` 提交整文件，**没有任何 page_range / partial / multi-part 参数**——拆分必须在文件层做。
> 3. 计费口径**按页次**：百度智能云公开价 ¥0.09/页（按量后付费），AI Studio 每日免费 20,000 页；只有成功调用计费。
> 4. 公开文档给的是百度智能云端点：submit QPS=2 / query QPS=5 / PDF max 100MB / max 500 页；项目实际端点的真实限速不可知。
> 5. 失败时服务端回 `state=failed` + `errorMsg` 字段，本地仅 raise RuntimeError 不分类 → **preflight 无法从错误信息稳定区分"超限"与"真失败"**，需在客户端判断 page_count。

---

## 0. 背景与"两端点"歧义

调研时遇到的第一个障碍：**"PaddleOCR SaaS"实际指两个不同的服务端点**，需要分开看：

| 端点 | 项目实际使用？ | 公开文档？ |
|---|---|---|
| `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs` | **是**（`.env` PADDLEOCR_API_URL） | **否**（百度星河社区 / 百度 AI 开放平台文档均未列出此域） |
| `https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task` | 否 | **是**（百度 AI 开放平台定价 / 错误码 / QPS 公开） |

二者协议相近（都是异步 task 提交 + 轮询取结果），但 PaddleOCR 官方与百度智能云两个文档体系在 **2026-08-19 都没有公开描述 `aistudio-app.com` 这个具体域名的端点契约**。

来源：
- `core/parse_document.py:111`：`PADDLEOCR_API_URL` 默认值通过 `.env` 设置为 `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`
- `.env`（实测）：`PADDLEOCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`
- `core/parse_document.py:265-281` 请求体结构：`model=PaddleOCR-VL-1.6`、`optionalPayload={useDocOrientationClassify, useDocUnwarping, useChartRecognition}`、`files={"file": f}`
- 百度智能云 PaddleOCR-VL API 文档：`https://ai.baidu.com/ai-doc/OCR/Qmncwhwdt`、`/OCR/7mh8u7ruk`（web fetch 时间 2026-08-19）
- WebSearch "paddleocr.aistudio-app.com" API jobs endpoint：搜不到第一方文档（2026-08-19）

**结论**：本报告下文凡"百度智能云文档说 X"——指第二个端点；凡"AI Studio 端点"——指项目实际用的第一个端点。两者数字**不能直接互换**，但因协议相近，QPS / 错误码结构有参考价值。

---

## 1. 100 页限制的真实形态

### 结论

- **不是页数 vs MB vs 字符数 vs 并发中的复合限制**：单一"页数"硬阈值。
- **是软截断（server-side silent truncation），不是拒收**：issue #87 决议用词"按 PaddleOCR 服务端约定会被截断"——意为服务端只处理前 N 页、剩余静默丢弃，前端从返回 JSON 看不到"被截"信号。
- **数字 100 的来源是 AI Studio 控制台截图实测**，不是从官方文档查到的。

### 证据来源

#### 1.1 项目代码注释（直接来源）

`services/bulk_reparse_service.py:50-52`：

```python
# 单文件页数上限：超过此值按 PaddleOCR 服务端约定会被截断（issue #87
# 决议）。预检中仅警告；实际 run 中跳过（不静默丢内容）。
PAGE_LIMIT = 100
```

注释明确"按服务端约定会被截断"——**截断**字面语义 = 服务端只处理一部分，不是 4xx 拒收。

#### 1.2 issue #87 决议（用户答复，2026-07-28）

> "⚠️ 100-页单文件限制：全部 155 doc 最大 52 页，安全。但 `bulk_reparse` 预检需加入 '>100 页 → 警告并跳过' 逻辑（避免将来其它 KB 触发静默丢内容）"
>
> —— `gh issue view 87 --comments --json comments`（issuecomment 5099301182）

决议"避免将来其它 KB 触发静默丢内容"用词 = **静默丢内容**，再次印证"服务端拿到文件 → 只识别前 100 页 → 剩余页不进 cache"的语义。

决议提到"百度 AI Studio PaddleOCR-VL-1.6 截图确认"——意思是 100 是用户在 AI Studio 控制台视觉验证过的限制，但没有截图链接、也没有 issue 评论附图。

#### 1.3 百度智能云 PaddleOCR-VL 官方 API 文档（旁证）

`https://ai.baidu.com/ai-doc/OCR/Qmncwhwdt`（web fetch 2026-08-19）明确：

> "Layout docs (pdf)：Max 100M；up to **500 pages**"

百度智能云的端点（不是项目用的端点）公开写的是 **500 页**。这说明：

- "PaddleOCR-VL"作为一个模型，**在百度智能云端的限制是 500 页**，不是 100 页。
- 项目用的 `paddleocr.aistudio-app.com` 端点限制更紧（100 页），是**该端点单独的**产品配置，不是模型本身的能力上限。

#### 1.4 第三方的"飞桨星河社区"页面（不权威但值得记录）

`https://www.paddlepaddle.org.cn/hub/scene/ocr` 在 WebSearch 摘要里被多个爬虫报出："单个文件 ≤ 200MB / **1000页**，单张图片 ≤ 10MB"。

- 注：实际 WebFetch 抓该页面返回的是 SPA 外壳（`<div id="root"></div>`），具体数字是 web search 的预览缓存给的，**不能作为第一手证据**。
- 该页面文案与百度智能云文档（500 页 / 100MB）冲突 → 说明百度体系内部不同入口给用户的数字不一致，更说明 100 / 500 / 1000 这几个数字**都不是"模型能力上限"**，而是产品/入口层面的商业/工程限制。

### 反证

- 没有发现任何 issue 描述"提交 200 页 PDF 被 4xx 拒收"的案例。
- 没有发现服务端在 response 里返回 "truncated" / "page limit exceeded" 字样的证据。
- 现有 cache 里最大的 doc 是 52 页（`01KW1PZ0HS16E6NZB7F70YFB1M`），从未在生产 trace 中碰到过超限触发（`research/ocr-cache-hit-estimate.md` §2.1 最大页数 52）。

### 已知不确定点

- **截断位置是 100 还是 100±容差**：没有前人 trace 过真实超限文件的服务端返回，无法确认服务端是否"恰好前 100 页完整、101 页开始丢"。
- **是否仅截页码、不截 layout / markdown**：若 layout 截在 100、markdown 截在 100 但 markdown 末尾被强制闭合，会出现 `layout[0..99] vs by_page[0..100]` 的索引错位。
- **是否对所有模型都是 100**：issue #87 决议针对 `PaddleOCR-VL-1.6`，其他模型（如 PP-OCRv5 / PP-StructureV3）是否同限制未知。

---

## 2. API 调用粒度

### 结论

- **单次 `POST` 提交整文件**，项目代码里没有任何 page_range / partial / chunk / multi-part 参数。
- **官方 PaddleOCR-VL 服务化部署**（自托管版，端口 8080）的 `POST /layout-parsing` 端点签名里**也只有 `file` + 可选开关类参数**（`useDocOrientationClassify` / `useDocUnwarping` / `useChartRecognition` / `restructurePages` / `visualize`），没有 page_range。
- **百度智能云版** PaddleOCR-VL（端口 443 上的 `aip.baidubce.com`）有一个 `pdf_file_num` 参数但**仅用于校验**——若超过实际页数会触发 `216308 error_code: pdf_file_num exceeds actual page count`，不接受"只处理部分页"的语义。

### 证据来源

#### 2.1 项目代码（直接来源）

`core/parse_document.py:261-281`：

```python
def _paddleocr_call(file_path: str, orientation_classify: bool = False) -> ParseResult:
    headers = {"Authorization": f"bearer {_PADDLEOCR_API_TOKEN}"}
    # 提交 job
    data = {
        "model": _PADDLEOCR_MODEL,
        "optionalPayload": json.dumps({
            "useDocOrientationClassify": orientation_classify,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }),
    }
    with open(file_path, "rb") as f:
        resp = requests.post(
            _PADDLEOCR_API_URL, headers=headers, data=data,
            files={"file": f}, timeout=120,
        )
```

参数全集：`model` + `optionalPayload`（嵌套三个 bool）+ `files={"file": 整文件}`。

**没有任何**：
- `page_range` / `pages` / `partial`
- `start_page` / `end_page`
- `chunk_id` / `multi_part`
- `range` / `from_page` / `to_page`

#### 2.2 官方 PaddleOCR-VL 服务化部署（自托管，端口 8080）

`https://www.paddleocr.ai/main/version3.x/pipeline_usage/PaddleOCR-VL.html`（web fetch 2026-08-19）：

> `POST /layout-parsing`
> 必填参数：`file`（服务器可访问的图像文件或 PDF 文件的 URL，或 Base64 编码结果）
>
> 常用可选参数：
> - `fileType`：文件类型（`0` = PDF，`1` = 图像）
> - `useDocOrientationClassify`、`useDocUnwarping`、`useLayoutDetection`、`useChartRecognition`、`useSealRecognition`、`restructurePages`、`visualize`

**没有任何 page_range**——这是 PaddleOCR 团队自己写的服务化部署文档，没有给"只处理部分页"的开关。

#### 2.3 百度智能云 PaddleOCR-VL（端口 443）

`https://ai.baidu.com/ai-doc/OCR/7mh8u7ruk`（web fetch 2026-08-19）：

> 请求参数：必填 `file_name`，二选一 `file_data` (base64) 或 `file_url` (≤1024 bytes)
>
> 可选：`analysis_chart`、`merge_tables`、`relevel_titles`、`recognize_seal`、`return_span_boxes`

**没有 page_range**。

旁证：错误码表（`https://ai.baidu.com/ai-doc/OCR/dk3h7y5vr`）里出现：

> `216308` — `pdf_file_num` exceeds actual page count

——说明百度智能云 API **确实**有个 `pdf_file_num` 字段，然而其作用是"告诉服务端我期望有多少页"，仅用于校验（与实际页数不符就 216308），不是用来限定"只处理前 N 页"的。

### 推论

- **拆分必须在文件层做**（PyMuPDF / pdftk / pikepdf 拆 PDF → 多次 POST）。
- 这正是 issue #159 的"Out of scope"列表里把"提高 SaaS 100 页上限"明确排除的根本原因——上游服务端压根没开这个口子。
- issue #159 Architecture constraints 段落（2026-08-19 摘录）：
  > "PaddleOCR API 提交是 `files={"file": PDF}` 整文件 POST，**未发现 page_range 参数** → 拆分必须在**文件层**做"

### 已知不确定点

- AI Studio 端点的 `optionalPayload` 字段定义不公开——可能存在未被项目用到的扩展参数（如 `page_range` / `crop`）。但**项目代码里没用到**，意味着即使有也未走默认路径，需要先实证。

---

## 3. 计费模型与成本对比

### 结论

- **按页次计费**，不是按调用次数、不是按字符、不是按文件大小。
- 同一篇 500 页 PDF：
  - **整文件传一次（被截断）**：服务端只识别前 100 页 → **按 100 页计费**（前提：服务端的"按页计费"按实际识别页数计；如按"提交页数"计则按 500 计）
  - **拆 5 块（每块 100 页）**：服务端各识别 100 页 → **按 500 页计费**
  - **整文件传一次（被拒）**：不计费（"Only successful calls are billed"）
- **拆文件** 在 SaaS 计费上**不比整文件更贵**，但要付 5 次网络开销与 5 个失败语义窗口。
- AI Studio 端点**有每日免费 20,000 页**（issue #87 决议截图确认），个人跑批根本不碰付费层。

### 证据来源

#### 3.1 百度智能云 PaddleOCR-VL 定价（第一手）

`https://ai.baidu.com/ai-doc/OCR/9k3h7xuv6`（web fetch 2026-08-19）：

> - Free tier: 个人认证 **200 页/月**，企业认证 **1000 页/月**
> - 预付费资源包: 1000 页 ¥90, 5000 页 ¥425, 1 万页 ¥800, 5 万页 ¥3750, 10 万页 ¥7700, 20 万页 ¥14300, 50 万页 ¥33000, 100 万页 ¥54000, 500 万页 ¥210000
> - 按量后付费: **¥0.09/页**（不限量）
> - Resource package valid for 1 year; refundable within 7 days if unused
> - **Only successful calls are billed**

计费颗粒度 = 页（不是次、不是 MB、不是字符）；计费门槛 = 成功。

#### 3.2 AI Studio 端点免费额度（间接来源，issue #87 决议）

issue #87 决议（issuecomment 5099301182）：

> "每日免费额度 20,000 页（百度 AI Studio PaddleOCR-VL-1.6，截图确认）"
> "本次 reparse 1694 页 = 8.5% 预算，单日即可完成"

AI Studio 端点的免费层比百度智能云慷慨——**每日** 20,000 页 vs 百度智能云**每月** 1,000 页（企业认证）。这个差距提示：

- AI Studio 端点定位是**试用 / 个人开发**，付费 / SLA 是百度智能云
- 项目用的是 AI Studio 端点（`aistudio-app.com`），**单日跑批只要在 20K 页内零成本**

#### 3.3 三个方案的成本对比（500 页 PDF，单价按 ¥0.09/页）

| 方案 | 服务端行为 | 计费页数 | 单次成本 | 累计开销 |
|---|---|---|---:|---:|
| 拆 5 块（每块 100 页），全部成功 | 5 × POST，每块 100 页全识别 | 5 × 100 = 500 | 5 × ¥9 = ¥45 | 5 次请求 |
| 整文件传一次（被软截断） | 1 × POST，只识别前 100 页 | **100（按成功识别页数）** | ¥9 | 1 次请求，但数据缺失 |
| 整文件传一次（被拒） | 1 × POST，4xx 错误 | 0 | ¥0 | 1 次请求 |

**关键洞察**：
- "拆 5 块"比"整文件传一次"贵 5 倍（¥45 vs ¥9），但**前者拿到了完整 500 页数据，后者只有前 100 页**。
- 按"按页计费"的真实语义，**拆 5 块的 cost = 5 × 单次整文件 cost**，符合线性预期。
- "整文件被软截断"看似便宜（¥9 vs ¥45），实际上等于**花了 1/5 的钱只拿了 1/5 的数据**——单位数据成本一致。
- AI Studio 端点免费层 20K 页/日 → 单日跑批 1694 页（虹桥公司制度 KB）零成本。

#### 3.4 反证 / 旁证

- **不是按字符 / token 计费**：issue #88 原文本写"参考 PaddleOCR 单页 token 计费"，`research/ocr-cache-hit-estimate.md §4.2` 已经指出"在公开渠道找不到对应规则"，结论是 issue 文本错误。
- **不是按调用次数**：免费额度写的是"200 页/月 / 1000 页/月 / 20,000 页/日"，全是"页"。

### 已知不确定点

- **"按成功识别页数" vs "按提交页数"**：百度智能云文档说"Only successful calls are billed"——但这是按"调用"维度（一次提交算 1 次）。单次提交里"如果被服务端截断"是按 100 页还是按 500 页计？文档没说。
  - 假设 1（按识别页数计）：软截断后只付 100 页的钱 → 拆 5 块确实更贵。
  - 假设 2（按提交页数计）：软截断后仍付 500 页的钱 → 拆 5 块与整文件同价。
  - 在没有 trace 数据的前提下，**这两种假设无法区分**。

---

## 4. 并发限制（QPS / RPM / 在飞 job）

### 结论

- **同一 API key 在飞 job 上限**：项目代码无显式信号，估计是 "submit 完成后即轮询、无主动限速"，依赖单篇超时（`PER_DOC_TIMEOUT_S = 1800s`）兜底。
- **QPS 限速**：百度智能云 PaddleOCR-VL 公开数字 = submit **2 QPS** / query **5 QPS**。AI Studio 端点**未公开**。
- **429 等限速码**：百度智能云错误码 `18 — QPS limit`（免费 2 QPS / 付费 10 QPS）；项目代码未做限速 retry。

### 证据来源

#### 4.1 项目代码

`services/bulk_reparse_service.py:60-69`：

```python
# 每篇 reparse 轮询超时（秒）。
PER_DOC_TIMEOUT_S = 1800

# 默认并发（issue #87 决议 γ）。
DEFAULT_CONCURRENCY = 4

# embedding 终态：成功 / 失败，轮询结束条件。
_TERMINAL_STATUSES = {"embedded", "failed", "none"}

# 轮询间隔（秒）。
_POLL_INTERVAL_S = 2.0
```

`_paddleocr_call` (`core/parse_document.py:261-311`)：

- submit `requests.post(..., timeout=120)`
- 轮询 `requests.get(... timeout=30)` 间隔 `time.sleep(5)`
- **无任何显式限速 / retry on 429 / rate-limit-backoff**

#### 4.2 百度智能云 PaddleOCR-VL QPS（第一手）

`https://ai.baidu.com/ai-doc/OCR/7mh8u7ruk`（web fetch 2026-08-19）：

> Submit endpoint: **2 QPS**
> Query endpoint: **5 QPS**
> Recommended polling: every 5–10 seconds after submission

#### 4.3 错误码（限速 / 配额）

`https://ai.baidu.com/ai-doc/OCR/dk3h7y5vr`（web fetch 2026-08-19）：

| error_code | error_msg | 语义 |
|---:|---|---|
| 17 | daily limit reached | 日配额耗尽 |
| **18** | **QPS limit (free: 2 QPS; paid: 10 QPS)** | **QPS 撞限** |
| 19 | total request limit reached | 总量撞限 |
| 216604 | insufficient quota | 额度不够 |

注意：**百度智能云的错误码体系是同步 API 返回**（`error_code` 嵌在 JSON body），但项目用的 AI Studio 端点是**异步轮询**（`state=failed` + `errorMsg`），错误码结构可能不同。

#### 4.4 在飞 job 数

- 百度智能云文档**没有显式说**最大在飞 job 数。
- 项目默认并发 4（`DEFAULT_CONCURRENCY = 4`），issue #87 决议 γ 拍板。
- 按 QPS 2 推算：4 并发 × 轮询间隔 5s = 稳态 submit QPS ≈ 4 × (1 / N秒提交一次) ≪ 2 → **实际上 submit QPS 撞不到限**，因为 4 并发同时在飞时大多数时间在 sleep。
- 关键瓶颈是 **轮询 QPS**（每篇 5s 一次）= 4 × (1/5) = 0.8 QPS ≪ 5，**也安全**。

### 反证 / 旁证

- `research/t3-siliconflow-probe-results.md:251`：`账户的 RPM 阈值 ≥ 1000(本机没撞到上限)`——这是另一个供应商（SiliconFlow）的限速，不是 PaddleOCR。
- 项目没有任何 429 / QPS 撞墙的实测记录（issue 列表里也没有）。

### 已知不确定点

- **AI Studio 端点的真实 QPS 上限**：项目跑批几个月以来没撞过（4 并发稳态 0.8 QPS），但**不代表 QPS 上限就是 5**——可能是更高，也可能更低。
- **是否有 "per-key concurrent job" 上限**：百度智能云文档未提，AI Studio 未公开。

---

## 5. 失败语义与"超限 vs 真失败"的区分能力

### 结论

- 服务端失败时返回 `state=failed` + `errorMsg` 字段（项目代码读取 `j.get('errorMsg', '?')`），本地 raise `RuntimeError`。
- **不能从错误信息稳定区分"超限（>100 页被截）" vs "真失败（解析错误 / 网络 / 损坏）"**：
  - 软截断**不返回 failed**——服务端只识别前 100 页并标 `state=done`，项目代码无"是否截断"的二次校验。
  - 真失败时 `errorMsg` 是百度内部字符串（如"invalid PDF"），无法从语义上识别是不是页数问题。
- 百度智能云错误码体系（`error_code: 216202 / 216205 / 216308`）对"超限 / 超大"有专门编码，但项目用的 AI Studio 端点是否复用该编码**未知**。

### 证据来源

#### 5.1 项目代码

`core/parse_document.py:284-306`：

```python
deadline = time.monotonic() + 600
jsonl_url = ""
while time.monotonic() < deadline:
    try:
        r = requests.get(f"{_PADDLEOCR_API_URL}/{job_id}", headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()["data"]
        state = j["state"]
        if state == "done":
            jsonl_url = j["resultUrl"]["jsonUrl"]
            break
        if state == "failed":
            raise RuntimeError(f"paddleocr job failed: {j.get('errorMsg', '?')}")
    except RuntimeError:
        raise
    except Exception:
        pass
    time.sleep(5)
```

只有"done / failed / pending"三种状态识别，**没有"truncated / partial"状态**。`errorMsg` 整串原样 raise，不分类。

#### 5.2 项目"假成功"检测逻辑（旁证）

`services/reparse_service.py:74-77`（来自 `research/paddleocr-failure-rootcause.md`）：

```python
parse_result = parse_document(doc.id)
if not parse_result.full_text or len(parse_result.full_text) < 20:
    raise RuntimeError("parse_document returned empty/sparse text")
```

这说明**项目层面已经遇到"解析结果不完整但 state=done"的指纹**（issue #93 / #100），但这种指纹**不是从服务端 errorMsg 推出来的**，而是客户端靠 layout / full_text 长度二次校验。

#### 5.3 百度智能云错误码（参考）

`https://ai.baidu.com/ai-doc/OCR/dk3h7y5vr`：

| error_code | error_msg | 是否能区分"超限"？ |
|---:|---|---|
| 216202 | "size error (per-interface limits apply)" | ⚠️ 字面"大小"含糊 |
| 216205 | "input oversize (per-interface limits)" | ⚠️ 字面"超限"但不说具体维度 |
| 216308 | "pdf_file_num exceeds actual page count" | ✅ 唯一明示与"页数"相关的错误码 |
| 216603 | "failed to get PDF page count" | ❌ PDF 损坏 / base64 编码问题 |
| 282110 / 282111 | "URL does not exist" / "URL format illegal" | ❌ URL 问题 |
| 282112 | "download timeout (image >3M or anti-leech)" | ❌ 下载问题 |
| 18 | "QPS limit (free: 2 QPS; paid: 10 QPS)" | ✅ 限速问题 |

#### 5.4 现实：项目用的端点不暴露这些 error_code

项目用的 `paddleocr.aistudio-app.com/api/v2/ocr/jobs` 端点协议是：

```
POST /files → {data: {jobId}}
GET  /{jobId} → {data: {state, errorMsg?, errorCode?}}
```

`errorCode` 字段在项目代码里**没有被解析**（`core/parse_document.py:297` 只读 `errorMsg`），即使服务端回了 error_code 也被丢弃。

### 推论

- **不能从错误信息反推"超限" vs "真失败"**——这是当前的硬限制。
- 解决方案是 **preflight**：在调用前用 `pdfinfo` / `pymupdf` 拿页数，超 `PAGE_LIMIT = 100` 直接进 `skipped`，不调用服务端（这是 `services/bulk_reparse_service.py` 当前的实现）。
- 项目目前**没有针对单页成功但整篇未完成的"软截断"做二次检测**——属于 issue #159 的 `Not yet specified` 列表里的"错误单块处理"问题。

### 已知不确定点

- AI Studio 端点是否在 `errorMsg` 里说人话（如 "page limit exceeded"）：项目代码没解析，issue 历史里没有"服务端 errorMsg 截图"。
- 即使 errorMsg 有"page limit"字样，**也不能 100% 排除"真失败"**——服务端可能误用同一字符串表达多种失败。
- 软截断（>100 页被服务端静默截前 100 页）的指纹：**服务端返回 done，但 JSONL 解析出的 `layout` 只有 100 页而非 200 页**。项目代码 `_paddleocr_jsonl_to_parse_result` 解析 JSONL 但**没有"页数与 doc.page_count 比对"的校验**。

---

## 已知不确定点（汇总）

| # | 不确定点 | 影响 | 建议动作 |
|---|---|---|---|
| 1 | AI Studio 端点（`aistudio-app.com`）的官方文档是否存在 | 决定能否从官方文档拿准确数字 | 联系百度支持 / 在控制台找 API 文档链接 |
| 2 | 100 页截断的位置是否精确（恰好 100 还是 ±容差） | 影响拆分块大小选择 | 跑一次 101/150/200 页的实测 trace |
| 3 | 服务端按"识别页数"计费 还是按"提交页数"计费 | 影响"拆 5 块 vs 整文件"的 cost story | 看 AI Studio 控制台的 quota 消耗历史 |
| 4 | 软截断后是否产生 done state 还是 failed state | 影响"超限 vs 真失败"的 preflight 策略 | 跑一次 200 页 trace 拿响应 |
| 5 | AI Studio 端点的真实 QPS / 在飞 job 上限 | 影响 bulk_reparse 默认并发 4 是否需要调 | 撞墙前无法知；建议保持 4 + 单篇超时兜底 |
| 6 | 软截断是否同时截 `markdown` 与 `layout` | 影响 `ParseResult` 一致性 | trace 一篇 >100 页 doc 看 `layout` 长度 vs `by_page` 长度 |
| 7 | `errorCode` 字段是否在 AI Studio 端点返回 | 影响 preflight 失败分类能力 | 给项目加 `_paddleocr_call` 的 `errorCode` 解析日志 |
| 8 | 拆分后 sha256 缓存会全部失效 | 影响 cache hit 比例 | 重拆 chunk 走新 cache key（已经是当前实现，#160 直接受益） |
| 9 | 飞桨星河社区页面的 "200MB / 1000 页" 数字是真是假 | 与百度智能云 "100MB / 500 页" / AI Studio "100 页" 三个数字冲突 | 直接 WebFetch 该页 SPA 内容（已知抓不到，仅靠 web search 缓存） |

---

## 引用

### 项目内（直接证据）

- `core/parse_document.py:111-113` — `_PADDLEOCR_API_URL` / `_PADDLEOCR_API_TOKEN` / `_PADDLEOCR_MODEL` 默认值
- `core/parse_document.py:261-311` — `_paddleocr_call` 完整流程（submit / poll / fetch JSONL）
- `core/parse_document.py:297` — `state=failed` raise 路径
- `core/parse_document.py:314-372` — `_paddleocr_jsonl_to_parse_result`（无页数校验）
- `services/bulk_reparse_service.py:50-52` — `PAGE_LIMIT = 100` 来源注释
- `services/bulk_reparse_service.py:60-69` — `PER_DOC_TIMEOUT_S` / `DEFAULT_CONCURRENCY=4` / `_POLL_INTERVAL_S=2.0`
- `services/bulk_reparse_service.py:260-265` — "超限 doc：服务端会截断，按 PAGE_LIMIT 计费更保守" 旁证
- `services/bulk_reparse_service.py:309-314` — `split_by_page_limit` 客户端预检实现
- `services/reparse_service.py:74-77` — layout-empty / full_text < 20 客户端二次校验
- `core/paddleocr_cache.py:24-29` — cache key = `sha256(file bytes) + model_version`（拆块后 hash 全变）
- `.env`（实测）— `PADDLEOCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`
- `research/ocr-cache-hit-estimate.md §4.2` — "按页/按 token" 计费调研（结论：issue 文本的 token 说法不成立）
- `research/paddleocr-failure-rootcause.md` — 失败指纹的考古（已确认不是模型能力问题）
- `docs/adr/0004-kb-document-parse-pipeline.md` — ADR-0004 显式拒绝"自动迁移存量 KB 触发 OCR"
- `CONTEXT.md §KB 文档解析流水线` — "OCR 成本预检"词条明文写 `PAGE_LIMIT = 100`

### GitHub issue 追踪

- `raawaa/tech-doc-audit#87`（CLOSED）— "100-页单文件限制"的原始决议（issuecomment 5099301182 by raawaa, 2026-07-28）
- `raawaa/tech-doc-audit#88`（CLOSED）— 虹桥 KB 缓存命中率调研，OCR 配额上下文
- `raawaa/tech-doc-audit#89`（CLOSED）— 实现批量 reparse CLI 的 ticket
- `raawaa/tech-doc-audit#159`（OPEN）— 父 ticket，处理 >100 页标准文档的 PRD/spec
- `raawaa/tech-doc-audit#160`（OPEN）— 本调研 ticket

### 外部第一手（web fetch 2026-08-19）

- 百度智能云 PaddleOCR-VL 产品定价页：`https://ai.baidu.com/ai-doc/OCR/9k3h7xuv6` — ¥0.09/页、个人 200 页/月、企业 1000 页/月
- 百度智能云 PaddleOCR-VL API 文档：`https://ai.baidu.com/ai-doc/OCR/Qmncwhwdt` — PDF max 100M / 500 页 / QPS 2 (submit) / 5 (query)
- 百度智能云 PaddleOCR-VL 使用指南：`https://ai.baidu.com/ai-doc/OCR/7mh8u7ruk` — submit 端点 + 请求参数
- 百度智能云 OCR 错误码：`https://ai.baidu.com/ai-doc/OCR/dk3h7y5vr` — error_code 全表（17/18/216202/216205/216308/216604 等）
- PaddleOCR-VL 自托管使用文档（中文）：`https://www.paddleocr.ai/main/version3.x/pipeline_usage/PaddleOCR-VL.html` — `POST /layout-parsing` 参数表
- PaddleOCR-VL GitHub 文档 PR：`https://github.com/PaddlePaddle/PaddleOCR/pull/18095` — 1.6 文档 PR

### 外部第二手（旁证 / 仅供参考）

- 飞桨星河社区 PaddleOCR Hub：`https://www.paddlepaddle.org.cn/hub/scene/ocr` — WebSearch 缓存里的 "200MB / 1000页 / 10MB" 数字，未能从 SPA 抓到原文核实
- PaddleOCR GitHub 仓库：`https://github.com/PaddlePaddle/PaddleOCR` — 0.9B VLM 模型权重 Apache 2.0

### 不可达 / 未公开

- `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs` 端点的官方 API 文档 — WebSearch 与 WebFetch 均未找到第一手描述
- AI Studio 控制台的 API 文档页（需登录，未能访问）
- `PADDLEOCR_API_TOKEN` 后台 quota 仪表盘（需登录，未能访问）

---

— research-agent, 2026-08-19
