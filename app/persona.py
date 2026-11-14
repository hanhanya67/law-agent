"""显式立场 + 语气自适应表达 —— 只调「怎么说」，绝不改「说什么」。

项目里已经有九处「不许猜」的设计（读图三道闸门、verdict 的 unknown、
条号回库核验、零命中拒答…）。凭语气悄悄认定「你是职工还是 HR」再改答案，
会把这套立身之本自己拆掉：同样一句「公司不给加班费怎么处理」，职工要的是
怎么要回来，单位 HR 要的是怎么合规，方向正好相反，而措辞是最不可靠的信号。

所以这里的规矩是：
  * **立场只能显式给**（用户选，或由已有的 ask_user 工具问一句），
    模块里连「从语气推断立场」的入口都不提供 —— 没有入口就不会被误用；
  * 语气只产出**表达建议**，且默认不生效，必须用户确认过才应用；
  * 表达层拿到的配置里不含任何答案正文，verdict / 金额 / 条号 / 引用
    一个字都不经过这个模块。这条由 test_persona 逐组合断言守着。
"""
from __future__ import annotations

import re
from typing import Any

STANCES: tuple[dict[str, str], ...] = (
    {"value": "employee", "label": "我是职工", "hint": "以劳动者为申请人视角给参考"},
    {"value": "employer", "label": "我是单位 HR", "hint": "以用人单位视角看合规与风险"},
    {"value": "agent", "label": "我是代理人", "hint": "代他人咨询，仍按劳动者视角给参考"},
    {"value": "explore", "label": "只是了解", "hint": "不代入任何一方，只看规则与案例"},
)
# 立场白名单取所有内核的并集：接入内核的立场值（buyer / party_a …）也要能
# 存进 user_facts，否则前端在消费/合同内核下一点「保存立场」就被 400 拒。
# 劳动法原有的四个值一个不少，所以 v118 的行为完全不变。
from .profiles import PROFILES as _PROFILES  # noqa: E402
_STANCE_VALUES = tuple(dict.fromkeys(
    s["value"] for p in _PROFILES.values() for s in p.stances))

# 口语 / 情绪 / 急迫：这类提问适合先给结论和大白话，条号细节往后放。
_PLAIN_KEYS = ("怎么办", "咋办", "气死", "凭什么", "欺负", "救命", "急", "能不能要回",
               "老板不给", "不给钱", "被坑", "求求", "到底", "说人话", "听不懂")
# 法条术语：提问者显然能读条号，可以保持标准密度。
_LEGAL_KEYS = ("第", "条", "劳动合同法", "劳动法", "仲裁时效", "二倍工资", "双倍工资",
               "经济补偿", "赔偿金", "举证责任", "司法解释", "条之规定", "依据")


def normalize_stance(raw: Any) -> str | None:
    """只认白名单里的显式取值。空值、拼错、模型瞎编的一律返回 None，
    不「就近猜一个」—— 猜错立场的代价是把整套建议方向反过来。"""
    s = str(raw or "").strip().lower()
    return s if s in _STANCE_VALUES else None


def detect_tone(question: str) -> dict[str, Any]:
    """识别表达偏好，输出的是**建议**不是身份。

    这里刻意不叫 detect_identity：语气能反映「想看多深」，反映不了
    「你是谁」。signals 留着给界面解释为什么给了这个建议，用户不认同
    一键就能关掉。
    """
    t = str(question or "")
    plain = [k for k in _PLAIN_KEYS if k in t]
    legal = [k for k in _LEGAL_KEYS if k in t]
    # 「第X条」这种才算真的在读法条；单字「条」可能是「条子」「条件」。
    has_article_no = bool(re.search(r"第[一二三四五六七八九十百零〇\d]+条", t))
    legal = [k for k in legal if k not in ("第", "条")] + (["第X条"] if has_article_no else [])
    suggested = "plain" if len(plain) > len(legal) else "standard"
    return {
        "suggested": suggested,
        "signals": (plain or legal)[:4],
        "suggested_only": True,
        "note": "依据提问用词推测的表达偏好，与身份、立场无关；默认不自动应用。",
    }


def next_steps(stance: str | None) -> list[str]:
    """立场唯一真正影响的内容：下一步该看什么。不给法律定性、不给金额。"""
    if stance == "employer":
        return [
            "核对用工台账、考勤与工资支付记录是否完整（争议证据多在单位掌握）",
            "检查规章制度中工时、加班审批、解除条件等条款是否与强制性规定冲突",
            "涉及解除或调岗的，确认是否已履行协商、告知与法定程序",
        ]
    if stance == "explore":
        return [
            "先看法条原文与下方典型案例的处理结果，再决定代入哪一方",
            "留意「部分支持」的案件：请求金额与最终支持金额常有差距",
        ]
    return [
        "先按「举证体检」补齐能证明用工关系与期间的材料",
        "确认仲裁时效起算日，必要时先提交申请保住权利",
        "金额与请求事项以本人核定为准，本系统不代为填写申请书",
    ]


def build_persona(question: str = "", *, stance: Any = None,
                  tone_mode: Any = None, confirmed: bool = False,
                  facts: dict | None = None) -> dict[str, Any]:
    """组装画像。facts 是长期记忆里读出的用户显式偏好，不是推断结果。

    confirmed=False 时，哪怕语气信号再强也保持 standard —— 「自动替用户
    切换表达方式」看起来贴心，实际是又一次拿弱信号改输出；必须让人点过。
    """
    st = normalize_stance(stance) or normalize_stance((facts or {}).get("stance"))
    tone = detect_tone(question)
    want = str(tone_mode or "").strip().lower()
    applied = bool(confirmed and want in ("plain", "standard"))
    mode = want if applied else "standard"
    return {
        "stance": st,
        "stance_source": "explicit" if st else "unset",
        "stance_options": [dict(s) for s in STANCES],
        "tone": dict(tone, applied=applied, requested=want or None),
        "mode": mode,
        "next_steps": next_steps(st),
        "note": "系统不会根据语气判断你的身份。立场由你选择；未选择时按职工视角给参考，"
                "并在这里如实标注。",
        "hard_boundary": "画像只影响表达与「下一步看什么」，"
                         "不改结论、金额、条号与引用，也不写进任何文书。",
    }


def presentation(persona: dict | None) -> dict[str, Any]:
    """给前端的表达配置。**不含答案正文**，也没有任何能改文字的字段。

    plain 模式只是把「依据」段折叠、把「结论」段提前显示；答案字符串
    本身原样传给渲染层。测试会断言这一点在任何 stance/mode 组合下都成立。
    """
    p = persona or {}
    plain = p.get("mode") == "plain"
    return {
        "mode": "plain" if plain else "standard",
        "fold_sections": ["依据"] if plain else [],
        "show_first": "结论",
        "badge": "简洁表述（可关闭）" if plain else "标准表述",
        "answer_unchanged": True,
        "cite_density": "collapsed" if plain else "normal",
    }
