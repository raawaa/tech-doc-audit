"""标准文件自动专业分类。

基于文件名中的关键词，将标准文件自动归类到 10 个专业领域。
用于：
1. 导入 KB 时自动标记文档的领域
2. Step 2 合理性审查时选择对应的审查角度
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

from core.logger import get_logger

_logger = get_logger(__name__)

# ── 专业领域定义 ───────────────────────────────────────────────────────────────

DOMAINS = {
    "01_建筑与结构": {
        "name": "建筑与结构",
        "code": "01",
        "keywords": [
            "建筑", "结构", "抗震", "地基", "基础", "混凝土", "砌体",
            "钢结构", "木结构", "组合结构", "加固", "隔震", "减震",
            "住宅", "办公", "民用", "公共建筑", "屋面", "幕墙",
            "装修", "装饰", "墙体", "楼", "门窗", "楼梯",
            "危险房屋", "鉴定",
            "医院", "中医", "学校", "体育", "场馆", "厂房", "仓库",
            "粮", "粮食", "平房仓", "冷库",
            "户外广告", "招牌", "标识",
            "公园", "动物园",
            "居住", "镇", "乡", "村", "用地", "规划",
        ],
        "std_prefixes": ["GB500", "GB5011", "GB502", "GB5035", "GB/T50"],
    },
    "02_市政给排水与燃气": {
        "name": "市政给排水与燃气",
        "code": "02",
        "keywords": [
            "给水", "排水", "污水", "水处理", "水质", "净水", "供水",
            "雨水", "管道", "管材", "泵站", "水池", "水箱",
            "节水", "灌溉",
            "燃气", "天然气", "液化气", "煤气", "输气", "燃具",
            "供热", "供暖", "热力", "采暖", "锅炉", "换热", "热泵", "地热",
            "游泳池", "给水排水",
            "喷泉", "水景",
            "水域", "保洁", "水域保洁",
        ],
        "std_prefixes": ["CJJ", "CJ/T", "GB/T513", "GB5001"],
    },
    "03_道路桥梁与交通": {
        "name": "道路桥梁与交通",
        "code": "03",
        "keywords": [
            "道路", "桥梁", "路面", "路基", "立交", "桥面", "涵洞",
            "隧道", "交通工程", "轨道交通", "地铁", "轻轨",
            "单轨", "铁路", "公路", "城市交通",
            "磁浮", "有轨电车",
            "浮置板", "轨道", "车辙", "沥青混合料",
            "梁桥", "箱梁", "组合梁",
        ],
        "std_prefixes": ["CJJ11", "CJJ", "GB/T512"],
    },
    "04_施工与验收": {
        "name": "施工与验收",
        "code": "04",
        "keywords": [
            "施工", "验收", "监理", "检测", "检验", "测试", "监测",
            "质量验收", "施工质量",
            "填筑", "回填", "开挖", "支护",
            "土壤固化", "固化剂",
            "降排水",
        ],
        "std_prefixes": ["GB5030", "GB/T503"],
    },
    "05_机电与电气": {
        "name": "机电与电气",
        "code": "05",
        "keywords": [
            "电气", "自控", "自动化", "照明", "配电", "供配电",
            "防雷", "接地", "火灾报警", "智能",
            "电梯", "起重", "升降", "施工机具", "机械",
            "设备", "压力容器", "阀门", "泵",
            "电力", "电线", "电缆", "变电站",
            "架空", "输电",
        ],
        "std_prefixes": ["GB5005", "GB/T50"],
    },
    "06_矿山冶金与化工": {
        "name": "矿山冶金与化工",
        "code": "06",
        "keywords": [
            "矿山", "冶金", "采矿", "选矿", "尾矿", "有色金属",
            "钢铁", "冶炼", "矿业", "矿井", "选煤",
            "化工", "化学", "石油", "石化",
            "制药", "医药", "洁净", "实验", "实验室",
            "水泥", "窑", "硅", "芯片", "集成电路",
            "沼气", "生物",
            "火工品", "炸药",
        ],
        "std_prefixes": ["GB508", "GB/T51"],
    },
    "07_节能环保与环卫": {
        "name": "节能环保与环卫",
        "code": "07",
        "keywords": [
            "节能", "环保", "绿色", "减排", "低碳",
            "环境", "生态", "污染", "能耗",
            "环卫", "垃圾", "公厕", "清扫", "粪便",
            "园林", "绿化", "市容", "景观",
            "海绵城市", "排水",
            "绿地", "居住绿地", "园林绿化",
        ],
        "std_prefixes": ["GB/T513", "CJJ"],
    },
    "08_消防与安全": {
        "name": "消防与安全",
        "code": "08",
        "keywords": [
            "消防", "防火", "灭火", "报警", "疏散", "耐火",
            "安全", "职业", "劳动", "防护",
            "卫生", "健康", "噪声", "粉尘",
            "人防", "防灾", "防洪", "防涝", "防风", "防雪",
        ],
        "std_prefixes": ["GB5001", "GB/T50"],
    },
    "09_信息技术与智能": {
        "name": "信息技术与智能",
        "code": "09",
        "keywords": [
            "信息", "软件", "网络", "通信", "监控",
            "数据", "系统", "智慧", "智能",
            "编码", "标识", "卡", "射频",
            "电子", "档案", "电子文件", "电子档案",
            "业务协同", "平台",
            "住房公积金",
        ],
        "std_prefixes": ["GB/T363", "GB/T51"],
    },
    "10_勘察测量与岩土": {
        "name": "勘察测量与岩土",
        "code": "10",
        "keywords": [
            "勘察", "测量", "测绘", "勘探", "岩土",
            "地质", "水文", "土工", "土的",
            "取土器",
        ],
        "std_prefixes": ["GB/T501", "GB5002"],
    },
}


def classify_document(filename: str, doc_content: str = "") -> str:
    """根据文件名（和可选的文档内容）对标准文件进行专业分类。

    Returns:
        领域编码，如 "01_建筑与结构"。无法识别时返回 "99_其他"。
    """
    name_lower = filename.lower()

    # 第一轮：按标准编号前缀匹配
    std_match = re.match(r'\[([A-Z]+[ /T\d]*\d+)', filename)
    if std_match:
        std_num = std_match.group(1).upper()
        for code, domain in DOMAINS.items():
            for prefix in domain["std_prefixes"]:
                p = prefix.upper()
                if std_num.startswith(p) or p.startswith(std_num[:len(p)]):
                    return code

    # 第二轮：按关键词匹配
    matches = []
    for code, domain in DOMAINS.items():
        score = 0
        for kw in domain["keywords"]:
            if kw in name_lower:
                score += 1
        if score > 0:
            matches.append((code, score))

    if matches:
        matches.sort(key=lambda x: -x[1])
        return matches[0][0]

    # 第三轮：如果有文档内容，从内容中提取关键词
    if doc_content:
        content_lower = doc_content.lower()
        matches = []
        for code, domain in DOMAINS.items():
            score = 0
            for kw in domain["keywords"]:
                if kw in content_lower[:5000]:
                    score += 1
            if score > 0:
                matches.append((code, score))
        if matches:
            matches.sort(key=lambda x: -x[1])
            return matches[0][0]

    return "99_其他"


def classify_directory(source_dir: str) -> dict[str, str]:
    """对整个目录的文件进行分类。

    Returns:
        {filename: domain_code}
    """
    from collections import Counter
    path = Path(source_dir)
    files = [f.name for f in path.iterdir() if f.suffix.lower() in (".md", ".pdf", ".doc", ".docx")]
    results = {}
    unclassified = []

    for fname in sorted(files):
        code = classify_document(fname)
        results[fname] = code
        if code == "99_其他":
            unclassified.append(fname)

    dist = Counter(results.values())
    print(f"\n=== 分类结果（共 {len(files)} 个文件）===")
    for code, count in dist.most_common():
        name = DOMAINS.get(code, {}).get("name", "未分类")
        print(f"  {code}: {count}")
    if unclassified:
        print(f"\n未分类文件（{len(unclassified)} 个）：")
        for f in unclassified[:20]:
            print(f"  {f}")
        if len(unclassified) > 20:
            print(f"  ... 还有 {len(unclassified) - 20} 个")

    return results


def save_classification(results: dict[str, str], output_path: str):
    """保存分类结果到 JSON 文件。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"分类结果已保存到: {path}")


def load_classification(path: str) -> dict[str, str]:
    """加载已保存的分类结果。"""
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
