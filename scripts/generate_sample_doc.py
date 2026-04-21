# scripts/generate_sample_doc.py
"""生成示例测试文档（PDF）"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import cm
import os


def generate_sample_standard_pdf(output_path: str):
    """生成示例技术标准 PDF 文档"""
    
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=2*cm,
        leftMargin=2*cm,
        topMargin=2*cm,
        bottomMargin=2*cm
    )
    
    styles = getSampleStyleSheet()
    
    # 自定义样式
    title_style = ParagraphStyle(
        'Title',
        parent=styles['Title'],
        fontSize=18,
        spaceAfter=30
    )
    
    heading_style = ParagraphStyle(
        'Heading',
        parent=styles['Heading1'],
        fontSize=14,
        spaceBefore=20,
        spaceAfter=10
    )
    
    clause_style = ParagraphStyle(
        'Clause',
        parent=styles['Normal'],
        fontSize=11,
        leftIndent=20,
        spaceBefore=5,
        spaceAfter=5
    )
    
    content = []
    
    # 标题
    content.append(Paragraph("建筑设备技术标准 GB/T 9999-2026", title_style))
    content.append(Spacer(1, 30))
    
    # 第一章 总则
    content.append(Paragraph("第一章 总则", heading_style))
    
    content.append(Paragraph("1.1 适用范围", clause_style))
    content.append(Paragraph(
        "本标准适用于工业与民用建筑中电气设备、暖通设备及给排水设备的技术要求和质量验收。",
        styles['Normal']
    ))
    
    content.append(Paragraph("1.2 基本要求", clause_style))
    content.append(Paragraph(
        "设备选型应符合国家现行标准的有关规定，并满足设计文件的技术要求。设备应具有产品质量合格证明文件。",
        styles['Normal']
    ))
    
    # 第二章 电气设备
    content.append(Paragraph("第二章 电气设备技术要求", heading_style))
    
    content.append(Paragraph("2.1 防护等级", clause_style))
    content.append(Paragraph(
        "2.1.1 户外安装的电气设备防护等级应不低于IP65。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "2.1.2 户内安装的电气设备防护等级应不低于IP54。",
        styles['Normal']
    ))
    
    content.append(Paragraph("2.2 接地要求", clause_style))
    content.append(Paragraph(
        "2.2.1 所有电气设备金属外壳必须可靠接地，接地电阻不应大于4Ω。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "2.2.2 接地导线截面积应不小于设备电源线截面积的1/2。",
        styles['Normal']
    ))
    
    content.append(Paragraph("2.3 额定电压", clause_style))
    content.append(Paragraph(
        "2.3.1 设备额定电压应与供电系统电压等级相匹配，偏差不应超过±10%。",
        styles['Normal']
    ))
    
    # 第三章 暖通设备
    content.append(Paragraph("第三章 暖通设备技术要求", heading_style))
    
    content.append(Paragraph("3.1 制冷设备", clause_style))
    content.append(Paragraph(
        "3.1.1 制冷机组制冷量应满足设计负荷要求，能效比(EER)应不低于3.2。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "3.1.2 制冷剂应采用环保型制冷剂，ODP值应为0。",
        styles['Normal']
    ))
    
    content.append(Paragraph("3.2 通风设备", clause_style))
    content.append(Paragraph(
        "3.2.1 风机风量应满足设计要求，噪声级不应超过85dB(A)。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "3.2.2 通风管道应采用不燃材料制作，保温材料应为A级防火材料。",
        styles['Normal']
    ))
    
    # 第四章 给排水设备
    content.append(Paragraph("第四章 给排水设备技术要求", heading_style))
    
    content.append(Paragraph("4.1 水泵设备", clause_style))
    content.append(Paragraph(
        "4.1.1 水泵流量和扬程应满足设计要求，效率应不低于75%。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "4.1.2 水泵应配备自动控制和保护装置，包括过载保护、缺水保护等。",
        styles['Normal']
    ))
    
    content.append(Paragraph("4.2 管道系统", clause_style))
    content.append(Paragraph(
        "4.2.1 给水管道材质应符合卫生标准，管径应满足流量计算要求。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "4.2.2 排水管道坡度应符合设计规范，最小坡度不应小于0.3%。",
        styles['Normal']
    ))
    
    # 第五章 验收要求
    content.append(Paragraph("第五章 验收要求", heading_style))
    
    content.append(Paragraph("5.1 文件资料", clause_style))
    content.append(Paragraph(
        "5.1.1 设备出厂合格证、检测报告、使用说明书等技术资料应齐全。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "5.1.2 安装记录、调试报告、验收报告等施工资料应完整。",
        styles['Normal']
    ))
    
    content.append(Paragraph("5.2 现场检验", clause_style))
    content.append(Paragraph(
        "5.2.1 设备外观检查：表面无损伤、涂层完整、标识清晰。",
        styles['Normal']
    ))
    content.append(Paragraph(
        "5.2.2 功能检验：设备运行正常、控制系统有效、安全装置可靠。",
        styles['Normal']
    ))
    
    # 构建 PDF
    doc.build(content)
    print(f"示例文档已生成: {output_path}")


if __name__ == "__main__":
    output_dir = "sample_docs"
    os.makedirs(output_dir, exist_ok=True)
    
    output_path = os.path.join(output_dir, "sample_standard.pdf")
    generate_sample_standard_pdf(output_path)