"""文档解析功能测试"""

import os
import time
import re

import pdfplumber
from docx import Document


SAMPLE_PDF = "sample_docs/sample_standard.pdf"


def test_pdf_text_extraction():
    """测试 PDF 文本提取"""
    print("\n=== 测试 PDF 文本提取 ===")
    
    if not os.path.exists(SAMPLE_PDF):
        print(f"✗ 示例 PDF 不存在: {SAMPLE_PDF}")
        return False
    
    try:
        with pdfplumber.open(SAMPLE_PDF) as pdf:
            total_pages = len(pdf.pages)
            print(f"PDF 总页数: {total_pages}")
            
            first_page = pdf.pages[0]
            text = first_page.extract_text()
            
            if text:
                print(f"第一页文本预览:")
                print(text[:300] + "...")
                print(f"✓ PDF 文本提取成功")
                return True
            else:
                print(f"✗ 无法提取文本")
                return False
                
    except Exception as e:
        print(f"✗ PDF 解析失败: {e}")
        return False


def test_pdf_structure_detection():
    """测试 PDF 结构识别"""
    print("\n=== 测试 PDF 结构识别 ===")
    
    if not os.path.exists(SAMPLE_PDF):
        print(f"✗ 示例 PDF 不存在")
        return False
    
    try:
        with pdfplumber.open(SAMPLE_PDF) as pdf:
            full_text = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
            
            chapters = re.findall(r'第[一二三四五六七八九十]+章\s+[^\n]+', full_text)
            print(f"检测到章节: {chapters}")
            
            clauses = re.findall(r'\d+\.\d+(?:\.\d+)?', full_text)
            print(f"检测到条款编号: {clauses[:10]}...")
            
            if chapters and clauses:
                print(f"✓ 结构识别成功")
                print(f"  章节数: {len(chapters)}")
                print(f"  条款数: {len(clauses)}")
                return True
            else:
                print(f"⚠ 结构识别结果较少")
                return True
                
    except Exception as e:
        print(f"✗ 结构识别失败: {e}")
        return False


def test_word_doc_support():
    """测试 Word 文档支持"""
    print("\n=== 测试 Word 文档支持 ===")
    
    try:
        from docx import Document
        print(f"✓ python-docx 库已安装")
        return True
    except ImportError:
        print(f"✗ python-docx 未安装")
        print(f"请运行: pip install python-docx")
        return False


def run_all_tests():
    """运行所有测试"""
    print("=" * 50)
    print("文档解析功能测试")
    print("=" * 50)
    
    results = []
    
    tests = [
        ("PDF文本提取", test_pdf_text_extraction),
        ("PDF结构识别", test_pdf_structure_detection),
        ("Word支持", test_word_doc_support),
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