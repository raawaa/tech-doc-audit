"""Benchmark CLI 命令。"""

import json
import typer
from pathlib import Path

from benchmark.models import BenchmarkConfig
from benchmark.runner import run_benchmark, load_test_cases
from benchmark.sweeper import sweep as do_sweep
from benchmark.reporter import format_console, format_json, format_sweep_table

app = typer.Typer(help="搜索基准测试与参数优化")


@app.command()
def run(
    kb_ids: str = typer.Option("", "--kb-ids", help="知识库 ID（逗号分隔，留空=自动探测）"),
    test_cases: str = typer.Option("benchmark/test_cases.yaml", "--cases"),
    output: str = typer.Option("", "--output", "-o", help="JSON 输出路径"),
    chunk_size: int = typer.Option(512, "--chunk-size"),
    overlap: int = typer.Option(128, "--overlap"),
    threshold: float = typer.Option(0.2, "--threshold"),
    top_k: int = typer.Option(5, "--top-k"),
):
    """用指定参数跑一次基准测试。"""
    _resolve_kb_ids(kb_ids)
    cases = load_test_cases(test_cases)
    config = BenchmarkConfig(
        max_chars=chunk_size,
        overlap=overlap,
        similarity_threshold=threshold,
        top_k=top_k,
    )
    result = run_benchmark(cases, _resolve_kb_ids(kb_ids), config)
    typer.echo(format_console(result))
    if output:
        Path(output).write_text(format_json(result), encoding="utf-8")
        typer.echo(f"结果已保存到: {output}")


@app.command()
def sweep(
    kb_ids: str = typer.Option("", "--kb-ids"),
    test_cases: str = typer.Option("benchmark/test_cases.yaml", "--cases"),
    output: str = typer.Option("", "--output", "-o", help="JSON 输出路径"),
    chunk_sizes: str = typer.Option("256,512,768", "--chunk-sizes"),
    overlaps: str = typer.Option("64,128,192", "--overlaps"),
    thresholds: str = typer.Option("0.1,0.2,0.3", "--thresholds"),
    top_ks: str = typer.Option("3,5", "--top-ks"),
    sort_by: str = typer.Option("mrr", "--sort"),
):
    """扫描参数组合，找出最优配置。"""
    ids = _resolve_kb_ids(kb_ids)
    cases_path = test_cases

    runs = do_sweep(
        kb_ids=ids,
        test_cases_path=cases_path,
        chunk_sizes=[int(x) for x in chunk_sizes.split(",")],
        overlaps=[int(x) for x in overlaps.split(",")],
        thresholds=[float(x) for x in thresholds.split(",")],
        top_ks=[int(x) for x in top_ks.split(",")],
    )

    typer.echo(format_sweep_table(runs, sort_by=sort_by))
    if output:
        data = [r.model_dump(mode="json") for r in runs]
        Path(output).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(f"结果已保存到: {output}")


@app.command()
def inspect(
    kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID"),
    max_chunks: int = typer.Option(10, "--max", help="最多显示 chunks"),
):
    """查看 KB 的 FAISS 索引状态。"""
    from core.index_manager import get_kb_index_built, _vectors_dir

    if not get_kb_index_built(kb_id):
        typer.echo(f"KB {kb_id} 没有 FAISS 索引")
        raise typer.Exit(1)

    d = _vectors_dir(kb_id)
    import os as _os
    index_path = d / "faiss.index"
    file_size = _os.path.getsize(str(index_path)) if index_path.exists() else 0
    typer.echo(f"FAISS 索引: {index_path}")
    typer.echo(f"文件大小: {file_size / 1024:.1f} KB")
    typer.echo(f"目录内容: {[p.name for p in d.iterdir()]}")


def _resolve_kb_ids(kb_ids_str: str) -> list[str]:
    """解析 KB ID 参数。空字符串时自动探测。"""
    if kb_ids_str:
        return [x.strip() for x in kb_ids_str.split(",") if x.strip()]
    import storage.kb_repo as kb_repo
    kbs = kb_repo.list_all()
    ids = [kb.id for kb in kbs]
    if not ids:
        # fallback：扫描 data/kbs/ 目录
        from pathlib import Path
        import os
        data_dir = Path(os.environ.get("AUDIT_DATA_DIR", "./data"))
        kbs_dir = data_dir / "kbs"
        if kbs_dir.exists():
            ids = [d.name for d in kbs_dir.iterdir() if d.is_dir()]
    return ids
