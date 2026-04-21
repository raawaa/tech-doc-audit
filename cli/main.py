import typer
from typing import Optional, Literal

import services.kb_service as kb_svc
import services.doc_service as doc_svc
import services.indexing_service as idx_svc

app = typer.Typer(help="技术文档审核系统 - 知识库管理 CLI")

kb_app = typer.Typer(help="知识库管理")
doc_app = typer.Typer(help="文档管理")
index_app = typer.Typer(help="索引管理")

app.add_typer(kb_app, name="kb")
app.add_typer(doc_app, name="doc")
app.add_typer(index_app, name="index")


@kb_app.command("create")
def kb_create(
    name: str = typer.Option(..., "--name", "-n", help="知识库名称"),
    category: str = typer.Option("national", "--category", "-c", help="分类: national/industry/enterprise"),
    description: str = typer.Option("", "--description", "-d", help="描述"),
):
    kb = kb_svc.create_kb(name=name, description=description, category=category)
    typer.echo(f"创建成功: {kb.id} | {kb.name} ({kb.category})")


@kb_app.command("list")
def kb_list(category: Optional[str] = typer.Option(None, "--category", "-c")):
    kbs = kb_svc.list_kbs(category=category)
    if not kbs:
        typer.echo("暂无知识库")
        return
    typer.echo(f"{'ID':<30} {'名称':<20} {'分类':<12} {'索引状态':<10}")
    typer.echo("-" * 80)
    for kb in kbs:
        typer.echo(f"{kb.id:<30} {kb.name:<20} {kb.category:<12} {kb.index_status:<10}")


@kb_app.command("delete")
def kb_delete(kb_id: str = typer.Option(..., "--id", help="知识库 ID"), force: bool = typer.Option(False, "--force", "-f")):
    if not force:
        confirm = typer.confirm(f"确认删除知识库 {kb_id}? 此操作将级联删除所有文档和索引。")
        if not confirm:
            typer.echo("已取消")
            raise typer.Exit()
    success = kb_svc.delete_kb(kb_id)
    if success:
        typer.echo(f"删除成功: {kb_id}")
    else:
        typer.echo(f"删除失败: {kb_id}")
        raise typer.Exit(1)


@doc_app.command("import")
def doc_import(
    kb_id: str = typer.Option(..., "--kb-id", help="目标知识库 ID"),
    file_path: str = typer.Option(..., "--file", "-f", help="文件路径"),
):
    import os
    if not os.path.exists(file_path):
        typer.echo(f"文件不存在: {file_path}")
        raise typer.Exit(1)
    with open(file_path, "rb") as f:
        content = f.read()
    original_name = os.path.basename(file_path)
    doc = doc_svc.import_document(kb_id, original_name, content)
    typer.echo(f"导入成功: {doc.id} | {doc.name} | 页数: {doc.page_count or 'N/A'}")


@doc_app.command("list")
def doc_list(kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID")):
    import storage.doc_repo as doc_repo
    docs = doc_repo.list_docs(kb_id)
    if not docs:
        typer.echo("该知识库暂无文档")
        return
    typer.echo(f"{'ID':<30} {'名称':<25} {'类型':<6} {'索引状态':<10}")
    typer.echo("-" * 80)
    for d in docs:
        typer.echo(f"{d.id:<30} {d.name:<25} {d.file_type:<6} {d.index_status:<10}")


@doc_app.command("delete")
def doc_delete(
    kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID"),
    doc_id: str = typer.Option(..., "--doc-id", help="文档 ID"),
):
    success = doc_svc.delete_document(kb_id, doc_id)
    if success:
        typer.echo(f"删除成功: {doc_id}")
    else:
        typer.echo(f"删除失败: {doc_id}")
        raise typer.Exit(1)


@index_app.command("build")
def index_build(
    kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID"),
    doc_id: str = typer.Option(..., "--doc-id", help="文档 ID"),
    model: str = typer.Option("qwen3.5:0.8b", "--model", "-m"),
):
    """为知识库中的指定文档构建索引"""
    import storage.doc_repo as doc_repo

    doc = doc_repo.get_doc(kb_id, doc_id)
    if not doc:
        typer.echo(f"文档不存在: {doc_id}")
        raise typer.Exit(1)

    typer.echo(f"开始构建文档 {doc.name} 的索引...")
    idx_svc.build_index_for_doc(doc, model)
    typer.echo(f"索引构建完成，状态: {doc.index_status}")


@index_app.command("rebuild")
def index_rebuild(
    kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID"),
    model: str = typer.Option("qwen3.5:0.8b", "--model", "-m"),
):
    typer.echo(f"开始重建知识库 {kb_id} 的索引...")
    idx_svc.rebuild_kb_index(kb_id, model)
    typer.echo("索引重建完成")


@index_app.command("status")
def index_status(kb_id: str = typer.Option(..., "--kb-id", help="知识库 ID")):
    kb = kb_svc.get_kb(kb_id)
    if not kb:
        typer.echo(f"知识库不存在: {kb_id}")
        raise typer.Exit(1)
    typer.echo(f"知识库: {kb.name}")
    typer.echo(f"索引状态: {kb.index_status}")
    import storage.doc_repo as doc_repo
    docs = doc_repo.list_docs(kb_id)
    typer.echo(f"文档数: {len(docs)}")
    for d in docs:
        typer.echo(f"  - {d.name}: {d.index_status}")


if __name__ == "__main__":
    app()
