# scripts/verify_all.py
"""阶段一综合验证脚本"""

import subprocess
import sys
import os


def run_test(test_file: str) -> bool:
    """运行单个测试文件"""
    print(f"\n{'='*60}")
    print(f"运行测试: {test_file}")
    print(f"{'='*60}")
    
    result = subprocess.run(
        [sys.executable, test_file],
        cwd=os.getcwd(),
        capture_output=False
    )
    
    return result.returncode == 0


def main():
    """运行所有验证测试"""
    print("=" * 60)
    print("阶段一：基础设施搭建与技术验证 - 综合测试")
    print("=" * 60)
    
    tests = [
        ("Ollama LLM 服务", "tests/test_ollama.py"),
        ("PageIndex 索引生成", "tests/test_pageindex_index.py"),
        ("PageIndex 检索功能", "tests/test_pageindex_search.py"),
        ("文档解析功能", "tests/test_doc_parser.py"),
    ]
    
    results = []
    
    for name, test_file in tests:
        if os.path.exists(test_file):
            success = run_test(test_file)
            results.append((name, success))
        else:
            print(f"\n⚠ 测试文件不存在: {test_file}")
            results.append((name, False))
    
    print("\n" + "=" * 60)
    print("综合验证结果")
    print("=" * 60)
    
    passed = sum(1 for r in results if r[1])
    total = len(results)
    
    for name, success in results:
        status = "✓ 通过" if success else "✗ 失败"
        print(f"{name}: {status}")
    
    print(f"\n总计: {passed}/{total} 验证项通过")
    
    if passed == total:
        print("\n🎉 阶段一验证完成！所有关键技术已验证可行。")
        print("可以进入阶段二：知识库管理模块开发。")
    else:
        print("\n⚠ 部分验证项失败，请检查对应服务的部署状态。")
    
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)