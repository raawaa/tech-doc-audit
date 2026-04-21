#!/usr/bin/env python
"""完整流程验证脚本"""

import os
import sys
import tempfile

# 设置测试数据目录
os.environ["AUDIT_DATA_DIR"] = tempfile.mkdtemp()

def main():
    print("=" * 60)
    print("技术文档审核系统 - 完整流程验证")
    print("=" * 60)

    # 1. 测试导入
    print("\n[1] 测试模块导入...")
    try:
        import services.kb_service as kb_svc
        import services.doc_service as doc_svc
        import services.audit_doc_service as audit_doc_svc
        import services.audit_task_service as task_svc
        import services.audit_analysis_service as analysis_svc
        print("   ✓ 所有模块导入成功")
    except Exception as e:
        print(f"   ✗ 导入失败: {e}")
        return False

    # 2. 测试知识库创建
    print("\n[2] 测试知识库创建...")
    try:
        kb = kb_svc.create_kb(
            name="验证知识库",
            description="用于流程验证",
            category="national"
        )
        print(f"   ✓ 知识库创建成功: {kb.id}")
    except Exception as e:
        print(f"   ✗ 知识库创建失败: {e}")
        return False

    # 3. 测试文档导入
    print("\n[3] 测试文档导入...")
    try:
        sample_path = "sample_docs/sample_standard.pdf"
        if os.path.exists(sample_path):
            with open(sample_path, "rb") as f:
                content = f.read()
            kb_doc = doc_svc.import_document(kb.id, "验证标准.pdf", content)
            print(f"   ✓ 文档导入知识库成功: {kb_doc.id}")
        else:
            print(f"   ⚠ 跳过（示例文档不存在）")
    except Exception as e:
        print(f"   ✗ 文档导入失败: {e}")

    # 4. 测试待审核文档上传
    print("\n[4] 测试待审核文档上传...")
    try:
        if os.path.exists(sample_path):
            with open(sample_path, "rb") as f:
                content = f.read()
            audit_doc = audit_doc_svc.upload_document("验证待审.pdf", content)
            print(f"   ✓ 待审核文档上传成功: {audit_doc.id}")
        else:
            print(f"   ⚠ 跳过（示例文档不存在）")
            return True
    except Exception as e:
        print(f"   ✗ 待审核文档上传失败: {e}")
        return False

    # 5. 测试文档解析
    print("\n[5] 测试文档解析...")
    try:
        audit_doc = audit_doc_svc.parse_document(audit_doc.id)
        print(f"   ✓ 文档解析成功: {audit_doc.page_count} 页")
        if audit_doc.parsed_content:
            print(f"   ✓ 提取文本长度: {len(audit_doc.parsed_content)} 字符")
    except Exception as e:
        print(f"   ✗ 文档解析失败: {e}")

    # 6. 测试审核任务创建
    print("\n[6] 测试审核任务创建...")
    try:
        task = task_svc.create_task(
            document_id=audit_doc.id,
            kb_ids=[kb.id],
            audit_types=["compliance"]
        )
        print(f"   ✓ 审核任务创建成功: {task.id}")
    except Exception as e:
        print(f"   ✗ 审核任务创建失败: {e}")
        return False

    print("\n" + "=" * 60)
    print("验证完成！")
    print("=" * 60)
    print("\n下一步:")
    print("  1. 启动 API: uvicorn api.main:app --reload")
    print("  2. 启动前端: cd frontend && npm run dev")
    print("  3. 执行审核: python -m cli audit-task run --id " + task.id)

    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
