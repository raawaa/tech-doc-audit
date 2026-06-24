"""跨知识库 LlamaIndex 检索器。

支持同时搜索多个 KB 的 FAISS 索引，合并结果后返回。
"""

from typing import Optional

from llama_index.core import QueryBundle
from llama_index.core.schema import NodeWithScore
from llama_index.core.retrievers import BaseRetriever

from core.index_manager import get_kb_index
from core.logger import get_logger

_logger = get_logger(__name__)


class CrossKBRetriever(BaseRetriever):
    """跨多 KB 的检索器。

    Args:
        kb_ids: 知识库 ID 列表。
        top_k: 每个 KB 的检索数量（最终合并后取 top_k）。
        use_reranker: 是否使用 reranker 重排序检索结果。
    """

    def __init__(
        self,
        kb_ids: list[str],
        top_k: int = 5,
        use_reranker: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)  # type: ignore[call-arg]
        self.kb_ids = kb_ids
        self.top_k = top_k
        self.use_reranker = use_reranker

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        """在多个 KB 中检索，合并排序后返回 top_k 个结果。"""
        query = query_bundle.query_str
        if not query or not self.kb_ids:
            return []

        all_nodes: dict[str, NodeWithScore] = {}
        for kb_id in self.kb_ids:
            try:
                index = get_kb_index(kb_id)
                retriever = index.as_retriever(similarity_top_k=self.top_k)
                nodes = retriever.retrieve(query)
                for node in nodes:
                    node.node.metadata["kb_id"] = kb_id
                    # 去重：按 node_id 去重，保留分数高的
                    nid = node.node.node_id
                    if nid not in all_nodes or (node.score or 0) > (all_nodes[nid].score or 0):
                        all_nodes[nid] = node
            except Exception as e:
                _logger.warning("retrieval failed for kb %s: %s", kb_id, e)
                continue

        results = sorted(all_nodes.values(), key=lambda n: n.score or 0, reverse=True)

        # ── Reranker 重排序（按需加载→推理→卸载）────────────────────────
        # 用 cross-encoder 对候选结果精确打分，弥补 bi-encoder ANN 精度损失
        if self.use_reranker and results:
            try:
                from core.settings import run_reranker
                reranked = run_reranker(results, query)
                if reranked:
                    results = reranked
            except Exception as e:
                _logger.warning("reranker postprocess failed, using raw ranking: %s", e)

        return results[: self.top_k]
