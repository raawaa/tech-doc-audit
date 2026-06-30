# 拆分文档向量化与知识库检索索引的术语及状态字段

历史上 KB 文档与知识库**共享** `index_status` 字段名与 `ready` 终态，但描述的是两个不同的领域生命周期（per-document 向量化 vs per-KB 检索服务可用性）。更严重的是，文档 `ready` 在 `import_document` 同步路径（`services/doc_service.py:88`）被设在向量化**之前**、且失败（`:129`）不回退，使该值既可表"已保存"又可表"已向量化"，主动误导。

决定：拆为两个独立概念与字段——文档向量化用 `embedding_status`（终态 `embedded`），知识库检索索引保留 `index_status`（终态从 `ready` 改 `searchable`）。前端 `Badge` 不再用同一标签渲染两者。同步导入路径向量化失败必须回退 `failed`。

## Considered Options

- **彻底拆（字段名 + 终态词，采纳）** — 根除认知债，但连锁改 models / API 契约 / 前端 Badge / 存储元数据迁移。
- **仅改终态标签（字段名维持 `index_status`）** — 消除约 80% 歧义、改动小，但字段同名二义性仍在。

## Consequences

- 与 ADR-0002 配套：术语拆清后，"字段是唯一真相"的写入契约才能无歧义地落到 `embedding_status` 与 `index_status` 各自。
- 存在已有元数据需一次性迁移（旧 `ready` → `embedded` / `searchable`）。
- "就绪 / ready" 作为状态词在本项目检索域内禁用（见 `CONTEXT.md` 知识库检索段的 `_Avoid_`）。
