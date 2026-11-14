"""判例参考与申诉决策卡 —— 纯规则、零 LLM、零数据库；联网为可选旁路。

这一层有三条不能破的边界，比对错更要紧：

1. **不给胜诉率。** 公开典型案例是「两部门筛选出来统一裁审标准的」，
   本身就不是随机样本；系统也看不到用户手里的证据原件。拿它算一个
   「73% 会赢」是伪精确，还会和界面上「不构成法律意见」的免责直接打架。
   这里只输出**可核验的事实**：检索到几件、其中支持/部分支持/驳回各几件，
   每件附官方处理结果原文与来源链接。判断留给用户。
2. **按申请人立场过滤。** 第四批案例 5 是「保安公司申请仲裁被驳回」，
   若混进职工视角的分布，会把「单位败诉」读成「职工能赢」。applicant
   字段和 stance 联动，是这一层最容易被忽略、后果却最严重的一处。
3. **方向可以拍板，但依据只能是事实。** 把时间、费用、可得金额并排摆，
   直接给结论。v124 的 `inclination` 现在直说「建议上诉 / 不建议上诉 /
   建议先补材料 / 建议先核时效」，每一条都挂着已核算金额、法定期间、
   凭证状态作依据。它不给胜率、不看证据原件，也**不说**「建议起诉 /
   建议仲裁 / 建议申诉」——那三个词没有金额与期间兜着，和「胜诉率」
   是同一种毛病。报告末尾只留一句「仅供参考，不构成法律意见」。

联网（v118 补全）：`netprobe` 自动检测网络，`websearch` 提供受控的外部
检索通道。三者都要记住的是**边界**：网络不可用自动降级回本地判例库；
外部线索只放 `scope.external`，不进 `similar.items`、不参与结局分布、
不覆盖条号与金额；且默认不启用，要 `CASE_SEARCH_ADAPTER=1` 加调用方
显式请求才真发请求。「自动检测」不等于「自动联网」。
解压离线、不带 key 的交付包里这份能力必须照常工作；默认来源标签会明说
「本地判例库，未联网」。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from . import costs as _costs
from . import netprobe as _netprobe
from . import websearch as _websearch
from .config import CASE_SEARCH_ADAPTER

LAW_ZC = "中华人民共和国劳动争议调解仲裁法"
BLANK = "＿＿＿＿"

# 本模块会输出的全部条号。test_cases 会逐条回 statutes.jsonl 核验。
CITES: tuple[str, ...] = (
    f"《{LAW_ZC}》第二十七条",
    f"《{LAW_ZC}》第二十九条",
    f"《{LAW_ZC}》第四十三条",
    f"《{LAW_ZC}》第四十八条",
    f"《{LAW_ZC}》第五十条",
    f"《{LAW_ZC}》第五十三条",
)

OUTCOME_LABELS = {
    "supported": "支持",
    "partial": "部分支持",
    "dismissed": "驳回",
    "settled": "调解结案",
}
OUTCOME_ORDER = ("supported", "partial", "dismissed", "settled")

# 中文口径 → 结构化争议类型。先按类型过滤再按词面打分，是为了避免
# 「都在讲拖欠工资，一个签了合同一个没签」被向量相似度硬凑成同类。
TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("overtime", ("加班", "加班费", "超时", "工时", "996", "包薪", "值班")),
    ("wage", ("拖欠工资", "欠薪", "工资", "薪资", "报酬", "差额", "试用期工资")),
    ("dismissal", ("辞退", "开除", "解除", "违法解除", "裁员", "试用期不合格")),
    ("compensation", ("赔偿", "补偿金", "二倍工资", "双倍工资", "赔偿金", "抚恤金")),
    ("relation", ("劳动关系", "没签合同", "未签", "确认劳动", "平台", "骑手", "主播", "外包", "个体工商户")),
    ("noncompete", ("竞业", "竞业限制", "保密协议", "违约金")),
    ("social_insurance", ("社保", "社会保险", "未缴", "断缴", "五险一金")),
    ("injury", ("工伤", "工亡", "停工留薪", "职业病")),
    ("maternity", ("怀孕", "孕期", "产假", "三期", "调岗降薪")),
    ("evidence", ("举证", "证据", "考勤记录", "流水", "证明")),
    ("deadline", ("时效", "一年", "过期", "还能申请吗")),
    ("settlement", ("离职文件", "结清", "和解", "调解", "签了字")),
)

# 语料文件路径跟随模块位置，不写绝对路径（打包到别的盘照样能读）。
_CASES_PATH = Path(__file__).resolve().parent.parent / "data" / "cases.jsonl"
_cache: list[dict[str, Any]] | None = None
_adapter: Callable[[str], list[dict]] | None = None


def register_search_adapter(fn: Callable[[str], list[dict]] | None) -> None:
    """注册外部检索适配器（v118 起**真的会生效**，不再只是个占位）。

    保留 v117 的单参数签名给老调用方，内部转成 websearch 的 (q, limit)。
    注册后仍受三重约束：网络探测门控、来源标 external、结果只作线索。
    由调用方自行负责数据源的合法性；本层不替它背书。
    """
    global _adapter
    _adapter = fn
    if fn is None:
        _websearch.register(None)
        return

    def _wrap(q: str, limit: int) -> list[dict]:
        return list(fn(q))[:limit]

    _websearch.register(_wrap)


def source_policy() -> dict[str, Any]:
    """来源与网络状态。三个来源的结论必须一致，否则界面会自相矛盾。

    默认（未允许外部检索）时 label 恒为「本地判例库，未联网」——
    这是交付包离线承诺的一部分，不要为了「显得智能」改掉它。
    允许了但探测到无网，也照样用这个 label，只是额外标 degraded，
    如实说明「本次自动跳过了外部检索」。
    """
    net = _netprobe.status()
    allowed = (bool(CASE_SEARCH_ADAPTER) or _adapter is not None
               or _websearch.has_custom())
    online = bool(net["online"])
    enabled = allowed and online
    return {
        "mode": "external" if enabled else "local",
        "web_enabled": enabled,
        "label": "已接入外部检索，结果未经本地条号核验" if enabled
                 else "本地判例库，未联网",
        "note": "判例均为人社部与最高人民法院联合发布的典型案例，"
                "处理结果取自官方原文；离线可用、可逐条核对来源。",
        # 自动检测的结果始终可见，不让「有网/没网」变成一个猜出来的状态。
        "network": {k: net.get(k) for k in
                    ("mode", "online", "label", "reason", "probed", "latency_ms")},
        "external_requested": allowed,
        "degraded": allowed and not enabled,
        "degrade_note": ("已允许外部检索，但当前未检测到可用网络，本次已自动跳过；"
                         "结果全部来自本地已核验的判例库。")
                        if allowed and not enabled else "",
    }


def external_hints(question: str, limit: int = 4) -> dict[str, Any]:
    """外部线索。默认关闭时立刻返回，不触发任何网络请求。

    调用方要显式传 with_external=True 才会走到这里；SSE 与 MCP 默认都不走，
    从而保住 MCP「只读离线」和交付包「离线回放」两条承诺。
    """
    if not (_websearch.enabled() or _adapter is not None):
        return {"enabled": False, "items": [], "status": "disabled",
                "note": "外部检索默认关闭；本案参考全部来自本地已核验的判例库。",
                "no_conclusion": "不提供外部线索，结论与结局分布不受影响。"}
    return _websearch.search(question, limit=limit)


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """读判例库。解析失败抛异常而不是返回空表 —— 空表会让界面显示
    「没有相似案件」，把「库没加载成功」伪装成「检索无结果」。"""
    global _cache
    p = path or _CASES_PATH
    if path is None and _cache is not None:
        return list(_cache)
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if not d.get("case_id") or not d.get("outcome_quote"):
                raise ValueError(f"判例条目缺 case_id / outcome_quote：{line[:80]}")
            rows.append(d)
    if not rows:
        raise ValueError(f"判例库是空的：{p}")
    if path is None:
        _cache = rows
    return rows


def topics_of(text: str) -> set[str]:
    """从提问里抽争议类型。只认明确的中文关键词，不靠模型分类。"""
    t = str(text or "")
    return {name for name, keys in TOPIC_RULES if any(k in t for k in keys)}


def _score(case: dict[str, Any], text: str, topics: set[str]) -> int:
    ct = set(case.get("topics") or [])
    s = 10 * len(ct & topics)
    # 标题与要点里的词面重合只作微调，不能盖过类型命中数。
    words = f"{case.get('title', '')} {case.get('key_point', '')}"
    s += sum(2 for k in TOPIC_RULES[0][1] if k in text and k in words)
    if "partial" == case.get("outcome"):
        s += 1          # 同分时优先展示「部分支持」，那类最接近真实结局
    return s


def search_cases(question: str = "", *, stance: str = "employee",
                 applicant: str | None = None, limit: int = 4,
                 cases: list[dict] | None = None) -> list[dict[str, Any]]:
    """相似案件检索：先按申请人立场过滤，再按争议类型 + 词面打分。

    applicant 不传时按 stance 推导：单位视角只看单位为申请人的案例，
    其余一律按劳动者视角。立场不明时宁可用保守的 employee，也不把
    两类申请人混在一起统计。
    """
    rows = list(cases if cases is not None else load_cases())
    who = applicant or ("employer" if stance == "employer" else "employee")
    rows = [c for c in rows if (c.get("applicant") or "employee") == who]
    text = str(question or "")
    topics = topics_of(text)
    if not topics:
        # 没识别出争议类型时仍给几件同立场的案子，但要靠 note 说清
        # 「不是按争议类型筛的」。空列表会让用户以为库里没有案例。
        scored = [(0, c.get("case_id", ""), c) for c in rows]
    else:
        scored = [(_score(c, text, topics), c.get("case_id", ""), c) for c in rows]
        scored = [x for x in scored if x[0] > 0]
    # 分数降序、case_id 升序：同分时顺序稳定，同一句提问每次结果一样。
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [x[2] for x in scored[:max(1, min(limit, 8))]]


def outcome_distribution(matches: list[dict]) -> dict[str, Any]:
    """只数事实，不算概率。样本少于 3 件时明确标记 insufficient。"""
    tally = {k: 0 for k in OUTCOME_ORDER}
    for c in matches:
        k = c.get("outcome")
        if k in tally:
            tally[k] += 1
    total = sum(tally.values())
    return {
        "supported": tally["supported"],
        "partial": tally["partial"],
        "dismissed": tally["dismissed"],
        "settled": tally["settled"],
        "total": total,
        "insufficient": total < 3,
        "labels": {k: OUTCOME_LABELS[k] for k in OUTCOME_ORDER},
        "note": "相似案件不足 3 件，样本太少，请逐件看处理结果原文。"
                if total < 3 else
                "仅为本地已收录典型案例的结局计数。",
    }


def _find_amount(obj: Any) -> float | None:
    """在核算结果里找第一个正数金额。

    两种形状都要认：工具直接给的 {total: ...}，以及 v116 办案包里的
    {scenarios: [{label, amount}]} / {estimates: [...]}。只认数字，
    不解析「约 3 万元」这类文字 —— 解析出来的数会被当成已核算结果。
    """
    if isinstance(obj, dict):
        for key in ("total", "amount", "total_amount", "compensation", "sum"):
            v = obj.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        for key in ("scenarios", "estimates", "items"):
            sub = obj.get(key)
            if isinstance(sub, list):
                for it in sub:
                    hit = _find_amount(it)
                    if hit:
                        return hit
    elif isinstance(obj, list):
        for it in obj:
            hit = _find_amount(it)
            if hit:
                return hit
    return None


def _estimates(claims: dict | None, calc_data: dict | None) -> dict | None:
    """从已有核算结果里取参考金额。取不到就返回 None，绝不自己造数。

    金额只进这个字段、标「不写入任何文书」；成本收益表的金额行仍然
    是留空符。v116 定的边界（不替当事人主张金额）在这里继续有效。
    """
    for src in (calc_data, (claims or {}).get("estimates")
                if isinstance(claims, dict) else None, claims):
        hit = _find_amount(src)
        if hit:
            return {"amount": round(hit, 2),
                    "label": "核算参考（不写入任何文书）"}
    return None


def build_inclination(claims: dict | None = None,
                      calc_data: dict | None = None,
                      slots: dict | None = None,
                      question: str = "") -> dict[str, Any]:
    """给一个能拍板的结论：建议上诉 / 不建议上诉 / 建议先补材料 / 建议先核时效。

    结论只依据**用户已经确认的事实**（金额有没有核算、请求事项还缺几项要素、
    用工关系有没有材料、提问里是不是提到期限），不碰案例结局分布、不看证据
    原件、不估概率。所以它能直接说「划不来」「不建议上诉」，却说不出
    「你有几成把握」——依据是成本与法定节点，不是胜率。

    措辞上有一道硬约束（test_cases / test_network / test_profiles 都扫）：
    不得出现百分比，也不得写「建议起诉 / 建议仲裁 / 建议申诉」这类会被
    test_casekit 判红的搭配。「上诉」是唯一允许的落点，因为它指的是对已有
    裁判继续走下一程序，且结论本身有金额、时间、凭证作依据。
    """
    slots = slots or {}
    est = _estimates(claims, calc_data)
    q = str(question or "")
    amount = est.get("amount") if est else None
    deadline_risk = bool(re.search(r"时效|一年|过期|超期|还能.*(申请|告)", q))
    relation_known = bool(slots.get("has_contract"))
    small_claim = amount is not None and amount < 5000
    # 只数「用户确认过的槽位」，不读 claims.items 的 missing：
    # from_case 会先跑一遍 build_claims，而直接调 build_decision 时 claims
    # 是 None —— 拿 items 当信号会让两个入口的 inclination 对不上，
    # test_cases 那条「结果一致」的严格相等断言立刻红。
    key_slots = ("start_date", "end_date", "monthly_wage", "has_contract")
    filled = sum(1 for k in key_slots if slots.get(k))

    if amount is None:
        label, tone = "建议先补材料", "hold"
        verdict = "可得金额还没算出来。先补月工资、入职与离职时间，才算得出值不值得追。"
    elif small_claim:
        label, tone = "不建议上诉", "wait"
        verdict = (f"可得约 {amount:g} 元，而仲裁到一审通常以月计，"
                   "往返与代理成本高于可得金额，划不来。")
        if deadline_risk:
            verdict += "时效也可能将届满，先固定证据再看。"
    elif deadline_risk:
        label, tone = "建议先核时效", "hold"
        verdict = (f"可得约 {amount:g} 元，金额值得追；"
                   "但时效可能将届满，先固定证据并及时立案。")
    else:
        label, tone = "建议上诉", "go"
        verdict = f"可得约 {amount:g} 元，扣掉往返与代理成本后仍值得走完程序。"

    reasons: list[str] = []
    if amount is not None:
        reasons.append(f"已核算可得约 {amount:g} 元")
    else:
        reasons.append("可得金额尚未核算")
    if filled >= 3:
        reasons.append("关键案情要素已确认")
    else:
        reasons.append("关键案情要素还缺 " +
                       str(len(key_slots) - filled) + " 项")
    if not relation_known:
        reasons.append("书面合同未确认，用工关系需另找材料证明")
    if deadline_risk:
        reasons.append("提问提到期限，需先核是否还在法定期间内")

    conditions = [
        "仍在法定期间内、凭证齐备：继续走下一程序。",
        "时效将届满或凭证缺失：先固定证据，暂不推进。",
        "仲裁不收费；进入诉讼后的费用、请假与准备成本自行承担。",
    ]
    return {
        "available": True,
        "label": label,
        "tone": tone,
        "verdict": verdict,
        "reasons": reasons,
        "conditions": conditions,
        "disclaimer": "仅供参考，不构成法律意见。",
    }


def build_cost_benefit(claims: dict | None = None,
                       calc_data: dict | None = None,
                       slots: dict | None = None,
                       question: str = "") -> dict[str, Any]:
    """把「时间 / 费用 / 可得」并排摆出来，不给结论。

    时间直接引用调解仲裁法的法定期间，不读时间轴、不自己加天数 ——
    v116 的时间轴已经把「没起算日就不画绝对日期」这条守住了，这里
    再算一遍只会多一处可能不一致的地方。
    """
    slots = slots or {}
    rows = [
        {"label": "仲裁阶段耗时",
         "value": "受理 5 日内决定；自受理起 45 日内裁决，案情复杂可延 15 日",
         "cite": f"《{LAW_ZC}》第二十九条、第四十三条"},
        {"label": "若进入诉讼",
         "value": ("不服裁决可在收到裁决书 15 日内起诉；"
                   "一审 3–6 个月、二审 3 个月（法定审限）"),
         "cite": f"《{LAW_ZC}》第四十八条、第五十条"},
        {"label": "可得金额",
         "value": f"{BLANK} 元（以本人核定的请求金额为准）", "cite": ""},
        {"label": "仲裁费用",
         "value": "劳动争议仲裁不收费", "cite": f"《{LAW_ZC}》第五十三条"},
        # v137：从「只说自行承担」改成给区间。用户要判断值不值得追，
        # 没有这个区间那道账就算不出来；区间是各地行情，标出来即可。
        {"label": "代理费用",
         "value": ("常见 3000–10000 元起，或按标的 10%–20%（各地指导价，"
                   "行情区间）；符合条件的可申请法律援助"),
         "cite": ""},
        {"label": "对方不履行",
         "value": "生效裁决/判决可依法申请人民法院强制执行", "cite": ""},
    ]

    conditionals: list[str] = []
    est = _estimates(claims, calc_data)
    if not est:
        conditionals.append("月工资与工作年限还没齐，先补齐再算金额。")
    if not slots.get("has_contract"):
        conditionals.append("书面合同还没确认，先按下方「举证体检」确认能证明用工关系的材料。")
    if re.search(r"时效|一年|过期|还能.*(申请|告)", str(question or "")):
        conditionals.append(
            "离职接近一年，先核仲裁时效（劳动关系终止之日起一年内）。"
            f"（《{LAW_ZC}》第二十七条）")
    conditionals.append(
        "仲裁不收费；往返、请假、准备材料与代理费自行承担。")

    # v126「值不值得追」：把「可得」与「要投入的」并排，只给条件式比较，
    # 不给胜率、不替人决定。小额争议是最容易被忽略的一类——追回几百元、
    # 却搭进去几个月的往返与代理费，程序走完了账面上仍是亏的。这句必须
    # 出现在主卡上，而不是藏进折叠附录里。
    amount = est.get("amount") if est else None
    small = amount is not None and amount < 5000
    _labor_cost = _costs.labor_lines(amount)
    if amount is None:
        headline = "可得金额还没算出来"
    elif small:
        headline = f"可得约 {amount:g} 元，划不来"
    else:
        headline = f"可得约 {amount:g} 元，值得追"
    comparison = {
        "has_amount": amount is not None,
        "amount": amount,
        "small_claim": small,
        "headline": headline,
        # v137：时间与费用不再写死「通常以月计」「自行承担」，改由 costs 模块
        # 按三类证据强度产出（法定含条号 / 法定但库外 / 行情区间）。
        # 用户要判断的是「划不划来」，那个判断必须站在 50 元诉讼费、
        # 0 元仲裁费、3–6 个月时间这些具体数字上。
        "lines": [
            (f"本轮核算参考约 {amount:g} 元。")
            if amount is not None else
            ("可得金额 " + BLANK + " 元，按凭证核定。"),
            _labor_cost["time"],
            _labor_cost["money"],
            _labor_cost["proxy"],
            _costs.summary_line(amount, small),
        ],
        "notes": list(_labor_cost["notes"]),
        "disclaimer": "仅供参考，不构成法律意见。",
    }

    return {
        "rows": rows,
        "estimate": est,
        "conditionals": conditionals[:4],
        "comparison": comparison,
        "disclaimer": "仅供参考，不构成法律意见。",
        # v124：挂在 cost_benefit 内部，不新增 decision 顶层键 ——
        # 那四键被 test_network / test_profiles / check_ui 三处钉死。
        "inclination": build_inclination(claims, calc_data, slots, question),
    }


def public_case(c: dict[str, Any]) -> dict[str, Any]:
    """输出给界面的一条案例。保留官方处理结果原文与来源，不改写。"""
    outcome = c.get("outcome") or "settled"
    return {
        "case_id": c.get("case_id", ""),
        "title": c.get("title", ""),
        "batch": c.get("batch", ""),
        "doc_no": c.get("doc_no", ""),
        "authority": c.get("authority", ""),
        "date": c.get("date", ""),
        "outcome": outcome,
        "outcome_label": OUTCOME_LABELS.get(outcome, "其他"),
        "outcome_quote": c.get("outcome_quote", ""),
        "key_point": c.get("key_point", ""),
        "articles": list(c.get("articles") or []),
        "source_url": c.get("source_url", ""),
        "note": c.get("note", ""),
    }


def build_decision(question: str = "", *, stance: str = "employee",
                   slots: dict | None = None, claims: dict | None = None,
                   calc_data: dict | None = None,
                   cases: list[dict] | None = None,
                   limit: int = 4,
                   with_external: bool = False,
                   external_limit: int = 4) -> dict[str, Any]:
    """决策卡总入口。extras / 独立接口 / MCP 三处共用这一个纯函数。

    `with_external` 默认 False：判例层默认是离线的，只有 HTTP 接口被显式
    带了 external=1（或前端点了「联网补充线索」）才真去联网。
    MCP 服务不传这个参数，因此它「只读、离线」的承诺不受影响。
    外部结果放在 scope.external —— 顶层四键必须保持不变，界面和回归
    都按这四键做结构断言。
    """
    if stance not in ("employee", "employer", "agent", "explore"):
        stance = "employee"
    # agent / explore 不是「申请人」，按劳动者视角给参考最稳妥，
    # 但 scope 里如实写明立场，不假装已经知道用户是谁。
    applicant = "employer" if stance == "employer" else "employee"
    matches = search_cases(question, stance=stance, applicant=applicant,
                           limit=limit, cases=cases)
    dist = outcome_distribution(matches)
    scope = {
        "stance": stance,
        "applicant": applicant,
        "topics": sorted(topics_of(question)),
        "source": source_policy(),
        "external": (external_hints(question, external_limit)
                     if with_external else
                     {"enabled": False, "items": [], "status": "disabled",
                      "note": "未请求外部检索；本案参考全部来自本地已核验的判例库。"}),
    }
    return {
        "scope": scope,
        "similar": {
            "distribution": dist,
            "items": [public_case(c) for c in matches],
            "note": dist["note"],
        },
        "cost_benefit": build_cost_benefit(claims, calc_data, slots, question),
        "policy": {
            "no_win_rate": "本系统不提供胜诉率或成功概率。",
            "cite_check": "所有条号均已回本地法条库核验，可点开查看原文。",
        },
    }


def cited_articles() -> list[str]:
    return list(CITES)


def local_case_articles() -> list[tuple[str, str]]:
    """判例库里登记的全部结构化条号，供测试回库核验。"""
    out: list[tuple[str, str]] = []
    for c in load_cases():
        for a in c.get("articles") or []:
            law, _, art = str(a).partition("#")
            if law and art:
                out.append((law, art))
    return out
