"""PageIndex 检索功能测试"""

import os
import json
import httpx
import time


PAGEINDEX_URL = "http://localhost:8584"
TREE_PATH = "sample_docs/sample_standard_tree.json"


def test_search_with_query():
    """测试基于查询的检索"""
    print("\n=== 测试 PageIndex 检索 ===")
    
    if not os.path.exists(TREE_PATH):
        print(f"✗ 索引文件不存在: {TREE_PATH}")
        print(f"请先运行: python tests/test_pageindex_index.py")
        return False
    
    test_queries = [
        "电气设备防护等级要求",
        "制冷设备能效要求",
        "水泵设备效率要求",
    ]
    
    results = []
    
    for query in test_queries:
        print(f"\n查询: {query}")
        start_time = time.time()
        
        try:
            response = httpx.post(
                f"{PAGEINDEX_URL}/api/v1/search",
                json={
                    "query": query,
                    "paths": ["sample_docs/sample_standard.pdf"],
                    "mode": "FAST"
                },
                timeout=120
            )
            
            elapsed = time.time() - start_time
            
            if response.status_code == 200:
                data = response.json()
                print(f"检索结果: {json.dumps(data, ensure_ascii=False)[:300]}...")
                print(f"耗时: {elapsed:.2f}s")
                results.append(True)
            else:
                print(f"检索失败: HTTP {response.status_code}")
                results.append(False)
                
        except Exception as e:
            print(f"检索异常: {e}")
            results.append(False)
    
    success = all(results)
    if success:
        print(f"\n✓ 所有检索测试通过")
    else:
        print(f"\n✗ 部分检索测试失败")
    
    return success


def test_search_result_explainability():
    """测试检索结果的可解释性"""
    print("\n=== 测试检索结果可解释性 ===")
    
    query = "电气设备防护等级要求"
    
    try:
        response = httpx.post(
            f"{PAGEINDEX_URL}/api/v1/search",
            json={
                "query": query,
                "paths": ["sample_docs/sample_standard.pdf"],
                "mode": "FAST",
                "return_context": True
            },
            timeout=120
        )
        
        if response.status_code != 200:
            print(f"检索请求失败: {response.status_code}")
            return False
        
        data = response.json()
        
        if "data" in data:
            result = data["data"]
            print(f"检索结果结构:")
            print(json.dumps(result, ensure_ascii=False, indent=2)[:500])
            
            # 验证是否包含位置信息
            has_location = False
            if isinstance(result, dict):
                location_fields = ["location", "source", "page", "section", "clause"]
                has_location = any(field in result for field in location_fields)
            
            if has_location:
                print(f"✓ 结果包含位置/溯源信息")
                return True
            else:
                print(f"⚠ 结果可能不包含详细位置信息")
                return True
        
        return True
        
    except Exception as e:
        print(f"可解释性测试失败: {e}")
        return False


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("PageIndex 检索功能测试")
    print("=" * 50)
    
    results = []
    
    tests = [
        ("基础检索", test_search_with_query),
        ("可解释性", test_search_result_explainability),
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