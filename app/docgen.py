# -*- coding: utf-8 -*-
"""文书生成。目前只有一种：劳动人事争议仲裁申请书。

最关键的取舍：**请求事项和金额一律留空**。

为什么不做成「一键生成完整申请书」：请求事项决定仲裁委审什么，漏写的
事项不会被裁（不受「不告不理」以外原则的保护），金额写错要当庭变更。
这两处让模型代笔，等于把一份有法律后果的文件交给一个会编的实体。所以
这里只做三件事——把已知事实填进固定位置、把必须自己写的部分明确标成
待填、把提交前必须核对的事项列出来。

事实与理由那一段也故意留了口子：只写「用你自己的话补」，不生成叙述。
生成的叙述读起来顺，但当事人照着抄进仲裁委，一旦与真实经过有出入，
就是自己写下的不利陈述。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

# 申请书里会用到、但缺失就写不出来的要素（案情要素，来自对话）
REQUIRED = ("start_date", "monthly_wage", "leave_reason")
NICE = ("end_date", "region", "has_contract")

# 主体信息，来自个人档案而不是对话。缺了这几项，申请人/被申请人两栏
# 就是空白的，整份文件交不出去——所以单独列一组，提示语也不一样。
PROFILE_REQUIRED = ("real_name", "phone", "company_name")
PROFILE_LABELS = {
    "real_name": "你的姓名", "id_card": "身份证号", "phone": "联系电话",
    "address": "你的住址", "region": "所在地区",
    "company_name": "单位全称", "company_code": "统一社会信用代码",
    "company_address": "单位注册地址", "company_legal_rep": "法定代表人",
    "commission": "仲裁委名称",
}

BLANK = "＿＿＿＿"

CN_NUM = "〇一二三四五六七八九"


def cn_date(s: str | None, fallback: str = "＿＿＿＿年＿＿月") -> str:
    """把 2023-06 / 2023-06-15 转成「2023 年 6 月（15 日）」。

    法律文书里日期写阿拉伯数字完全可以，但「2023 年 6 月」比「2023-06」
    更接近仲裁委的书写习惯，也避免当事人误以为是某种编号。
    """
    if not s:
        return fallback
    m = re.match(r"(\d{4})\D*?(\d{1,2})(?:\D*?(\d{1,2}))?", str(s))
    if not m:
        return fallback
    y, mo = int(m.group(1)), int(m.group(2))
    d = m.group(3)
    if d:
        return f"{y}年{mo}月{int(d)}日"
    return f"{y}年{mo}月"


def _contract_line(slots: dict[str, Any]) -> str:
    v = slots.get("has_contract")
    if v is True:
        return "，双方订立了书面劳动合同"
    if v is False:
        return "，双方未订立书面劳动合同"
    return "，双方＿＿＿＿（是否订立书面劳动合同，请填写）"


def _end_line(slots: dict[str, Any]) -> str:
    if slots.get("end_date"):
        return f"双方劳动关系于{cn_date(slots['end_date'])}解除或终止。"
    return "双方劳动关系于＿＿＿＿年＿＿月解除或终止（请填写）。"


def _reason_line(slots: dict[str, Any]) -> str:
    r = slots.get("leave_reason")
    if r:
        return f"解除方式：{r}。"
    return "解除方式：＿＿＿＿（公司辞退 / 协商解除 / 被迫离职 / 主动辞职，请填写）"


def _pf(profile: dict[str, Any] | None, key: str, blank: str = BLANK) -> str:
    """取档案字段，空则返回占位线。

    不做任何补齐或猜测：档案里写的是「张 三」，文书里就照原样出，不会
    自作主张去掉空格；档案里没写，就用下划线，让人一眼看出还没填。
    """
    return ((profile or {}).get(key) or "").strip() or blank


def _commission_line(profile: dict[str, Any] | None) -> str:
    """「此致」后面那一行。

    用户可能填全称（杭州市劳动人事争议仲裁委员会），也可能只填地名
    （杭州市）。两种都得拼对，不能拼成「杭州市……委员会 劳动人事争议
    仲裁委员会」这种重复。
    """
    c = ((profile or {}).get("commission") or "").strip()
    if not c:
        return "＿＿＿＿ 劳动人事争议仲裁委员会"
    if "仲裁" in c:
        return c
    return f"{c} 劳动人事争议仲裁委员会"


def _statements_block(items: list[dict[str, Any]]) -> str:
    """把用户自己在对话里说过的事实性内容，按时间顺序摘成起草线索。

    为什么不直接生成一段通顺叙述：见文件头。这里只做时间线摘录——
    句子是用户的原话，出处可查；改写成连贯叙述的活留给他自己，那样
    写出来的东西他才能负责。
    """
    items = [it for it in (items or []) if (it.get("text") or "").strip()]
    if not items:
        return ("  （这一段用你自己的话写：什么时候发生了什么、你向谁提过"
                "什么要求、对方怎么答复、有没有书面或聊天记录。写清日期和"
                "在场人。仲裁员最先看的就是这一段，它比法律依据重要得多。）")
    out = ["  （下面是你在这件事的对话里说过的话，按时间顺序摘录，可作为",
           "   起草线索。请据此用自己的话改写成连贯叙述；与事实不符的要删掉。）"]
    for i, it in enumerate(items, 1):
        t = (it.get("text") or "").strip().replace("\n", " ")
        d = str(it.get("date") or "").strip()
        out.append(f"  {i}. {('[' + d + '] ') if d else ''}{t}")
    return "\n".join(out)


def _day(ts: Any) -> str:
    try:
        return date.fromtimestamp(float(ts)).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def pick_statements(messages: list[dict[str, Any]], *,
                    min_len: int = 12, max_items: int = 12,
                    per_item: int = 120) -> list[dict[str, Any]]:
    """从整个案子的对话里挑出「事实陈述」，按时间顺序排。

    为什么必须挑：一个案子聊十几轮，中间混着「你好」「那我能拿多少」
    「谢谢」。全塞进申请书是噪音；让当事人自己从上万字里往上翻更糟。

    判据很土但够用：
      1. 是用户说的（助手的话不是当事人陈述）；
      2. 长度过线（太短的几乎都是追问，不是事实）；
      3. 去重（同一句反复问只留第一次）；
      4. 排除「纯提问」——以问号收尾且不含数字。夹着数字的提问
         （「我 3 个月工资没发，能要多少」）通常本身就含事实，要留。

    宁可多留一句让用户自己删，也别漏掉关键事实——漏了，他照着
    这份草稿写出来的申请书就是残缺的。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in messages or []:
        if (m.get("role") or "") != "user":
            continue
        t = (m.get("content") or "").strip().replace("\n", " ")
        if len(t) < min_len:
            continue
        key = t[:40]
        if key in seen:
            continue
        seen.add(key)
        if t.endswith(("?", "？")) and not any(c.isdigit() for c in t):
            continue
        out.append({"text": t[:per_item], "date": _day(m.get("ts"))})
    # 超过上限时保留首尾：开头通常交代来龙去脉，结尾往往是用户
    # 想明白之后补的关键细节（「对了，我还有微信记录」）。
    if len(out) > max_items:
        half = max(1, max_items // 2)
        out = out[:half] + out[-half:]
    return out


def arbitration(slots: dict[str, Any], statement: str = "",
                as_of: date | None = None,
                profile: dict[str, Any] | None = None,
                statements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """按案情要素 + 个人档案填一份申请书骨架。

    slots 是 CaseSlots.known() 的结果（案情，来自对话）；
    profile 是 profiles 表那一行（主体信息，跨案件不变）；
    statements 是整件事里用户说过的原话摘录，按时间排序。

    statement 是 v8x 之前的单条入参，保留只为不破坏旧调用方。
    """
    as_of = as_of or date.today()
    s = slots or {}
    p = profile or {}

    wage = s.get("monthly_wage")
    wage_txt = f"{wage}元" if wage else "＿＿＿＿元（请填写）"

    # 新调用传 statements（多条、带日期）；旧调用只有一条 statement。
    stmts: list[dict[str, Any]] = (
        statements if statements is not None
        else ([{"text": statement}] if statement else []))

    body = f"""劳动人事争议仲裁申请书

申请人
  姓　　名：{_pf(p, 'real_name')}
  身份证号：{_pf(p, 'id_card')}
  住　　址：{_pf(p, 'address')}
  联系电话：{_pf(p, 'phone')}

被申请人
  单位名称：{_pf(p, 'company_name', '＿＿＿＿＿＿＿＿（写营业执照上的全称，一个字都不能差）')}
  统一社会信用代码：{_pf(p, 'company_code')}
  住　　所：{_pf(p, 'company_address')}
  法定代表人：{_pf(p, 'company_legal_rep')}

仲裁请求
  （每一项都要有具体金额和期间。只写「依法赔偿」等于没提，仲裁委不会裁。）
  1. 请求裁决被申请人支付＿＿＿＿年＿＿月至＿＿＿＿年＿＿月拖欠的工资
     （含加班费），共计＿＿＿＿元；
  2. 请求裁决被申请人支付违法解除劳动合同赔偿金＿＿＿＿元；
  3. 请求裁决被申请人支付未订立书面劳动合同的二倍工资差额＿＿＿＿元；
  4. ＿＿＿＿＿＿＿＿（其他请求，没有就划掉）

  ※ 金额不要自己估。回到对话里问一句「我能拿多少赔偿」，系统会按你填的
    月工资和在职时间把两种口径都算给你，核对后再抄进这里。

事实与理由

  一、基本事实
  申请人于{cn_date(s.get('start_date'))}入职被申请人处，从事＿＿＿＿岗位{
      _contract_line(s)}。申请人月工资{wage_txt}，由被申请人通过＿＿＿＿
  （银行转账 / 现金 / 微信）发放。
  {_end_line(s)}
  {_reason_line(s)}

  二、争议经过
{_statements_block(stmts)}

  三、法律依据
  （取决于你主张什么。回到对话里问「我能主张什么」，按系统列出的条文填。
   不要从别处抄条号——引错法条比不引更糟，会让人觉得你没读过原文。）

证据清单
  1. 劳动合同（未签的，用工资流水、考勤记录、工作沟通记录证明劳动关系）；
  2. 工资发放记录（银行流水，标注每月到账金额与日期）；
  3. 解除或辞退的通知、沟通记录；
  4. 考勤记录、排班表、加班审批记录；
  5. ＿＿＿＿＿＿＿＿（其他）

此致
{_commission_line(p)}

                              申请人（签名）：＿＿＿＿＿＿
                              ＿＿＿＿ 年 ＿＿ 月 ＿＿ 日

————————————————————————————————————
本文件由工具按你提供的信息填充，不是法律文书成品。请求事项、金额、
法律依据三处必须你自己确认后才能提交。
"""

    missing = [k for k in REQUIRED if not s.get(k)]
    missing_nice = [k for k in NICE if not s.get(k)]
    missing_profile = [k for k in PROFILE_REQUIRED if not p.get(k)]

    warnings: list[str] = []
    if "start_date" in missing:
        warnings.append("入职时间没填，事实与理由第一段是空的。"
                        "在职时长直接决定补偿月数，这个必须有。")
    if "monthly_wage" in missing:
        warnings.append("月工资没填，请求金额一栏全是空白。"
                        "写错工资基数会让整份申请书的金额都错。")
    if "leave_reason" in missing:
        warnings.append("离职方式没填，而这决定了你到底是主张经济补偿（N）"
                        "还是赔偿金（2N），两者差一倍。")
    if s.get("has_contract") is None:
        warnings.append("不确定有没有签书面合同。「没签」本身可以主张二倍工资，"
                        "这一项填错会整条请求作废。")
    # 主体信息缺失单独提示，并直接告诉用户去哪儿补——在对话里补不了，
    # 只能去「设置 → 个人档案」，不说清楚他会一直在聊天框里解释自己叫什么。
    if missing_profile:
        names = "、".join(PROFILE_LABELS.get(k, k) for k in missing_profile)
        warnings.append(f"申请人/被申请人栏还空着：{names}。"
                        f"去「设置 → 个人档案」补上，不用重填整份。")

    checklist = [
        "被申请人的**全称**和统一社会信用代码：从营业执照或国家企业信用"
        "信息公示系统抄，写错一个字都可能被要求补正，白跑一趟。",
        "管辖：向**劳动合同履行地或用人单位所在地**的仲裁委提出"
        "（《劳动争议调解仲裁法》第二十一条）。两地都申请时，由履行地管辖。",
        "时效：一般一年，从你知道权利被侵害之日起算；讨薪在劳动关系存续期间"
        "不受这一年限制，但离职后要在一年内提出（第二十七条）。",
        "份数：申请书一式两份（被申请人多一个就多一份），证据原件带上看，"
        "交复印件。",
        "费用：劳动争议仲裁不收费（第五十三条）。",
        "签名：末尾必须**手写签名**，打印的名字不算。",
    ]

    return {
        "title": "劳动人事争议仲裁申请书",
        "text": body,
        "used": {k: v for k, v in s.items() if v is not None},
        "profile_used": {k: v for k, v in p.items()
                         if k in PROFILE_LABELS and v},
        "missing": missing,
        "missing_optional": missing_nice,
        "missing_profile": missing_profile,
        "n_statements": len(stmts),
        "warnings": warnings,
        "checklist": checklist,
        "as_of": as_of.isoformat(),
        # complete 保持老语义：只看案情要素齐不齐。档案是后加的维度，
        # 混进去会把所有老调用方（和既有测试）的判定一起改掉，
        # 那种「顺手改语义」的代价通常过几周才显现。
        "complete": not missing,
        # ready 才是「这份东西现在能不能交」：要素 + 主体信息都齐。
        "ready": not missing and not missing_profile,
    }


def filename(as_of: date | None = None) -> str:
    d = as_of or date.today()
    return f"劳动仲裁申请书_{d.strftime('%Y%m%d')}.txt"


# ---------------------------------------------------------------- v124
# 接入内核的「起诉状 / 申请书」骨架。和 arbitration() 同一套规矩：
# 请求事项、金额、法律依据留空，事实段只摘用户原话，绝不代笔。
# 按内核换的只有抬头、当事人栏、受理机关、证据清单和几句提示 ——
# 换的是格式，不是「一键生成完整诉状」。
#
# 为什么单独一张表而不复用 domains._CLAIMS：那边是「可勾选的候选项」，
# 一旦自动写进请求事项，就等于替当事人决定了告什么，边界会破。
_DOMAIN_DOC: dict[str, dict[str, Any]] = {
    "consumer": {
        "title": "消费与食品安全争议 · 民事起诉状（骨架）",
        "party_b": "被告（经营者 / 生产者，写营业执照上的全称）",
        "organ": "＿＿＿＿ 人民法院",
        "claim_hint": "退款、增加赔偿是两项不同请求，分开写、各填各的金额。",
        "evidence": [
            "订单、支付记录、发票或电子凭证",
            "商品实物、原包装、标签与同批次照片（食品尽量保留原包装）",
            "与经营者沟通、协商、投诉的完整记录",
            "检验报告、就医记录等能证明损害的材料（如有）",
        ],
        "check": "赔偿档位以实际情形与现行条文为准；主张哪一项由你选定。",
    },
    "civil": {
        "title": "合同争议 · 民事起诉状（骨架）",
        "party_b": "被告（合同相对方，写全称）",
        "organ": "＿＿＿＿ 人民法院",
        "claim_hint": "违约金、继续履行、确认效力是不同请求，分开写；金额按合同与损失核定。",
        "evidence": [
            "合同原件、补充协议与全部附件",
            "往来函件、聊天记录、付款与交付凭证",
            "催告、协商的送达与留痕记录",
            "能证明实际损失的票据与计算依据",
        ],
        "check": "引用条号前先确认是否仍现行有效；旧单行法条号须换成现行条文。",
    },
    "traffic": {
        "title": "交通事故损害赔偿 · 民事起诉状（骨架）",
        "party_b": "被告（侵权人 / 车辆所有人 / 承保保险公司，按责任主体列明）",
        "organ": "＿＿＿＿ 人民法院",
        "claim_hint": "医疗费、误工费、护理费等逐项列明并各附票据，不要合并成一个总数。",
        "evidence": [
            "道路交通事故认定书及复核材料（如有）",
            "病历、诊断证明、出院记录与全部医疗票据",
            "误工证明、护理证明、收入与完税材料",
            "交强险 / 商业险保单与理赔沟通记录",
        ],
        "check": "赔偿顺位按交强险先行、不足部分按责任比例，具体以认定书与保险合同约定为准。",
    },
    "family": {
        "title": "婚姻家庭与继承争议 · 民事起诉状（骨架）",
        "party_b": "被告（按争议相对方列明）",
        "organ": "＿＿＿＿ 人民法院",
        "claim_hint": "财产分割、子女抚养、继承份额是不同的请求，分开写清各自范围。",
        "evidence": [
            "财产取得时间、登记名义、出资与还贷流水",
            "结婚证、出生医学证明、亲属关系证明",
            "遗嘱原件、死亡证明、遗产清单（继承争议）",
            "子女实际生活与照料情况的证明材料",
        ],
        "check": "身份关系与财产范围须以登记机关与原件为准。",
    },
}


def _domain_complaint(pid: str, slots: dict[str, Any],
                      profile: dict[str, Any] | None,
                      statements: list[dict[str, Any]] | None,
                      as_of: date) -> dict[str, Any]:
    cfg = _DOMAIN_DOC[pid]
    p = profile or {}
    ev = "\n".join(f"  {i}. {x}" for i, x in enumerate(cfg["evidence"], 1))
    body = f"""{cfg['title']}

{cfg.get('party_a', '原告')}
  姓　　名：{_pf(p, 'real_name')}
  身份证号：{_pf(p, 'id_card')}
  住　　址：{_pf(p, 'address')}
  联系电话：{_pf(p, 'phone')}

{cfg['party_b']}
  名称 / 姓名：{_pf(p, 'company_name', '＿＿＿＿＿＿＿＿（写全称，一个字都不能差）')}
  统一社会信用代码 / 身份证号：{_pf(p, 'company_code')}
  住　　所：{_pf(p, 'company_address')}
  联系人 / 法定代表人：{_pf(p, 'company_legal_rep')}

诉讼请求
  1. ＿＿＿＿＿＿＿＿＿＿＿＿＿＿（写清请求内容、金额与期间）
  2. ＿＿＿＿＿＿＿＿＿＿＿＿＿＿
  3. ＿＿＿＿＿＿＿＿＿＿＿＿＿＿（没有就划掉）
  ※ {cfg['claim_hint']}
  ※ 金额一律由本人按凭证核定后填写。

事实与理由
{_statements_block(statements or [])}

  法律依据
  （回到对话里问「我能主张什么」，按系统列出的现行条文自己填写。
   不要从别处抄条号；{cfg['check']}）

证据清单
{ev}
  5. ＿＿＿＿＿＿＿＿（其他）

此致
{cfg['organ']}

                              具状人（签名）：＿＿＿＿＿＿
                              ＿＿＿＿ 年 ＿＿ 月 ＿＿ 日

————————————————————————————————————
本文件是按你提供的信息填充的骨架，不是法律文书成品。请求事项、金额、
法律依据三处必须你自己确认后才能提交；仅供参考，不构成法律意见。
"""
    missing_profile = [k for k in PROFILE_REQUIRED if not p.get(k)]
    warnings = []
    if missing_profile:
        names = "、".join(PROFILE_LABELS.get(k, k) for k in missing_profile)
        warnings.append(f"当事人栏还空着：{names}。去「设置 → 个人档案」补上，"
                        f"不用重填整份。")
    if not (statements or []):
        warnings.append("还没有可摘录的事实陈述。先在对话里把经过说清楚，"
                        "这份骨架的事实段才有起草线索。")
    return {
        "title": cfg["title"],
        "text": body,
        "used": {k: v for k, v in (slots or {}).items() if v is not None},
        "profile_used": {k: v for k, v in p.items()
                         if k in PROFILE_LABELS and v},
        "missing": [],
        "missing_optional": [],
        "missing_profile": missing_profile,
        "n_statements": len(statements or []),
        "warnings": warnings,
        "checklist": [
            "对方主体全称须与登记证照一致，写错可能被要求补正。",
            "管辖法院按争议性质与被告住所地确定，提交前先核对。",
            "诉讼请求逐项写清，漏写的部分法院不会主动替你补。",
            "签名必须手写；份数按被告人数加一份准备。",
            "费用与减免以受理机关的规定为准。",
        ],
        "as_of": as_of.isoformat(),
        "complete": False,      # 接入内核不核算金额，永远不宣称「要素齐了」
        "ready": False,         # 更不宣称「可以提交」
    }


def for_profile(pid: str, slots: dict[str, Any] | None = None,
                profile: dict[str, Any] | None = None,
                statements: list[dict[str, Any]] | None = None,
                as_of: date | None = None) -> dict[str, Any]:
    """按内核取文书骨架。劳动法走原有 arbitration()，其余四内核走通用起诉状。

    未知 pid 回落劳动法，和 profiles.resolve 同一口径，不猜内核。
    """
    as_of = as_of or date.today()
    if pid not in _DOMAIN_DOC:
        return arbitration(slots or {}, as_of=as_of, profile=profile,
                           statements=statements)
    return _domain_complaint(pid, slots or {}, profile, statements, as_of)


def filename_for(pid: str, as_of: date | None = None) -> str:
    d = as_of or date.today()
    if pid in _DOMAIN_DOC:
        return f"{_DOMAIN_DOC[pid]['title'].split(' · ')[0]}起诉状骨架_{d.strftime('%Y%m%d')}.txt"
    return filename(d)

