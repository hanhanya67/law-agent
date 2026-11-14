"""生成 data/cases.jsonl —— 判例库的**可插拔导入器**。

为什么有这个脚本，而不是手写一份 jsonl 就完事：

1. 语料来自人社部 + 最高法联合发布的劳动人事争议典型案例（第二、三、四批，
   共 21 件）。每一条的「处理结果」都是官方原文，不是我们概括的，更不是
   模型编的。把源数据和产物分开，别人能逐条回去核对。
2. 条号**自动回 statutes.jsonl 核验**。案例原文里引了《社会保险法》
   《工伤保险条例》等本项目语料库没有的法，这些绝不许写进结构化 articles
   字段——否则界面上摆一条「依据」却点不开原文，比不摆更糟。核验不通过
   直接报错，不静默丢弃。
3. 加案例 = 在 CASES 里加一条再跑一次本脚本。判例库是「一核多库」的
   第 4 个插槽，不是焊死在代码里的常量表。

用法：python scripts/build_cases.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "statutes.jsonl"
OUT = ROOT / "data" / "cases.jsonl"

BATCHES: dict[str, dict[str, str]] = {
    "B2": {
        "batch": "第二批",
        "doc_no": "人社部函〔2021〕90号",
        "date": "2021-06-30",
        "url": "https://www.court.gov.cn/zixun/xiangqing/319151.html",
        "theme": "工时与加班费",
    },
    "B3": {
        "batch": "第三批",
        "doc_no": "人社部函〔2023〕36号",
        "date": "2023-04-24",
        "url": "https://www.court.gov.cn/zixun/xiangqing/401172.html",
        "theme": "新就业形态劳动关系认定",
    },
    "B4": {
        "batch": "第四批",
        "doc_no": "人社部函〔2025〕28号",
        "date": "2025-04-09",
        "url": "https://www.mohrss.gov.cn/xxgk2020/fdzdgknr/"
               "ldgx_4234/ldrsdjzc/202504/t20250416_540532.html",
        "theme": "社会保险与竞业限制",
    },
}

AUTHORITY = "人力资源社会保障部、最高人民法院"
LAW_ZC = "中华人民共和国劳动争议调解仲裁法"
LAW_LC = "中华人民共和国劳动合同法"
LAW_L = "中华人民共和国劳动法"
LAW_J = "最高人民法院关于审理劳动争议案件适用法律问题的解释（一）"

# outcome: supported 支持 / partial 部分支持 / dismissed 驳回 / settled 调解结案
# applicant: employee 劳动者为申请人 / employer 用人单位为申请人
#   ——这一项不能省。第四批案例 5 是「保安公司申请仲裁被驳回」，
#   混进职工视角的结局分布，会把「单位败诉」读成「职工能赢」，结论正好反过来。
# articles 只登记 statutes.jsonl 里真实存在的条号，格式「法名#中文条号」。

CASES: tuple[dict, ...] = (
    # ---------------------------------------------------------------- 第二批
    dict(id="B2-01", title="劳动者拒绝违法超时加班安排，用人单位能否解除劳动合同",
         topics=("overtime", "dismissal", "compensation"), outcome="supported",
         applicant="employee", claim=8000, awarded=8000,
         quote="仲裁委员会裁决某快递公司支付张某违法解除劳动合同赔偿金8000元（裁决为终局裁决）。",
         point="规章制度规定的工时严重违反法定延长工作时间上限的，该规定无效；劳动者拒绝违法超时加班不构成「不符合录用条件」。",
         articles=(f"{LAW_L}#第四十一条", f"{LAW_L}#第四十三条", f"{LAW_LC}#第二十六条")),
    dict(id="B2-02", title="劳动者与用人单位订立放弃加班费协议，能否主张加班费",
         topics=("overtime", "wage"), outcome="supported",
         applicant="employee", claim=24000, awarded=24000,
         quote="仲裁委员会裁决某科技公司支付张某2020年6月至12月加班费24000元。",
         point="放弃加班费的格式条款免除用人单位法定责任、排除劳动者权利，显失公平，认定无效。",
         articles=(f"{LAW_LC}#第二十六条", f"{LAW_J}#第三十五条", f"{LAW_L}#第四十四条")),
    dict(id="B2-03", title="用人单位未按规章制度履行加班审批手续，能否认定劳动者加班事实",
         topics=("overtime", "wage", "evidence"), outcome="supported",
         applicant="employee", claim=50000, awarded=50000,
         quote="仲裁委员会裁决某医药公司支付吴某2019年12月至2020年11月加班费50000元。一审法院判决与仲裁裁决一致，一审判决已生效。",
         point="单位未实际履行加班审批手续，不影响对「用人单位安排」加班事实的认定；考勤、聊天记录、会议纪要可形成证据链。",
         articles=(f"{LAW_L}#第四十四条",)),
    dict(id="B2-04", title="用人单位与劳动者约定实行包薪制，是否需要依法支付加班费",
         topics=("overtime", "wage"), outcome="supported",
         applicant="employee", claim=17000, awarded=17000,
         quote="仲裁委员会裁决某汽车服务公司支付周某加班费差额17000元（裁决为终局裁决）。",
         point="包薪制不得违反最低工资保障与加班费支付标准，约定工资低于法定标准折算结果的应补差额。",
         articles=(f"{LAW_L}#第四十七条", f"{LAW_L}#第四十八条")),
    dict(id="B2-05", title="用人单位未与劳动者协商一致增加工作任务，劳动者是否有权拒绝",
         topics=("overtime", "dismissal", "compensation"), outcome="supported",
         applicant="employee", claim=14000, awarded=14000,
         quote="仲裁委员会裁决某报刊公司支付张某违法解除劳动合同赔偿金14000元（裁决为终局裁决）。",
         point="大幅增加工作任务构成变更劳动合同约定内容，须协商一致；劳动者拒绝变相强迫加班，单位据此解除属违法解除。",
         articles=(f"{LAW_LC}#第三十一条", f"{LAW_LC}#第三十五条")),
    dict(id="B2-06", title="处理加班费争议，如何分配举证责任",
         topics=("overtime", "wage", "evidence"), outcome="supported",
         applicant="employee", claim=10000, awarded=10000,
         quote="仲裁委员会裁决某教育咨询公司支付林某加班费10000元（裁决为终局裁决）。",
         point="劳动者能证明加班证据由单位掌握而单位拒不提供的，可推定加班事实存在，单位承担不利后果。",
         articles=(f"{LAW_ZC}#第六条", f"{LAW_J}#第四十二条")),
    dict(id="B2-07", title="劳动者超时加班发生工伤，用工单位、劳务派遣单位是否承担连带赔偿责任",
         topics=("overtime", "injury", "compensation"), outcome="partial",
         applicant="employee", claim=1193821, awarded=766911.55,
         quote="一审法院判决：某服务公司和某传媒公司连带赔偿惠某等医疗费、一次性工亡补助金、丧葬补助金、供养亲属抚恤金合计766911.55元。二审驳回上诉，维持原判。",
         point="请求 1193821 元、判 766911.55 元：长期超时加班致工亡，派遣单位与用工单位连带赔偿，已付款项与显失公平的赔偿协议差额部分补足。",
         articles=(f"{LAW_L}#第三十八条", f"{LAW_L}#第四十一条", f"{LAW_LC}#第九十二条")),
    dict(id="B2-08", title="用人单位以规章制度形式否认劳动者加班事实是否有效",
         topics=("overtime", "wage", "evidence"), outcome="supported",
         applicant="employee", claim=32000, awarded=32000,
         quote="一审法院判决：某网络公司支付常某加班费差额32000元。双方不服均提起上诉，二审驳回上诉，维持原判。",
         point="「21:00 之后才起算加班」等不合理规章制度条款不具合理性，法院可否定其效力，按考勤与系统记录认定加班事实。",
         articles=(f"{LAW_LC}#第四条",)),
    dict(id="B2-09", title="劳动者在离职文件上签字确认加班费已结清，是否有权请求支付欠付的加班费",
         topics=("overtime", "wage", "settlement"), outcome="partial",
         applicant="employee", claim=82261, awarded=24404.89,
         quote="一审法院判决：驳回肖某加班费的诉讼请求。肖某不服提起上诉，二审法院改判：某科技公司与某快递公司连带支付肖某加班费24404.89元。",
         point="一审驳回、二审改判；请求 82261 元、终判 24404.89 元。离职文件「已结清」与事实不符且非真实意思表示的，不能据此否定欠付加班费。",
         articles=(f"{LAW_J}#第三十五条",)),
    dict(id="B2-10", title="加班费的仲裁时效应当如何认定",
         topics=("overtime", "wage", "deadline"), outcome="partial",
         applicant="employee", claim=46293, awarded=18120,
         quote="一审法院判决：某建筑公司支付张某加班费18120元。张某与某建筑公司均未提起上诉，一审判决已生效。",
         point="请求 46293 元、判 18120 元。加班费属劳动报酬，劳动关系存续期间不受一年普通时效限制，但劳动关系终止后应在一年内提出。",
         articles=(f"{LAW_ZC}#第二十七条", f"{LAW_L}#第四十四条")),
    # ---------------------------------------------------------------- 第三批
    dict(id="B3-01", title="如何认定网约货车司机与平台企业之间是否存在劳动关系",
         topics=("relation", "platform", "compensation"), outcome="supported",
         applicant="employee", claim=None, awarded=None,
         quote="仲裁委员会裁决：某信息技术公司向刘某支付解除劳动合同经济补偿。",
         point="平台通过派单、考勤、奖惩实施较强劳动管理，人格/经济/组织从属性齐备的，即便书面约定「合作关系」仍认定劳动关系。",
         articles=(f"{LAW_LC}#第七条",)),
    dict(id="B3-02", title="如何认定网约配送员与平台企业之间是否存在劳动关系",
         topics=("relation", "platform"), outcome="dismissed",
         applicant="employee", claim=None, awarded=None,
         quote="仲裁委员会裁决：驳回徐某的仲裁请求。",
         point="众包骑手可完全自主决定上线时间与接单量，人格从属性与组织从属性较弱的，不足以认定劳动关系。",
         articles=(f"{LAW_LC}#第七条",)),
    dict(id="B3-03", title="外卖平台用工合作企业通过劳务公司招用网约配送员，如何认定劳动关系",
         topics=("relation", "platform", "outsourcing"), outcome="supported",
         applicant="employee", claim=None, awarded=None,
         quote="仲裁委员会裁决：何某与某货运代理公司于2019年7月28日至2019年12月19日期间存在劳动关系。",
         point="外包协议未实际履行、实际由站点承包企业实施考勤与管理的，按劳动管理事实认定劳动关系主体，防止「隐蔽劳动关系」。",
         articles=(f"{LAW_LC}#第七条",)),
    dict(id="B3-04", title="劳动者注册个体工商户与平台企业订立合作协议，能否认定劳动关系",
         topics=("relation", "platform", "dismissal", "compensation"), outcome="supported",
         applicant="employee", claim=None, awarded=None,
         quote="仲裁委员会裁决：孙某与某物流公司于2020年9月6日至2020年12月10日期间存在劳动关系，某物流公司向孙某支付违法解除劳动合同赔偿金。",
         point="被安排注册个体工商户但用工管理方式未变的，仍认定劳动关系；以合作之名规避用工责任的，解除仍需支付赔偿金。",
         articles=(f"{LAW_LC}#第七条", f"{LAW_LC}#第八十七条")),
    dict(id="B3-05", title="如何认定网络主播与文化传播公司之间是否存在劳动关系",
         topics=("relation", "platform"), outcome="dismissed",
         applicant="employee", claim=None, awarded=None,
         quote="一审法院判决：李某与某文化传播公司之间不存在劳动关系。二审法院判决：驳回上诉，维持原判。",
         point="经纪合同以培训包装、收益分成为主、直播时间内容自主决定的，体现平等协商而非从属性，属民事关系。",
         articles=(f"{LAW_LC}#第七条",)),
    dict(id="B3-06", title="如何认定网约家政服务人员与家政公司之间是否存在劳动关系",
         topics=("relation", "platform"), outcome="supported",
         applicant="employee", claim=None, awarded=None,
         quote="一审法院判决：宋某与某家政公司于2019年11月1日至2020年1月21日期间存在劳动关系。二审驳回上诉，维持原判。",
         point="员工制家政企业统一培训、派单、按月发薪并限制多平台就业的，从属性齐备，应认定劳动关系。",
         articles=(f"{LAW_LC}#第七条",)),
    # ---------------------------------------------------------------- 第四批
    dict(id="B4-01", title="工伤职工能否根据诊断证明书主张延长停工留薪期",
         topics=("injury", "wage", "social_insurance"), outcome="dismissed",
         applicant="employee", claim=None, awarded=None,
         quote="仲裁委员会裁决：驳回彭某的仲裁请求。",
         point="停工留薪期延长须经设区的市级劳动能力鉴定委员会确认；仅凭医院诊断证明书、且不配合现场鉴定的，不予支持。",
         articles=(f"{LAW_L}#第七十二条",)),
    dict(id="B4-02", title="用人单位能否因女职工怀孕调岗降薪",
         topics=("wage", "maternity", "dismissal"), outcome="supported",
         applicant="employee", claim=None, awarded=None,
         quote="仲裁委员会裁决：某科技公司按照17000元/月的标准补齐赵某孕期工资差额。",
         point="未经协商、无医疗机构证明不能适应原劳动，单方令孕期职工退出项目组并按基本工资发薪属变相降薪，应补齐差额。",
         articles=(f"{LAW_L}#第五十条",)),
    dict(id="B4-03", title="病亡职工的遗属能否以未依法缴纳社会保险费为由要求用人单位补足抚恤金",
         topics=("social_insurance", "compensation"), outcome="supported",
         applicant="employee", claim=36633.36, awarded=36633.36,
         quote="人民法院判决：某出租车公司向张某欣、张某颖、张某萍支付非因工死亡的抚恤金差额36633.36元。",
         point="单位断缴社保致遗属少领抚恤金的，应赔偿差额损失。",
         articles=(f"{LAW_L}#第七十二条",)),
    dict(id="B4-04", title="劳动者自行承担用人单位应缴未缴的社会保险费的，用人单位应否承担赔偿责任",
         topics=("social_insurance", "compensation"), outcome="supported",
         applicant="employee", claim=None, awarded=None,
         quote="人民法院判决：某公司支付李某社会保险费损失。",
         point="劳动者代单位承担了本应由单位缴纳的社保费，该部分属劳动者损失，可要求单位赔偿。",
         articles=(f"{LAW_L}#第七十二条",)),
    dict(id="B4-05", title="主体不适格，竞业限制条款是否有效",
         topics=("noncompete",), outcome="dismissed",
         applicant="employer", claim=None, awarded=None,
         quote="仲裁委员会裁决：驳回某保安公司的仲裁请求。",
         point="普通保安岗位难以知悉商业秘密，不属竞业限制适格主体，条款对其不具约束力；单位主张违约金被驳回。",
         articles=(f"{LAW_LC}#第二十三条", f"{LAW_LC}#第二十四条")),
)

OUTCOMES = {"supported", "partial", "dismissed", "settled"}
APPLICANTS = {"employee", "employer"}


def corpus_articles() -> set[tuple[str, str]]:
    have: set[tuple[str, str]] = set()
    for line in CORPUS.open(encoding="utf-8"):
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if d.get("law_name") and d.get("article_no_cn"):
            have.add((d["law_name"], d["article_no_cn"]))
    return have


def main() -> int:
    have = corpus_articles()
    ids: set[str] = set()
    rows: list[dict] = []
    for c in CASES:
        cid = c["id"]
        assert cid not in ids, f"重复 case_id: {cid}"
        ids.add(cid)
        assert c["outcome"] in OUTCOMES, f"{cid} outcome 非法"
        assert c["applicant"] in APPLICANTS, f"{cid} applicant 非法"
        assert c["quote"] and len(c["quote"]) > 8, f"{cid} 缺处理结果原文"
        for a in c["articles"]:
            law, _, art = a.partition("#")
            assert (law, art) in have, (
                f"{cid} 引用了语料库里没有的条号：{a}。"
                "案例原文若引了库外法律，只写进 point 文字，不做结构化依据。")
        meta = BATCHES[cid[:2]]
        rows.append({
            "case_id": cid,
            "batch": meta["batch"],
            "doc_no": meta["doc_no"],
            "date": meta["date"],
            "theme": meta["theme"],
            "authority": AUTHORITY,
            "title": c["title"],
            "topics": list(c["topics"]),
            "applicant": c["applicant"],
            "outcome": c["outcome"],
            "outcome_quote": c["quote"],
            "claim_amount": c["claim"],
            "awarded_amount": c["awarded"],
            "articles": list(c["articles"]),
            "key_point": c["point"],
            "source_url": meta["url"],
            "note": "处理结果原文与要点摘要，非全文；请以官方原文为准。",
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["outcome"]] = tally.get(r["outcome"], 0) + 1
    print(f"生成 {OUT.relative_to(ROOT)}：{len(rows)} 件，结局分布 {tally}")
    print("来源：", " / ".join(sorted({r['doc_no'] for r in rows})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
