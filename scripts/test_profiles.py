# -*- coding: utf-8 -*-
"""v119 可插拔法律内核回归：内核隔离 + 劳动法零回归 + 专属能力 + 真服务绑定。

这一套要证明的不是「多了两个下拉选项」，而是四件容易被包装糊弄过去的事：

1. **劳动法内核一个字都没变。** 提示词逐字等于原常量、办案包顶层恰好
   五块、检索分数与旧构造逐位一致、定性规则与建议原文不变。
2. **内核真的隔开了。** 消费内核检索不到劳动合同法条文，取条也取不到；
   答案与办案包里不能冒出「12333 / 劳动仲裁 / 经济补偿」这些劳动争议话术。
3. **「不同 RAG 不同功能」是真的。** 消费内核出惩罚性赔偿测算、合同内核
   出效力核查，且都不给胜率、不建议起诉、没金额就留空。
4. **会话绑定不是前端自嗨。** 真起服务，新会话按选的内核入库，已有会话
   忽略后来传的 profile（防串库），办案包接口按会话内核重算。

全程回放模式、临时库、不碰外网、不需要 key。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import domains, extras, persona, profiles, prompts, verdict  # noqa: E402
from app.config import CORPUS_PATH, DOMAIN_CORPUS_PATH, GENERIC_BOOST  # noqa: E402
from app.retrieval import LawIndex, get_index, get_profile_index  # noqa: E402
from app.schemas import Evidence  # noqa: E402

TOTAL = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"   {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def ev(article_id: str, law: str, no: int, content: str = "") -> Evidence:
    return Evidence(article_id=article_id, law_name=law, article_no=str(no),
                    article_no_cn=f"第{no}条", content=content,
                    score=1.0, source="direct", status="现行有效")


def scrub(obj):
    """扫「有没有给胜率」之前，先剔掉 no_* 这类**否定声明**字段。

    「本系统不提供胜诉率」这句话里当然含「胜诉率」三个字，把它算成
    「给了胜率」是假红灯；但直接把关键词从正则里删掉又是真放宽标准
    （真写一句「胜诉率 73%」就漏了）。正确做法是把声明与结论分开扫，
    再单独断言声明必须存在。
    """
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()
                if not (str(k).startswith("no_") or k == "hard_boundary")}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


# 劳动争议话术。接入内核的提示词、办案包、答案里都不该出现这些词。
_LABOR_SMELL = ("劳动仲裁", "12333", "经济补偿", "二倍工资", "仲裁申请书",
                "劳动保障监察", "入职时间", "离职方式")


# ---------------------------------------------------------------- [1] 注册表

def check_registry() -> None:
    print("[1] 内核注册表与白名单")
    ok("恰好五个内核",
       set(profiles.ids()) == {"labor", "consumer", "civil", "traffic", "family"},
       str(profiles.ids()))
    ok("默认内核写死是 labor", profiles.DEFAULT_ID == "labor")
    for bad in ("", None, "CRIMINAL", "labor ", "  ", 123, "__proto__"):
        ok(f"非法取值 {bad!r} 一律回落劳动法",
           profiles.resolve(bad).id == "labor")
    ok("大写+空格能归一到合法内核",
       profiles.resolve(" CIVIL ").id == "civil")
    ok("传 LawProfile 对象原样返回",
       profiles.resolve(profiles.PROFILES["consumer"]).id == "consumer")
    ok("未切换时 get() 是劳动法", profiles.get().id == "labor")
    with profiles.use("consumer"):
        ok("use() 内切到消费内核", profiles.get().id == "consumer")
        with profiles.use("civil"):
            ok("嵌套 use() 取内层", profiles.get().id == "civil")
        ok("内层退出后恢复外层", profiles.get().id == "consumer")
    ok("退出后恢复劳动法", profiles.get().id == "labor")
    ok("use() 遇异常也恢复", _use_with_raise())
    meta = profiles.all_meta()
    ok("meta 五条且字段齐全",
       len(meta) == 5 and all(
           {"id", "name", "short", "desc", "domain", "laws", "all_laws",
            "tool_count", "version_demo"} <= set(m) for m in meta))
    ok("meta 不暴露语料路径或配置值",
       "statutes.jsonl" not in json.dumps(meta, ensure_ascii=False))
    # v121 界面层：顶部索引条、首屏内核卡、示例问题全靠这几个字段。缺一个
    # 页面不会报错，只会退化成「五个长得一样的入口」——所以只能在这里钉住。
    seals = [m["seal"] for m in meta]
    ok("每个内核都有单字印章且互不相同",
       all(len(s) == 1 for s in seals) and len(set(seals)) == 5,
       "".join(seals))
    ok("每个内核都有专属能力短名",
       all(m["capability"] for m in meta),
       " / ".join(m["capability"] for m in meta))
    ok("每个内核都有首屏标语",
       all(len(m["tagline"]) >= 10 for m in meta))
    ok("每个内核三格事实且键值都不空",
       all(len(m["facts"]) == 3
           and all(f["k"] and f["v"] for f in m["facts"]) for m in meta))
    ok("每个内核四条示例且标签不重",
       all(len(m["examples"]) == 4 for m in meta)
       and all(len({e["tag"] for e in m["examples"]}) == 4 for m in meta))
    ok("五个内核的示例没有一条重复",
       len({e["text"] for m in meta for e in m["examples"]}) == 20)
    # 这一条是「不同内核不同功能」最容易被糊弄过去的地方：把劳动法那四条
    # 原样复制给五张卡，界面上看着也有五个领域。所以查头条示例必须落在本域。
    _kw = {"labor": ("工资", "合同", "辞退", "仲裁"),
           "consumer": ("过期", "退", "赔偿", "预付", "标签"),
           "civil": ("合同", "违约金", "条", "签字"),
           "traffic": ("车", "赔", "伤残", "险", "事故"),
           "family": ("离婚", "抚养", "继承", "遗嘱", "房")}
    ok("每个内核的头条示例都落在本领域",
       all(any(k in m["examples"][0]["text"] for k in _kw[m["id"]])
           for m in meta),
       " / ".join(m["examples"][0]["text"][:12] for m in meta))
    # v121：首屏大字与引导语。前一版这两句是写死的，五个内核共用一句
    # 「把情况说清楚，我帮你找依据」——进门第一眼看到的那行字，正好是
    # 最证明不了「这是五个领域」的东西。所以这里要求各不相同，且劳动法
    # 那种「什么时候入职」的问法不许出现在别的法上。
    heads = [m["headline"] for m in meta]
    ok("五个内核的首屏大字互不相同",
       len(set(heads)) == 5 and all(len(h) >= 8 for h in heads),
       " / ".join(h[:10] for h in heads))
    ok("首屏引导语都不为空且都带例子提示",
       all("例子" in m["lede"] and len(m["lede"]) >= 20 for m in meta))
    ok("别的法不借用劳动法的问法",
       all("入职" not in m["headline"] and "入职" not in m["lede"]
           for m in meta if m["id"] != "labor"))
    ok("四个接入内核各带一个专属能力",
       {m["id"]: m["domain"] for m in meta} ==
       {"labor": "", "consumer": "punitive_damages", "civil": "contract_check",
        "traffic": "traffic_compensation", "family": "family_split"})
    ok("接入内核工具表收窄且劳动法仍是九个",
       all(len(profiles.PROFILES[p].tools) == 5
           for p in ("consumer", "civil", "traffic", "family"))
       and len(profiles.PROFILES["labor"].tools) == 9)
    ok("劳动法内核不做法律名过滤",
       profiles.PROFILES["labor"].corpus_laws is None)
    ok("接入内核语料文件真实存在", DOMAIN_CORPUS_PATH.is_file())


def _use_with_raise() -> bool:
    try:
        with profiles.use("consumer"):
            raise ValueError("boom")
    except ValueError:
        pass
    return profiles.get().id == "labor"


# ---------------------------------------------------------------- [2] 提示词

def check_prompts() -> None:
    print("[2] 提示词：劳动法逐字不变 / 接入内核不串味")
    names = ("INTENT_SYSTEM", "DECISION_SYSTEM", "ANSWER_SYSTEM",
             "SLOT_SYSTEM", "DECOMPOSE_SYSTEM", "JUDGE_SYSTEM",
             "VERIFY_SYSTEM", "ASK_SYSTEM", "VISION_SYSTEM", "DECISION_USER")
    for n in names:
        ok(f"labor 的 {n} 逐字等于原常量",
           prompts.system(n, "labor") == getattr(prompts, n))
    ok("不传 profile 时默认读劳动法",
       prompts.system("INTENT_SYSTEM") == prompts.INTENT_SYSTEM)
    for pid in ("consumer", "civil"):
        with profiles.use(pid):
            p = profiles.get()
            for n in ("INTENT_SYSTEM", "DECISION_SYSTEM", "ANSWER_SYSTEM",
                      "JUDGE_SYSTEM", "DECOMPOSE_SYSTEM"):
                t = prompts.system(n)
                ok(f"{pid} 的 {n} 是独立模板", t != getattr(prompts, n))
                ok(f"{pid} 的 {n} 不含劳动争议话术",
                   not any(w in t for w in _LABOR_SMELL))
            ok(f"{pid} 的意图边界写的是本领域",
               p.scope_hint in prompts.system("INTENT_SYSTEM"))
            ok(f"{pid} 的决策提示词保留 __TOOLS__ 占位",
               "__TOOLS__" in prompts.system("DECISION_SYSTEM"))
            ok(f"{pid} 的答案提示词保留全部占位符",
               all(x in prompts.system("ANSWER_SYSTEM") for x in
                   ("__SLOTS__", "__VERDICT__", "__DEADLINE__",
                    "__CALC__", "__DOC__", "__EVIDENCE__")))
            ok(f"{pid} 的答案提示词明写不给胜率",
               "胜率" in prompts.system("ANSWER_SYSTEM"))
    # v138：接入内核开始抽自己的槽位了，模板也从「输出空对象」改成字段表。
    # 这两条一起钉：① 它列的是本内核的字段；② 劳动争议那几项一个字都没进去。
    _slot_consumer = prompts.system("SLOT_SYSTEM", "consumer")
    ok("SLOT 接入模板列的是本内核字段（不再是空对象）",
       "{}" not in _slot_consumer
       and all(k in _slot_consumer for k in ("goods", "price", "problem"))
       and "商品或服务" in _slot_consumer, _slot_consumer[:80])
    ok("SLOT 接入模板不把劳动争议字段列成待抽字段",
       all("  " + w + " " not in _slot_consumer
           for w in ("monthly_wage", "leave_reason", "start_date")))
    ok("SLOT 劳动法内核模板逐字不变",
       prompts.system("SLOT_SYSTEM", "labor") == prompts.SLOT_SYSTEM)
    with profiles.use("civil"):
        ok("VISION/VERIFY 是领域无关常量，原样复用",
           prompts.system("VERIFY_SYSTEM") == prompts.VERIFY_SYSTEM
           and prompts.system("VISION_SYSTEM") == prompts.VISION_SYSTEM)
        # v135：ASK 不再领域无关。静态四选一截住追问时，模型连用户原问
        # 都看不到，所以接入内核改走 _domain_ask，带上内核名与 in_scope。
        _ask_civil = prompts.system("ASK_SYSTEM")
        _cp = profiles.resolve("civil")
        ok("ASK 在接入内核走领域模板（贴合原问 + 内核边界）",
           _ask_civil != prompts.ASK_SYSTEM
           and _cp.name in _ask_civil
           and _cp.scope_hint in _ask_civil
           and "贴合用户原问" in _ask_civil
           and "不许出现劳动用工" in _ask_civil,
           _ask_civil[:60])
    with profiles.use("labor"):
        ok("劳动法内核的 ASK_SYSTEM 逐字不变",
           prompts.system("ASK_SYSTEM") == prompts.ASK_SYSTEM)


# ---------------------------------------------------------------- [2.6] v135
def check_v135_open_gate() -> None:
    """v135：入口不再一票否决；追问必须真的看过用户原问。

    这一节全部用回放 / 假 `_chat`，**不烧一次真 key**。在线端到端交给
    `check_live.py` 第 9 节（那边才需要真模型）。
    """
    from pathlib import Path as _Path
    from app.llm import LLM, _guess_intent

    print("[2.6] v135：无论问什么，先尝试理解")

    # ---- 1. 回放粗分：本领域但措辞偏的说法不再被当成「说不清」----
    ok("回放：偏门「债务怎么分配」判正常咨询（补了「债务」）",
       _guess_intent("债务怎么分配", "family") == "case_consult")
    ok("回放：偏门「有条狗怎么分配」判正常咨询（补了「狗」）",
       _guess_intent("有条狗怎么分配", "family") == "case_consult")
    ok("回放：消费偏门「外卖吃出异物」判正常咨询",
       _guess_intent("外卖吃出异物", "consumer") == "case_consult")
    ok("回放：真正无关的「如何烤蛋糕」仍是 clarify（没放宽到乱答）",
       _guess_intent("如何烤蛋糕", "family") == "clarify")
    ok("回放：劳动法内核的越界判定一字未改",
       _guess_intent("如何烤蛋糕") == "out_of_scope")
    ok("回放：v133 的三句判据仍成立",
       _guess_intent("离婚", "family") == "clarify"
       and _guess_intent("孩子抚养权怎么判", "family") == "case_consult"
       and _guess_intent("买到过期食品", "consumer") == "case_consult")

    # ---- 2. 在线追问：静态四选一降到兜底，模型必须看到原问 ----
    from types import SimpleNamespace as _NS

    _ASK_TEXT = "您问的是这 100 万算不算夫妻共同债务，还是双方各还多少？"

    def _resp(text: str):
        return _NS(choices=[_NS(message=_NS(content=text))])

    seen_sys: list[str] = []
    seen_usr: list[str] = []
    seen_kw: list[dict] = []

    def fake_chat(system: str, user: str, **kw):
        seen_sys.append(system)
        seen_usr.append(user)
        seen_kw.append(kw)
        return _resp(_ASK_TEXT)

    llm = LLM()
    llm.online = True
    llm.maybe_recover = lambda: False           # type: ignore[method-assign]
    llm._chat = fake_chat                       # type: ignore[method-assign]
    fam = profiles.resolve("family")
    with profiles.use("family"):
        q1 = llm.ask(["region"], "家里有一百万的债务怎么分")
        q2 = llm.ask(["region"], "有条狗怎么分配")
    ok("在线追问不再返回内核那句静态四选一",
       q1 != fam.clarify_question and q2 != fam.clarify_question, (q1, q2))
    ok("两次追问都把用户原问交给了模型",
       len(seen_usr) == 2 and "债务" in seen_usr[0]
       and "有条狗" in seen_usr[1], seen_usr)
    ok("在线追问用的是领域模板（含内核边界与贴合要求）",
       len(seen_sys) == 2
       and all(fam.scope_hint in s and "贴合用户原问" in s for s in seen_sys),
       seen_sys[0][:60] if seen_sys else "")
    ok("接入内核追问关掉思考（小预算别被 reasoning 吃光）",
       len(seen_kw) == 2
       and all(k.get("allow_think") is False for k in seen_kw[:2]),
       seen_kw[:2])

    # ---- 3. 调用失败：回落内核自带那句，不串味 ----
    llm2 = LLM()
    llm2.online = True
    llm2.maybe_recover = lambda: False          # type: ignore[method-assign]

    def boom(*a, **k):
        raise RuntimeError("simulated offline")

    llm2._chat = boom                           # type: ignore[method-assign]
    with profiles.use("family"):
        q3 = llm2.ask(["region"], "我离婚了")
    ok("模型失败时回落内核自带追问、不串味",
       q3 == fam.clarify_question
       and not any(w in q3 for w in ("离职", "辞退", "月工资", "入职")), q3)

    # ---- 4. labor 在线链路逐字不变 ----
    llm3 = LLM()
    llm3.online = True
    llm3.maybe_recover = lambda: False          # type: ignore[method-assign]
    llm3._chat = fake_chat                      # type: ignore[method-assign]
    with profiles.use("labor"):
        llm3.ask(["leave_reason"], "你好")
    ok("劳动法内核在线追问仍用原 ASK_SYSTEM",
       seen_sys[-1] == prompts.ASK_SYSTEM, seen_sys[-1][:60])
    ok("劳动法内核的追问调用参数一字未加（不关思考）",
       "allow_think" not in seen_kw[-1], seen_kw[-1])
    ok("劳动法内核传给模型的 user 串逐字不变",
       seen_usr[-1] == "用户问题：你好\n缺失要素：是公司辞退、自己辞职，还是被迫离职",
       seen_usr[-1])

    # ---- 7. v137 任务记忆：只进接入内核的 system 前缀 ----
    from app.llm import _task_brief, _rule_memory
    from app.schemas import AgentState as _AS   # 本函数后面还 import 了一次同名符号，
    # 直接用会变成 UnboundLocal；换个别名绕开，别去改后面的代码。
    st7 = _AS(session_id="t-mem", question="那这个能要多少",
                     task_goal="追回被拖欠的 3 个月工资",
                     task_summary="已确认月薪 8000，未签合同",
                     task_recent=["用户：公司拖欠三个月工资", "助手：先按二倍工资算"],
                     profile_facts="所在城市=杭州")
    b7 = _task_brief(st7)
    ok("任务背景含目标 / 进展 / 用户事实 / 最近轮次",
       all(k in b7 for k in ("目标：", "进展：", "用户说过：", "最近几轮：")), b7[:80])
    ok("空记忆不产生前缀（避免给每轮白加一段）",
       _task_brief(_AS(session_id="e", question="x")) == "")
    _llm_src = (_Path(__file__).resolve().parents[1] / "app" / "llm.py").read_text(
        encoding="utf-8")
    ok("前缀只加在非 labor 分支（labor 的 system 逐字不变）",
       'if _profiles.get().id != "labor":' in _llm_src
       and "_tb = _task_brief(state)" in _llm_src)
    # 回放版摘要必须自给自足：34 套全离线，这条路不能返回空
    rm = _rule_memory("那这个能要多少", {"goal": "追回工资"},
                      [{"role": "user", "content": "公司拖欠三个月工资"}],
                      "月工资=8000")
    ok("离线也能产出目标与摘要",
       rm["goal"] == "追回工资" and "月工资=8000" in rm["summary"], rm)
    rm2 = _rule_memory("公司没签合同能要二倍工资吗", {},
                       [], "（暂无已知案情要素）")
    ok("没有旧目标时用第一句压成目标", rm2["goal"] == "公司没签合同能要二倍工资", rm2)
    ok("槽位为空时不把「暂无」写进摘要", "暂无" not in rm2["summary"], rm2)

    # ---- 8. 入口归一：接线防呆 ----
    src = (_Path(__file__).resolve().parents[1] / "app" / "agent.py").read_text(
        encoding="utf-8")
    ok("入口归一同时限定「在线」与「非 labor」（离线回放一字不改）",
       'self.llm.online and _profiles.get().id != "labor"' in src
       and 'state.intent in ("clarify", "out_of_scope")' in src)
    ok("归一后走 case_consult，进入检索与 judge",
       'state.intent = "case_consult"' in src)
    _gate_pos = src.index('state.intent = "case_consult"')
    # 寒暄分支（:307）也 emit 了一次 intent，必须从归一位置**往后**找，
    # 否则 str.index() 命中更早那处，断言会莫名其妙地红。
    ok("归一发生在 emit(intent) 之前（轨迹不撒谎）",
       _gate_pos < src.index('emit("intent", {"intent": state.intent})',
                             _gate_pos))

    # ---- 6. v135-b：放开之后才暴露的两处「答案里串劳动争议口径」----
    # 真 key 实测「我离婚了」的【建议】第 4 条让用户去补入职/离职日期与
    # 月工资，还写了「也没有查当地社平工资」。以前接入内核在入口就被短路、
    # 根本走不到生成，这两段 labor 专属 brief 一直没机会露面。
    from app.llm import _calc_brief, _region_brief, _NO_CALC
    from app.schemas import AgentState, Budget
    st = AgentState(session_id="t-nocalc", question="我离婚了",
                    budget=Budget(steps_left=2))
    with profiles.use("family"):
        cb = _calc_brief(st)
        rb = _region_brief(st)
        da = prompts.system("ANSWER_SYSTEM")
    ok("接入内核的「没算过金额」不再让用户补入职/离职/月工资",
       all(w not in cb for w in ("入职", "离职", "月工资", "社平工资", "工龄")),
       cb[:80])
    ok("接入内核的地区参数不再提社平工资", rb == "（无）", rb)
    ok("接入内核答案铁律明禁搬用别的领域参数",
       "当前分区不是劳动争议" in da and "当地社平工资" not in da
       and not any(w in da for w in _LABOR_SMELL), da[:80])
    with profiles.use("labor"):
        lb = _calc_brief(st)
        lr = _region_brief(st)
    ok("劳动法内核的 _NO_CALC 逐字不变", lb == _NO_CALC, lb[:60])
    ok("劳动法内核的地区参数原句不变",
       lr == "（本轮没有单独查社平工资；不要凭空说出任何社平数字）", lr)


# ---------------------------------------------------------------- [2.5] 追问

def check_clarify() -> None:
    """v133/v138：clarify 的追问必须按内核问，且问的是本内核真缺的那一项。

    为什么单开一节：这条链路上有一个很容易被忽略的不对称——`LLM.ask()` 的
    标签表原来是劳动法专用的四项（离职方式 / 月工资 / 入职时间 / 地区），
    接入内核算不出自己的缺失项，于是「换了分区，问什么都回问离职」。

    v138 起四个接入内核有了自己的字段表（`LawProfile.slot_labels` +
    `slot_priority` + `slot_asks`），问的就是本内核排第一的缺失要素；
    内核那句静态 `clarify_question` 降为「要素问全了 / 键不认识」时的兜底。

    这一节既要钉住接入内核不串味，也要钉住**劳动法内核一个字都没变**。
    """
    from app.agent import AgentRunner
    from app.llm import LLM
    from app.memory import MemoryStore
    from app.schemas import AgentState, Budget, CaseSlots

    _calc_budget_guard = LLM._calc_budget_guard        # 静态方法，不是模块级函数

    print("[2.5] clarify 追问：按内核问，不拿劳动法要素表糊人")
    llm = LLM()
    llm.online = False                       # 本节只看文案来源，不联网
    # 本机有 key 的时候，run() 开头会调 maybe_recover()，冷却过了就把 online
    # 翻回 True——那样这一节会静默变成联网用例。钉死它，这一节永远离线。
    llm.maybe_recover = lambda: False        # type: ignore[method-assign]

    # ---- 劳动法：逐字保持 ----
    with profiles.use("labor"):
        q_labor = llm.ask(CaseSlots().missing_critical() or ["region"], "你好")
    ok("劳动法内核的 clarify 追问逐字不变",
       q_labor == "为了给到准确结论，想先确认一下：您是公司辞退、自己辞职，还是被迫离职？",
       q_labor)

    # ---- 四个接入内核：问本领域的事 ----
    # v138：追问不再用内核那句静态四选一，而是按**本内核第一个缺失要素**问。
    # 静态那句降为兜底：要素都问全了、或者内核还没配字段表时才用它。
    from app import dslots as _dslots
    ok("只有劳动法内核留空（沿用原链路）",
       profiles.resolve("labor").clarify_question == "")
    for pid in ("consumer", "civil", "traffic", "family"):
        p = profiles.resolve(pid)
        miss = _dslots.missing(p, {})
        with profiles.use(pid):
            q = llm.ask(miss[:1], "帮我看看")
            # 传一个本内核没有的键（劳动法的键就够）：必须回到内核那句中文，
            # 不能把 `leave_reason` 这种内部字段名念给用户。
            q_bad = llm.ask(["leave_reason"], "帮我看看")
        ok(f"{pid} 有自己的一句澄清问句（兜底用）", bool(p.clarify_question.strip()))
        ok(f"{pid} 的 clarify 追问问的是本内核第一个缺失要素",
           q == f"想先确认一下：{_dslots.ask_for(p, miss[0])}？", q)
        ok(f"{pid} 收到不认识的字段时回到内核那句，不吐英文键名",
           q_bad == p.clarify_question, q_bad)
        ok(f"{pid} 的追问不含劳动争议话术",
           not any(w in q for w in _LABOR_SMELL), q)
        ok(f"{pid} 的追问不出现「离职/辞退/月工资/入职」",
           not any(w in q for w in ("离职", "辞退", "月工资", "入职")), q)

    # ---- 端到端：婚家内核问「对方出轨」，走的就是 clarify 这条分支 ----
    # 把意图判定钉成 clarify，是为了只测这条分支本身——回放模式下
    # `_guess_intent` 对两个字的短句给什么答案与本节无关。
    p = profiles.resolve("family")
    store = MemoryStore(Path(tempfile.mkdtemp()) / "clarify.db")
    runner = AgentRunner(get_profile_index(p), llm, store)
    llm.classify_intent = lambda *a, **k: "clarify"     # type: ignore[method-assign]
    with profiles.use(p):
        st = runner.run("对方出轨", session_id="t-clarify")
    ans = st.draft or ""
    ok("婚家内核 clarify 时问的是婚姻本身（不是离职方式）",
       all(w in ans for w in ("财产", "孩子")) and "损害赔偿" in ans, ans)
    ok("婚家内核的 clarify 答案里没有劳动争议话术",
       not any(w in ans for w in _LABOR_SMELL), ans)
    # 同一句话再问一遍时，换个要素问，不原样重放。
    with profiles.use(p):
        st_again = runner.run("对方出轨", session_id="t-clarify")
    ok("同一会话问第二遍时不重放同一句追问",
       (st_again.draft or "") != ans, (st_again.draft or "")[:60])

    # ---- 算钱三闸不该在非劳动法内核里开火 ----
    # region 会被「个人档案」种进槽位，所以只判「有没有 region」是不够的：
    # 婚家用户档案里填了北京，问「出轨能赔多少」就会去查北京社平工资。
    st = AgentState(session_id="t-calc", question="出轨能赔多少",
                    budget=Budget(steps_left=2))
    st.slots = CaseSlots(region="北京")
    st.evidences = [ev("m-1091", "民法典", 1091)]
    with profiles.use("family"):
        g_family = _calc_budget_guard(st)
    with profiles.use("labor"):
        g_labor = _calc_budget_guard(st)
    ok("婚家内核下算钱闸不开火（不查社平、不派算钱工具）", g_family is None)
    ok("同样的状态在劳动法内核下仍然开火（闸本身没被关死）",
       g_labor is not None
       and g_labor.tool_calls
       and g_labor.tool_calls[0].name in ("query_region_params",
                                          "calculate_compensation"),
       str(g_labor.tool_calls[0].name if g_labor and g_labor.tool_calls else None))

    # ---- 回放模式的意图粗分也必须看内核 ----
    # 这一条是改完追问才发现的：回放没有模型读 `scope_hint`，只能靠劳动法
    # 那两张词表判越界，于是四个接入内核在没 key 时对任何问题都回一句
    # 「本系统只处理劳动法相关的问题」——分区等于不存在。
    from app.llm import _guess_intent
    ok("回放：婚家「离婚」判 clarify（问一句比答错好）",
       _guess_intent("离婚", "family") == "clarify")
    ok("回放：婚家「孩子抚养权怎么判」按正常咨询走，不判越界",
       _guess_intent("孩子抚养权怎么判", "family") == "case_consult")
    ok("回放：消费「买到过期食品」按正常咨询走",
       _guess_intent("买到过期食品", "consumer") == "case_consult")
    ok("回放：婚家「如何烤蛋糕」不再是「越界」（越界词表是劳动法的）",
       _guess_intent("如何烤蛋糕", "family") == "clarify")
    ok("回放：劳动法内核的判断一字未改（烤蛋糕仍判越界）",
       _guess_intent("如何烤蛋糕") == "out_of_scope")
    ok("回放：劳动法内核的条号查询不受影响",
       _guess_intent("劳动合同法第38条", "labor") == "statute_lookup")

    # ---- 拒答文案也要按内核 ----
    p2 = profiles.resolve("family")
    llm.classify_intent = lambda *a, **k: "out_of_scope"   # type: ignore[method-assign]
    with profiles.use(p2):
        st2 = runner.run("帮我写一首情诗", session_id="t-refuse")
    ok("婚家内核的拒答说的是婚家（不自称只处理劳动法）",
       (st2.draft or "") == p2.refuse_text, (st2.draft or "")[:60])
    ok("婚家内核的拒答里没有「劳动法」三个字", "劳动法" not in (st2.draft or ""))
    with profiles.use("labor"):
        st3 = runner.run("帮我写一首情诗", session_id="t-refuse-labor")
    ok("劳动法内核的拒答文案逐字不变",
       (st3.draft or "").startswith("这个系统只处理劳动法相关的问题"),
       (st3.draft or "")[:40])

    # ---- 期限追问也只在劳动法内核问 ----
    # `_guess_dispute` 的取值全是劳动争议，`calculate_deadline` 给的规则条号
    # 也是调解仲裁法 27/48/49/50 条。婚家问诉讼时效，原来会被追问一句
    # 「离职（或被辞退、收到裁决书）是哪一年哪一个月」——同一处病、第三回。
    llm.classify_intent = lambda *a, **k: "case_consult"    # type: ignore[method-assign]
    with profiles.use(profiles.resolve("family")):
        st4 = runner.run("离婚后财产分割的诉讼时效是多久", session_id="t-dl-fam")
    ok("婚家内核不问「离职是哪一年哪一个月」",
       not any(w in (st4.pending_question or "")
               for w in ("离职", "辞退", "裁决书")), st4.pending_question or "")
    ok("婚家内核不调劳动法的期限工具",
       "calculate_deadline" not in [s.tool for s in st4.scratchpad])
    with profiles.use("labor"):
        st5 = runner.run("被辞退后仲裁时效是多久", session_id="t-dl-labor")
    ok("劳动法内核的期限追问照旧（含起算日）",
       "哪一年哪一个月" in (st5.pending_question or ""), st5.pending_question or "")


# ---------------------------------------------------------------- [2.6] 空结果

def check_empty_refusal() -> None:
    """v133：一条条文都检索不到时，那段话也在替用户假设劳动法。

    原来的文案是「把具体情形说得再细一点（是否签了合同、干了多久、怎么离职的、
    月工资大概多少）」——婚家分区检索空一次，用户又被问一遍离职方式。
    和 clarify 那条追问同源：**文案在替系统假设用户是劳动者**。
    """
    from app.agent import AgentRunner, _REFUSE_EMPTY
    from app.llm import LLM
    from app.memory import MemoryStore

    class _NoHits:
        """search 永远返回空，其余能力委托给真索引（模拟「一条都没命中」）。"""

        def __init__(self, real):
            self._real = real

        def search(self, *a, **k):
            return []

        def __getattr__(self, name):
            return getattr(self._real, name)

    print("[2.6] 检索不到条文时，拒答与建议也要按内核说")
    llm = LLM()
    llm.online = False
    llm.maybe_recover = lambda: False        # type: ignore[method-assign]
    store = MemoryStore(Path(tempfile.mkdtemp()) / "empty.db")
    for pid in ("consumer", "civil", "traffic", "family"):
        p = profiles.resolve(pid)
        r = AgentRunner(_NoHits(get_profile_index(p)), llm, store)
        with profiles.use(p):
            st = r.run("邻居家的树挡到我家采光了", session_id="e-" + pid)
        d = st.draft or ""
        ok(f"{pid} 空结果拒答里带本领域的补充提示",
           bool(p.empty_hint) and p.empty_hint in d, d[:60])
        ok(f"{pid} 空结果拒答不再叫人「说清怎么离职的」",
           not any(w in d for w in ("离职", "月工资", "劳动合同", "劳动仲裁")),
           d[:60])
        ok(f"{pid} 空结果拒答指向律师而不是劳动仲裁机构",
           "执业律师" in d, d[-30:])
    with profiles.use("labor"):
        st = AgentRunner(_NoHits(get_index(CORPUS_PATH)), llm, store).run(
            "邻居家的树挡到我家采光了", session_id="e-labor")
    ok("劳动法内核的空结果拒答逐字不变",
       (st.draft or "") == _REFUSE_EMPTY, (st.draft or "")[:40])

    # ---- 「有证据但管不到」那段，最后一句也不能把人送到劳动仲裁去 ----
    from app.agent import _irrelevant_text
    from app.schemas import AgentState, Budget, Verdict
    stx = AgentState(session_id="t-irr", question="邻居家的树挡到我家采光了",
                     budget=Budget(steps_left=3))
    stx.evidences = [ev("m-288", "民法典", 288)]
    stx.verdict = Verdict(kind="unknown", label="", legal="", reason="",
                          relevant=False, gap="缺的是相邻关系的具体事实")
    with profiles.use(profiles.resolve("family")):
        txt_f = _irrelevant_text(stx)
    with profiles.use("labor"):
        txt_l = _irrelevant_text(stx)
    ok("婚家内核答不上来时不让用户去打 12333",
       "12333" not in txt_f and "劳动人事争议仲裁" not in txt_f, txt_f[-40:])
    ok("婚家内核答不上来时建议找律师", "执业律师" in txt_f, txt_f[-30:])
    ok("劳动法内核这句仍是仲裁委 + 12333（逐字不变）",
       "12333" in txt_l and "劳动人事争议仲裁委员会" in txt_l, txt_l[-30:])

    # ---- 首屏问候：分区里的第一眼 ----
    with profiles.use(profiles.resolve("family")):
        g_f = (AgentRunner(get_profile_index(profiles.resolve("family")), llm,
                           MemoryStore(Path(tempfile.mkdtemp()) / "g.db"))
               .run("你好", session_id="g-fam").draft or "")
    ok("婚家首屏不说自己是劳动法助手", "劳动法" not in g_f, g_f[:24])
    ok("婚家首屏举的例子是婚家的",
       "离婚" in g_f or "抚养权" in g_f, g_f[:80])
    ok("婚家首屏不再教人「怎么离职的」", "离职" not in g_f)
    with profiles.use("labor"):
        g_l = (AgentRunner(get_index(CORPUS_PATH), llm,
                           MemoryStore(Path(tempfile.mkdtemp()) / "g2.db"))
               .run("你好", session_id="g-lab").draft or "")
    ok("劳动法首屏问候逐字不变", g_l.startswith("您好，我是劳动法咨询助手。"),
       g_l[:20])


# ---------------------------------------------------------------- [3] 检索分库

def check_retrieval() -> None:
    print("[3] 检索分库：劳动法索引零改动 / 接入内核不串库")
    full = LawIndex(CORPUS_PATH)                 # 默认参数 = v118 构造
    cached = get_index(CORPUS_PATH)
    q = "没签劳动合同 二倍工资 拖欠工资"
    a = [(round(s, 6), x["id"]) for s, x in full.search(q)]
    b = [(round(s, 6), x["id"]) for s, x in cached.search(q)]
    ok("默认 LawIndex 与单例索引分数逐位一致", a == b, str(a[:2]))
    ok("主语料一条没少", len(full.articles) == 2066, str(len(full.articles)))
    ok("默认索引不含接入库条文",
       not any(x["law_short"] in ("消费者权益保护法", "食品安全法")
               for x in full.articles))
    ok("labor 内核拿到的就是这份主索引",
       get_profile_index(profiles.PROFILES["labor"]) is cached)
    ok("GENERIC_BOOST 仍是劳动法口径",
       abs(profiles.PROFILES["labor"].boost - GENERIC_BOOST) < 1e-9)

    for pid, expected in (("consumer", {"消费者权益保护法", "食品安全法"}),
                          ("civil", {"民法典", "合同法"}),
                          ("traffic", {"民法典", "道路交通安全法"}),
                          ("family", {"民法典"})):
        p = profiles.PROFILES[pid]
        idx = get_profile_index(p)
        laws = {x["law_short"] for x in idx.articles}
        ok(f"{pid} 索引只含本领域法律", laws == expected, str(sorted(laws)))
        ok(f"{pid} 索引独立缓存、不与主索引混用", idx is not cached)
        ok(f"{pid} 索引加权不压制本领域",
           p.boost == 1.0 and not p.boost_laws)
    ci = get_profile_index(profiles.PROFILES["consumer"])
    # 不能写 get_article("劳动合同法","82") is None：get_article 有一步
    # 「条号唯一时忽略法律名」的兜底。语料只有 8 条摘录时它返回 None，
    # 补成 217 条全文后食安法第 82 条在库，兜底就会拿它顶上——断言假红。
    ok("消费内核索引里没有劳动合同法",
       "劳动合同法_82" not in ci.by_id
       and all(x["law_short"] != "劳动合同法" for x in ci.articles))
    ok("消费内核检索命中食品安全法第 148 条",
       any(x["id"] == "食品安全法_148"
           for _, x in ci.search("买到过期食品 十倍赔偿", topk=6)))
    ok("消费内核检索不到劳动争议解释一",
       all(x["law_short"] != "劳动争议解释一"
           for _, x in ci.search("劳动争议 仲裁", topk=6)))
    vi = get_profile_index(profiles.PROFILES["civil"])
    ok("合同内核检索命中民法典第 585 条",
       any(x["id"] == "民法典_585"
           for _, x in vi.search("约定的违约金过高 能减少吗", topk=6)))
    # 条号题走 get_article 精确取，不该指望 BM25 排序：问「合同法第114条」
    # 时 BM25 会把 112 / 344 / 51 等排在前面，这是分词层面的已知现象，
    # 项目本来就有 _law_near_article + get_article 来处理它。
    old = vi.get_article("合同法", "114")
    ok("合同内核版本题能精确取到已废止的合同法第 114 条",
       old is not None and old.get("status") == "已废止"
       and "违约金" in old.get("content", ""))
    ok("旧条号确实收录在合同内核索引里",
       "合同法_114" in vi.by_id)

    # 道交：民法典侵权责任编全量在库，只额外挂 4 条道交法摘录
    ti = get_profile_index(profiles.PROFILES["traffic"])
    ok("道交内核检索命中民法典第 1179 条",
       any(x["id"] == "民法典_1179"
           for _, x in ti.search("交通事故 误工费 护理费 赔偿项目", topk=8)))
    ok("道交内核能按条号取到道交法第 76 条",
       (ti.get_article("道路交通安全法", "76") or {}).get("article_no") == "76")
    ok("道交内核索引里没有刑法或消保法",
       not ({x["law_short"] for x in ti.articles} &
            {"消费者权益保护法", "食品安全法", "刑法"}))
    # 注意不能写 get_article("劳动合同法","82") is None：get_article 有一步
    # 「条号唯一时忽略法律名」的兜底，会拿民法典第 82 条顶上，断言会假红。
    ok("道交内核索引里没有劳动合同法",
       "劳动合同法_82" not in ti.by_id
       and all(x["law_short"] != "劳动合同法" for x in ti.articles))

    # 婚家：零新语料，只是把主索引按「民法典」过滤一遍
    fi = get_profile_index(profiles.PROFILES["family"])
    ok("婚家内核语料恰好是民法典全量", len(fi.articles) == 1260,
       str(len(fi.articles)))
    ok("婚家内核检索命中离婚冷静期条款",
       any(x["id"] in ("民法典_1076", "民法典_1077")
           for _, x in fi.search("协议离婚 冷静期 三十天", topk=8)))
    ok("婚家内核检索命中法定继承顺序",
       any(x["id"] in ("民法典_1127", "民法典_1130")
           for _, x in fi.search("遗产 法定继承 第一顺序", topk=8)))
    ok("婚家内核索引里没有劳动合同法",
       "劳动合同法_82" not in fi.by_id
       and all(x["law_short"] != "劳动合同法" for x in fi.articles))


# ---------------------------------------------------------------- [4] 定性

def check_verdict() -> None:
    print("[4] 定性规则表按内核切换")
    ok("劳动法规则表原样返回", verdict.active_rules() is verdict._RULES)
    ok("劳动法通用建议逐字不变",
       verdict.common_advice() == verdict.COMMON_ADVICE)
    with profiles.use("consumer"):
        ok("消费内核规则表已切换",
           verdict.active_rules() is not verdict._RULES)
        ok("消费内核通用建议不含 12333 / 劳动仲裁",
           not any(w in verdict.common_advice() for w in ("12333", "劳动仲裁")))
        v = verdict.judge_mock(
            "花了 200 元买到过期食品，能赔多少",
            [ev("食品安全法_148", "食品安全法", 148),
             ev("消费者权益保护法_55", "消费者权益保护法", 55)])
        ok("消费定性命中食品安全法 148",
           "食品安全法" in str(v.basis) and v.kind == "claimable",
           f"{v.kind} / {v.basis}")
        v2 = verdict.judge_mock(
            "商家卖假货 虚假宣传",
            [ev("消费者权益保护法_55", "消费者权益保护法", 55)])
        ok("消费定性命中消保法 55", "消费者权益保护法" in str(v2.basis))
    with profiles.use("civil"):
        v = verdict.judge_mock(
            "约定的违约金过高，能请求减少吗",
            [ev("民法典_585", "民法典", 585)])
        ok("合同定性命中民法典 585", "民法典" in str(v.basis))
        v2 = verdict.judge_mock(
            "合同法第114条还有效吗",
            [ev("合同法_114", "合同法", 114, "违约金"),
             ev("民法典_585", "民法典", 585, "违约金")])
        ok("版本题不再抄旧条原文，提示已废止",
           "废止" in (v2.legal + v2.label + v2.reason), str(v2.label))
    ok("退出内核后规则表恢复劳动法",
       verdict.active_rules() is verdict._RULES)
    with profiles.use("traffic"):
        v = verdict.judge_mock(
            "被车撞了，对方全责，误工费能赔多少",
            [ev("道路交通安全法_76", "道路交通安全法", 76),
             ev("民法典_1179", "民法典", 1179)])
        ok("道交定性命中赔偿项目或责任划分",
           "民法典" in str(v.basis) or "道路交通安全法" in str(v.basis),
           f"{v.kind} / {v.basis}")
        ok("道交通用建议不含劳动仲裁话术",
           not any(w in verdict.common_advice() for w in ("12333", "劳动仲裁")))
    with profiles.use("family"):
        v = verdict.judge_mock(
            "离婚时房子怎么分，孩子抚养费给多少",
            [ev("民法典_1062", "民法典", 1062),
             ev("民法典_1085", "民法典", 1085)])
        ok("婚家定性命中共同财产或抚养费",
           "民法典" in str(v.basis), f"{v.kind} / {v.basis}")
        ok("婚家通用建议不给比例",
           not re.search(r"[三四五六七]成|对半分", verdict.common_advice()))
    ok("退出内核后规则表仍是劳动法",
       verdict.active_rules() is verdict._RULES)
    ok("立场白名单覆盖五个内核且保留原四值",
       all(persona.normalize_stance(x) == x for x in
           ("employee", "employer", "agent", "explore", "buyer", "seller",
            "party_a", "party_b", "injured", "driver", "insurer",
            "spouse_a", "spouse_b", "heir")))
    ok("立场白名单仍拒绝瞎编值",
       persona.normalize_stance("CEO ") is None)


# ---------------------------------------------------------------- [5] 办案包

def check_extras() -> None:
    print("[5] 办案包：劳动法恰好五块 / 接入内核专属块")
    q = "公司拖欠两个月工资，没签合同"
    base = extras.from_case(q)
    ok("劳动法顶层恰好五块",
       set(base) == {"timeline", "claims", "evidence_check", "decision",
                     "persona"}, str(sorted(base)))
    ok("劳动法没有 domain 块", "domain" not in base)

    # SSE 答案流走的是 build_extras（AgentState 入口），不是 from_case。
    # 只测 from_case 会留一个真实的覆盖洞：把 build_extras 的分支改坏，
    # 全套照样全绿，而用户看到的办案包已经是错的。
    from app.schemas import AgentState, Budget  # noqa: E402

    def st(qq: str):
        return AgentState(session_id="t", question=qq,
                          budget=Budget(steps_left=8))

    s = extras.build_extras(st(q))
    ok("SSE 答案流的 build_extras 劳动法恰好五块",
       set(s) == set(base), str(sorted(set(s) ^ set(base))))
    ok("SSE 答案流与独立接口五块逐字段一致",
       all(s[k] == base[k] for k in base))
    with profiles.use("consumer"):
        s2 = extras.build_extras(st("买到过期食品能赔多少"))
        ok("SSE 答案流的 build_extras 消费内核带 domain",
           "domain" in s2 and s2["domain"]["id"] == "punitive_damages")
        ok("SSE 答案流的 build_extras 消费内核仍是六块",
           set(s2) == {"timeline", "claims", "evidence_check", "decision",
                       "persona", "domain"})

    for pid, domain_id in (("consumer", "punitive_damages"),
                           ("civil", "contract_check")):
        with profiles.use(pid):
            r = extras.from_case("买到过期食品能赔多少" if pid == "consumer"
                                 else "违约金过高能减少吗")
            ok(f"{pid} 顶层是五块 + domain",
               set(r) == {"timeline", "claims", "evidence_check", "decision",
                          "persona", "domain"}, str(sorted(r)))
            ok(f"{pid} 专属块 id 正确", r["domain"]["id"] == domain_id)
            blob = json.dumps(r, ensure_ascii=False)
            clean = json.dumps(scrub(r), ensure_ascii=False)
            ok(f"{pid} 办案包不含劳动争议话术",
               not any(w in blob for w in _LABOR_SMELL))
            ok(f"{pid} 办案包不给胜率或建议起诉",
               not re.search(r"胜诉率|成功率|赢面|(?:不?建议|应当)(?:起诉|申诉|仲裁)",
                             clean))
            ok(f"{pid} 但保留了「不提供胜率」的明确声明",
               "no_win_rate" in blob and "胜率" in blob)
            d = r["decision"]
            ok(f"{pid} 判例卡仍是四键",
               set(d) == {"scope", "similar", "cost_benefit", "policy"})
            ok(f"{pid} 不把劳动争议案例串进来",
               d["similar"]["items"] == []
               and d["similar"]["distribution"]["insufficient"] is True)
            ok(f"{pid} 立场选项是领域化的",
               r["persona"]["stance"] is None
               and r["persona"]["stance_source"] == "unset"
               and r["persona"]["stance_options"] != persona.STANCES)
            ok(f"{pid} 请求事项金额留空",
               "＿＿＿＿" in json.dumps(r["claims"], ensure_ascii=False))
    ok("退出内核后办案包恢复恰好五块",
       set(extras.from_case(q)) == set(base))


# ---------------------------------------------------------------- [6] 专属能力

def check_domains() -> None:
    print("[6] 专属能力：金额只算用户给的 / 不许猜")
    ok("BLANK 占位符写死", domains.BLANK == "＿＿＿＿")
    d = domains.punitive_damages("花了 200 元买到过期食品，能赔多少")
    ok("消费场景读到 200 元", d["price_read"] == 200)
    ten = next(s for s in d["scenarios"] if "十倍" in s["label"])
    ok("价款十倍且 1000 元保底", ten["amount"] == 2000, str(ten["amount"]))
    ok("金额文本不是占位符", ten["amount_text"] == "2000")
    d2 = domains.punitive_damages("买了 30 元的假货，说能退一赔三吗")
    tri = next(s for s in d2["scenarios"] if "三倍" in s["label"])
    ok("价款三倍不足 500 按 500", tri["amount"] == 500, str(tri["amount"]))
    d3 = domains.punitive_damages("买到过期食品能赔多少")
    ok("没给金额时读数为空", d3["price_read"] is None)
    ok("没给金额时金额一律留空",
       all(s["amount"] is None and s["amount_text"] == "＿＿＿＿"
           for s in d3["scenarios"]))
    ok("没给金额时明说请填实际金额", "实际支付金额" in d3["note"])
    # 这两个分支曾经没有断言：把「金额超范围」那行改坏，全套照样全绿。
    # 补上之后，「无匹配 / 0 元 / 超大数」三条留空路径都有人看着。
    ok("0 元不算真实交易，金额留空",
       domains.punitive_damages("花了 0 元买到过期食品")["price_read"] is None
       and all(s["amount"] is None for s in
               domains.punitive_damages("花了 0 元")["scenarios"]))
    ok("超大金额不采信，金额留空",
       domains.punitive_damages("花了 999999999999 元买到过期食品")
       ["price_read"] is None)
    ok("中文数字金额不硬猜（五百元不解析成 500）",
       domains.punitive_damages("花了五百元买到假货")["price_read"] is None)
    ok("十倍与三倍不能叠加（提示择一）",
       "择一" in json.dumps(domains.punitive_damages("过期食品"), ensure_ascii=False))
    ok("专属块声明不给胜率/建议索赔，正文里也没有伪精确数字",
       "no_win_rate" in d and "不提供" in d["no_win_rate"]
       and not re.search(r"\d+(?:\.\d+)?\s*%|胜率\s*\d+|成功率\s*\d+",
                         json.dumps(scrub(d), ensure_ascii=False))
       and "建议索赔" not in json.dumps(scrub(d), ensure_ascii=False))
    ok("专属块声明不猜金额", "no_guess" in d)

    c = domains.contract_check("合同法第114条还有效吗，违约金过高怎么调整")
    ok("合同核查按问句列出相关项且都标了 hit",
       2 <= len(c["items"]) <= 5
       and all(isinstance(x["hit"], bool) for x in c["items"]),
       str(len(c["items"])))
    ok("版本衔接项命中", any(x["id"] == "version" and x["hit"] for x in c["items"]))
    ok("版本项指明民法典 585 且说明旧条废止",
       any("第五百八十五条" in x["basis"] and "废止" in x["rule"]
           for x in c["items"]))
    ok("违约金项命中", any(x["id"] == "penalty" and x["hit"] for x in c["items"]))
    c2 = domains.contract_check("你好")
    ok("问句无主题时列全五项待核对", len(c2["items"]) == 5
       and all(not x["hit"] for x in c2["items"]))
    ok("合同核查不给具体倍数、不预测结果",
       # v136：原来判「本系统不给具体倍数」——那句是系统自述，已改成
       # 「倍数由裁判机关裁量」。判据跟着改成判语义，不再咬原话。
       "倍数由裁判机关裁量" in json.dumps(c, ensure_ascii=False)
       and "只列法定要件与条号" in c["note"])

    # ---- 道交内核：给计算式，不给数字
    t = domains.traffic_compensation(
        "被车撞了，误工费和护理费能赔多少，住院花了 5000 元")
    ok("道交清单列满十个赔偿项目", len(t["scenarios"]) == 10,
       str(len(t["scenarios"])))
    ok("道交命中项排在最前",
       t["scenarios"][0].get("hit") and t["scenarios"][1].get("hit"))
    ok("道交金额一律留空（连用户给的 5000 也不采用）",
       all(s["amount"] is None and s["amount_text"] == domains.BLANK
           for s in t["scenarios"]))
    ok("道交给出交强险三个分项限额",
       len(t["insurance"]["limits"]) == 3
       and t["insurance"]["basis"] == "《道路交通安全法》第七十六条")
    ok("交强险限额标明以保单为准", "保单" in t["insurance"]["note"])
    ok("道交声明责任比例以事故认定书为基础",
       # v136：原来判「不重新划分责任」——那句是系统自述，已改成陈述句。
       # 判据改成判语义：条件里必须点明「责任比例」取自「事故认定书」。
       any("责任比例" in c and "事故认定书" in c for c in t["conditions"]),
       t["conditions"])
    ok("道交明说精神损害没有法定计算式",
       any("不设固定公式" in s["formula"] and "没有法定计算式" in s["note"]
           for s in t["scenarios"]))
    ok("道交不给胜率、不估算", "no_win_rate" in t and "no_guess" in t)
    ok("道交正文不出现赔偿数字",
       not re.search(r"\d[\d,\.]*\s*元(?!.*保单)", "".join(
           s["formula"] + s["note"] for s in t["scenarios"])))

    # ---- 婚家内核：只给要件，不给比例
    f = domains.family_split("离婚房子怎么分，孩子抚养费给多少")
    ok("婚家清单七项齐全", len(f["items"]) == 7, str(len(f["items"])))
    ok("婚家命中项排在最前", f["items"][0]["hit"] and f["items"][1]["hit"])
    ok("婚家不给分割比例",
       not re.search(r"[三四五六七]成|对半分|各分一半",
                     json.dumps(f, ensure_ascii=False)))
    ok("婚家明说旧法条号已废止",
       any("废止" in c for c in f["conditions"]))
    ok("婚家继承顺序项存在且带条号",
       any(x["id"] == "inherit_order" and "第一千一百二十七条" in x["basis"]
           for x in f["items"]))
    ok("婚家不问主题时也列全七项",
       all(not x["hit"] for x in domains.family_split("你好")["items"]))


# ---------------------------------------------------------------- [7] 真服务

def _sse_data(raw: bytes) -> list[dict]:
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except (ValueError, TypeError):
                pass
    return out


def check_service() -> None:
    print("[7] 真服务：内核绑定、防串库、办案包重算")
    from _client import Client
    from _svc import start_service, stop_service
    db = Path(tempfile.mkdtemp(prefix="law_prof_")) / "p.db"
    env = {"LAW_DB_PATH": str(db), "ADMIN_PASSWORD": "Probe#Profiles",
           "DEEPSEEK_API_KEY": ""}
    svc, base = start_service(env=env)
    try:
        c = Client(base)
        ok("登录临时账号", bool(c.login("admin", "Probe#Profiles")))
        _, meta = c.get("/api/meta")
        ok("meta 返回五个内核与默认值",
           len(meta.get("profiles") or []) == 5
           and meta.get("default_profile") == "labor")
        ok("meta 明说不按提问猜领域", "不根据提问自动猜测" in
           str(meta.get("profile_note", "")))

        # 新会话显式选消费内核
        st, raw = c.raw("/api/chat", {
            "question": "花了 200 元买到过期食品，能赔多少",
            "profile": "consumer"})
        ok("消费内核提问返回 200", st == 200, str(st))
        evs = _sse_data(raw)
        sid = next((d.get("session_id") for d in evs if "session_id" in d), "")
        ok("SSE 带回 session_id", bool(sid))
        _, sess = c.get("/api/chat/session/" + sid) if sid else (0, {})
        ok("会话落库 profile_id=consumer", sess.get("profile_id") == "consumer",
           str(sess.get("profile_id")))
        ex = next((d for d in evs if "domain" in d), None)
        ok("SSE extras 带专属能力块", ex is not None)
        if ex:
            ok("专属块是惩罚性赔偿测算",
               ex["domain"].get("id") == "punitive_damages")
            ok("专属块不含劳动争议话术",
               not any(w in json.dumps(ex, ensure_ascii=False)
                       for w in _LABOR_SMELL))
        answer = json.dumps(evs, ensure_ascii=False)
        ok("答案流不出现劳动仲裁 / 经济补偿",
           "劳动仲裁" not in answer and "经济补偿" not in answer)

        # 已有会话：再传别的 profile 也不能改内核
        st2, raw2 = c.raw("/api/chat", {
            "question": "那经营者不认怎么办",
            "session_id": sid, "profile": "civil"})
        ok("已有会话继续提问返回 200", st2 == 200, str(st2))
        _, sess2 = c.get("/api/chat/session/" + sid)
        ok("已有会话忽略后来传的 profile（防串库）",
           sess2.get("profile_id") == "consumer", str(sess2.get("profile_id")))

        # 办案包接口按会话内核重算
        _, pack = c.post("/api/case/extras", {"session_id": sid})
        ok("办案包接口按会话内核出 domain", pack.get("domain", {}).get("id")
           == "punitive_damages")
        ok("办案包接口回带 profile", pack.get("profile") == "consumer")
        ok("办案包接口顶层仍是五块+domain",
           {"timeline", "claims", "evidence_check", "decision",
            "persona", "domain"} <= set(pack))

        # 劳动法会话：一切照旧
        st3, raw3 = c.raw("/api/chat", {
            "question": "没签劳动合同，还拖欠两个月工资"})
        ok("不传 profile 时默认劳动法", st3 == 200)
        evs3 = _sse_data(raw3)
        ex3 = next((d for d in evs3 if "timeline" in d), None)
        ok("劳动法 SSE 办案包恰好五块 + 两个附带字段",
           ex3 is not None
           and {"timeline", "claims", "evidence_check", "decision",
                "persona"} <= set(ex3) and "domain" not in ex3)
        sid3 = next((d.get("session_id") for d in evs3
                     if "session_id" in d), "")
        _, sess3 = c.get("/api/chat/session/" + sid3) if sid3 else (0, {})
        ok("老会话未绑定内核时回落 labor",
           sess3.get("profile_id") == "labor", str(sess3.get("profile_id")))

        # 图片证据录入在接入内核下明确关掉
        st4, img = c.post("/api/evidence/extract", {
            "session_id": sid, "image": "AAAA"})
        ok("接入内核下图片录入返回 400 且说明原因",
           st4 == 400 and "profile_unsupported" in json.dumps(img,
                                                              ensure_ascii=False),
           str(st4))
        # 非法内核回落劳动法，不猜
        st5, raw5 = c.raw("/api/chat", {
            "question": "公司不给加班费", "profile": "CRIMINAL"})
        ok("非法内核名不报错、回落劳动法", st5 == 200)
        sid5 = next((d.get("session_id") for d in _sse_data(raw5)
                     if "session_id" in d), "")
        _, sess5 = c.get("/api/chat/session/" + sid5) if sid5 else (0, {})
        ok("非法内核落库为 labor", sess5.get("profile_id") == "labor")
    finally:
        stop_service(svc)


# ---------------------------------------------------------------- [7] 任务归属
def check_task_profile() -> None:
    """任务也要记得自己是哪部法的（v122）。

    为什么单独测这一层：任务的法域在两处会被读——任务卡的配色、进入任务时
    整页换不换皮。两处都靠 `_TASK_SQL` 那一个 COALESCE，而它有两个分支
    （任务自己的列 / 最近一次会话），写错一个分支时页面上的表现是
    「有的任务有颜色、有的默认成劳动法」，看起来像随机。
    """
    import sqlite3
    from app.memory import MemoryStore

    tmp = Path(tempfile.mkdtemp()) / "tp.db"
    try:
        st = MemoryStore(tmp)
        uid = 1

        # 建的时候带内核 → 查回来要还是它（空任务没有会话可回落）
        t1 = st.create_task(uid, "买到过期食品", profile_id="consumer")
        ok("空任务记住建它时选的内核",
           st.get_task(t1["task_id"], uid)["profile_id"] == "consumer")

        # 不带内核建 → 回落劳动法，不是 NULL 也不是空串
        t2 = st.create_task(uid, "拖欠工资")
        ok("没指定内核的任务回落 labor",
           st.get_task(t2["task_id"], uid)["profile_id"] == "labor")

        # 任务有了第一个会话 → 采纳会话的内核
        st.ensure_session("ts1", uid, t2["task_id"], profile_id="family")
        ok("任务采纳第一个会话的内核",
           st.get_task(t2["task_id"], uid)["profile_id"] == "family")

        # 同一任务里再来一条别的内核的会话 → 任务的内核不许被改
        st.ensure_session("ts2", uid, t2["task_id"], profile_id="civil")
        ok("后来的会话不改任务的法域",
           st.get_task(t2["task_id"], uid)["profile_id"] == "family")

        # 显式标过的任务，不被会话覆盖
        t3 = st.create_task(uid, "交通理赔", profile_id="traffic")
        st.ensure_session("ts3", uid, t3["task_id"], profile_id="labor")
        ok("显式标过的任务不被会话覆盖",
           st.get_task(t3["task_id"], uid)["profile_id"] == "traffic")

        # 老库场景：任务这一列是 NULL、会话里记着内核 → 按会话回落
        st._conn.execute("UPDATE tasks SET profile_id=NULL WHERE task_id=?",
                         (t3["task_id"],))
        st._conn.commit()
        ok("老任务按最近一次会话回落",
           st.get_task(t3["task_id"], uid)["profile_id"] == "labor")

        # 列表里每条都要带 profile_id，前端才画得出各自的颜色
        items = st.list_tasks(uid)
        ok("任务列表每条都带 profile_id",
           all(it.get("profile_id") for it in items) and len(items) == 3,
           str([it.get("profile_id") for it in items]))

        # v123：老库里的无主会话按内核分桶，不合并成一个任务。
        # 合并的后果不是「少一个任务」，而是先问食安再问劳动法变成同一个
        # 案子：要素合并、文书摘录、任务卡颜色全串在一起。
        st.ensure_session("loose_a", uid, None, profile_id="consumer")
        st.ensure_session("loose_b", uid, None, profile_id="labor")
        st.assign_loose_to_task(uid)
        tid_a = st.session_task("loose_a")
        tid_b = st.session_task("loose_b")
        ok("无主会话按内核分成两个任务",
           bool(tid_a) and bool(tid_b) and tid_a != tid_b,
           f"{tid_a} / {tid_b}")
        ok("劳动法那个桶仍叫「历史咨询」",
           st.get_task(tid_b, uid)["title"] == "历史咨询",
           st.get_task(tid_b, uid)["title"])
        ok("非劳动法的桶名字带内核名",
           st.get_task(tid_a, uid)["title"] == "历史咨询 · 消费与食品安全",
           st.get_task(tid_a, uid)["title"])
        ok("桶自己记住内核，不跟着最后一条会话变色",
           st.get_task(tid_a, uid)["profile_id"] == "consumer",
           st.get_task(tid_a, uid)["profile_id"])
        st.close()
    finally:
        import shutil
        shutil.rmtree(tmp.parent, ignore_errors=True)


def check_auto_task() -> None:
    """每一次新咨询自动落成一个任务（v123）。

    为什么非要走真服务：任务是在 /api/chat 里建的，前端靠 SSE 的 session
    事件把 task 接过去画任务条。只测 MemoryStore 测不到这条链——「建了任务
    但没告诉前端」和「压根没建任务」在库里的样子几乎一样，而用户在界面上
    看到的是两种完全不同的东西（有任务条 / 没有）。

    也要挡住反向的错误：把新法的会话塞进旧法的任务，库能写成功、接口也回
    200，只有把「旧任务的会话数没变」这件事数出来才看得见。
    """
    from _client import Client
    from _svc import start_service, stop_service
    db = Path(tempfile.mkdtemp(prefix="law_auto_")) / "a.db"
    svc, base = start_service(env={"LAW_DB_PATH": str(db),
                                   "ADMIN_PASSWORD": "Probe#Auto",
                                   "DEEPSEEK_API_KEY": ""})
    try:
        c = Client(base)
        ok("登录临时账号（自动任务）", bool(c.login("admin", "Probe#Auto")))

        def ask(q, profile=None, sid=None, tid=None):
            body = {"question": q}
            if profile:
                body["profile"] = profile
            if sid:
                body["session_id"] = sid
            if tid:
                body["task_id"] = tid
            st, raw = c.raw("/api/chat", body)
            evs = _sse_data(raw)
            return st, next((d for d in evs if "session_id" in d), {})

        st, s1 = ask("买到过期食品能赔多少", "consumer")
        ok("没有任务时提问返回 200", st == 200, str(st))
        ok("SSE 的 session 事件带回 task 对象",
           bool(s1.get("task")) and s1.get("task_id") == s1["task"].get("task_id"),
           str(s1)[:140])
        ok("自动建的任务拿第一句提问当名字",
           (s1.get("task") or {}).get("title") == "买到过期食品能赔多少",
           str((s1.get("task") or {}).get("title")))
        ok("自动建的任务记住这次咨询的内核",
           (s1.get("task") or {}).get("profile_id") == "consumer")
        tid1 = s1.get("task_id")

        # 同一会话接着问：还是这个任务，不新建
        st, s2 = ask("那经营者不认怎么办", None, sid=s1.get("session_id"))
        ok("同一会话追问不新建任务", s2.get("task_id") == tid1,
           f"{s2.get('task_id')} vs {tid1}")
        _, tl = c.get("/api/tasks")
        ok("一次咨询只产生一个任务", len(tl.get("items") or []) == 1,
           str([t.get("title") for t in tl.get("items") or []]))

        # 换内核再问、手里还攥着旧 task_id（前端切了法没退任务）→ 不许塞进去
        st, s3 = ask("公司没签合同能要二倍工资吗", "labor", tid=tid1)
        ok("换内核后不落进旧法的任务",
           bool(s3.get("task_id")) and s3["task_id"] != tid1,
           f"{s3.get('task_id')} vs {tid1}")
        ok("为它新开的是劳动法任务",
           (s3.get("task") or {}).get("profile_id") == "labor")
        _, old = c.get("/api/tasks/" + tid1)
        ok("旧任务一个会话都没多",
           (old.get("task") or {}).get("n_sessions") == 1,
           str((old.get("task") or {}).get("n_sessions")))
        _, tl = c.get("/api/tasks")
        ok("两次咨询是两个任务", len(tl.get("items") or []) == 2,
           str([t.get("title") for t in tl.get("items") or []]))
        ok("两个任务分属两个法",
           {t.get("profile_id") for t in tl.get("items") or []}
           == {"consumer", "labor"},
           str([(t.get("title"), t.get("profile_id")) for t in tl.get("items") or []]))

        # 寒暄不该变成任务名（「你好」当标题等于没有名字）
        st, s4 = ask("你好", "labor")
        ok("寒暄不用「你好」当任务名",
           (s4.get("task") or {}).get("title") not in ("你好", ""),
           str((s4.get("task") or {}).get("title")))
    finally:
        stop_service(svc)


def main() -> int:
    print("可插拔法律内核回归（v119）")
    print("=" * 62)
    check_registry()
    check_prompts()
    check_clarify()
    check_v135_open_gate()
    check_empty_refusal()
    check_retrieval()
    check_verdict()
    check_extras()
    check_domains()
    check_service()
    check_task_profile()
    check_auto_task()
    print("=" * 62)
    if FAILED:
        print(f"{TOTAL} 项中 {len(FAILED)} 项失败：")
        for n in FAILED:
            print("  - " + n)
        return 1
    print(f"{TOTAL} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
