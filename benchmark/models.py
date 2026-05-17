from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


class ExpectedChunk(BaseModel):
    """预期应该被检索到的内容片段。"""
    content_keywords: list[str] = Field(..., min_length=1)
    relevance: Literal["must_match", "should_match"] = "must_match"


class TestCase(BaseModel):
    """一个 benchmark 测试用例。"""
    id: str
    topic_id: str = ""
    query: str
    description: str = ""
    expected_chunks: list[ExpectedChunk] = Field(default_factory=list, min_length=1)


class BenchmarkConfig(BaseModel):
    """当前正在测试的参数配置。"""
    max_chars: int = 512
    overlap: int = 128
    similarity_threshold: float = 0.2
    top_k: int = 5
    acceptance_threshold: float = 0.35


class SingleResult(BaseModel):
    """单个测试用例的指标。"""
    test_id: str
    query: str
    precision_at_k: float = 0.0
    recall: float = 0.0
    reciprocal_rank: float = 0.0
    num_results_returned: int = 0
    num_expected: int = 0
    matched: int = 0
    details: str = ""


class AggregateMetrics(BaseModel):
    """聚合指标。"""
    mean_precision: float = 0.0
    mean_recall: float = 0.0
    mrr: float = 0.0
    total_cases: int = 0
    cases_with_match: int = 0
    per_topic: dict[str, dict] = Field(default_factory=dict)


class BenchmarkRun(BaseModel):
    """一次 benchmark 运行的全部结果。"""
    config: BenchmarkConfig
    per_case: list[SingleResult] = Field(default_factory=list)
    aggregate: AggregateMetrics = Field(default_factory=AggregateMetrics)
    duration: float = 0.0
    run_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    kb_ids: list[str] = Field(default_factory=list)


class SweepResult(BaseModel):
    """参数扫描结果"""
    runs: list[BenchmarkRun] = Field(default_factory=list)
    sorted_by: str = "mrr"
