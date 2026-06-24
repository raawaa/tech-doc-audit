"""知识库需求提取器。

将 KB 标准文档中的条款拆成原子化可检查需求（AtomicRequirement），
为"需求锚定审核"（Phase 1）做准备。

流程：
1. 读取 KB 中所有文档的原始内容
2. 按条款编号切分文档
3. 逐条送给 LLM（temperature=0）提取原子需求
4. 持久化存储结果
"""

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from llama_index.core.llms import ChatMessage, MessageRole
from llama_index.core.prompts import ChatPromptTemplate

from core.logger import get_logger
from core.settings import get_llm, get_embed_model
from core.text_extraction import extract_text
from models.requirement import AtomicRequirement, AtomicRequirementList
from storage.kb_repo import get as get_kb
from storage.doc_repo import get_doc as get_kb_doc, list_docs as list_kb_docs

_logger = get_logger(__name__)

# ── 存储路径 ────────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))


def _requirements_path(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "requirements" / "requirements.json"


def _requirements_index_path(kb_id: str) -> Path:
    return DATA_DIR / "kbs" / kb_id / "requirements" / "index.json"


# ── LLM Prompt ─────────────────────────────────────────────────────────────────


SYSTEM_PROMPT = """你是一个标准条款分析专家。你的任务是将标准/制度文件中的条款拆解为原子化的、可独立检查的需求。

## 什么是原子需求？

原子需求是一个最小、自包含、可验证的要求。例如标准原文"质保期自验收合格之日起计算，不得少于12个月"可拆为：
1. "质保期自验收合格之日起计算"（check_type=semantic）
2. "质保期不得少于12个月"（check_type=threshold, expected_value="≥12个月"）

## 输出要求

每条原子需求必须包含：
- requirement_text: 需求的具体文字描述
- check_type: 检查类型
  - threshold: 数值阈值检查（≥、≤、>、<）
  - exists: 检查某内容是否存在
  - equals: 检查精确匹配
  - range: 检查是否在范围内
  - semantic: 语义判断（无法用规则表达的需要LLM判断）
- expected_value: 如果是 threshold/equals/range，给出期望值
- category: 需求归类（如 quality_warranty, payment_terms, technical_spec, legal_compliance, general）
- keywords: 3-5 个检索关键词，用于在文档中定位相关段落

## 重要原则
- 如果一条条款包含多个独立的要求，拆分为多条原子需求
- 如果条款只是定义或说明性文字，不包含可检查的要求，可以跳过
- 保持需求的原始语义，不要过度解释或添加原文没有的要求

## 输出格式
请以 JSON 格式输出，包含 requirements 数组和可选的 notes 字段。
每条 requirement 必须包含 requirement_text, check_type, category, keywords 字段。
不要输出其他内容。"""


_prompt = ChatPromptTemplate(
    message_templates=[
        ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
        ChatMessage(
            role=MessageRole.USER,
            content="""请分析以下标准条款，提取其中的原子需求。

【来源标准】
标准名称：{standard_name}
标准编号：{standard_id}
条款编号：{clause_number}

【条款原文】
{clause_text}

请提取该条款中包含的所有原子需求。如果该条款不包含可检查的需求（如仅为定义或说明），可以返回空列表。""",
        ),
    ]
)


# ── 条款切分 ────────────────────────────────────────────────────────────────────

# 条款编号正则：匹配 "5.2"、"5.2.1"、"（二）"、"2）" 等
_CLAUSE_NUMBER_RE = re.compile(
    r"^[（(]?\d+(?:\.\d+)*[）\)、\.\s]+",
    re.MULTILINE,
)

# 章节标题正则
_CHAPTER_HEADING_RE = re.compile(
    r"^#{1,6}\s+.+$|^第[一二三四五六七八九十]+[章节编].*$",
    re.MULTILINE,
)


def _split_into_clauses(text: str) -> list[tuple[str, str]]:
    """将标准文档原文按条款编号切分为列表。

    Returns:
        [(clause_number, clause_text), ...]
    """
    if not text:
        return []

    lines = text.split("\n")
    clauses: list[tuple[str, str]] = []
    current_num = ""
    current_lines: list[str] = []

    def _flush():
        if current_num and current_lines:
            clauses.append((current_num, "\n".join(current_lines).strip()))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_lines:
                current_lines.append("")
            continue

        # 检测是否为章节标题（跳过）
        if _CHAPTER_HEADING_RE.match(stripped) and len(stripped) < 100:
            _flush()
            current_num = ""
            current_lines = []
            continue

        # 检测是否为新的条款编号
        m = _CLAUSE_NUMBER_RE.match(stripped)
        if m:
            num = m.group(0).strip("（）()、. \t")
            if num != current_num:
                _flush()
                current_num = num
                remaining = stripped[m.end():].strip()
                current_lines = [remaining] if remaining else []
                continue

        current_lines.append(stripped)

    _flush()
    return clauses


# ── 需求提取 ────────────────────────────────────────────────────────────────────


def extract_requirements(
    kb_id: str,
    llm=None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> list[AtomicRequirement]:
    """从 KB 的所有文档中提取原子需求。

    Args:
        kb_id: 知识库 ID。
        llm: LLM 实例（可选，默认从 settings 延迟加载）。
        on_progress: 进度回调 (doc_name, current, total)。

    Returns:
        提取的原子需求列表（已持久化）。
    """
    kb = get_kb(kb_id)
    if not kb:
        raise ValueError(f"Knowledge base not found: {kb_id}")

    llm = llm or get_llm()
    all_requirements: list[AtomicRequirement] = []
    doc_ids = kb.document_ids
    total = len(doc_ids)
    requirement_index = 0

    for idx, doc_id in enumerate(doc_ids):
        doc = get_kb_doc(kb_id, doc_id)
        if not doc:
            _logger.warning("Document %s not found in KB %s, skipping", doc_id, kb_id)
            continue

        if on_progress:
            on_progress(doc.name, idx, total)

        _logger.info("Extracting requirements from: %s", doc.name)

        try:
            # 提取文档文本
            text = extract_text(doc.file_path)
            if not text:
                _logger.warning("Empty text for document %s, skipping", doc.name)
                continue

            # 按条款编号切分
            clauses = _split_into_clauses(text)
            _logger.info(
                "Document '%s': extracted %d clauses",
                doc.name, len(clauses),
            )

            # 逐条送 LLM 提取原子需求
            doc_requirements: list[AtomicRequirement] = []
            for clause_num, clause_text in clauses:
                if len(clause_text.strip()) < 10:
                    continue

                try:
                    results = _extract_from_clause(
                        llm,
                        standard_name=doc.name,
                        standard_id=doc.name,
                        clause_number=clause_num,
                        clause_text=clause_text[:2000],  # 截断过长的条款
                    )
                    for req in results:
                        req.requirement_id = f"req_{kb_id}_{doc_id}_{requirement_index}"
                        req.source_kb_id = kb_id
                        req.source_doc_id = doc_id
                        req.source_doc_name = doc.name
                        # 如果 standard_id 未指定，用文档名
                        if not req.standard_id:
                            req.standard_id = doc.name
                        requirement_index += 1
                    doc_requirements.extend(results)
                except Exception as e:
                    _logger.warning(
                        "Failed to extract requirements from clause %s of %s: %s",
                        clause_num, doc.name, e,
                    )

            all_requirements.extend(doc_requirements)
            _logger.info(
                "Document '%s': extracted %d atomic requirements",
                doc.name, len(doc_requirements),
            )

        except Exception as e:
            _logger.error("Failed to process document %s: %s", doc.name, e)

    # 持久化
    if all_requirements:
        _persist_requirements(kb_id, all_requirements)
        _logger.info(
            "Total: %d atomic requirements extracted from KB '%s'",
            len(all_requirements), kb_id,
        )
    else:
        _logger.warning("No requirements extracted from KB '%s'", kb_id)

    return all_requirements


def _extract_from_clause(
    llm,
    *,
    standard_name: str,
    standard_id: str,
    clause_number: str,
    clause_text: str,
) -> list[AtomicRequirement]:
    """用 LLM 从单条条款中提取原子需求。

    使用普通 chat + JSON 解析路径（跳过 as_structured_llm），
    因为 DeepSeek API 不完全兼容 OpenAI 的 structured output 格式。
    """
    messages = _prompt.format_messages(
        standard_name=standard_name,
        standard_id=standard_id,
        clause_number=clause_number,
        clause_text=clause_text,
    )

    try:
        response = llm.chat(messages)
        result = _parse_json_fallback(response.message.content or "")
    except Exception:
        return []

    if not result or not result.requirements:
        return []

    return result.requirements


def _parse_json_fallback(content: str) -> Optional[AtomicRequirementList]:
    """当 structured_llm 不可用时的降级解析。"""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return AtomicRequirementList.model_validate(data)
    except Exception:
        return None


# ── 持久化 ──────────────────────────────────────────────────────────────────────


def _persist_requirements(kb_id: str, requirements: list[AtomicRequirement]):
    """将需求列表持久化到磁盘。"""
    path = _requirements_path(kb_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = [req.model_dump(mode="json") for req in requirements]
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_requirements(kb_id: str) -> list[AtomicRequirement]:
    """从磁盘加载已提取的需求。"""
    path = _requirements_path(kb_id)
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [AtomicRequirement(**item) for item in data]
    except Exception as e:
        _logger.error("Failed to load requirements for KB %s: %s", kb_id, e)
        return []


def delete_requirements(kb_id: str):
    """删除 KB 的需求提取结果。"""
    path = _requirements_path(kb_id)
    if path.exists():
        path.unlink()
    index_path = _requirements_index_path(kb_id)
    if index_path.exists():
        index_path.unlink()


def get_requirement_count(kb_id: str) -> int:
    """获取 KB 的原子需求数量。"""
    path = _requirements_path(kb_id)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data)
    except Exception:
        return 0


# ── CLI 集成 ───────────────────────────────────────────────────────────────────


def extract_requirements_cli(kb_id: str):
    """CLI 入口：从 KB 提取需求并打印统计。"""
    print(f"Extracting requirements from KB: {kb_id}")

    def on_progress(name, current, total):
        print(f"  [{current + 1}/{total}] Processing: {name}")

    requirements = extract_requirements(kb_id, on_progress=on_progress)
    print(f"\nDone! Extracted {len(requirements)} atomic requirements.")
    print(f"Saved to: {_requirements_path(kb_id)}")

    # 按类别统计
    categories = {}
    for req in requirements:
        cat = req.category or "uncategorized"
        categories[cat] = categories.get(cat, 0) + 1
    if categories:
        print("\nBy category:")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {count}")
