# -*- coding: utf-8 -*-
"""
Agent 主循环。整个项目的心脏，也是唯一真正的原创部分。

四级规划（这是"规划"这个要素的全部内容）：
  L0 意图路由  这一步该不该查、查哪一种
  L1 查询分解  一个问题拆成几次检索（多跳）
  L2 迭代检索  查不到就改写重查，最多 3 轮（本轮由模型自行决定，硬预算兜底）
  L3 答案自检  答完逐条核对引用是否成立，不成立就标注或拒答

终止条件全部写死，不交给模型判断：
  步数用完 / 模型主动收工 / 连续两轮无新证据 / 超时 / 上下文条数上限
模型在循环里天然乐观，没有硬预算就必然出现某次演示卡三分钟。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from . import extras as _extras
from . import dslots as _dslots
from . import facts as _facts
from . import persona as _persona
from . import profiles as _profiles
from .cite import extract_cites, find_article, law_same, norm_law_key  # noqa: F401
from .cite import resolve_cites
from .config import MAX_SECONDS, MAX_STEPS
from .llm import PLAN_CAP, LLM, needs_calc
from .memory import MemoryStore
from .retrieval import LawIndex
from .schemas import AgentState, Budget, CaseSlots, Evidence, Step
from .tools import ToolContext, all_schemas, dispatch
from .verdict import classify_kind

Emit = Callable[[str, dict], None]

# 免责声明的固定开头。追问要插在它前面（见 stop 前的 pending_question 分支）。
DISCLAIMER_MARK = "本回答仅供参考，不构成法律意见"

# 追问自带的引导语：「为了给到准确结论，想先确认一下：」。只取冒号前不超过
# 30 字的那种，避免把「您月工资大概多少？」里正常出现的冒号也当引导语。
_ASK_LEAD_RE = re.compile(r"^[^：:\n]{0,30}[：:]\s*(.+)$", re.S)


def strip_ask_lead(text: str) -> str:
    """剥掉追问自带的引导语，只留问题本身。

    llm.ask() 返回的是整句「为了给到准确结论，想先确认一下：您月工资大概多少？」。
    上层还要再加一句「另外，为了给你更准确的结论，还需要确认：」，两层叠在一起
    就是「还需要确认：为了给到准确结论，想先确认一下：……」——两句引导语抢着
    说话。剥一层就够，留哪一层由调用方决定。
    """
    s = (text or "").strip()
    m = _ASK_LEAD_RE.match(s)
    return m.group(1).strip() if m else s


def should_run_plan(plan: list[str], question: str, intent: str) -> bool:
    """L1.5 要不要按计划确定性执行检索。

    抽成模块级函数是为了能被单测钉住——这条判断踩过两次坑，
    每次都是"看起来多余的条件，其实在挡一个真实的漏"：

    ① 多计划必须执行。分解出三条子句却只搜一条，是这套架构最容易漏的地方。
    ② 单计划一般不执行，交给 _mock_decide 里 `if not state.evidences` 兜底。
       但 statute_lookup 时兜底会失效：mock_decide 先用 get_article 取回条号，
       证据集非空，这条计划就永远不跑了。h12（问「劳动合同的违约金能不能按
       民法典 585 调低」，计划里那条「不得约定违约金」本该带到劳动合同法 25 条）
       因此 recall 0.0。
    ③ 但 statute_lookup 也不能无条件执行——计划可能就是原句本身，
       拿原句去搜只会带回一堆含「合同法」的无关条文、还吃掉一步预算，
       把 check_validity 挤掉，替代条文 585 反而丢了（v01/v06 实测掉点）。
       所以加第三个前提：计划必须与问句不同，即真的发生了改写。
    """
    if len(plan) > 1:
        return True
    return intent == "statute_lookup" and plan != [question]

_REFUSE_OUT = ("这个系统只处理劳动法相关的问题——劳动合同、工资、社保、"
               "解除与终止、经济补偿这些。您问的方向不在范围内。")
_REFUSE_EMPTY = ("您提到的问题，在当前收录的条文中未找到直接依据。\n\n"
                 "我没有凭印象补一个条文号给您，因为那可能把您引向错误的方向。\n\n"
                 "建议：把具体情形说得再细一点（是否签了合同、干了多久、"
                 "怎么离职的、月工资大概多少），或者直接咨询当地劳动仲裁机构。")


def _empty_text() -> str:
    """「一条条文都没检索到」时说什么。按内核换掉括号里那句提示。

    为什么不能共用一段：这段话的后半句是「把具体情形说细一点（是否签了合同、
    干了多久、怎么离职的、月工资大概多少）」——婚家分区检索一次没命中，用户
    又会被问一遍离职方式，和 clarification 那条追问是同一个病。劳动法内核
    留空 = 逐字返回原常量。
    """
    hint = (_profiles.get().empty_hint or "").strip()
    if not hint:
        return _REFUSE_EMPTY
    return ("您提到的问题，在当前收录的条文中未找到直接依据。\n\n"
            "我没有凭印象补一个条文号给您，因为那可能把您引向错误的方向。\n\n"
            f"建议：把具体情形说得再细一点（{hint}），或者直接咨询执业律师。")

# 有检索结果，但条文管不到用户问的事。这和「没检索到」是两种不同的拒答，
# 文案必须区分——前者要解释「我看到了相近条文，但它不适用」，
# 否则用户会以为系统坏了。
#
# 这段文案曾经是写死的一句话，里面列了一串话题：「如果您问的是劳动合同、
# 工资、加班费、社保、解除与经济补偿，换一种说法再问一次」。实测撞得很惨：
# 用户问「厂子倒闭了，欠我的工资还能拿到吗」——问的就是工资——系统回「如果
# 您问的是工资……换一种说法再问一次」。同一句话里既没回答用户，又把用户的
# 问题当成了没问过的问题。
#
# 根因是这段文案在替系统假设「用户问的大概不在这些话题里」，可系统并不知道
# 用户问了什么方向。现在改成只陈述系统**确实知道**的两件事：命中了几条条文、
# 缺的是哪一块（judge 给出的 gap）。不确定的一律不写。
def _irrelevant_text(state: AgentState) -> str:
    n = len(state.evidences)
    top = state.evidences[0] if state.evidences else None
    hit = f"检索命中了 {n} 条条文" if n else "检索有结果"
    if top is not None:
        hit += f"（比如{top.cite()}）"
    parts = [
        "您问的这件事，在当前收录的条文里没有直接规定。",
        f"{hit}，但它们规范的不是这件事——我没有拿它硬套，那样给出的结论会误导您。",
    ]
    if state.verdict is not None and state.verdict.gap:
        parts.append(f"您问的这部分，{state.verdict.gap}。")
    # 最后这句原来写死「当地劳动人事争议仲裁委员会（12333）」。婚家分区答不上来时
    # 让用户去打 12333 问劳动仲裁，等于把他送到错的门——他的问题在法院。
    if (_profiles.get().empty_hint or "").strip():
        parts.append("建议：把具体情形说得再细一点，或直接咨询执业律师。")
    else:
        parts.append("建议：把具体情形说得再细一点，或直接咨询当地劳动人事争议仲裁委员会"
                     "（12333 也能问到当地口径）。")
    return "\n\n".join(parts)

_MAX_EVIDENCE = 20   # 多路检索第一轮就能拿到 12 条，卡 14 会让后续检索与计算没机会跑


def _call_key(call) -> str:
    """一次工具调用的指纹：工具名 + 规范化参数。

    参数按 key 排序，否则 {'query':..,'topk':6} 和 {'topk':6,'query':..}
    会被当成两次不同的调用，去重就漏了。json.dumps 而不是 str(dict)，
    是为了让嵌套结构（list/dict 参数）也有稳定表示。
    """
    try:
        body = json.dumps(call.args or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        body = repr(call.args)          # 参数里有不可序列化的值时，退化成 repr
    return f"{call.name}|{body}"

# 回放答案的提示语。写在模块级而不是 run() 里，是为了让测试能直接比对文案，
# 也为了让「什么算回放」只有一处定义。
_DEGRADED_TEXT = ("本轮答案来自离线回放：模型接口这次没有调用成功，内容是本地模板"
                  "加检索到的条文拼出来的，不是模型实时生成。右上角状态里有失败原因。")


def degraded_note(llm: object) -> str:
    """本轮答案是不是回放出来的。正常走在线时返回空串。

    为什么要有这句话：402 之后 `llm.online` 变 False，后续每一轮都由模板作答，
    但流向用户的事件里一个提示都没有——答案看着和平时一样有结论、有依据，
    用户分不出来。之前「你好为什么没反应」那类困惑就是这么来的：
    表现出来像「模型不会答」，实际是「根本没走到模型」。
    """
    if getattr(llm, "online", True):
        return ""
    return _DEGRADED_TEXT


# 引用抽取与条号解析统一在 app/cite.py —— 产品层和评测层必须共用同一套，
# 否则会出现「评测按严标准抽出 122 处引用说全部合法，产品层只核验了其中一部分」。
# 这里保留 _find_article 这个旧名做别名，下游（tools / eval）不用一次性全改。
_find_article = find_article


def _backfill_citations(state: AgentState, index: LawIndex) -> tuple[list[str], list[str]]:
    """把答案引用到、但本轮证据里没有的条文回库补取。

    生成的答案里经常出现证据之外的真条文：模型记得没错，只是这次检索没捞到。
    对这种情形，判「无依据引用」既冤枉也没用——正确做法是把它取回来核对，
    让它成为可溯源的证据。取不回来的，才是真幻觉。

    返回 (补进来的 id, 库里也没有的引用文本)。

    抽取用的是 app/cite.extract_cites，与评测层完全同一套。改动前这里是一个
    只认书名号的 `_CITE_RE`，而评测层认承前省略、裸法律名、存在性回退——
    结果「《劳动合同法》第四十四条…；第四十六条」这种写法，评测算出两处引用
    并算作命中，产品层却只核验了 44 条，46 条根本没进证据池。
    指标比产品聪明，方向是危险的，所以合到一处。
    """
    have = {e.article_id for e in state.evidences}
    extra, phantom = resolve_cites(state.draft, index, have)
    filled: list[str] = []
    for art in extra:
        state.evidences.append(Evidence(
            article_id=art["id"], law_name=art["law_name"],
            article_no=art["article_no"], article_no_cn=art["article_no_cn"],
            content=art["content"], score=0.0, source="citation_expand",
            status=art.get("status", "现行有效"),
            repealed_note=art.get("repealed_note") or ""))
        filled.append(art["id"])
    return filled, phantom


def _profile_seed(profile: dict | None) -> dict:
    """个人档案里能当案情要素用的字段。

    只取 region 一个。档案里还存着姓名、身份证、单位名称，那些是写申请书
    用的，不进槽位——槽位里的东西会被当成「用户在对话里说过的事实」，
    反到答案里去（比如正文里突然冒出真名和身份证号）。档案是填给文书用的，
    不是填给对话用的。
    """
    if not profile:
        return {}
    region = str(profile.get("region") or "").strip()
    return {"region": region} if region else {}


class AgentRunner:
    def __init__(self, index: LawIndex, llm: LLM, store: MemoryStore):
        self.index = index
        self.llm = llm
        self.store = store

    # ---------------------------------------------------------------- 入口
    def run(self, question: str, session_id: str = "default",
            emit: Emit | None = None, user_id: int | None = None,
            task_id: str | None = None,
            profile: dict | None = None) -> AgentState:
        emit = emit or (lambda e, d: None)
        question = (question or "").strip()
        # user_id 默认 None：命令行和评测脚本不走账号体系，传 None 表示「未归属」，
        # 这样同一个 run() 不用分两套。Web 端由 server 层传真实用户 id。
        # task_id 同理：不传就还是「一次会话一个池子」，传了才会把要素
        # 往任务级合并，跨会话的记忆才连得起来。
        self.store.ensure_session(session_id, user_id, task_id)

        state = AgentState(session_id=session_id, question=question,
                           budget=Budget(steps_left=MAX_STEPS))
        # v137：任务目标与摘要一起带进来。它只走生成侧（见 llm.generate_stream
        # 里对非劳动内核的前缀注入），不进检索、不进定性、不进依据——
        # 「结论只能站在条文与已核算的事实上」这条不因为多了记忆而松。
        if task_id:
            _mem = self.store.task_memory(task_id)
            state.task_goal = _mem.get("goal") or ""
            state.task_summary = _mem.get("summary") or ""
        # 最近几轮原话：只取用户句和助手答案的开头，不整段复制——
        # 助手答案动辄上千字，原样塞进去等于把上下文预算全吃掉，
        # 而这里要解决的只是「那这个呢」里的「这个」指什么。
        _rec = self.store.recent(session_id, 6)
        state.task_recent = [
            ("用户：" if m["role"] == "user" else "助手：")
            + re.sub(r"\s+", " ", str(m["content"] or ""))[:160]
            for m in _rec if str(m["content"] or "").strip()]
        # v137 画像：用户自己说过的事实，一行串好。只读、不改槽位——
        # 槽位是「本案事实」，画像是「这个人」，两者混了就会拿上一个案子的
        # 职业去解释这一案子的事。
        if user_id is not None:
            try:
                _fs = self.store.list_profile_facts(user_id)
                state.profile_facts = "；".join(
                    f"{_facts.FACT_LABELS.get(f['key'], f['key'])}={f['value']}"
                    for f in _fs if f.get("value"))
            except Exception:                   # noqa: BLE001
                state.profile_facts = ""
        disk = self.store.load_slots(session_id)
        state.slots = disk

        # v138：接入内核自己的案情要素。和上面那张 `case_slots` 是两套字段，
        # 只有非劳动法内核才读——劳动法内核读到的永远是空 dict，一行都不碰。
        _pid = _profiles.get().id
        if _pid != "labor":
            state.dslots = self.store.load_domain_slots(session_id, _pid)
            # 上一轮问过哪些要素：判据是「上一条助手消息里出现过某一项的
            # 追问原文」。为什么不单独存一张表：追问原文就写在答案里，
            # 库里已经有这句话了，再存一份就会有不一致的那一天。
            _hist = [str(m["content"] or "") for m in self.store.recent(session_id, 4)
                     if m["role"] == "assistant"]
            _tail = _hist[-1] if _hist else ""
            _p0 = _profiles.get()
            state.asked_slots = [
                k for k in _dslots.priority(_p0)
                if _dslots.ask_for(_p0, k) and _dslots.ask_for(_p0, k) in _tail]

        # 档案打底：用户在「个人档案」里填过所在地区，就不该再在对话里被问
        # 「您在哪个城市」——同一件事问两遍，是最容易被当成「它什么都不记得」
        # 的那种细节。档案是最弱的一层来源：会话里说过的、任务里记下的都能盖掉它。
        seed = _profile_seed(profile)
        if seed:
            base = {**seed, **disk.known()}
            state.slots = CaseSlots(**base)
            if base != disk.known():
                # 落盘：案情档案面板读的是会话级槽位，不写下去会出现
                # 「模型知道我在杭州，档案面板却是空的」。
                self.store.save_slots(session_id, state.slots)

        # 同一任务里新开的会话，要接着用前面记下的案情。只读会话级槽位的话，
        # 用户在任务里说过「2023 年 6 月入职」，点「新对话」再问还得说第四遍——
        # 而任务面板上明写着「多次咨询共享案情要素」。承诺和实际不一致，
        # 比不承诺更糟：用户会以为自己记错了。
        # 用「任务打底、会话覆盖」：会话里的值可能是刚纠正过的，不能被旧值盖掉。
        if task_id:
            inherited = self.store.load_task_slots(task_id).known()
            mine = state.slots.known()
            merged = {**inherited, **mine}
            if merged != mine:
                state.slots = CaseSlots(**merged)
                # 落盘：案情档案面板读的是会话级槽位。不写下去，用户会看到
                # 「对话里明明记得，档案面板却是空的」，然后以为两处对不上。
                self.store.save_slots(session_id, state.slots)

            # 这个案子上一轮查过的条文，带进来当背景。
            # 不带的话，同一个案子问第四遍时它还是从零检索——每轮都要重新
            # 碰一次「这次能不能命中」的运气。
            # 只带 6 条且标成 recall：全量搬会顶到 _MAX_EVIDENCE，本次该查的
            # 新条文反而没机会查。标来源是让模型知道「这是旧的，未必相关」。
            for it in self.store.load_task_evidence(task_id, limit=6):
                try:
                    it = dict(it)
                    it["source"] = "recall"
                    it["score"] = 0.0
                    state.add_evidence([Evidence(**it)])
                except (TypeError, ValueError):
                    continue            # 旧版本存下来的脏数据，跳过就是
        self.store.save_message(session_id, "user", question)

        # 上一轮降级过就先看看冷却期过没过：过了一次性把 online 置回来试一把，
        # 不通会在下面的调用里重新降级。放在这里而不是定时器里，是因为这个
        # 项目没有常驻调度器——没人提问的时候，恢复也没有意义。
        self.llm.maybe_recover()

        # ---- 记忆写入：先抽槽位，再决定后面怎么走 ----
        # 两套字段各存一处：劳动法走 `CaseSlots`（八个固定字段），接入内核走
        # `dslots`（字段表在内核配置里）。**分支在这里显式分开**，而不是让
        # 同一个 dict 流进两种容器——`CaseSlots(**json)` 会把不认识的键静默
        # 丢掉，而「静默丢」是这套系统里最难查的一类故障。
        patch = self.llm.extract_slots(question)
        if _pid == "labor":
            changed = state.slots.merge(patch)
            if changed:
                self.store.save_slots(session_id, state.slots)
            emit("memory", {"slots": state.slots.known(), "changed": changed})
        else:
            _p = _profiles.get()
            changed = _dslots.merge(state.dslots, _p, patch)
            if changed:
                self.store.save_domain_slots(session_id, _pid, state.dslots)
            # labels 一并送出去：前端「记下」那一步原来显示 `problem=过期`，
            # 字段名是英文的。带上中文名之后显示「发现的问题=过期」。
            emit("memory", {"slots": _dslots.known(_p, state.dslots),
                            "changed": changed, "labels": _dslots.spec(_p)})

        # ---- L0 意图路由 ----
        # 寒暄单独走一条路：不进意图分类、不进补充判定、也不进检索。
        # 「问你好却回了上一题答案」就是从「你好」被当成补充信息开始的，
        # 所以在入口处先截住——这条路跟有没有 key、模型在不在线都无关。
        # 放在 classify_intent 之前是为了省一次调用：寒暄不需要判意图。
        if _is_greeting(question):
            state.intent = "greet"
            emit("intent", {"intent": state.intent})
            return self._greet(state, emit)

        state.intent = self.llm.classify_intent(question, state.case_brief())

        # 补充信息识别：本轮只是补事实（"我在杭州，月薪8000"），不是新问题。
        # 这时候应该沿用上一轮的提问重答，而不是把补充当新问题再问一遍。
        # 这一步是记忆层真正产生价值的地方——没有它，多轮对话就是三次独立问答。
        if _looks_like_supplement(question):
            prev = [m["content"] for m in self.store.recent(session_id, 8)
                    if m["role"] == "user" and m["content"] != question]
            if prev:
                state.question = prev[-1]
                state.intent = self.llm.classify_intent(state.question,
                                                        state.case_brief())
                emit("context", {"carried_from": state.question,
                                 "supplement": question})

        # ---- v135：在线的接入内核，入口不再一票否决 ----
        # v133 为了治「问什么都回问是否离职」，把 clarify / out_of_scope
        # 改成了下面直接 return 静态文案。串味治好了，但模型判完意图就没有
        # 第二次机会：用户原问没进检索，L1 分解、L1.5 检索、L2 循环、
        # judge 自检一层都没跑，看到的是「问偏一点就把人推出门」。
        #
        # 接入内核 + 真模型在线时，把这两个意图归一成正常咨询，判定权交给
        # 已经写好的三处证据出口：零召回 → 依据不足（本文件 :480）、
        # judge 判不相关 → 一条都答不了（:501）、部分覆盖 → 答一半 +
        # 【未覆盖】（:511）。真需要追问时由 decide() 在循环里派 ask_user，
        # 走 v135 那个「带用户原问」的贴合追问，而不是入口的四选一。
        #
        # 劳动法内核、以及全部离线回放路径**一字不改**：34 套回归和 v133
        # 的文案断言都钉在这条原链路上。寒暄仍在上面由 _is_greeting 截住。
        if (self.llm.online and _profiles.get().id != "labor"
                and state.intent in ("clarify", "out_of_scope")):
            state.intent = "case_consult"

        emit("intent", {"intent": state.intent})
        # v136：这里原来还有一条 note「（初判「clarify」，先按原问检索，再由证据
        # 决定怎么答）」。两个理由删掉它：
        #  1) 它是**在 token 之前**发的，前端 `note` 分支把它当纯文本节点 append
        #     进答案区，随后 `ctx.answer.textContent += token` 会把这句话卷进正文
        #     开头——用户看到的是「（初判…）【结论】…」粘成一行，复制/导出/反馈
        #     也把它当正文带走。
        #  2) 它解释的是内部路由，而轨迹区已经把真实路线（`意图` 那一步）显示出来，
        #     说两遍没有任何信息增量。用户对这类「解释自己怎么想」的句子容忍度为零。

        # 题型决定答案骨架。判题型要先给结论，主张型要先给权利清单，
        # 计算型要先给数字——所有题型共用一个模板，就是"机械化"的根源。
        state.question_kind = classify_kind(state.question)

        if state.intent == "out_of_scope":
            # 拒答文案也要按内核说。原来一律是「本系统只处理劳动法」，婚家
            # 分区里被拒一次，等于系统自己否认了这个分区存在。
            return self._refuse(state,
                                (_profiles.get().refuse_text or "").strip()
                                or _REFUSE_OUT, emit)

        if state.intent == "clarify":
            # v133 起这里按内核问；v138 起接入内核算得清「缺哪一项」了。
            #
            # 为什么不能再用 `state.slots.missing_critical()`：那是劳动法那
            # 四项（离职方式 / 月工资 / 入职时间 / 地区），「对方出轨」会被
            # 问成「是公司辞退、自己辞职，还是被迫离职」。
            # 现在接入内核拿自己的字段表算缺失项：婚家第一句问「您这次想
            # 解决的是哪一件事」，消费第一句问「商品出了什么问题」。
            # 要素都问全了（`missing` 为空）才回落内核那句静态澄清问句。
            p = _profiles.get()
            if p.id != "labor":
                # 问过的不再问：上一轮已经问过「您这次想解决的是哪一件事」，
                # 这一轮还是没答上来，就换个要素问，而不是原样重放那句。
                miss = [k for k in _dslots.missing(p, state.dslots)
                        if k not in state.asked_slots]
                q = self.llm.ask(miss[:1], question, known=state.case_brief())
                return self._ask(state, q, emit)
            hint = (p.clarify_question or "").strip()
            if hint:
                return self._ask(state, hint, emit)
            q = self.llm.ask(state.slots.missing_critical() or ["region"], question)
            return self._ask(state, q, emit)

        # 注意：这里**不**因为"要算钱但缺槽位"就直接拦下来追问。
        # 用户的问法常常是"按什么标准算"，那是在知识性问题，先检索再说话。
        # 真正的追问交给 Agent 在循环里决策（见 llm._mock_decide / 工具 ask_user），
        # 这样"问标准"能拿到答案，"要算我的钱"才会被追问。

        # ---- L1 查询分解（用 state.question，补充信息场景下它已被换成上一问）----
        state.query_plan = self.llm.decompose(state.question, state.case_brief())
        if len(state.query_plan) > 1:
            emit("plan", {"queries": state.query_plan})

        # ---- L1.5 按计划确定性执行检索 ----
        # 分解出三条子句却只搜一条，是这套架构最容易漏的地方：把执行权交给 LLM，
        # 它常常只挑最有把握的一两条，剩下的静默丢弃，而且不会说明。
        # 实测「公司以我不能胜任为由辞退，除了给补偿还要提前通知吗」的分解里，
        # 「不能胜任工作 解除劳动合同 情形」这条能命中《劳动合同法》第四十六条，
        # 但那轮检索根本没执行它 —— 四道多跳题（m03/m04/m07/m09）的召回率
        # 就一起卡在同一处，而证据池里明明已经有别的 14 条。
        # 所以计划里每条子句都在这里执行一次：这一步不交给模型判断，是确定的。
        # 主循环仍然保留，模型可以在此基础上继续改写、补充、精查。
        ctx = ToolContext(index=self.index, state=state, profile=profile or {})
        if should_run_plan(state.query_plan, state.question, state.intent):
            for sub in state.query_plan[:PLAN_CAP]:
                if len(state.evidences) >= _MAX_EVIDENCE:
                    break
                emit("tool_start", {"step": len(state.scratchpad) + 1,
                                    "tool": "search_statute",
                                    "args": {"query": sub, "topk": 6}})
                res = dispatch("search_statute", {"query": sub, "topk": 6}, ctx)
                step = Step(index=len(state.scratchpad) + 1, tool="search_statute",
                            args={"query": sub, "topk": 6},
                            summary=res.summary,
                            elapsed_ms=res.elapsed_ms, error=res.error,
                            data=res.data,
                            evidence_ids=[e.article_id for e in res.evidence])
                state.scratchpad.append(step)
                self.store.save_trace(session_id,
                                      self.store.count_messages(session_id) // 2, step)
                emit("tool_end", {"step": step.index, "tool": "search_statute",
                                  "summary": res.summary, "ms": res.elapsed_ms,
                                  "error": res.error,
                                  "evidence": [e.article_id for e in res.evidence]})
                if res.evidence:
                    state.add_evidence(res.evidence)

        # ---- L2 迭代检索：主循环 ----
        no_progress = 0
        turn = self.store.count_messages(session_id) // 2
        # 同参数调用的指纹。模型会在循环里把同一批检索式重复发好几轮——
        # 真 key 实测：两条不同问题都是「3 条查询 × 3 轮 = 9 步」，步数预算
        # 全烧在重复上，calculate_compensation / check_validity 一次都轮不到。
        # 提示词那边已经把「已检索过什么」递给模型（见 _describe_searched），
        # 这里是兜底：模型仍然重复时不执行、不记步、不发事件。
        seen_calls: set[str] = set()

        while state.budget.steps_left > 0 and state.budget.elapsed() < MAX_SECONDS:
            decision = self.llm.decide(state, all_schemas())
            if decision.is_final or not decision.tool_calls:
                break

            progressed = False
            ran_this_round = 0
            for call in decision.tool_calls[:3]:      # 单轮最多并发 3 个，防失控
                # 已经挂起一个追问就别再问了：模型有时会在同一轮里连问两三个，
                # 用户被连环追问会直接跑掉。
                if call.name == "ask_user" and state.pending_question:
                    continue
                ck = _call_key(call)
                if ck in seen_calls:
                    # 完全静默：不发 tool_start。发了轨迹里会多出一行「跳过」，
                    # 而用户要的是「没有这 6 步」，不是「有 6 步被标成跳过」。
                    continue
                seen_calls.add(ck)
                ran_this_round += 1
                emit("tool_start", {"step": len(state.scratchpad) + 1,
                                    "tool": call.name, "args": call.args})
                res = dispatch(call.name, call.args, ctx)

                step = Step(index=len(state.scratchpad) + 1, tool=call.name,
                            args=call.args, summary=res.summary,
                            elapsed_ms=res.elapsed_ms, error=res.error,
                            data=res.data,
                            evidence_ids=[e.article_id for e in res.evidence])
                state.scratchpad.append(step)
                self.store.save_trace(session_id, turn, step)
                emit("tool_end", {"step": step.index, "tool": call.name,
                                  "summary": res.summary, "ms": res.elapsed_ms,
                                  "error": res.error,
                                  "evidence": [e.article_id for e in res.evidence]})

                if res.evidence:
                    progressed = state.add_evidence(res.evidence) > 0 or progressed

                # 工具要求追问：**不中断**，把问题挂起，先把手上的证据用起来。
                # 早先这里直接 return self._ask(...)，结果是：已经查到 3 条条文，
                # 因为一个「你是公司不签还是自己拒签」的追问，全部证据被丢弃，
                # 用户只拿到一句反问——这正是评测里 s01 判「引用错误」的原因。
                # 现在改成：继续定性、生成、给条件性结论，最后把追问附在末尾。
                if res.need_user_input:
                    state.pending_question = res.question or "能再具体说说吗？"

            state.budget.steps_left -= 1

            # 整轮都是重复调用：模型手上已经没有新招，再问它只会再发一遍。
            # 不在这里断，就会白烧掉剩下的每一步（每步一次决策调用，约 2 秒）。
            if not ran_this_round:
                break

            # 已经有证据 + 已经决定要追问 → 检索到此为止。
            # 再查下去是浪费：模型已经认为「关键事实不确定」，多检索也补不上，
            # 只会把每条约 2 秒的决策成本乘上去（实测占单次问答总耗时的 60%）。
            #
            # 例外是「钱还没算」：用户问的是金额、槽位也齐，这时收工等于让模型
            # 自己算术——真 key 实测它会写出一个精确到元的数字（872,352），
            # 社平工资还是凭记忆写的。宁可多花两步，也必须让工具给这个数。
            if state.pending_question and state.evidences and not needs_calc(state):
                break

            if progressed:
                no_progress = 0
            else:
                no_progress += 1
                if no_progress >= 2:
                    break
            if len(state.evidences) >= _MAX_EVIDENCE:
                break

        # ---- 没有证据：诚实拒答，不编 ----
        # 例外是「生成文书」：申请书里的条号写死在模板里，不依赖本轮检索。
        # 按原来的判断，用户说「帮我写份仲裁申请书」会因为一条条文都没查到
        # 而被拒答——他要的是纸，不是法条分析。
        if not state.evidences and not any(
                s.tool == "draft_document" and not s.error for s in state.scratchpad):
            return self._refuse(state, _empty_text(), emit)

        # ---- 定性：先判断合法与否，再谈依据和建议 ----
        # 这一步独立于生成，因为它能被单独检验：判断错了是定性错，
        # 不是措辞问题。前端也会把它渲染成轨迹里的一步。
        state.verdict = self.llm.judge(state)
        emit("verdict", {"kind": state.verdict.kind,
                         "label": state.verdict.label,
                         "legal": state.verdict.legal,
                         "reason": state.verdict.reason,
                         "coverage": state.verdict.coverage,
                         "relevant": state.verdict.relevant,
                         "missing_part": state.verdict.missing_part,
                         "basis": state.verdict.basis})

        # 定性环节判定「一条都答不了」→ 立刻拒答，不进生成。
        # 放在生成之前：既省一次 5~7 秒的调用，也彻底断掉「拿相近条文硬套」
        # 这条路。实测无答案题 14 条里错了 10 条，根子就在这。
        # 注意 partial **不**走这里：那是「答一半」的信号，要继续生成。
        if not state.verdict.relevant:
            return self._refuse(state, _irrelevant_text(state), emit)

        # ---- 生成答案（流式）----
        state.draft = self.llm.generate_stream(state, emit)

        # partial 必须留下「哪一部分答不了」。prompt 里写了要求，但这类
        # 「记得在尾巴上加一段」的指令，模型丢掉的概率不低——而丢掉之后
        # 用户会以为这是个完整答案，比拒答更危险。所以兜一层：
        # 模型没写就由系统补，写明缺什么，不让它静默消失。
        if state.verdict.coverage == "partial" and "未覆盖" not in state.draft:
            # 同上：让婚家用户去打 12333 问劳动仲裁是把他送到错的门。
            where = ("建议咨询执业律师核实口径。" if (_profiles.get().empty_hint or "").strip()
                     else "建议向当地劳动人事争议仲裁委员会或 12333 核实口径。")
            tail = (f"\n\n【未覆盖】{state.verdict.missing_part}——"
                    f"这一部分当前收录的条文里没有对应规定，别只按上面的结论去主张，"
                    f"{where}")
            state.draft += tail
            emit("token", {"text": tail})
            # v136：这里原来还有一条 note「（这是部分回答：有一问本库答不了，
            # 已在末尾标明）」。它把【未覆盖】那段话又说了一遍，且「已在末尾
            # 标明」是自指——用户刚看完末尾那行，不需要再被告知一次。

        # ---- 生成后补核验 ----
        # 模型引用的条文，有一部分是真条文、只是本轮检索没捞到。把它取回来
        # 核对，比直接判「无依据引用」有用得多——引用错误曾是最大的一类失败
        # （50 条里 19 条），prompt 里写「不得引用未提供的条文」完全没有用。
        filled, phantom = _backfill_citations(state, self.index)
        if filled:
            emit("note", {"text": f"（另补入 {len(filled)} 条被引用条文）"})

        # ---- L3 答案自检 ----
        # 传 index：自检要和评测用同一套解析（cite.extract_cites），
        # 才能认出「同法第七十七条」这种写法，也才能判断条号在库里存不存在。
        vr = self.llm.verify(state.draft, state.evidences, self.index)
        emit("verify", {"passed": vr.passed,
                        "issues": [i.model_dump() for i in vr.issues],
                        "citations": [e.cite() for e in state.evidences]})

        if not vr.passed and vr.issues:
            issues = vr.issues
            # 文书题：申请书的「法律依据」一栏是**故意留空**的（让当事人问完金额、
            # 自己确认后再填），所以正文里必然没有条号，「未标注任何条文依据」
            # 是百分之百的假警报——留着会让用户以为系统查不到条文。
            # 只滤这一条：引错条号（引用了库里没有的条）仍然照实提示。
            if any(s.tool == "draft_document" and not s.error for s in state.scratchpad):
                issues = [i for i in issues if i.reason != "答案未标注任何条文依据"]
            if issues:
                note = "；".join(i.reason for i in issues[:2])
                state.note = note
                suffix = f"\n\n（核对提示：{note}。这部分结论请以官方条文原文为准。）"
                state.draft += suffix
                emit("note", {"text": suffix})

        # 挂起的追问补在这里：先给能给的，再问该问的。
        # 顺序很重要——用户先看到答案，才会愿意回答那个问题。
        if state.pending_question:
            # pending_question 自己常常已经带了引导语（llm.ask 返回的是
            # 「为了给到准确结论，想先确认一下：您月工资大概多少？」），
            # 直接拼在前面就成了「还需要确认：为了给到准确结论，想先确认
            # 一下：……」——两句引导语抢着说话，像两个人同时开口。
            # 剥掉它自带的那层，只留我们这一句。
            pq = strip_ask_lead(state.pending_question)
            suffix = f"\n\n另外，为了给你更准确的结论，还需要确认：{pq}"
            # 免责声明必须是最后一行。追问拼在它后面，读起来像"附言"——
            # 声明已经把话说完了，再回头问一句，顺序是反的。
            cut = state.draft.rfind(DISCLAIMER_MARK)
            if cut >= 0:
                state.draft = (state.draft[:cut].rstrip() + suffix
                               + "\n\n" + state.draft[cut:])
            else:
                state.draft += suffix
            emit("note", {"text": suffix})

        state.status = "answered"
        state.citations = [e.cite() for e in state.evidences]
        self.store.save_message(session_id, "assistant", state.draft)
        self.store.save_slots(session_id, state.slots)
        # 记到任务上，下一轮（可能是新会话）带着开工。
        # 只记 answered：拒答和追问时的证据往往是被判为「不相关」的那批，
        # 存下去等于把噪声喂给下一轮。
        if task_id and state.evidences:
            self.store.save_task_evidence(
                task_id, [e.model_dump() for e in state.evidences])

        # 提示语放在 done 之前：前端这时已经把答案 token 流完了，这条会接在
        # 正文末尾。存库的仍是 state.draft（不掺提示语）——库里存的是答案本身，
        # 提示描述的是「这一次的运行环境」，不该跟着答案永久留存。
        note = degraded_note(self.llm)
        if note:
            emit("degraded", {"text": note})

        # ---- 办案包：答案之外的五块结构化产物 ----
        # 放在自检之后、done 之前：它是本轮结论的确定性投影，不是新一轮决策，
        # 所以既不进工具表（那里已经 9 个，添一个就多一个选错的机会），
        # 也不改 ANSWER_SYSTEM 的逐字答案。纯规则生成，回放模式下同样发得出。
        #
        # v117：显式画像在这一层从长期记忆里读（build_extras 仍是零数据库的
        # 纯函数）。命令行 / 86 题评测走 user_id=None，facts 为空，行为与 v116
        # 完全一致：画像如实标 unset，不把默认职工视角伪装成用户选过。
        try:
            facts = (self.store.get_user_facts(user_id)
                     if user_id is not None else {})
            stance = _persona.normalize_stance(facts.get("stance"))
            pcfg = {"tone_mode": facts.get("tone_mode"),
                    "confirmed": bool(facts.get("confirmed"))}
            emit("extras", _extras.build_extras(
                state, stance=stance, persona=pcfg))
        except Exception as e:                      # noqa: BLE001
            # 附加产物绝不能把已经生成好的答案弄丢。它是显示层的东西，
            # 出错只在轨迹里留一句，答案照常 done。
            emit("note", {"text": f"（办案包暂不可用：{type(e).__name__}）"})

        # ---- v137 任务记忆：目标与滚动摘要 ----
        # 放在 done 之前、办案包之后：它是「记下来给下一轮用」的收尾动作，
        # 不参与本轮任何判断。失败只留一句轨迹，答案照常 done——
        # 记忆坏掉是少记一件事，不能变成「这次白问」。
        try:
            self._update_task_memory(state, task_id, session_id, emit)
        except Exception as e:                      # noqa: BLE001
            emit("note", {"text": f"（任务记忆暂不可用：{type(e).__name__}）"})

        # 用户说过的身份事实（城市/职业/单位性质）。只记用户明确说过的句子，
        # 抽不到就空着；档案页能看见、能删。
        try:
            if user_id is not None:
                _got = _facts.extract(state.question)
                # 槽位里已经有地区（那是从用户话里抽出来的，同样算「他说过」），
                # 就补成画像里的城市项——两套抽取各管一段，不互相覆盖。
                _reg = str(getattr(state.slots, "region", "") or "").strip()
                if _reg and "city" not in _got:
                    _got["city"] = {"value": _reg,
                                    "source": state.question[:40]}
                if _got:
                    self.store.add_profile_facts(user_id, _got)
        except Exception:                           # noqa: BLE001
            pass

        emit("done", {"status": state.status,
                      "answer": state.draft,
                      "citations": state.citations,
                      "kind": state.question_kind,
                      "verdict": state.verdict.model_dump() if state.verdict else None,
                      "steps": len(state.scratchpad),
                      "elapsed_ms": int(state.budget.elapsed() * 1000)})
        return state

    # ---------------------------------------------------------------- 分支收尾
    def _refuse(self, state: AgentState, text: str, emit: Emit) -> AgentState:
        state.status = "refused"
        state.draft = text
        for chunk in _chunks(text):
            emit("token", {"text": chunk})
        self.store.save_message(state.session_id, "assistant", text)
        emit("done", {"status": "refused", "answer": text,
                      "steps": len(state.scratchpad),
                      "elapsed_ms": int(state.budget.elapsed() * 1000)})
        return state

    def _ask(self, state: AgentState, question: str, emit: Emit) -> AgentState:
        state.status = "answered"
        state.intent = "clarify" if state.intent == "unknown" else state.intent
        state.draft = question
        for chunk in _chunks(question):
            emit("token", {"text": chunk})
        self.store.save_message(state.session_id, "assistant", question)
        emit("done", {"status": "clarify", "answer": question,
                      "steps": len(state.scratchpad),
                      "elapsed_ms": int(state.budget.elapsed() * 1000)})
        return state

    def _update_task_memory(self, state: AgentState, task_id: str | None,
                            session_id: str, emit: Emit) -> None:
        """维护任务的 goal 与滚动摘要。

        节奏是刻意的：goal 只在还没有的时候生成一次（一句话不该每轮变），
        摘要每隔 4 轮才重写一次（每轮都写等于每轮多烧一次调用，而且摘要
        会跟着最后一句跑偏，越写越像最后那一问）。
        """
        if not task_id:
            return
        turns = max(0, self.store.count_messages(session_id) // 2)
        cur = self.store.task_memory(task_id)
        is_labor = _profiles.get().id == "labor"
        need_goal = not (cur.get("goal") or "").strip()
        upto = int(cur.get("summary_upto") or 0)
        # 摘要只对**会读它**的内核生成。labor 的 system 是逐字冻结的，
        # 摘要写出来也没人读，每 4 轮白烧一次调用；goal 例外——它显示在
        # 任务卡上，labor 用户也看得见，所以那个只生成一次的开销值得花。
        need_sum = (not is_labor) and (
            (not (cur.get("summary") or "").strip()) or (turns - upto >= 4))
        if not (need_goal or need_sum):
            return
        res = self.llm.task_memory(
            question=state.question, prev=cur,
            recent=self.store.recent(session_id, 6),
            slots_brief=state.case_brief(),
            profile_name=_profiles.get().name)
        patch: dict[str, Any] = {}
        if need_goal and (res.get("goal") or "").strip():
            patch["goal"] = res["goal"].strip()
        if need_sum and (res.get("summary") or "").strip():
            patch["summary"] = res["summary"].strip()
            patch["summary_upto"] = turns
        if not patch:
            return
        self.store.set_task_memory(task_id, **patch)
        emit("memory", {"slots": self._slots_known(state), "changed": [],
                        "goal": patch.get("goal") or cur.get("goal") or ""})

    @staticmethod
    def _slots_known(state: AgentState) -> dict[str, Any]:
        """「记下了什么」的统一出口。接入内核送自己的字段，劳动法送那八项。"""
        p = _profiles.get()
        if p.id != "labor":
            return _dslots.known(p, state.dslots)
        return state.slots.known()

    def _greet(self, state: AgentState, emit: Emit) -> AgentState:
        """只回一句话，不检索、不调模型。

        为什么不交给模型：寒暄本身没有法律信息，为了说句「你好」烧一次
        调用毫无意义，而且在回放模式下模型本来就不在线。代价是文案写死，
        但这段文字的功能是**引导用户把情况说出来**，不是聊天，写死反而稳定。

        一个细节：已经聊过的人问候，不该再听一遍完整介绍。
        """
        talked = any(m["role"] == "assistant"
                     for m in self.store.recent(state.session_id, 10))
        if talked:
            text = ("在。前面说到的情况我都记着，接着说就行；"
                    "想问新的事也可以直接问。")
        else:
            p = _profiles.get()
            if p.id == "labor":
                text = ("您好，我是劳动法咨询助手。\n\n"
                        "我按现行有效的条文和司法解释回答，引的每一条都能在库里"
                        "核对到；查不到就直接说查不到，不编条文号。\n\n"
                        "可以直接把情况说出来，比如：\n"
                        "· 公司拖欠我三个月工资，也没签合同，我能要什么\n"
                        "· 工作一年半被辞退，经济补偿怎么算\n"
                        "· 离职半年了，还能不能申请劳动仲裁\n\n"
                        "说得越具体（干了多久、月薪多少、怎么离职的），依据越准。")
            else:
                # 这段是分区里**第一眼**看到的字。原来五个内核共用劳动法那段
                # （「我是劳动法咨询助手」+ 三个劳动争议例子），用户在婚家分区
                # 说句「你好」就被告知来错了地方——比答错更早赶客。
                ex = "".join(f"· {q}\n" for _, q in p.examples[:3])
                text = (f"您好，我是{p.name}分区的咨询助手。\n\n"
                        f"我按现行有效的条文和司法解释回答，引的每一条都能在库里"
                        f"核对到；查不到就直接说查不到，不编条文号。\n\n"
                        f"可以直接把情况说出来，比如：\n{ex}\n"
                        f"说得越具体（{p.empty_hint}），依据越准。")
        state.status = "answered"
        state.draft = text
        for chunk in _chunks(text):
            emit("token", {"text": chunk})
        self.store.save_message(state.session_id, "assistant", text)
        emit("done", {"status": "answered", "answer": text,
                      "steps": 0,
                      "elapsed_ms": int(state.budget.elapsed() * 1000)})
        return state


def _chunks(text: str, size: int = 4):
    for i in range(0, len(text), size):
        yield text[i:i + size]


_Q_WORDS = ("吗", "什么", "怎么", "怎样", "如何", "多少", "是否",
            "能不能", "可以", "该", "?" , "？", "有没有")


# 寒暄 / 无信息量的应答。**必须用整句精确匹配，不能用 `in` 判定**：
# 「你好，公司拖欠工资怎么办」里含「你好」，用 in 会把它当寒暄吞掉。
#
# 这张表的由来是一次真实的误判：用户问「你好」，系统回了上一题（违约金）
# 的答案，一字不差——因为「你好」短、无疑问词、无法律动作词，被下面的
# _looks_like_supplement 判成「补充信息」，然后拿上一轮的问题重答了一遍。
# 复现：同一会话先问合同法114条，再问「你好」，两轮 draft 完全相同。
#
# 这个坑在回放模式下必然出现（交付包没有 key，全是回放模式），
# 所以修在入口处：寒暄不进补充判定，也不进检索。
_GREET_WORDS = {
    "你好", "您好", "你们好", "大家好", "hi", "hello", "hey", "嗨", "哈喽",
    "hellothere", "hithere", "goodmorning", "goodafternoon", "goodevening",
    "在吗", "在不在", "在么", "有人吗", "在",
    # 按时段打招呼。只认这几种固定说法，不按「含『好』就算寒暄」判——
    # 「这合同好不好」也含「好」，那样会把真问题吞掉。
    "早上好", "早安", "上午好", "中午好", "下午好", "晚上好", "晚安",
    "谢谢", "谢谢你", "多谢", "感谢", "thanks", "thankyou", "thx",
    "好的", "好", "嗯", "嗯嗯", "哦", "ok", "okay", "收到", "明白", "了解",
    "知道了", "行", "可以", "没事", "辛苦了",
}

_PUNCT = " \t\r\n\u3000，。！？!?,.、~～…—－-“”\"'（）()【】[]"


def _norm_greet(text: str) -> str:
    """去掉标点空白并转小写，只为了跟 _GREET_WORDS 对齐。

    不删内容字符：「你好，公司拖欠我工资」归一化后仍是长句，不会误命中。
    """
    t = (text or "").strip()
    for ch in _PUNCT:
        t = t.replace(ch, "")
    return t.lower()


def _is_greeting(text: str) -> bool:
    """整句就是一句寒暄，没有任何案情信息。"""
    t = _norm_greet(text)
    # 14 是为了容纳 goodafternoon 这种长英文问候；中文问候都远低于这个数。
    # 真正防误吞的是「整句精确匹配」，长度只是挡住异常长的整句。
    if not t or len(t) > 14:
        return False
    if t in _GREET_WORDS:
        return True
    # 「你好呀」「您好啊」「谢谢啦」——去掉尾部语气词再试一次
    core = t.rstrip("呀啊呢吧哦啦嘛哈")
    return bool(core) and core in _GREET_WORDS


def _looks_like_supplement(text: str) -> bool:
    """判断这句是补充事实还是提出新问题。

    规则简单但有效：句子短、且不含任何疑问标记，也没有法律动作词。
    宁可漏判（把补充当新问题重问一遍），也不要把真问题误判成补充。

    但寒暄必须排除在外：它既不是补充，也不是新问题，说它是补充会让
    上一轮的答案被原样重放一遍（见 _GREET_WORDS 的注释）。
    """
    if _is_greeting(text):
        return False
    if len(text) > 20:
        return False
    if any(w in text for w in _Q_WORDS):
        return False
    if any(w in text for w in ("辞退", "开除", "拖欠", "不给", "没签", "未签",
                               "加班", "社保", "解除", "仲裁", "起诉")):
        return False
    return True
