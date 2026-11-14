# -*- coding: utf-8 -*-
"""全部数据结构。状态机、工具返回、证据、槽位都在这里定义。

一个原则：能结构化的一律结构化。Agent 的可靠性来自"每一步都有明确的
类型契约"，而不是靠 Prompt 里多写两句"请严格按格式输出"。
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

SLOT_LABELS = {
    "region": "所在地区",
    "employment_type": "用工形式",
    "has_contract": "是否签书面合同",
    "start_date": "入职时间",
    "end_date": "离职时间",
    "monthly_wage": "月工资",
    "leave_reason": "离职方式",
    "social_insurance": "社保缴纳情况",
}

# 追问优先级：离职方式决定请求权基础，月薪决定金额，其余可后补
SLOT_PRIORITY = ["leave_reason", "monthly_wage", "start_date", "region"]


class CaseSlots(BaseModel):
    """案情要素。这是本项目记忆层的核心：把对话信息抽成可计算字段。"""

    region: str | None = None
    employment_type: str | None = None
    has_contract: bool | None = None
    start_date: str | None = None
    end_date: str | None = None
    monthly_wage: int | None = None
    leave_reason: str | None = None
    social_insurance: str | None = None

    def known(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}

    def brief(self) -> str:
        """拼成一小段给模型看的文字，替代把整段历史对话塞进上下文。"""
        known = self.known()
        if not known:
            return "（暂无已知案情要素）"
        parts = [f"{SLOT_LABELS.get(k, k)}={v}" for k, v in known.items()]
        return "；".join(parts)

    def merge(self, patch: dict[str, Any]) -> list[str]:
        """合并新抽取到的槽位，返回真正发生变化的字段名。"""
        changed: list[str] = []
        for k, v in patch.items():
            if v is None or k not in type(self).model_fields:
                continue
            if getattr(self, k) != v:
                setattr(self, k, v)
                changed.append(k)
        return changed

    def missing_critical(self) -> list[str]:
        """按优先级返回仍缺失、且会显著影响结论的槽位。"""
        return [k for k in SLOT_PRIORITY if getattr(self, k) is None]


class Verdict(BaseModel):
    """定性结论。这是「先回答正常还是不正常」那一步的结构化载体。

    为什么单独建模而不是在生成时随手写一句话：
    用户问「正常吗」要的是一个判断，不是条文摘抄。判断必须能独立自检
    （它引用的是哪条、理由是什么），否则它只是文案装饰。

    kind:
      illegal     不符合规定 / 不正常
      claimable   行为违法且用户可主张具体权利（二倍工资、加付赔偿金等）
      conditional 结论取决于未确定的案情（例如「实习期」还是在试用期）
      definition  问的是计算口径或定义，不存在合法与否（「奖金算不算工资」）
      legal       符合规定
      unknown     无法判断
    """

    kind: Literal["illegal", "claimable", "conditional",
                  "definition", "legal", "unknown"] = "unknown"
    # 条文能回答用户多少。这是定性之前的前置判断：
    # 「有证据」和「证据能回答问题」是两件事——实测无答案题（产假天数、
    # 工伤赔偿）检索照样能命中字面相近的条文，模型拿到就当依据硬答，
    # 14 条里错了 10 条。所以把相关性单独抠出来，none 直接拒答。
    #
    # 为什么从布尔改成三档（v19）：
    #   「欠我的工资还能拿到吗」这类问题，库里有条文能答一半（追讨欠薪的投诉
    #   与支付令途径），另一半答不了（破产清算的清偿顺序）。布尔值只有「答」
    #   和「不答」两个出口，于是整题拒答，用户连那半个有用的事实都拿不到。
    #   partial 的语义是「答能答的、明说答不了的」，**不是拒答**。
    coverage: Literal["full", "partial", "none"] = "full"
    # partial 时才有值：哪一问能答（结论）、哪一问答不了（缺什么）。
    answered_part: str = ""
    missing_part: str = ""
    label: str = "无法判断"          # 对用户说的话：「不正常」「可以主张」
    legal: str = ""                  # 法律定性：「不符合《劳动合同法》第二十条规定」
    reason: str = ""                 # 一句话理由，接在 label 后面
    basis: list[str] = Field(default_factory=list)   # 支撑该定性的条文 cite()
    # 只在 coverage=none 时有值：这件事**由什么调整**、而本库缺了哪一块。
    # 为什么需要它：拒答本身不是缺陷（库里确实没有的东西不能编），但拒答
    # 不交代缺什么就是缺陷。实测「厂子倒闭了，欠我的工资还能拿到吗」，
    # 库里有第四十四条（破产时合同终止），却只回了一句「没有直接规定」，
    # 用户拿不到任何可执行的信息，也不知道该去查哪部法。
    # 约定：只写法律名称或规则类别，不写条号——条号无法在本库核验，写出来
    # 就是一处没有出处的引用。
    gap: str = ""

    @property
    def relevant(self) -> bool:
        """下游只关心「能不能答」。保留这个名字，各处判断条件不用跟着改。"""
        return self.coverage != "none"

    def head(self) -> str:
        """渲染成结论段的第一行。"""
        # 口径型问题（「奖金算不算工资」）没有合法与否可言，
        # 硬套「不正常」会让人觉得系统在装。直接给口径。
        if self.kind == "definition":
            return self.reason
        if self.kind == "unknown":
            return self.label + ("。" if self.label else "")
        tail = f"——{self.reason}" if self.reason else ""
        return f"{self.label}{tail}"


class Evidence(BaseModel):
    """一条证据 = 一段法条原文。自检、溯源都靠它。"""

    article_id: str
    law_name: str
    article_no: str
    article_no_cn: str
    content: str
    score: float = 0.0
    # recall = 上一轮（同一案子）已经查过、这次带进来的。它可能是本问的答案，
    # 也可能只是背景——所以必须让模型看见来源，不能混进本次检索结果里充数。
    source: Literal["direct", "precise", "citation_expand", "recall"] = "direct"
    status: str = "现行有效"
    # 整部法仍有效、但本条某款已被后法废止时的说明。
    # 例：解释（一）第三十二条整体有效，第一款已于 2025-09-01 废止。
    repealed_note: str = ""

    def cite(self) -> str:
        return f"《{self.law_name}》{self.article_no_cn}"


class Step(BaseModel):
    """工作记忆的一步。前端轨迹时间轴直接渲染这个。"""

    index: int
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    elapsed_ms: int = 0
    error: str | None = None
    # 工具返回的结构化数据。生成阶段要用（例如计算结果的两种情形），
    # 只留 summary 字符串的话，后面得靠正则去反解，非常脆。
    data: dict[str, Any] = Field(default_factory=dict)
    # 这一步拿到的条文 id。生成时用它决定「哪些条文该进依据」：
    # 多跳问题的答案往往分散在不同法条里，只展示分数最高的那条会漏掉另一半。
    evidence_ids: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    steps_left: int = 8
    started_at: float = Field(default_factory=time.time)

    def elapsed(self) -> float:
        return time.time() - self.started_at


class AgentState(BaseModel):
    session_id: str
    question: str
    intent: str = "unknown"
    # 题型决定答案骨架：判题型要有结论、主张型要有清单、计算型要先给数字。
    # 之前所有题型共用「根据某条规定：……」一种写法，这就是答案机械化的根因。
    question_kind: str = "lookup"     # judge / claim / calc / procedure / lookup
    verdict: Verdict | None = None
    query_plan: list[str] = Field(default_factory=list)
    slots: CaseSlots = Field(default_factory=CaseSlots)
    # v138：接入内核自己的案情要素（消费的「商品/金额/问题」、婚家的
    # 「争议焦点/财产/子女」…）。为什么不复用 `slots`：`CaseSlots` 是
    # 劳动法写死的八项，pydantic 校验会把不认识的键静默丢掉，而且
    # 「月工资」会被商品价格误命中。两套字段各存一份，字段表在内核配置里。
    # 劳动法内核下它恒为空，所有老链路读到的还是 `slots`。
    dslots: dict[str, str] = Field(default_factory=dict)
    # v138：上一轮已经问过的要素（字段名）。由 agent 从「上一条助手消息里
    # 出现过哪几项追问原文」反推出来，不落库。
    # 为什么需要「问过就别再问」这道闸：用户答「已经花了三万二」，如果规则
    # 没抽出来，下一轮就会把同一个问题再问一遍——而「问了又问」是用户最先
    # 崩塌的地方（v133/v135 修的都是这一类）。宁可这一轮不追问，也不能重复。
    asked_slots: list[str] = Field(default_factory=list)
    scratchpad: list[Step] = Field(default_factory=list)
    evidences: list[Evidence] = Field(default_factory=list)
    draft: str = ""
    citations: list[str] = Field(default_factory=list)
    status: Literal["running", "answered", "refused", "failed"] = "running"
    note: str = ""
    # 待追问。工具 ask_user 触发时记在这里，**不打断流程**——
    # 早先它直接 return，把已经检索到的证据全扔了，用户只拿到一句反问。
    # 正确做法是先给条件性结论，再把问题附在末尾。
    pending_question: str = ""
    budget: Budget = Field(default_factory=Budget)
    # v137 任务级记忆：由 agent 从库里读进来，只作生成侧的背景。
    # 不进 verdict、不进 evidence —— 结论的依据只能是条文与已核算的事实。
    task_goal: str = ""
    task_summary: str = ""
    # 最近几轮原话（「用户：…」「助手：…」），只给生成侧做指代消解用。
    task_recent: list[str] = Field(default_factory=list)
    # 用户自己说过的事实（城市 / 职业 / 单位性质），一行串好直接给生成侧。
    profile_facts: str = ""

    def case_brief(self) -> str:
        """当前内核「已经知道什么」，一行串好给提示词用。

        两个来源拼在一起，而不是二选一：
          · `dslots`  — 接入内核自己的要素（v138）；
          · `slots`   — 劳动法那八项。接入内核下它只会有「所在地区」这一项
                        （个人档案里种进来的），但那也是用户说过的事实，
                        丢掉就等于又问他一遍在哪个城市。
        劳动法内核仍然只读 `slots`，逐字与 v137 一致。
        """
        from . import dslots as _d
        from . import profiles as _profiles
        p = _profiles.get()
        if p.id == "labor":
            return self.slots.brief()
        text = _d.brief(p, self.dslots)
        labels = _d.spec(p)
        extra = [f"{SLOT_LABELS.get(k, k)}={v}" for k, v in self.slots.known().items()
                 if k not in labels and str(v).strip()]
        if not extra:
            return text
        return "；".join(extra) if text == _d.EMPTY else text + "；" + "；".join(extra)

    def has_evidence(self, article_id: str) -> bool:
        return any(e.article_id == article_id for e in self.evidences)

    def add_evidence(self, ev: list[Evidence]) -> int:
        """去重后追加，返回真正新增的条数（无进展检测依赖这个）。"""
        added = 0
        for e in ev:
            if not self.has_evidence(e.article_id):
                self.evidences.append(e)
                added += 1
        return added


class ToolResult(BaseModel):
    """工具统一返回。注意 error 不抛异常，而是作为观察结果回灌给模型。"""

    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    error: str | None = None
    need_user_input: bool = False
    question: str | None = None
    elapsed_ms: int = 0


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """模型的一步决策：要么继续调工具，要么收工。"""

    thought: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    is_final: bool = False


class VerifyIssue(BaseModel):
    conclusion: str
    reason: str


class VerifyResult(BaseModel):
    passed: bool = True
    issues: list[VerifyIssue] = Field(default_factory=list)
