"""检索服务。

向量检索为首选（FAISS），提供格式化结果供审核使用。
"""

from core.logger import get_logger

_logger = get_logger(__name__)


def get_kb_content_for_audit(kb_ids: list[str], clause_text: str) -> str:
    """获取相关知识库内容用于审核分析。"""
    from services.vector_search import get_kb_content
    try:
        return get_kb_content(kb_ids, clause_text)
    except Exception as e:
        _logger.warning("vector kb content failed: %s", e)
        return "未找到相关标准依据。"
