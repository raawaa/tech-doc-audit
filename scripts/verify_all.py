# scripts/verify_all.py
"""运行当前 pytest 测试套件。"""

import subprocess
import sys


def main():
    print("=" * 60)
    print("技术文档审核系统 - 测试套件")
    print("=" * 60)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
        cwd=__file__.rsplit("/", 1)[0] if "/" in __file__ else ".",
    )

    passed = result.returncode == 0

    print("\n" + "=" * 60)
    if passed:
        print("🎉 所有测试通过！")
    else:
        print("✗ 部分测试失败，详情见上。")

    return passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)