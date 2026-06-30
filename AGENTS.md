# AGENTS.md

给 Kimi Code（本项目 AI 助手）的工作指引。

## 运行测试前必须释放 GPU 显存

后端进程加载 bge-m3 模型后占用 ~2-5GB 显存，另开 Python 脚本会因 CUDA OOM 失败。

```bash
# 先杀后端
pkill -f "uvicorn api.main" && sleep 2

# 验证释放
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader

# 测试完成后重启
nohup uvicorn api.main:app --port 8000 > /tmp/backend.log 2>&1 &
```

## 关键修复记录（不要回退）

| 项目 | 文件 | 说明 |
|------|------|------|
| DeepSeek thinking 禁用 | `core/settings.py` get_llm() | `additional_kwargs={'extra_body': {'thinking': {'type': 'disabled'}}}` |
| reranker 按需加载/卸载 | `core/settings.py` run_reranker() | 不再常驻显存，每次加载→推理→del+gc+empty_cache |
| Agentic Markdown 解析 | `services/agentic_audit.py` _parse_action_fallback() | 支持 ```json 代码块 + 中文 Markdown 混合格式 |
| 审核去重 | `services/audit_task_service.py` _deduplicate_issues() | 按 cited_excerpt 去重 |

## 常用命令

```bash
uv run pytest tests/ -v --tb=short          # 全部测试
uv run uvicorn api.main:app --port 8000     # 启动 API
cd frontend && npm run dev                  # 前端开发
```

## 已知限制

- GTX 1070 Ti 8GB → bge-m3(2.2G) + reranker(2.2G) 紧张，reranker 已改为按需模式
- 知识库为标准规范，与招标文档领域不完全匹配，审核需依赖 LLM 自洽性检查

## Agent skills

### Issue tracker

Issues live as GitHub issues in `raawaa/tech-doc-audit` (use the `gh` CLI). External PRs are **not** a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical labels, each named after its role (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
