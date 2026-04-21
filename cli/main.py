import typer
from typing import Optional, Literal

import services.kb_service as kb_svc
import services.doc_service as doc_svc
import services.indexing_service as idx_svc
import services.audit_doc_service as audit_doc_svc
import services.structure_service as structure_svc
import services.temp_index_service as temp_index_svc

app = typer.Typer(help="技术文档审核系统 - CLI")

kb_app = typer.Typer(help="知识库管理")
doc_app = typer.Typer(help="知识库文档管理")
index_app = typer.Typer(help="知识库索引管理")
audit_app = typer.Typer(help="待审核文档管理")

app.add_typer(kb_app, name="kb")
app.add_typer(doc_app, name="doc")
app.add_typer(index_app, name="index")
app.add_typer(audit_app, name="audit")


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


# ===== 待审核文档管理 =====

@audit_app.command("upload")
def audit_upload(
    file_path: str = typer.Option(..., "--file", "-f", help="文件路径"),
):
    """上传待审核文档。"""
    import os
    if not os.path.exists(file_path):
        typer.echo(f"文件不存在: {file_path}")
        raise typer.Exit(1)
    with open(file_path, "rb") as f:
        content = f.read()
    original_name = os.path.basename(file_path)
    doc = audit_doc_svc.upload_document(original_name, content)
    typer.echo(f"上传成功: {doc.id} | {doc.name}")
    typer.echo(f"状态: {doc.status}")


@audit_app.command("list")
def audit_list(status: Optional[str] = typer.Option(None, "--status", "-s", help="按状态筛选")):
    """列出待审核文档。"""
    docs = audit_doc_svc.list_documents()
    if status:
        docs = [d for d in docs if d.status == status]
    if not docs:
        typer.echo("暂无待审核文档")
        return
    typer.echo(f"{'ID':<30} {'名称':<25} {'状态':<15} {'页数':<6}")
    typer.echo("-" * 85)
    for d in docs:
        typer.echo(f"{d.id:<30} {d.name:<25} {d.status:<15} {d.page_count or 'N/A':<6}")


@audit_app.command("parse")
def audit_parse(
    doc_id: str = typer.Option(..., "--id", help="文档 ID"),
):
    """解析文档，提取文本。"""
    doc = audit_doc_svc.parse_document(doc_id)
    typer.echo(f"解析完成: {doc.id}")
    typer.echo(f"状态: {doc.status}")
    typer.echo(f"页数: {doc.page_count or 'N/A'}")
    if doc.parsed_content:
        typer.echo(f"文本长度: {len(doc.parsed_content)} 字符")


@audit_app.command("structure")
def audit_structure(
    doc_id: str = typer.Option(..., "--id", help="文档 ID"),
):
    """分析文档结构。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        typer.echo(f"文档不存在: {doc_id}")
        raise typer.Exit(1)

    if not doc.parsed_content:
        typer.echo("文档未解析，正在解析...")
        doc = audit_doc_svc.parse_document(doc_id)

    if not doc.structure:
        typer.echo("正在分析文档结构...")
        doc = structure_svc.analyze_document_structure(doc_id)

    s = doc.structure
    if s:
        typer.echo(f"\n文档结构:")
        if s.title:
            typer.echo(f"  标题: {s.title}")
        typer.echo(f"  章节数: {len(s.chapters)}")
        typer.echo(f"  条款数: {s.total_clauses}")
        for ch in s.chapters[:5]:  # 只显示前5个章节
            typer.echo(f"  - {ch.number or ''} {ch.title} ({len(ch.clauses)} 条款)")
    else:
        typer.echo("未识别到文档结构")


@audit_app.command("process")
def audit_process(
    doc_id: str = typer.Option(..., "--id", help="文档 ID"),
):
    """完整处理文档：解析 + 结构分析 + 索引。"""
    doc = audit_doc_svc.get_document(doc_id)
    if not doc:
        typer.echo(f"文档不存在: {doc_id}")
        raise typer.Exit(1)

    typer.echo(f"处理文档: {doc.name}")

    # 1. 解析
    typer.echo("  [1/3] 解析文档...")
    doc = audit_doc_svc.parse_document(doc_id)

    # 2. 结构分析
    typer.echo("  [2/3] 分析结构...")
    if doc.parsed_content:
        try:
            doc = structure_svc.analyze_document_structure(doc_id)
        except Exception as e:
            typer.echo(f"  结构分析失败: {e}")

    # 3. 构建索引
    typer.echo("  [3/3] 构建索引...")
    doc = temp_index_svc.build_temp_index(doc)

    typer.echo(f"\n处理完成: {doc.id}")
    typer.echo(f"最终状态: {doc.status}")
    if doc.structure:
        typer.echo(f"识别章节: {len(doc.structure.chapters)}, 条款: {doc.structure.total_clauses}")


@audit_app.command("delete")
def audit_delete(
    doc_id: str = typer.Option(..., "--id", help="文档 ID"),
    force: bool = typer.Option(False, "--force", "-f"),
):
    """删除待审核文档。"""
    if not force:
        confirm = typer.confirm(f"确认删除文档 {doc_id}?")
        if not confirm:
            typer.echo("已取消")
            raise typer.Exit()
    success = audit_doc_svc.delete_document(doc_id)
    if success:
        temp_index_svc.delete_temp_index(doc_id)
        typer.echo(f"删除成功: {doc_id}")
    else:
        typer.echo(f"删除失败: {doc_id}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
