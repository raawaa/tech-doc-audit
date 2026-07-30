# OCR 缓存命中率调研 — 虹桥公司制度 KB

- **调研目标**: 回答 issue #88 — 虹桥公司制度 KB（`01KW1PG49FQDAEYV0W1H2H309E`）155 篇缺 `pages/{doc_id}.json` 的 doc 中，实际需要走 PaddleOCR 的有多少
- **调研日期**: 2026-07-28
- **调研性质**: read-only，不触发任何 reparse
- **方法**: 静态分析 `core/paddleocr_cache.py` + 遍历 meta 文件 + sha256 文件字节 + 扫缓存目录 + `pdfinfo` 取 PDF 页数

## TL;DR

**0 / 155 命中（0.00%），155 / 155 未命中（100.00%）。本次 reparse 将对全部 155 篇 doc 真正消耗 PaddleOCR 配额，零缓存可省。** 总计 1694 页需重 OCR（PDF 平均 10.93 页 / 篇，中位 9 页，最大 52 页，总 76.7 MB）。

## 1. `paddleocr_cache` 契约

源文件：`core/paddleocr_cache.py`。

### 1.1 缓存文件存储位置

- **模块级常量**（`core/paddleocr_cache.py:18-19`）：

  ```python
  _DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "data"))
  CACHE_DIR: Path = _DATA_DIR / ".cache" / "paddleocr"
  ```

- 默认相对路径 `data/.cache/paddleocr/`；可通过 `AUDIT_DATA_DIR` 环境变量覆盖根目录
- 写入路径由 `_cache_path()` 计算（`core/paddleocr_cache.py:34-36`）：

  ```python
  def _cache_path(file_path: str, model_version: str = _MODEL_VERSION) -> Path:
      """``{sha256}_{model_version}.json``。"""
      return CACHE_DIR / f"{_file_hash(file_path)}_{model_version}.json"
  ```

### 1.2 缓存 key 是什么

- **缓存文件名 = `{sha256(file 字节)}_{PADDLEOCR_MODEL}.json`**
- `_file_hash()` 是 `hashlib.sha256(file contents)`（`core/paddleocr_cache.py:25-31`），分块读 64 KiB
- `model_version` 默认 `"PaddleOCR-VL-1.6"`，来自 `os.environ.get("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")`（`core/paddleocr_cache.py:22`）

> ⚠️ **缓存 key 与 meta 中的 `content_hash` 等价但来源不同**：
> - meta 字段 `content_hash` 在 `services/doc_service.py:77` 生成 — `hashlib.sha256(content).hexdigest()`（导入时的 PDF 字节）
> - cache 命名键 `_file_hash(file_path)` 在 `core/paddleocr_cache.py:25-31` 同样 `hashlib.sha256(文件字节).hexdigest()`
>
> 经验证 155 doc 中 `content_hash == file_hash`（同一 sha256(file contents)），可作为命中判定的等价字段使用

### 1.3 `get_cached(file_path)` 命中判定

源：`core/paddleocr_cache.py:39-79`。判定流程：

1. `CACHE_DIR.exists()` 否则 None（line 62-63）
2. 拼 `path = _cache_path(file_path)`，文件不存在 → None（line 64-66）
3. `json.loads(path.read_text())` 失败 → None（line 67-71，**降级为未命中，不抛**）
4. `entry.get("version") != _MODEL_VERSION` → None（line 72-73，**模型升级自动失效**）
5. `entry.get("file_hash") != _file_hash(file_path)` → None（line 74-75，**PDF 内容变更自动失效**）
6. **V8 cache defense**（issue #57，`core/paddleocr_cache.py:76-78`）：若 `entry.source == "fallback_pdfplumber"` 且 `PADDLEOCR_API_TOKEN` + `PADDLEOCR_API_URL` 当前已配置 → 视为污染，强制返回 None，触发 PaddleOCR 重跑
7. 全部通过 → 返回 `entry.get("result")`

### 1.4 `save_cached(file_path, result, *, model_version, source)` 写入契约

源：`core/paddleocr_cache.py:91-120`。

```python
entry = {
    "version": model_version,
    "file_hash": _file_hash(file_path),    # 写入时再算一次 sha256
    "parsed_at": datetime.now(timezone.utc).isoformat(),
    "source": source,                       # paddleocr | fallback_pdfplumber | fallback_docx | fallback_plain | empty
    "result": result,
}
path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
```

- `source` 字段标识 cache 内容来源解析器（issue #57 引入）
- 写入路径 `parent.mkdir(parents=True, exist_ok=True)` 自动创建
- 覆盖已有条目

### 1.5 事实 vs 推测

| 事实 | 推测（未直接验证） |
|---|---|
| cache key = sha256(file bytes) + model_version | 不同 doc 的 file_path 即使指向同一文件（如同一 doc 重复上传）也会产生同一 cache 文件名（因为 sha256 一致） |
| V8 defense 在 PaddleOCR 凭证就位后强制重跑 fallback_pdfplumber 条目 | — |
| 命中则 reparse 不消耗 OCR 配额（`core/parse_document.py:120` `use_cache=True` 默认走缓存路径） | 当 `PADDLEOCR_API_TOKEN` / `PADDLEOCR_API_URL` 未配置时，V8 defense 不触发，但仍可命中旧 cache 条目并直接返回 result |

## 2. 155 doc 的 file_hash 列表

源数据：
- meta 目录：`data/kbs/01KW1PG49FQDAEYV0W1H2H309E/meta/`（共 157 个文件，其中 2 个 doc 已有 `pages/`）
- 文件名约定：`meta/<doc_id>.json`（doc_id 是 ULID-like 字符串，如 `01KW1PGAA9QPH5Q7PGNMA8H25A`）
- 完整 CSV（155 行：doc_id, original_name, content_hash, file_hash, file_size_bytes, pages, cache_hit）见本文件末尾附录 A，也单独保存在 `/tmp/full_research.csv`

### 2.1 全局汇总

