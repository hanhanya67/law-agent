# -*- coding: utf-8 -*-
"""临时脚本：把《工伤保险条例》（2010修订版，现行有效）关键条文追加进语料。

只加 r07/n06（工伤十级一次性伤残补助金）与 e09（工伤认定时限）真正需要的条文，
不整部法灌进去，避免检索噪声。run 完即删。
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "statutes.jsonl"
URL = ("https://www.mohrss.gov.cn/xxgk2020/fdzdgknr/zcfg/fg/"
       "202011/t20201103_394950.html")

ARTS = [
    {
        "id": "工伤保险条例_17", "law_name": "工伤保险条例",
        "law_short": "工伤保险条例", "article_no": "17", "article_no_cn": "第十七条",
        "chapter_path": "第三章 工伤认定",
        "content": ("职工发生事故伤害或者按照职业病防治法规定被诊断、鉴定为职业病，"
                    "所在单位应当自事故伤害发生之日或者被诊断、鉴定为职业病之日起30日内，"
                    "向统筹地区社会保险行政部门提出工伤认定申请。遇有特殊情况，"
                    "经报社会保险行政部门同意，申请时限可以适当延长。"
                    "用人单位未按前款规定提出工伤认定申请的，工伤职工或者其近亲属、工会组织"
                    "在事故伤害发生之日或者被诊断、鉴定为职业病之日起1年内，"
                    "可以直接向用人单位所在地统筹地区社会保险行政部门提出工伤认定申请。"
                    "按照本条第一款规定应当由省级社会保险行政部门进行工伤认定的事项，"
                    "根据属地原则由用人单位所在地的设区的市级社会保险行政部门办理。"
                    "用人单位未在本条第一款规定的时限内提交工伤认定申请，"
                    "在此期间发生符合本条例规定的工伤待遇等有关费用由该用人单位负担。"),
        "status": "现行有效", "effective_date": "2011-01-01",
        "superseded_by": "", "repealed_note": "", "hierarchy_level": 2,
        "source_url": URL,
    },
    {
        "id": "工伤保险条例_20", "law_name": "工伤保险条例",
        "law_short": "工伤保险条例", "article_no": "20", "article_no_cn": "第二十条",
        "chapter_path": "第三章 工伤认定",
        "content": ("社会保险行政部门应当自受理工伤认定申请之日起60日内作出工伤认定的决定，"
                    "并书面通知申请工伤认定的职工或者其近亲属和该职工所在单位。"
                    "社会保险行政部门对受理的事实清楚、权利义务明确的工伤认定申请，"
                    "应当在15日内作出工伤认定的决定。"
                    "作出工伤认定决定需要以司法机关或者有关行政主管部门的结论为依据的，"
                    "在司法机关或者有关行政主管部门尚未作出结论期间，"
                    "作出工伤认定决定的时限中止。"
                    "社会保险行政部门工作人员与工伤认定申请人有利害关系的，应当回避。"),
        "status": "现行有效", "effective_date": "2011-01-01",
        "superseded_by": "", "repealed_note": "", "hierarchy_level": 2,
        "source_url": URL,
    },
    {
        "id": "工伤保险条例_33", "law_name": "工伤保险条例",
        "law_short": "工伤保险条例", "article_no": "33", "article_no_cn": "第三十三条",
        "chapter_path": "第五章 工伤保险待遇",
        "content": ("职工因工作遭受事故伤害或者患职业病需要暂停工作接受工伤医疗的，"
                    "在停工留薪期内，原工资福利待遇不变，由所在单位按月支付。"
                    "停工留薪期一般不超过12个月。伤情严重或者情况特殊，"
                    "经设区的市级劳动能力鉴定委员会确认，可以适当延长，但延长不得超过12个月。"
                    "工伤职工评定伤残等级后，停发原待遇，按照本章的有关规定享受伤残待遇。"
                    "工伤职工在停工留薪期满后仍需治疗的，继续享受工伤医疗待遇。"
                    "生活不能自理的工伤职工在停工留薪期需要护理的，由所在单位负责。"),
        "status": "现行有效", "effective_date": "2011-01-01",
        "superseded_by": "", "repealed_note": "", "hierarchy_level": 2,
        "source_url": URL,
    },
    {
        "id": "工伤保险条例_37", "law_name": "工伤保险条例",
        "law_short": "工伤保险条例", "article_no": "37", "article_no_cn": "第三十七条",
        "chapter_path": "第五章 工伤保险待遇",
        "content": ("职工因工致残被鉴定为七级至十级伤残的，享受以下待遇："
                    "（一）从工伤保险基金按伤残等级支付一次性伤残补助金，标准为："
                    "七级伤残为13个月的本人工资，八级伤残为11个月的本人工资，"
                    "九级伤残为9个月的本人工资，十级伤残为7个月的本人工资；"
                    "（二）劳动、聘用合同期满终止，或者职工本人提出解除劳动、聘用合同的，"
                    "由工伤保险基金支付一次性工伤医疗补助金，由用人单位支付一次性伤残就业补助金。"
                    "一次性工伤医疗补助金和一次性伤残就业补助金的具体标准"
                    "由省、自治区、直辖市人民政府规定。"),
        "status": "现行有效", "effective_date": "2011-01-01",
        "superseded_by": "", "repealed_note": "", "hierarchy_level": 2,
        "source_url": URL,
    },
    {
        "id": "工伤保险条例_62", "law_name": "工伤保险条例",
        "law_short": "工伤保险条例", "article_no": "62", "article_no_cn": "第六十二条",
        "chapter_path": "第七章 法律责任",
        "content": ("用人单位依照本条例规定应当参加工伤保险而未参加的，"
                    "由社会保险行政部门责令限期参加，补缴应当缴纳的工伤保险费，"
                    "并自欠缴之日起，按日加收万分之五的滞纳金；逾期仍不缴纳的，"
                    "处欠缴数额1倍以上3倍以下的罚款。依照本条例规定应当参加工伤保险"
                    "而未参加工伤保险的用人单位职工发生工伤的，"
                    "由该用人单位按照本条例规定的工伤保险待遇项目和标准支付费用。"
                    "用人单位参加工伤保险并补缴应当缴纳的工伤保险费、滞纳金后，"
                    "由工伤保险基金和用人单位依照本条例的规定支付新发生的费用。"),
        "status": "现行有效", "effective_date": "2011-01-01",
        "superseded_by": "", "repealed_note": "", "hierarchy_level": 2,
        "source_url": URL,
    },
    {
        "id": "工伤保险条例_64", "law_name": "工伤保险条例",
        "law_short": "工伤保险条例", "article_no": "64", "article_no_cn": "第六十四条",
        "chapter_path": "第八章 附则",
        "content": ("本条例所称工资总额，是指用人单位直接支付给本单位全部职工的劳动报酬总额。"
                    "本条例所称本人工资，是指工伤职工因工作遭受事故伤害或者患职业病"
                    "前12个月平均月缴费工资。本人工资高于统筹地区职工平均工资300%的，"
                    "按照统筹地区职工平均工资的300%计算；本人工资低于统筹地区职工平均工资60%的，"
                    "按照统筹地区职工平均工资的60%计算。"),
        "status": "现行有效", "effective_date": "2011-01-01",
        "superseded_by": "", "repealed_note": "", "hierarchy_level": 2,
        "source_url": URL,
    },
]


def main() -> None:
    existing = set()
    lines = [ln for ln in SRC.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for ln in lines:
        try:
            existing.add(json.loads(ln)["id"])
        except Exception:
            pass
    dup = [a["id"] for a in ARTS if a["id"] in existing]
    if dup:
        print("已存在，跳过：", dup)
        arts = [a for a in ARTS if a["id"] not in existing]
    else:
        arts = ARTS
    with SRC.open("a", encoding="utf-8") as f:
        for a in arts:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    print(f"追加 {len(arts)} 条；语料现共 {len(lines) + len(arts)} 条")


if __name__ == "__main__":
    main()
