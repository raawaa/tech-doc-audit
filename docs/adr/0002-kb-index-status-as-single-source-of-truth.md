# 知识库索引状态以字段为唯一真相，FAISS 文件为可重生缓存

历史上"知识库索引是否已建"有两套并存的真相来源：`kb.index_status` 元数据字段，与 `default__vector_store.json` 文件是否存在（`get_kb_index_built`）。两者由不同代码路径独立写入且都不完整，必然分叉——典型表现是字段显示"未建立"却仍能检索（auto-rebuild 不回写字段）。

决定：以 `kb.index_status` 字段为**唯一真相**，FAISS 文件（含 `.npy` 文档向量缓存）降级为可从字段与文档重生的缓存。"重建成功后在 per-KB 锁内写回字段"作为 `rebuild_kb_index` 的内置契约，而非外包给调用方，使 reindex 按钮、auto-rebuild、批量导入等所有调用方共享同一段保证。

## Considered Options

- **字段为真相、文件为缓存（采纳）** — 单一真相，调用方免维护。
- **删除字段、永远问文件** — 也单一，但字段同时承载 UI 进度态（building + 进度），删字段会丢失人机交互所需的中间态。
- **保留双真相、补齐所有写入点** — 不可持续，每新增一个调用方就是新的分叉点。

## Consequences

- auto-rebuild（懒加载自愈）现在会短暂经历 `building → searchable`，UI 会闪一下"构建中"——这是诚实的，可接受。
- 任何新增的索引构建路径（CLI、迁移脚本）无需关心写字段，由 `rebuild_kb_index` 保证。
- 同步降级到 `services/doc_service.py: import_document` 同步路径的遗留 bug 需一并修复：向量化失败必须回退到 `failed`，不能停在 `ready`（见 CONTEXT.md 术语重构 ADR-0003 配套）。