| 指标 | 值 |
|---|---|
| KB 总 doc 数（meta/*.json） | 157 |
| 缺 `pages/{doc_id}.json` 的 doc 数 | **155** |
| 已有 `pages/{doc_id}.json` 的 doc 数 | 2 |
| `cache_exists`（缓存目录有对应文件） | **0**（仅 2 个 cache 文件，但都不在 155 列表中）|
| `version_match` 且 `file_hash_match`（严格命中） | **0** |
| 未命中 doc 数 | **155** |
| 未命中 doc 总页数（`pdfinfo` 统计） | **1694** |
| 未命中 doc 总字节 | 80,397,478 字节（76.7 MB）|
| 平均页数 / doc | 10.93 |
| 中位页数 / doc | 9 |
| 最小页数 | 1 |
| 最大页数 | 52 |
| `data/.cache/paddleocr/` 目录总文件数 | **2**（恰好对应 2 个已生成 pages 的 doc：见 §2.2）|

### 2.2 为什么命中数是 0

`data/.cache/paddleocr/` 下只有 2 个文件：

```
3a647ddee408643de4015111ffdf297567eb69d07bda92cd840273286b4ff94b_PaddleOCR-VL-1.6.json
fdd141061ddb6e1b5800245f0be5a41292a91209b8326e84aa448b8a30631ec7_PaddleOCR-VL-1.6.json
```

这 2 个 cache 条目的 file_hash 与 KB 中已生成 pages 的 2 个 doc 一一对应：

- `01KW1QXZ5AKBGK34BDJRV1X4JZ` （file_hash = `3a647ddee408643d...`）
- `01KW1QYMMAQG851X4T92Z1DGHD` （file_hash = `fdd141061ddb6e1b5...`）

其余 155 个 doc 历史上**从未被 OCR 过**（无 cache 条目存在），因而 reparse 时必然走 PaddleOCR 全量推理。

### 2.3 文件结构抽样（验证用）

`data/kbs/01KW1PG49FQDAEYV0W1H2H309E/meta/01KW1PGAA9QPH5Q7PGNMA8H25A.json`：

```json
{
  "id": "01KW1PGAA9QPH5Q7PGNMA8H25A",
  "kb_id": "01KW1PG49FQDAEYV0W1H2H309E",
  "name": "WJ-AQ-08-2015-A2_公司控制区通行证管理规定(2015).pdf",
  "original_name": "WJ-AQ-08-2015-A2_公司控制区通行证管理规定(2015).pdf",
  "file_type": "pdf",
  "file_path": "data/kbs/01KW1PG49FQDAEYV0W1H2H309E/docs/WJ-AQ-08-2015-A2_公司控制区通行证管理规定2015_01KW1PGAA9QPH5Q7PGNMA8H25A.pdf",
  "page_count": null,
  "created_at": "2026-06-26T10:09:45.801532",
  "embedding_status": "embedded",
  "content_hash": "4fca48cb4e4842f44c31dd079f8411dcddeee100f163fbb0e4056fa604b70724",
  ...
}
```

- `file_path` 是相对路径，指向 `data/kbs/<kb_id>/docs/<safe_name>_<doc_id>.<ext>`
- `page_count` 在 meta 中为 null — **实际页数需要 `pdfinfo` 解析 PDF 元数据**（本调研用 `pdfinfo` 批量提取）

### 2.4 155 doc 明细

完整 155 行明细见附录 A（按页数升序）。`hit` 列 100% 为 `miss`。

## 3. 命中分类

| 分类 | 数量 | 占比 |
|---|---:|---:|
| 命中（cache_exists + version_match + file_hash_match） | **0** | **0.00%** |
| 未命中 | **155** | **100.00%** |
| 防御性未命中（V8 cache defense fallback_pdfplumber 强制重跑） | 0 | 0.00% |

**核心结论：本次 reparse 将对全部 155 篇 doc 真正调用 PaddleOCR API，零缓存可省。**

## 4. OCR 配额估算

### 4.1 调用量

- doc 数：155
- 总页数：1694 页
- 平均：10.93 页/doc，中位 9 页/doc，最大 52 页/doc
- 总输入：76.7 MB

### 4.2 计费规则 — ⚠️ issue 文本与事实不符

issue #88 描述"参考 PaddleOCR 单页 token 计费"是误导。经查询 PaddleOCR 官方与百度智能云文档：

- **PaddleOCR-VL 模型权重开源**（Apache 2.0，[github.com/PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)），官方并未发布"PaddleOCR-VL-1.6 API pricing per token"计费规则
- 最接近的"按页计费"接口是百度智能云的通用 OCR API：**按"次 / 张 / 页次"计费**，而非 token 计费。常见量级：
  - 通用文字识别：约 **0.005–0.01 元/次**（每张图片一次）
  - 高精度文字识别：约 **0.02 元/次**
  - 表格 / 公式识别：单次更高
- PaddleOCR-VL 输出的结构化文本（行、块、表格、公式）**沿用"按页/按次"计费**而非 token 计费

**结论**：本仓库 `core/parse_document.py:186-236` 走的是 `_PADDLEOCR_API_URL` + `_PADDLEOCR_API_TOKEN` 指向的外部服务，**该项目的 PaddleOCR 服务端实际计费规则本调研无法确定**。建议在 reparse 前先确认服务端定价口径（按页次？按文件？）。

### 4.3 配额估算范围（量级参考）

> 仅作量级参考。实际配额取决于项目所部署的 PaddleOCR 服务端定价 + 资源包折扣。

| 计费口径 | 单价 | 155 doc × 10.93 页 ≈ 1694 页次 | 1694 页次费用 |
|---|---:|---|---:|
| 通用 OCR（0.005 元/次）| 0.005 元 | 1694 次 | **8.47 元** |
| 通用 OCR（0.01 元/次）| 0.01 元 | 1694 次 | **16.94 元** |
| 高精度 OCR（0.02 元/次）| 0.02 元 | 1694 次 | **33.88 元** |

> 上述范围 **不含资源包折扣**（预付费阶梯通常有 30–50% 折扣）。

### 4.4 时间成本（次要）

- `_paddleocr_call` 单次超时 600s（`core/parse_document.py:210`），按文档典型几十秒 ~ 几分钟
- 155 doc 顺序串行 reparse：**粗估 30 分钟 ~ 数小时**（强烈依赖并发策略，issue #86 留作 Not yet specified）

## 5. 不确定项

1. **`PADDLEOCR_MODEL` 环境变量值**：本调研假设默认值 `PaddleOCR-VL-1.6`，与 cache 目录里 2 个 cache 文件后缀一致（`..._PaddleOCR-VL-1.6.json`）。若部署环境设置过其他值，命中率会更低（更多不命中），不影响"0 命中"结论
2. **`content_hash` 与 `file_hash` 跨函数等价性**：本调研验证 155 doc 中二者完全相等，因为二者都来自 `hashlib.sha256(file_bytes)`。如果未来 `doc_service.file_hash` 改成例如 `sha256(file_path)` 或加入 salt，二者会脱钩；本结论的等价性依赖于"导入时存的字节 = 文件当前字节"，未做时序变化的检查（这些 doc 没有再上传历史，结论稳定）
3. **PaddleOCR 计费口径**：issue 文本的"按 token 计费"说法在公开渠道找不到对应规则。最可能口径是"按页次 / 按文件次"（百度智能云 OCR 的通用规则），具体单价值得在 reparse 前与运维确认 `_PADDLEOCR_API_URL` 服务端的真实定价
4. **page_count 字段**：meta 中 `page_count` 为 null，本调研用 `pdfinfo` 提取。如运行本调研的 `pdfinfo` 版本对加密/损坏 PDF 行为异常，可能导致个别 doc 页数误读（本次未观察到异常）
5. **重复 doc 的 cache 写入路径**：本调研**未发现**任何 doc_id 不同但 content_hash 相同的 doc。所有 155 doc 的 content_hash 唯一（附录 A 可验证）。如未来发生同 bytes 重复上传，cache key 会冲突，第二篇 reparse 会复用第一篇的 cache 产物（不会双倍消耗 OCR）

## 附录 A：155 doc 完整列表

按页数升序，hash 取前 16 字符 + `...` 提示。完整 hash 见 `/tmp/full_research.csv`。

| doc_id | original_name | pages | size (B) | content_hash (前 16) | file_hash (前 16) | hit |
|---|---|---:|---:|---|---|---|
| 01KW1R09A15RBME4V6W9MNY5KK | 机关党委、工会职责（2015）.pdf | 1 | 626,919 | f091ce4566068c48... | f091ce4566068c48... | miss |
| 01KW1R5EFEQGWR2FQTZBTBWGE2 | 沪机场集虹人(2015)100号_人力资源部职责（2015）.pdf | 1 | 734,383 | 092096e4a7a6906e... | 092096e4a7a6906e... | miss |
| 01KW1R67RDMVY0X0EGQHHPWP60 | 沪机场集虹人(2015)100号_工会办公室职责（2015）.pdf | 1 | 1,145,746 | 52fa418b522c6653... | 52fa418b522c6653... | miss |
| 01KW1R6ME7TEA7XHQW2MKD2D8W | 沪机场集虹人(2015)100号_纪检监察室职责（2015）.pdf | 1 | 731,059 | 182994e01cb4d98f... | 182994e01cb4d98f... | miss |
| 01KW1R70ZECSJV7FTWE2JQQFE8 | 沪机场集虹人(2015)100号_运行指挥中心职责.pdf | 1 | 1,142,508 | aa30c5137fd2dff1... | aa30c5137fd2dff1... | miss |
| 01KW1R8K2GBVKC1B2RDB726H0E | 沪机场集虹人[2018]61号_关于组建建设管理部的通知_（2018）.pdf | 1 | 170,872 | 3afec0690e37a74b... | 3afec0690e37a74b... | miss |
| 01KW1PQ8KVX9DFQ86EMHA9HTD9 | WJ-BG-02-2005-A0_公司信息工作管理规定(2005).pdf | 2 | 526,725 | 3141eb6da89cf180... | 3141eb6da89cf180... | miss |
| 01KW1Q34T183MV21T9DD185QJ1 | ZD-BG-07-2022-B0_公司门户网站信息发布工作管理办法(2022).pdf | 2 | 559,496 | 5b1a8e4bac72aab2... | 5b1a8e4bac72aab2... | miss |
| 01KW1Q3AVAMKXQWCRGXJHYVV0P | ZD-BG-08-2022-B0_航站楼旅客遗留、遗失物品处理办法(2022).pdf | 2 | 541,624 | 3e92e2a52dea3130... | 3e92e2a52dea3130... | miss |
| 01KW1QB8V4P3HH91C8RN660RKG | ZD-FS-18-2024-B0_关于印发公司经营类文件及合同规范化_审核意见工作指引的通知.pdf | 2 | 150,604 | d637b824a5589d00... | d637b824a5589d00... | miss |
| 01KW1R0FHV2VQYHCQBCHFA3AP7 | 沪机场虹人[2022]95号_关于成立虹桥公司应急管理领导小组_及其办公室(总值班室)的通知_（2022）.pdf | 2 | 480,290 | a95462fddcae0978... | a95462fddcae0978... | miss |
| 01KW1R0NT5G2VXP7302DBCB1R2 | 沪机场虹人[2024]107号_关于公司应急办（总值班室）组织架构调整_及相关人员工作调动的通知_（2024）.pdf | 2 | 157,287 | 40f93972fbc52f59... | 40f93972fbc52f59... | miss |
| 01KW1R58C6J7WMPP8ZJ3NQG80X | 沪机场虹采[2025]96号_关于印发公司供应商信息库管理指引、_公司采购评审专家和专家库管理指引的通知.pdf | 2 | 150,756 | 2794e37ba03587ad... | 2794e37ba03587ad... | miss |
| 01KW1R5MRN3YQFNV8MNF52MRQ8 | 沪机场集虹人(2015)100号_党委办公室职责_（2015）.pdf | 2 | 736,617 | f3263f76e8763694... | f3263f76e8763694... | miss |
| 01KW1R5V5GRQHC00HSC6BHMDDB | 沪机场集虹人(2015)100号_办公室职责（2015）.pdf | 2 | 140,154 | df08875532746463... | df08875532746463... | miss |
| 01KW1R61D3EBQ418150C0J50R6 | 沪机场集虹人(2015)100号_安全管理部（治安保卫部）（2015）.pdf | 2 | 450,561 | 6b451d9e25be5cbb... | 6b451d9e25be5cbb... | miss |
| 01KW1R6E73NC7SAPBJV1ARB8BV | 沪机场集虹人(2015)100号_服务管理部(法务审计室)职责_（2015）.pdf | 2 | 113,379 | fb4d6950309cf269... | fb4d6950309cf269... | miss |
| 01KW1R6TNYPW0YEY52NGC2V6DS | 沪机场集虹人(2015)100号_财务部职责（2015）.pdf | 2 | 860,960 | e2f3b9e884fb1cb7... | e2f3b9e884fb1cb7... | miss |
| 01KW1R77AYN7CTY2NCK169FSBF | 沪机场集虹人[2010]74号_场区管理部职责（2010）.pdf | 2 | 173,978 | b63609ae9a5e91d5... | b63609ae9a5e91d5... | miss |
| 01KW1R7KXFK0RZ50A6W81VVDW6 | 沪机场集虹人[2010]74号_机电信息保障部职责（2010）.pdf | 2 | 174,233 | 138c320d22d140f0... | 138c320d22d140f0... | miss |
| 01KW1R80BV84QYKYXS4WNTYG5N | 沪机场集虹人[2010]74号_能源保障部职责（2010）.pdf | 2 | 244,811 | d30110592da0aac8... | d30110592da0aac8... | miss |
| 01KW1R8SKG8HKA8M42Q3ZMG1QP | 沪机场集虹人[2020]9号_关于设立招标采购部的通知_（2020）.pdf | 2 | 397,505 | b252b149ccac4482... | b252b149ccac4482... | miss |
| 01KW1Q46DTADW9M0JHQ7EH6P4Y | ZD-BG-12-2023-B0_公司信息公开管理暂行办法(2023).pdf | 3 | 901,735 | 82fc816a25a4486e... | 82fc816a25a4486e... | miss |
| 01KW1QAW7ABAH2ZHHDA3P3PHPV | ZD-FS-17-2024-B0_关于印发上海虹桥国际机场有限_责任公司法务总监、合规总监履职目录_（2024_版）的通知.pdf | 3 | 167,446 | a304a52463aa7d47... | a304a52463aa7d47... | miss |
| 01KW1QK4QQTS8VXHK92HVQDJ2Z | ZD-JH-06-2023-B0_公司统计数据质量责任管理办法(2023).pdf | 3 | 514,316 | 9d4b1c24a01d60fe... | 9d4b1c24a01d60fe... | miss |
| 01KW1QP8XT3EJ4XQNATEG6HGS6 | ZD-RZ-05-2021-B0_公司中层干部年度绩效考核办法（试行）(20162021).pdf | 3 | 750,877 | 6f8d1964393eb3f2... | 6f8d1964393eb3f2... | miss |
| 01KW1QTBHM17194AHW4XKEV3V7 | ZD-XF-01-2025-B0_公司献血工作管理办法(2025).pdf | 3 | 135,372 | 106e9ac4a052b13e... | 106e9ac4a052b13e... | miss |
| 01KW1QV8Y06ZH8X5K11YW2157E | ZD-XX-04-2022-B1_公司网信委及网信办工作管理办法_(2022).pdf | 3 | 589,515 | 023fc6f806d196c5... | 023fc6f806d196c5... | miss |
| 01KW1R1YHCS9DVP4W99DY3RS4A | 沪机场虹委[2025]32号_公司党委会前置研究讨论事项清单(2025)_1.pdf | 3 | 268,924 | c951b56d0e2c7581... | c951b56d0e2c7581... | miss |
| 01KW1R7DJ5APKYGVH6J65K1JEY | 沪机场集虹人[2010]74号_安检护卫保障部职责（2010）.pdf | 3 | 200,153 | 722c3cedee984d33... | 722c3cedee984d33... | miss |
| 01KW1R86DZMGGEHQ9Y4FMQH1XT | 沪机场集虹人[2010]74号_运行指挥中心(飞行区管理部)_职责（2010）.pdf | 3 | 247,928 | 0f5000a4acb2880d... | 0f5000a4acb2880d... | miss |
| 01KW1R8CTT7QWW5FHZQV0JY82F | 沪机场集虹人[2018]60号_关于公司组织架构及相关管理职责优化调整的通知_（2018）.pdf | 3 | 298,748 | 30c35401a8f838f8... | 30c35401a8f838f8... | miss |
| 01KW1Q2AVBVGDMP0V3EM3TRSE5 | ZD-BG-04-2022-B0_公司密码电报管理办法(2022).pdf | 4 | 514,512 | 02e24b3462effabd... | 02e24b3462effabd... | miss |
| 01KW1QA89M7JW2FBGR6QQ34X8K | ZD-CW-09-2023-B0_公司信息系统资产财务管理办法(2023).pdf | 4 | 559,567 | d2e664526052a623... | d2e664526052a623... | miss |
| 01KW1QAEHQ8KRZFZFPCX58ZGB3 | ZD-DSB-01-2022-B0_公司董事会对经理层授权事项管理办法(2022).pdf | 4 | 559,561 | f8893ed5a3899fa2... | f8893ed5a3899fa2... | miss |
| 01KW1QPF7R35QEQCE7Q8FPQHYR | ZD-RZ-06-2022-B0_公司劳防用品管理办法(2022).pdf | 4 | 545,820 | 906cb621834a2b9c... | 906cb621834a2b9c... | miss |
| 01KW1R0VVA7XSKBW02NHMNY6FP | 沪机场虹人[2024]155号_关于公司法务审计机构设置调整的通知_(2024).pdf | 4 | 242,430 | 26bfa6ff2282428b... | 26bfa6ff2282428b... | miss |
| 01KW1R3AXCYK3CPGB8C058YX2G | 沪机场虹应[2023]63号_关于印发上海虹桥国际机场突发事件应急预案的通知(2023).pdf | 4 | 1,777,568 | 388841ed10d9d168... | 388841ed10d9d168... | miss |
| 01KW1R483GW54D21ZZA3NTKC7K | 沪机场虹董[2024]234号_关于印发《虹桥机场公司重点课题实施方案》_的通知.pdf | 4 | 273,239 | 173115a52ca73e65... | 173115a52ca73e65... | miss |
| 01KW1R4EEQ635Q3FKTN28BFZQP | 沪机场虹财[2021]7号_关于进一步规范使用项目建设管理费的通知(2021).pdf | 4 | 570,766 | cc4cedc3e769d60b... | cc4cedc3e769d60b... | miss |
| 01KW1R7T4E81JP1FZ01VRY4DET | 沪机场集虹人[2010]74号_消防急救保障部职责（2010）.pdf | 4 | 323,377 | 827863d135f1f6f3... | 827863d135f1f6f3... | miss |
| 01KW1Q1XVVW31HK66RP6XEG7CN | ZD-BG-02-2025-B1_公司行政会议管理办法_(2025).pdf | 5 | 135,666 | 4a6900c807bd5134... | 4a6900c807bd5134... | miss |
| 01KW1Q404JF2Q8D94Y13SJS8TQ | ZD-BG-11-2022-B0_公司业务接待工作管理规定(2022).pdf | 5 | 547,538 | d32d1b3d6bad7d79... | d32d1b3d6bad7d79... | miss |
| 01KW1Q4RY66RCA2M34Y04J69PG | ZD-BG-14-2024-B0_公司公务用车管理办法(2024).pdf | 5 | 141,667 | 723c3dbcc4b3a0d9... | 723c3dbcc4b3a0d9... | miss |
| 01KW1Q6NCB5SMRKBJVKJH2V6SV | ZD-CW-04-2022-B0_公司航空性业务收费结算管理办法(2022).pdf | 5 | 547,066 | d11719ad00814821... | d11719ad00814821... | miss |
| 01KW1QJS4ZVJQHZBBFFJ5XTXHQ | ZD-JH-05-2023-B0_公司统计管理办法(2023).pdf | 5 | 767,401 | 97c957dec5df652c... | 97c957dec5df652c... | miss |
| 01KW1QN7J5HNHS3N8MWWJRKB3T | ZD-RS-10-2024-B0_公司机关科级及以下管理人员选配工作实施办法_(2024).pdf | 5 | 203,471 | fcf7d7c8ca27095e... | fcf7d7c8ca27095e... | miss |
| 01KW1QP2CTNTGTX1FQTWPRZAKD | ZD-RZ-04-2021-B0_公司绩效奖励分配办法（试行）(20152021).pdf | 5 | 662,194 | b5e70fe19410a668... | b5e70fe19410a668... | miss |
| 01KW1QZ79BSWT4ERF6DE1XMP85 | ZD-ZB-03-2021-B0_公司工程项目审价管理办法(2021).pdf | 5 | 767,360 | 8ace1d806fb885b5... | 8ace1d806fb885b5... | miss |
| 01KW1QZMS0CDJYFGEV35TYWK64 | 公司供应商信息库管理指引（2025）.pdf | 5 | 219,931 | cd57df8bb312f487... | cd57df8bb312f487... | miss |
| 01KW1R8ZV7FFADC3449K2JMQE5 | 治安保卫重点单位分类说明.pdf | 5 | 497,983 | 8272d3d00a0057c8... | 8272d3d00a0057c8... | miss |
| 01KW1PQEM66E5JP1G0FF5EZGPJ | WJ-BG-02-2013-A3_公司督办工作实施办法(2013).pdf | 6 | 748,158 | ce66284b3662ed5e... | ce66284b3662ed5e... | miss |
| 01KW1PR5EFKVV99HSWQS76ZFKX | ZD-AQ-01-2023-B1_公司内部治安保卫工作规定(2023).pdf | 6 | 749,356 | 94e9e06dbd62a341... | 94e9e06dbd62a341... | miss |
| 01KW1Q5727ACB52WPCSAJ3NDZZ | ZD-CQ-02-2025-B0_公司场容环境卫生管理办法_(2025).pdf | 6 | 153,937 | e6c78f79ca175a6e... | e6c78f79ca175a6e... | miss |
| 01KW1Q7A9NRFFE9NJP8S4VFP6F | ZD-CW-07-2023-B0_公司存货管理办法(2023).pdf | 6 | 687,105 | 85e3694492eca63d... | 85e3694492eca63d... | miss |
| 01KW1QCAG0M8SG6940SEH7VSYF | ZD-FW-03-2025-B1_公司一线员工服务行为规范_(2025).pdf | 6 | 219,596 | 6f7f0bca2690c66c... | 6f7f0bca2690c66c... | miss |
| 01KW1QCRXGMHTT755VP8RCJRR4 | ZD-FW-07-2023-B0_公司服务创新管理办法(2023).pdf | 6 | 912,061 | 49adb0569f737749... | 49adb0569f737749... | miss |
| 01KW1QJBA6QQATD3NPY372MECE | ZD-JH-01-2021-B0_公司经营资源管理办法(2021).pdf | 6 | 685,913 | 3d30d51d1d33242d... | 3d30d51d1d33242d... | miss |
| 01KW1QM6N27YJPDAVJ3N610YVP | ZD-JS-10-2022-B0_公司设备管理信息系统使用管理办法(2022).pdf | 6 | 425,018 | 39be5cf94f2cc192... | 39be5cf94f2cc192... | miss |
| 01KW1QZE18Y5HKQZQJ6SXBATAV | 公司会计档案管理细则(2005).pdf | 6 | 866,060 | 0dcb21c0cd33d46b... | 0dcb21c0cd33d46b... | miss |
| 01KW1PXWTB3KNGE78NVNKQHF7M | ZD-AQ-12-2022-B0_公司安全管理体系(SMS)信息系统使用管理规定(2022).pdf | 7 | 550,464 | 5e13f77d9dabd94d... | 5e13f77d9dabd94d... | miss |
| 01KW1Q4Z3PZ08K6VKS09KB030S | ZD-CQ-01-2025-B0_公司场区绿化管理办法_(2025).pdf | 7 | 163,241 | fa38485ae26326ca... | fa38485ae26326ca... | miss |
| 01KW1QET8Z75H02TGEPQTA4VH5 | ZD-FW-15-2021-B0_公司激励管理人员担当作为实行_容错纠错的实施办法(试行)(2021).pdf | 7 | 692,207 | c0a35218dec0d01c... | c0a35218dec0d01c... | miss |
| 01KW1QKAYJMDV683VC1TCA6FGV | ZD-JH-08-2024-B0_公司受托管理企业监管实施细则(试行)_(2024).pdf | 7 | 152,584 | 005d99c6de78f3d2... | 005d99c6de78f3d2... | miss |
| 01KW1QKHANGBV5EAF778V1CA46 | ZD-JS-02-2021-B0_公司标准化机房管理办法(试行)(2021).pdf | 7 | 569,400 | f523fc5bca9eadaf... | f523fc5bca9eadaf... | miss |
| 01KW1QN0T80WZ9C9DS23E4081R | ZD-RS-09-2024-B0_公司员工因私出国(境)管理办法_（2024）.pdf | 7 | 224,664 | 5aef65da78d8e2e5... | 5aef65da78d8e2e5... | miss |
| 01KW1QNVR357C2RFA6ZQQ5ZYYG | ZD-RZ-03-2023-B0_公司职业技能等级评聘管理办法(2023).pdf | 7 | 982,958 | c3a97396c1996950... | c3a97396c1996950... | miss |
| 01KW1QPNMXGFK1QANRP1KTW3GP | ZD-RZ-07-2023-B0_公司岗位资质及出证上岗管理办法(试行)(2023).pdf | 7 | 1,004,968 | 6c1b4402345f9af5... | 6c1b4402345f9af5... | miss |
| 01KW1QVW3PERZPM2JT6KRNXMK4 | ZD-XX-08-2024-B1_公司数据质量管理办法_(2024).pdf | 7 | 161,446 | cd26d654f67e525a... | cd26d654f67e525a... | miss |
| 01KW1QXGQ2TE5PF1MABKD4PW7D | ZD-XX-2024-06-B1_公司数据运维管理办法_(2024).pdf | 7 | 154,126 | 19ad5787d4655ee1... | 19ad5787d4655ee1... | miss |
| 01KW1R2H50HW7CG5CP8XVTFHM7 | 沪机场虹委[2025]47号_公司党委会议"第一议题"制度实施办法(试行)(2025).pdf | 7 | 306,456 | 0d3ce010ccdeb135... | 0d3ce010ccdeb135... | miss |
| 01KW1PGAA9QPH5Q7PGNMA8H25A | WJ-AQ-08-2015-A2_公司控制区通行证管理规定(2015).pdf | 8 | 676,207 | 4fca48cb4e4842f4... | 4fca48cb4e4842f4... | miss |
| 01KW1PZHSB544F38EA0PVJ73ZG | ZD-AQ-16-2025-B1_公司相关方安全管理办法_(2025).pdf | 8 | 135,678 | 755d146fbea27680... | 755d146fbea27680... | miss |
| 01KW1Q6VTBHCHNRTKGD6D51065 | ZD-CW-05-2022-B0_公司发票使用管理办法(2022).pdf | 8 | 776,445 | c5bdae8036dd6c28... | c5bdae8036dd6c28... | miss |
| 01KW1QB2898TTM7FYST9YSSTEM | ZD-FS-18-2024-B0_公司经营类文件及合同规范化审核意见工作指引.pdf | 8 | 130,817 | 954d7dc9604f122d... | 954d7dc9604f122d... | miss |
| 01KW1QC3SF25CM90E9RYK307NZ | ZD-FW-02-2023-B1_公司服务质量监督检查管理办法(2023).pdf | 8 | 1,194,703 | 65917236ce9662aa... | 65917236ce9662aa... | miss |
| 01KW1QF0Y6A4CPBFB512RTXT9P | ZD-FW-16-2021-B0_公司重大决策法律审核和重大项目_法律论证实施暂行办法(2021).pdf | 8 | 167,881 | 4b05d0f0f08db834... | 4b05d0f0f08db834... | miss |
| 01KW1QGKSDR4JMHWRJXVQ6RJ8W | ZD-GJ-08-2024-B1_公司特种设备隐患排查治理管理办法_(2024).pdf | 8 | 354,563 | b685820704c2db53... | b685820704c2db53... | miss |
| 01KW1QNMWVF6P5HARN75BVR17T | ZD-RZ-02-2023-B0_公司专业技术职务评聘管理办法(2023).pdf | 8 | 835,959 | 8455ab518923c9c9... | 8455ab518923c9c9... | miss |
| 01KW1QV238S6CPQ8TJ1QFRZ6WZ | ZD-XX-03-2021-B0_公司办公电脑信息安全使用管理规定（试行）_(20162021).pdf | 8 | 727,959 | 4f08be105b04de58... | 4f08be105b04de58... | miss |
| 01KW1R2QR3A2FWK821YJFH7KAV | 沪机场虹安[2021]96号_关于进一步加强公司视频监控系统安全管理工作的通知_(2021).pdf | 8 | 1,128,088 | 6b54e0078454dee9... | 6b54e0078454dee9... | miss |
| 01KW1R41ATGWNDEVTBRXKDNDK5 | 沪机场虹法[2025]182号_关于印发公司规章制度管理指引的通知.pdf | 8 | 330,550 | 7aafe5c42bdb5893... | 7aafe5c42bdb5893... | miss |
| 01KW1PS9A0V72VB1FRREZ7CV08 | ZD-AQ-05-2023-B1_公司不安全事件调查管理办法(2023).pdf | 9 | 644,389 | a2a09776eb1a893d... | a2a09776eb1a893d... | miss |
| 01KW1QHG8P5NCAAXJACG4JTVDH | ZD-GJ-13-2025-B0_公司工程建设管理办法（2025）.pdf | 9 | 154,530 | 49e9f46d46ee9515... | 49e9f46d46ee9515... | miss |
| 01KW1QMD5SJ9YDY24KDMPCME54 | ZD-JS-12-2024-B0_公司重点用能设备能效限额要求管理办法_(2024).pdf | 9 | 263,085 | 219f8d3920970ba1... | 219f8d3920970ba1... | miss |
| 01KW1QNDZDY1WJ3W7ZC0QP4NJC | ZD-RZ-01-2023-B0_公司兼职教师管理规定(2023).pdf | 9 | 1,025,994 | 717774d4e3de8b04... | 717774d4e3de8b04... | miss |
| 01KW1QT4GZZ76QARXD515A1KH2 | ZD-SC-07-2025-B1_公司组织绩效管理办法_(2025).pdf | 9 | 151,660 | c7fa35303c3c8da1... | c7fa35303c3c8da1... | miss |
| 01KW1QZV64QST9PGKCRX10HKY1 | 公司法务总监、合规总监履职目录_(2024_版).pdf | 9 | 177,070 | 1cbfb8013e3fc51f... | 1cbfb8013e3fc51f... | miss |
| 01KW1R1GAZQ6QBW3YW2XS4073K | 沪机场虹委[2023]25号_公司保密工作管理规定(2023).pdf | 9 | 958,036 | 404805572f870302... | 404805572f870302... | miss |
| 01KW1R1QCNMFNE4YRDQ9TQHGCB | 沪机场虹委[2025]31号_公司党委会前置研究讨论事项清单(2025).pdf | 9 | 353,483 | e036af4d053b6e3f... | e036af4d053b6e3f... | miss |
| 01KW1R24T7Z4SEKPDR2DKVRBN4 | 沪机场虹委[2025]33号_公司"三重一大"决策实施办法(2025).pdf | 9 | 439,601 | 4d78cb113aa773a1... | 4d78cb113aa773a1... | miss |
| 01KW1PYAFMW1WEVSW9RM09AWEJ | ZD-AQ-14-2023-B0_公司安全绩效考核实施细则(2023).pdf | 10 | 786,654 | cca41abbf68f1581... | cca41abbf68f1581... | miss |
| 01KW1Q243WBNK61TW0EMQ5GQD7 | ZD-BG-03-2022-B0_公司行政印章企业证照介绍信管理办法(2022).pdf | 10 | 792,102 | d3e7678781ab42c1... | d3e7678781ab42c1... | miss |
| 01KW1Q737JR1Y6PY3VHS7WJFG7 | ZD-CW-06-2024-B1_公司运行服务外包项目管理办法_(2024).pdf | 10 | 157,612 | ffba78ef72c402ef... | ffba78ef72c402ef... | miss |
| 01KW1QSPD5Y2RXZ5Q0CAK2TTQX | ZD-RZ-08-2024-B0_公司岗位级别管理实施办法_(2024).pdf | 10 | 224,248 | 758a1ec9de078fd5... | 758a1ec9de078fd5... | miss |
| 01KW1QSXGQ7XRJJ5GEBS72CE7W | ZD-SC-03-2024-B0_公司不动产租赁管理办法_(2024).pdf | 10 | 165,464 | cc1d41996debff28... | cc1d41996debff28... | miss |
| 01KW1QWW36NJSX9C6F5DTE05WA | ZD-XX-11-2024-B0_公司科技创新奖励管理实施细则(2024).pdf | 10 | 363,581 | d429faad7925da9b... | d429faad7925da9b... | miss |
| 01KW1R123NQG1QNABVDZQV326W | 沪机场虹人[2024]76号_关于公司部分组织架构及相关管理职责优化调整的通知（2024）.pdf | 10 | 358,782 | 089fd92ce5ca93a1... | 089fd92ce5ca93a1... | miss |
| 01KW1PNYCZC7R1A6B5QMBZSHMJ | WJ-AQ-10-2015-A1_公司危险品管理规定(2015).pdf | 11 | 651,765 | 3572df263f3d2113... | 3572df263f3d2113... | miss |
| 01KW1PWM1P0V3D88SZPJ2N4A00 | ZD-AQ-07-2022-B0_公司施工项目安全管理办法(2022).pdf | 11 | 769,615 | cd759b7f422af909... | cd759b7f422af909... | miss |
| 01KW1Q3GY2QVGVTB5RR8BXX0FY | ZD-BG-09-2022-B0_公司档案管理规定(2022).pdf | 11 | 575,551 | 4ed28552cb5bd160... | 4ed28552cb5bd160... | miss |
| 01KW1QCZBDJNSYG9SS4XKJDXW2 | ZD-FW-08-2023-B0_公司服务质量风险管理办法(2023).pdf | 11 | 847,130 | 7e0c5cd741c64cca... | 7e0c5cd741c64cca... | miss |
| 01KW1QDEV0X50V9JFXM0TTG5M9 | ZD-FW-12-2021-B0_公司法律事务工作管理办法(2021).pdf | 11 | 694,954 | d6a6fbd657449e5d... | d6a6fbd657449e5d... | miss |
| 01KW1QFWNN079RPK7CZ0B10YFN | ZD-GJ-04-2024-B1_公司维修维护项目管理办法_(2024).pdf | 11 | 160,927 | d6472dc9559b4802... | d6472dc9559b4802... | miss |
| 01KW1QKR1SDBA3CCEDH9B63PH6 | ZD-JS-06-2021-B0_公司能源和计量管理规定(2021).pdf | 11 | 811,054 | 36b36d5417ab595f... | 36b36d5417ab595f... | miss |
| 01KW1R4MSSFV2QT2XQX0EBNA4P | 沪机场虹采[2024]247号_公司不良行为供应商处理指引(2024).pdf | 11 | 383,689 | 657a38ce1c14d85d... | 657a38ce1c14d85d... | miss |
| 01KW1PRC0F0H29W1TMQT469DPH | ZD-AQ-02-2023-B0_公司安全吹哨人自愿报告管理办法(试行)(2023).pdf | 12 | 875,779 | 152ad212ded53ba9... | 152ad212ded53ba9... | miss |
| 01KW1Q2V45D28NNEHJ4JRKMRK9 | ZD-BG-06-2022-B0_公司办公设备管理办法(2022).pdf | 12 | 623,218 | 3a39bac1c13e4dfd... | 3a39bac1c13e4dfd... | miss |
| 01KW1Q4CPHT1YWXT5YJWA0JF8S | ZD-BG-13-2025-B2_公司总经理办公会议事决策规则_(2025).pdf | 12 | 431,696 | 0f83d5541b8a9f5b... | 0f83d5541b8a9f5b... | miss |
| 01KW1QKZGHXNZA9GFA5A6573WH | ZD-JS-07-2023-B1_公司特种设备使用管理办法_(2023).pdf | 12 | 592,673 | d31cc8cd7aeaa262... | d31cc8cd7aeaa262... | miss |
| 01KW1QXQEJ9D3WYK84NQ6BSBQ2 | ZD-YJ-01-2024-B1_公司值班管理规定(2024).pdf | 12 | 209,695 | 94cd7cf134cb3e5f... | 94cd7cf134cb3e5f... | miss |
| 01KW1R025HSQ4JYEXBRY7AS47C | 公司采购评审专家和专家库管理指引_(2025).pdf | 12 | 290,902 | 1027b061def926b9... | 1027b061def926b9... | miss |
| 01KW1R18W34Q6FMBP9FP5941P9 | 沪机场虹办[2021]34号_关于印发上海虹桥国际机场有限责任公司_董事会议事规则的通知.pdf | 12 | 899,867 | eb79ef3c95ddd6d8... | eb79ef3c95ddd6d8... | miss |
| 01KW1PY395WC376AZT1V9ZNSAC | ZD-AQ-13-2025-B1_公司全员安全责任追究管理办法_(2025).pdf | 13 | 216,704 | b270764528619d67... | b270764528619d67... | miss |
| 01KW1Q5R8REDVXTM755D690QRE | ZD-CW-01-2022-B0_公司固定资产财务管理办法(2022).pdf | 13 | 786,118 | 45fa1ce336b78690... | 45fa1ce336b78690... | miss |
| 01KW1Q66MHAJVSWMMT4KK3T6ZQ | ZD-CW-02-2022-B0_公司资金管理办法(2022).pdf | 13 | 780,583 | 2ce9a45fd3399a52... | 2ce9a45fd3399a52... | miss |
| 01KW1QHQ3ZV75SKRV39693H6YV | ZD-GJ-14-2025-B1_公司工程建设项目变更管理办法(2025).pdf | 13 | 235,817 | a52fe08463c27927... | a52fe08463c27927... | miss |
| 01KW1QJHXENYDXSD6E8PSS05ND | ZD-JH-02-2021-B0_公司招商管理办法(2021).pdf | 13 | 592,988 | ea5a65de1201e068... | ea5a65de1201e068... | miss |
| 01KW1R4VRPHMA63CRZT14CYHSP | 沪机场虹采[2024]268号_运行服务外包类项目评标办法编制指引(试行)(2024).pdf | 13 | 355,340 | c7b32fbf9ea87b50... | c7b32fbf9ea87b50... | miss |
| 01KW1QAMQSNEW80R8VSBZ7QKJZ | ZD-FS-17-2024-B0_公司合规管理办法(试行)(2024).pdf | 14 | 178,866 | 19e1a0fca575a459... | 19e1a0fca575a459... | miss |
| 01KW1QE4VY936QJXAQGNE49PSK | ZD-FW-13-2021-B0_公司内部审计管理办法(2021).pdf | 15 | 987,116 | d4de2846e4039d28... | d4de2846e4039d28... | miss |
| 01KW1QFG84K43W4GGW7QWMTN00 | ZD-GJ-03-2024-B1_公司固定资产投资项目管理办法_(2024).pdf | 15 | 171,281 | baca938a737a1355... | baca938a737a1355... | miss |
| 01KW1QW2W4WC29BE8CHRQTWWA3 | ZD-XX-09-2024-B0_公司新基建项目管理细则(试行)_(2024).pdf | 15 | 690,120 | 271eac2cc65de992... | 271eac2cc65de992... | miss |
| 01KW1R33NGN0GCNYS496KJ4KY2 | 沪机场虹安[2024]65号_关于印发公司运行安全偏离与豁免前置程序的通知_(2024).pdf | 15 | 450,689 | 1f8ce3c94c0e9d60... | 1f8ce3c94c0e9d60... | miss |
| 01KW1Q6DHXTRT5DT1C0H58A4CK | ZD-CW-03-2025-B1_公司全面预算管理办法(2025).pdf | 16 | 225,794 | e9b5593593765eda... | e9b5593593765eda... | miss |
| 01KW1Q7GN67HVYD4NA65BD5R49 | ZD-CW-08-2023-B0_公司财务付款及报销管理办法(2023).pdf | 16 | 991,559 | 60ef989438b4ec41... | 60ef989438b4ec41... | miss |
| 01KW1Q5DJCE1Y7JECHMJG6H44G | ZD-CQ-03-2025-B0_上海虹桥机场场区施工占路掘路管理规定_(2025).pdf | 17 | 633,666 | 2d65aaee0e438efc... | 2d65aaee0e438efc... | miss |
| 01KW1QGTJXVS1HRT8XT9VY0N8E | ZD-GJ-09-2024-B1_公司特种设备风险管控管理办法_(2024).pdf | 17 | 467,094 | 69be9c97f24eab1b... | 69be9c97f24eab1b... | miss |
| 01KW1QVF3FDHGYEWMVW9QVBDWD | ZD-XX-07-2024-B1_公司数据安全管理办法(试行).pdf | 17 | 251,853 | b4d706b9c9b9bab8... | b4d706b9c9b9bab8... | miss |
| 01KW1QBW3K8097GWBCVW78TSSG | ZD-FW-01-2023-B1_公司服务质量工作管理规定(2023).pdf | 18 | 444,680 | 09ffd15fc259ec4f... | 09ffd15fc259ec4f... | miss |
| 01KW1QCGX58H0YN7YMD1ZKZ853 | ZD-FW-06-2023-B1_公司服务质量投诉管理办法(2023).pdf | 18 | 1,332,293 | 4ee72714480eb2b1... | 4ee72714480eb2b1... | miss |
| 01KW1QYZ1CKV2ZKS6JPD7J6QJE | ZD-ZB-02-2024-B1_公司合同管理办法_(2024).pdf | 18 | 267,059 | 12ac753ae2e6eecd... | 12ac753ae2e6eecd... | miss |
| 01KW1QMS19B5SGJRFYNS92Q64W | ZD-NY-04-2023-B0_虹桥机场西区共同沟管理办法(2023).pdf | 19 | 974,123 | 77ce2a2c222dc475... | 77ce2a2c222dc475... | miss |
| 01KW1Q3QYRGCKVVFEGKC6XK4K7 | ZD-BG-10-2022-B0_公司文书档案管理办法(2022).pdf | 20 | 571,972 | 5322969252d6ce2e... | 5322969252d6ce2e... | miss |
| 01KW1QX86JKBF5BXC3H9FNQZR4 | ZD-XX-2024-05-B2_公司数据治理管理办法_(2024).pdf | 20 | 226,589 | 59a08d276b51a3b6... | 59a08d276b51a3b6... | miss |
| 01KW1Q2JWK99MEYP8MQDVB45XJ | ZD-BG-05-2024-B1_公司信访工作管理办法_(2024).pdf | 21 | 441,546 | 0bca948b40d005da... | 0bca948b40d005da... | miss |
| 01KW1QBENZM8H3KS9DAPZ48BS0 | ZD-FS-19-2024-B0_公司标准化管理办法(试行)_(2024).pdf | 21 | 705,743 | f810a0f09f30ccff... | f810a0f09f30ccff... | miss |
| 01KW1QD6AJYBBNY93WKBB8JT6S | ZD-FW-11-2024-B1_公司规章制度管理规定_(2024).pdf | 21 | 221,767 | da0ad9688f029853... | da0ad9688f029853... | miss |
| 01KW1QH7DW95GE46GB83YPCYMS | ZD-GJ-11-2025-B0_公司环境保护管理制度(2025).pdf | 25 | 246,534 | 969ee3b55b7b3022... | 969ee3b55b7b3022... | miss |
| 01KW1QF7FFQCG49HGEKYZC4D07 | ZD-GJ-01-2024-B3_公司设施设备管理规定_(2024).pdf | 26 | 236,838 | 0093868f7d863098... | 0093868f7d863098... | miss |
| 01KW1QHYFN0FK1F4S3KDP5D6X2 | ZD-JG-01-2023-B0_虹桥国际机场不停航施工管理规定(2023).pdf | 26 | 328,201 | 45117bb3f94ec7b5... | 45117bb3f94ec7b5... | miss |
| 01KW1PQR4PZBAFTGTEG4MJ3VAX | ZD-AJ-01-2021-B0_上海虹桥国际机场航站楼门禁管理办法(2021).pdf | 27 | 1,613,621 | bfe642ffef5ce312... | bfe642ffef5ce312... | miss |
| 01KW1Q15CBE7YY282J1V1NEJTB | ZD-BG-01-2022-B0_公司公文处理管理办法(2022).pdf | 27 | 386,647 | 8c8355a29199512f... | 8c8355a29199512f... | miss |
| 01KW1QECGRDP16CV92S0MEKFC4 | ZD-FW-14-2021-B0_公司违规经营投资责任追究实施办法（试行）(2021).pdf | 28 | 1,187,869 | a366db096934e0ae... | a366db096934e0ae... | miss |
| 01KW1QYC5X6K576VQEYSMQSGFB | ZD-YJ-2023-03-B0_公司突发事件应急预案管理实施细则(2023).pdf | 28 | 231,352 | 32f1cd3bfa304c4d... | 32f1cd3bfa304c4d... | miss |
| 01KW1QWA9J7QFAAVDGT038VVJ5 | ZD-XX-10-2024-B1_公司科技项目管理办法_(2024).pdf | 31 | 308,225 | 1526fe6e0d26a353... | 1526fe6e0d26a353... | miss |
| 01KW1PSNEW46GGYMG52XZ26K5Y | ZD-AQ-06-2025-B1_公司消防安全管理规定_(2025).pdf | 32 | 289,141 | b485f3abee811e53... | b485f3abee811e53... | miss |
| 01KW1PYH6T93WNP2QRR9F99Q87 | ZD-AQ-15-2024-B0_公司安全教育培训管理办法(试行)_(2024).pdf | 32 | 332,787 | f0e82bea34531ed0... | f0e82bea34531ed0... | miss |
| 01KW1R3J2F86B39ZKD68BZR3YH | 沪机场虹指[2024]89号_关于修订《上海虹桥国际机场使用手册_管理规定》和《上海虹桥国际机场使用许可_管理规则》的通知.pdf | 38 | 1,831,928 | b3e0d73a8dca2882... | b3e0d73a8dca2882... | miss |
| 01KW1PWV2DV577C7HJZAQT3FYQ | ZD-AQ-09-2024-B3_公司安全信息管理办法(2024).pdf | 39 | 464,832 | 1796fdc7a177f3b4... | 1796fdc7a177f3b4... | miss |
| 01KW1PXAPK5XVFJKB3422FY921 | ZD-AQ-11-2024-B0_公司法定自查管理办法_（2024）.pdf | 41 | 478,670 | 89ea8022cab029d0... | 89ea8022cab029d0... | miss |
| 01KW1PRS3ER0QX2EEWNAEQSTMS | ZD-AQ-03-2024-B1_公司安全风险分级管控和隐患排查治理_双重预防工作机制管理办法_（2024）.pdf | 42 | 1,039,304 | 409d253a2e44f598... | 409d253a2e44f598... | miss |
| 01KW1QG3VDTNTWSB38TEQ2YP6X | ZD-GJ-05-2024-B1_公司科技档案管理办法_(2024).pdf | 46 | 417,966 | 250c1331e2da11b1... | 250c1331e2da11b1... | miss |
| 01KW1QTHR9TF69HENCTP5MZEV2 | ZD-XX-01-2024-B3_公司网络安全管理办法(2024).pdf | 47 | 368,152 | 9d8a17a7cfa12189... | 9d8a17a7cfa12189... | miss |
| 01KW1PZ0HS16E6NZB7F70YFB1M | ZD-AQ-16-2025-B1_公司全员安全生产责任制实施办法(试行)_（2025）.pdf | 52 | 388,079 | 0ee5b6c01644aff6... | 0ee5b6c01644aff6... | miss |

## 附录 B：调研脚本

- `/tmp/ocr_cache_research.py` — 遍历 meta → 算 file_hash → 对比 cache 目录
- `/tmp/page_count.py` — 用 `pdfinfo` 批量取 PDF 页数
- 完整 CSV 输出：`/tmp/full_research.csv`（含 doc_id / original_name / content_hash / file_hash / file_size_bytes / pages / cache_hit）

均为一次性脚本，放在系统临时目录里，未污染项目目录。

---

**信息来源**（计费规则部分）：
- [PaddlePaddle/PaddleOCR GitHub](https://github.com/PaddlePaddle/PaddleOCR) — 模型权重 Apache 2.0
- [百度智能云 OCR 文字识别](https://cloud.baidu.com) — 按"次 / 张 / 页次"计费，常见量级 0.005–0.02 元/次