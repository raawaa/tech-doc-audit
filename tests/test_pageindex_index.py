# tests/test_pageindex_index.py
"""PageIndex 索引生成测试"""

import os
import json
import httpx
import time


PAGEINDEX_URL = "http://localhost:8584"
SAMPLE_DOC_PATH = "sample_docs/sample_standard.pdf"


def test_pageindex_service_running():
    """测试 PageIndex 服务是否运行"""
    print("\n=== 测试 PageIndex 服务状态 ===")
    
    try:
        response = httpx.get(f"{PAGEINDEX_URL}/api/v1/search/status", timeout=10)
        assert response.status_code == 200, f"服务返回状态码: {response.status_code}"
        
        data = response.json()
        print(f"✓ PageIndex 服务正常运行")
        print(f"服务状态: {data}")
        return True
    except httpx.ConnectError:
        print(f"✗ PageIndex 服务未启动，请先运行 PageIndex 服务")
        print(f"启动方式: 在 PageIndex 目录执行 python -m pageindex serve")
        return False
    except Exception as e:
        print(f"✗ 服务检查失败: {e}")
        return False


def test_generate_pageindex_tree():
    """测试 PageIndex 树索引生成"""
    print("\n=== 测试 PageIndex 索引生成 ===")
    
    if not os.path.exists(SAMPLE_DOC_PATH):
        print(f"✗ 示例文档不存在: {SAMPLE_DOC_PATH}")
        print(f"请先运行: python scripts/generate_sample_doc.py")
        return False
    
    print(f"文档路径: {SAMPLE_DOC_PATH}")
    
    try:
        from pageindex import PageIndexTree
        
        print("开始生成 PageIndex 树索引...")
        start_time = time.time()
        
        # 创建索引
        tree = PageIndexTree(
            pdf_path=SAMPLE_DOC_PATH,
            model="qwen3.5:0.8b",
            max_pages_per_node=5,
            max_tokens_per_node=15000
        )
        
        # 生成树结构
        tree_structure = tree.generate()
        
        elapsed = time.time() - start_time
        print(f"索引生成完成，耗时: {elapsed:.2f}s")
        
        # 保存索引
        output_path = "sample_docs/sample_standard_tree.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(tree_structure, f, ensure_ascii=False, indent=2)
        
        print(f"索引已保存: {output_path}")
        print(f"✓ PageIndex 索引生成成功")
        
        return True
    except ImportError:
        print("PageIndex 库未正确安装")
        print("请运行: pip install git+https://github.com/VectifyAI/PageIndex.git")
        return False
    except Exception as e:
        print(f"✗ 索引生成失败: {e}")
        return False


def test_verify_tree_structure():
    """验证生成的树索引结构"""
    print("\n=== 验证树索引结构 ===")
    
    tree_path = "sample_docs/sample_standard_tree.json"
    
    if not os.path.exists(tree_path):
        print(f"✗ 索引文件不存在: {tree_path}")
        return False
    
    with open(tree_path, "r", encoding="utf-8") as f:
        tree = json.load(f)
    
    # 检查基本结构
    assert "title" in tree or "nodes" in tree, "树结构缺少必要字段"
    
    print(f"树结构内容预览:")
    print(json.dumps(tree, ensure_ascii=False, indent=2)[:500] + "...")
    
    print(f"✓ 树索引结构验证通过")
    return True


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("PageIndex 索引生成测试")
    print("=" * 50)
    
    results = []
    
    tests = [
        ("服务状态", test_pageindex_service_running),
        ("索引生成", test_generate_pageindex_tree),
        ("结构验证", test_verify_tree_structure),
    ]
    
    for name, test_func in tests:
        start_time = time.time()
        success = test_func()
        elapsed = time.time() - start_time
        results.append((name, success, elapsed))
    
    print("\n" + "=" * 50)
    print("测试结果汇总")
    print("=" * 50)
    
    passed = sum(1 for r in results if r[1])
    total = len(results)
    
    for name, success, elapsed in results:
        status = "✓ 通过" if success else "✗ 失败"
        print(f"{name}: {status} (耗时: {elapsed:.2f}s)")
    
    print(f"\n总计: {passed}/{total} 测试通过")
    
    return passed == total


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)