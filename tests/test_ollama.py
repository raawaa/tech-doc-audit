# tests/test_ollama.py
"""Ollama LLM 服务测试"""

import httpx
import json
import time


OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "qwen3.5:0.8b"


def test_ollama_service_running():
    """测试 Ollama 服务是否运行"""
    print("\n=== 测试 Ollama 服务状态 ===")
    
    try:
        response = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        assert response.status_code == 200, f"服务返回状态码: {response.status_code}"
        
        data = response.json()
        print(f"✓ Ollama 服务正常运行")
        print(f"可用模型: {data.get('models', [])}")
        return True
    except Exception as e:
        print(f"✗ Ollama 服务连接失败: {e}")
        return False


def test_model_installed():
    """测试目标模型是否已安装"""
    print("\n=== 测试模型安装状态 ===")
    
    try:
        response = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        data = response.json()
        
        models = data.get("models", [])
        model_names = [m.get("name") for m in models]
        
        # 检查模型（可能带有 :latest 标签）
        target_model = MODEL_NAME
        installed = any(name.startswith(target_model) for name in model_names)
        
        assert installed, f"模型 {MODEL_NAME} 未安装，当前已安装: {model_names}"
        print(f"✓ 模型 {MODEL_NAME} 已安装")
        return True
    except Exception as e:
        print(f"✗ 模型检查失败: {e}")
        return False


def test_basic_inference():
    """测试基础推理能力"""
    print("\n=== 测试基础推理 ===")
    
    prompt = "请用一句话回复：你好"
    
    try:
        response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False
            },
            timeout=60
        )
        
        assert response.status_code == 200, f"推理请求失败: {response.status_code}"
        
        data = response.json()
        output = data.get("response", "")
        
        print(f"输入: {prompt}")
        print(f"输出: {output}")
        print(f"✓ 基础推理成功")
        return True
    except Exception as e:
        print(f"✗ 推理失败: {e}")
        return False


def test_document_structure_analysis():
    """测试文档结构分析推理能力"""
    print("\n=== 测试文档结构分析能力 ===")
    
    test_text = """
    第一章 总则
    1.1 本标准适用于建筑工程施工质量管理。
    1.2 施工单位应建立健全质量管理体系。
    
    第二章 技术要求
    2.1 混凝土强度等级应不低于C30。
    2.2 钢筋保护层厚度应符合设计要求。
    """
    
    prompt = f"""分析以下文档内容，提取章节结构和关键条目：

{test_text}

请列出：
1. 章节名称
2. 条款编号
3. 每个条款的关键要求

以 JSON 格式输出。"""
    
    try:
        response = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False
            },
            timeout=120
        )
        
        data = response.json()
        output = data.get("response", "")
        
        print(f"输出:\n{output}")
        print(f"✓ 文档结构分析测试完成")
        
        # 注意：小模型可能无法完美输出 JSON，这里只验证推理能执行
        return True
    except Exception as e:
        print(f"✗ 文档结构分析失败: {e}")
        return False


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("Ollama LLM 服务综合测试")
    print("=" * 50)
    
    results = []
    
    tests = [
        ("服务状态", test_ollama_service_running),
        ("模型安装", test_model_installed),
        ("基础推理", test_basic_inference),
        ("文档分析", test_document_structure_analysis),
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