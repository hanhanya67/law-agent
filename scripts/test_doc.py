# -*- coding: utf-8 -*-
"""仲裁申请书的端到端测试：工具、决策、生成、答案层四段都要管住。

为什么单独一个脚本：文书是最容易「看起来对、实际不能用」的功能。

  - 最危险的不是生成失败，而是**生成得太顺**——把赔偿金额替你估一个数字
    填进去。当事人拿去提交，仲裁委按他写错的金额立了案，改请求要走变更
    程序，庭前调解的筹码也没了。所以正文里「仲裁请求金额」必须永远是
    ＿＿＿＿，工具只能填事实、不能填价钱。这条要用断言钉死。
  - 第二危险的是个人信息混进案情槽位。档案里的姓名、电话是「一次填、
    永久用」的，一旦被当成案情记忆写进 slots，下次换个案子问「我能拿
    多少」，系统会拿着上一个人的信息去算。
  - 第三是决策边界。用户说「帮我写个辞职信」时若触发文书工具，会秒回
    一份仲裁申请书——答非所问，且显得系统在推销。

跑法：
    ./.venv/Scripts/python.exe scripts/test_doc.py
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试库建在系统临时目录，退出时整目录删掉。理由见下面 `_t_e2e.db` 那处注释：
# 测试产物不该写进 data/，那是交付目录。
_TMPDIR = Path(tempfile.mkdtemp(prefix="lawagent_test_"))
atexit.register(shutil.rmtree, _TMPDIR, ignore_errors=True)

# 强制回放：这一套全部断言都基于 `_mock_*` 的确定性输出，一旦有 key，
# `maybe_recover()` 会在 600 秒冷却后自动联回在线，端到端那几条就会去调真模型，
# 结果时好时坏（实测过一次：加了 key 跑，端到端直接没调文书工具）。
# 在线能力由 `eval/run_eval.py --online` 负责，不在这里测。
os.environ.pop("DEEPSEEK_API_KEY", None)

from app import docgen                                            # noqa: E402
from app.config import CORPUS_PATH                                # noqa: E402
from app.llm import LLM, _doc_brief                               # noqa: E402
from app.retrieval import LawIndex                                # noqa: E402
from app.schemas import AgentState, CaseSlots, Step                # noqa: E402
from app.tools import DocArgs, ToolContext, draft_document         # noqa: E402

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" +
          (f"    {detail}" if detail and not cond else ""))


IDX = LawIndex(CORPUS_PATH)

FULL_PROFILE = {
    "real_name": "张三",
    "phone": "13800000000",
    "company_name": "某某科技有限公司",
    "id_card": "110101199001011234",
}


def mk_state(**kw) -> AgentState:
    st = AgentState(session_id="_t", question="帮我写仲裁申请书")
    if kw.pop("slots", None) is not False:
        st.slots = CaseSlots(region="北京", start_date="2024-03", end_date="2025-09",
                             monthly_wage=12000, leave_reason="公司辞退",
                             has_contract=True)
    for k, val in kw.items():
        setattr(st, k, val)
    return st


def draft(state: AgentState, profile: dict | None = None):
    return draft_document(DocArgs(), ToolContext(index=IDX, state=state,
                                                 profile=profile if profile is not None else FULL_PROFILE))


def body_of(res) -> str:
    return (res.data or {}).get("text") or ""


def block(text: str, head: str, tail: str) -> str:
    """截出两个小标题之间的一段，用于「只在这一段里断言」。"""
    i = text.find(head)
    j = text.find(tail, i + 1)
    return text[i:j] if i >= 0 and j > i else ""


def main() -> int:
    print("\n== 要素齐：该填的填、不该填的不填 ==")
    st = mk_state()
    r = draft(st)
    b = body_of(r)
    ok("工具不报错", not r.error, str(r.error))
    ok("ready = True（可以交付）", (r.data or {}).get("ready") is True)
    ok("missing 为空", (r.data or {}).get("missing") == [],
       str((r.data or {}).get("missing")))
    ok("missing_profile 为空", (r.data or {}).get("missing_profile") == [],
       str((r.data or {}).get("missing_profile")))
    ok("summary 说的是「已生成」而不是「还不能交」",
       "已生成" in r.summary and "还不能直接交" not in r.summary, r.summary)
    ok("正文标题是劳动人事争议仲裁申请书", "劳动人事争议仲裁申请书" in b)
    for sec in ("申请人", "被申请人", "仲裁请求", "事实与理由", "证据清单", "此致"):
        ok(f"正文含「{sec}」段", sec in b)
    ok("正文末尾有「不是法律文书成品」的自认",
       "不是法律文书成品" in b)
    # 已给的事实必须真的落到纸面，否则等于没填
    ok("月工资 12000 落进正文", "月工资12000元" in b)
    ok("入职时间落进正文", "2024年3月入职" in b)
    ok("离职时间落进正文", "2025年9月解除" in b)
    ok("解除方式落进正文", "解除方式：公司辞退" in b)
    ok("已签合同的事实落进正文", "订立了书面劳动合同" in b)
    ok("个人档案的姓名落进正文", "姓　　名：张三" in b)
    ok("个人档案的电话落进正文", "13800000000" in b)
    ok("个人档案的单位名落进正文", "某某科技有限公司" in b)
    ok("档案没填的住址留空占位（不编）", "住　　址：＿＿＿＿" in b)

    print("\n== 最关键：请求事项的金额必须留空 ==")
    # 工具能算钱（v109），但**不能**把算出来的钱写进申请书。算错了是建议，
    # 写进仲裁请求就是当事人的诉讼主张，性质完全不同。
    req = block(b, "仲裁请求", "事实与理由")
    ok("截到了仲裁请求段（否则下面的断言全是空列表里恒真）", len(req) > 80,
       f"len={len(req)}")
    ok("仲裁请求段里没有任何具体金额", not re.search(r"\d+\s*元", req),
       str(re.findall(r"\d+\s*元", req))[:120])
    ok("仲裁请求段里每处金额都是 ＿＿＿＿ 占位", req.count("＿＿＿＿元") >= 3,
       f"count={req.count('＿＿＿＿元')}")
    ok("明确要求用户自己核对金额后再抄", "金额不要自己估" in req)

    print("\n== 缺要素：只报真缺的，且骨架照给 ==")
    st2 = mk_state(slots=False)
    st2.slots = CaseSlots(start_date="2024-03")
    r2 = draft(st2, profile={})
    m = (r2.data or {}).get("missing") or []
    ok("缺月工资被报出来", "月工资" in m, str(m))
    ok("缺离职方式被报出来", "离职方式" in m, str(m))
    ok("已给的入职时间不被误报", "入职时间" not in m, str(m))
    ok("ready = False", (r2.data or {}).get("ready") is False)
    ok("summary 明说「还不能直接交」", "还不能直接交" in r2.summary, r2.summary)
    ok("缺要素不是报错，正文照给", len(body_of(r2)) > 800, str(len(body_of(r2))))
    mp = (r2.data or {}).get("missing_profile") or []
    ok("空档案时报缺姓名", "你的姓名" in mp, str(mp))
    ok("空档案时报缺电话", "联系电话" in mp, str(mp))
    ok("空档案时报缺单位全称", "单位全称" in mp, str(mp))
    ok("正文里姓名是占位而不是 None", "姓　　名：＿＿＿＿" in body_of(r2),
       "出现 None 说明 _pf 兜底漏了")
    ok("正文里不出现字符串 None", "None" not in body_of(r2))

    print("\n== 档案不许污染案情槽位 ==")
    # 这是 ToolContext.profile 存在的理由：档案是「人」的，槽位是「案」的。
    st3 = mk_state()
    before = dict(st3.slots.known())
    draft(st3)
    after = dict(st3.slots.known())
    ok("档案字段没被写进槽位",
       not any(k in after for k in ("real_name", "phone", "company_name", "id_card")),
       str(after))
    ok("槽位内容一字未变", before == after, f"{before} -> {after}")

    print("\n== 决策侧：什么时候该写、什么时候不该写 ==")
    llm = LLM()
    llm.online = False
    # 只设 online=False 不够：`.env` 里有 key 时 LLM_ENABLED 为真，
    # `AgentRunner.run()` 每轮开头会调 maybe_recover()，冷却 600 秒一过就自己
    # 联回在线。实测过一次——端到端那两条因此去调了真模型，然后失败。
    llm.maybe_recover = lambda: False        # noqa: E731

    def decide(q: str, **kw) -> tuple[list[str], bool]:
        s = AgentState(session_id="_d", question=q)
        for k, v in kw.items():
            setattr(s, k, v)
        d = llm._mock_decide(s)
        return [c.name for c in d.tool_calls], d.is_final

    names, _ = decide("帮我写一份劳动仲裁申请书")
    ok("「帮我写一份劳动仲裁申请书」→ 调文书工具", names == ["draft_document"], str(names))
    ok("文书题不先去检索（写文书不需要先查条文）",
       "search_statute" not in names, str(names))
    names, _ = decide("我要准备一份仲裁申请书")
    ok("「我要准备一份仲裁申请书」也算要文书", "draft_document" in names, str(names))
    names, _ = decide("劳动仲裁申请书怎么写")
    ok("「申请书怎么写」给模板（问法≠不要）", "draft_document" in names, str(names))
    names, _ = decide("帮我写个辞职信")
    ok("「帮我写个辞职信」不误触发（有「写」字但不是仲裁申请书）",
       "draft_document" not in names, str(names))
    names, _ = decide("仲裁申请书要交什么材料")
    ok("「要交什么材料」是咨询，不生成正文",
       "draft_document" not in names, str(names))
    # 已经生成过：必须收手。实测模型会顺手再多查一次条文。
    s_done = AgentState(session_id="_d2", question="帮我写一份劳动仲裁申请书")
    s_done.scratchpad = [Step(index=0, tool="draft_document",
                              data={"text": "x"})]
    d = llm._mock_decide(s_done)
    ok("文书已生成 → is_final（不再多调工具）", d.is_final, str([c.name for c in d.tool_calls]))
    ok("文书已生成 → 不再调任何工具", not d.tool_calls, str([c.name for c in d.tool_calls]))

    print("\n== 生成侧：正文要原样递进 prompt ==")
    s_brief = mk_state()
    doc_step = Step(index=0, tool="draft_document",
                    data={"text": body_of(r), "missing": [],
                          "missing_profile": [], "ready": True})
    s_brief.scratchpad = [doc_step]
    brief = _doc_brief(s_brief)
    ok("有文书时 brief 非空", bool(brief))
    ok("brief 里有正文内容（否则模型手里没东西可交）",
       "劳动人事争议仲裁申请书" in brief, brief[:120])
    ok("brief 要求原样交出、不要重写",
       "原样" in brief and "不要重写" in brief, brief[:200])
    # 没有文书时必须给一句「没有」的占位，不能返回空串：
    # ANSWER_SYSTEM 里是「【已生成的文书】\n__DOC__」，替换成空串就变成
    # 一个光秃秃的小标题，模型容易顺着它编一份不存在的文书。
    ok("没有文书时给出明确的「没有」占位（不是空串）",
       _doc_brief(mk_state()).strip() == "（本轮没有生成文书）",
       repr(_doc_brief(mk_state())))

    print("\n== 答案层：文书不走三段式 ==")
    s_gen = mk_state()
    # 设成真实的题型。文书题经 classify_kind 会落到 claim（「申请书」里有
    # 「申请」两字），默认值 lookup 是另一条代码路径，不设会测错分支。
    s_gen.question_kind = "claim"
    s_gen.scratchpad = [doc_step]
    got: list[str] = []
    text = llm._mock_generate(s_gen, lambda k, p: got.append(k) if k == "token" else None)
    ok("文书题有答案", bool(text))
    ok("文书题不出【结论】", "【结论】" not in text, text[:80])
    ok("文书题不出【依据】", "【依据】" not in text, text[:80])
    ok("文书题不套【建议】三段", "【建议】" not in text, text[:80])
    ok("答案里带完整正文", "劳动人事争议仲裁申请书" in text and "证据清单" in text)
    ok("答案结尾有免责声明", "不构成法律意见" in text)
    ok("答案包含正文自己的免责段（不是被裁掉的）",
       "不是法律文书成品" in text)
    ok("要素齐时说明「金额故意留空」", "故意留空" in text, text[-260:])

    # 缺要素时，答案应该把「缺什么」讲清楚，而不是装作齐了
    s_gen2 = mk_state(slots=False)
    s_gen2.slots = CaseSlots(start_date="2024-03")
    s_gen2.question_kind = "claim"
    r_miss = draft(s_gen2, profile={})
    s_gen2.scratchpad = [Step(index=0, tool="draft_document", data=r_miss.data)]
    text2 = llm._mock_generate(s_gen2, lambda k, p: None)
    ok("缺要素时答案里逐项列出", "还缺这些案情要素" in text2, text2[-300:])
    ok("缺档案时指路「个人档案」", "个人档案" in text2, text2[-300:])
    ok("缺要素时不说「故意留空」（要素都没齐，谈不上）",
       "故意留空" not in text2, text2[-300:])

    # 崩溃路径：question_kind 是 lookup、但没有证据、只有文书。
    # 这是「有文书、无证据」的合法状态——文书不检索条文，evidences 就是空的。
    # 原代码 lookup 分支第一句 `main_ev = (valid or dead)[0]` 直接 IndexError，
    # 整个回答崩掉。现有流程里文书题恰好被分成 claim 所以没暴露，但默认值就是
    # lookup，任何绕过 classify_kind 的调用方都会踩到。
    s_lk = mk_state()
    s_lk.question_kind = "lookup"
    s_lk.scratchpad = [doc_step]
    try:
        text3 = llm._mock_generate(s_lk, lambda k, p: None)
        crashed = False
    except IndexError as e:
        text3, crashed = "", True
        err = e
    ok("lookup + 无证据 + 有文书 不崩", not crashed,
       "" if not crashed else str(err))
    ok("上述情况下仍然交付完整文书",
       "劳动人事争议仲裁申请书" in (text3 or ""), (text3 or "")[:80])

    print("\n== 答案层：文书题不许出「未标注条文依据」的假警报 ==")
    # 申请书的「法律依据」一栏是故意留空的，正文必然没有条号。
    # 自检若照实提示，用户会以为系统查不到条文——这是假警报，必须滤掉。
    from app.agent import AgentRunner                                  # noqa: E402
    from app.memory import MemoryStore                                  # noqa: E402
    # 建在系统临时目录，不落在 data/。原来写死 data/_tmp_td.db，跑一次
    # 就在交付目录留一个 100 KB 的库，而且从不删——交付包里会多出一个
    # 谁也说不清来历的 _tmp_td.db。测试产物没有理由写进交付目录。
    store = MemoryStore(_TMPDIR / "_t_e2e.db")
    # atexit 是逆序执行的：这行注册比上面 rmtree 晚，所以先 close 再删目录。
    # 顺序反过来，Windows 上文件句柄还开着，rmtree 会静默失败。
    atexit.register(store.close)
    runner = AgentRunner(IDX, llm, store)
    st_e2e = runner.run("帮我写一份劳动仲裁申请书", session_id="_t_e2e")
    ok("端到端 status = answered", st_e2e.status == "answered", st_e2e.status)
    ok("真的调了文书工具",
       [s.tool for s in st_e2e.scratchpad] == ["draft_document"],
       str([s.tool for s in st_e2e.scratchpad]))
    ok("端到端答案不含「未标注任何条文依据」假警报",
       "未标注任何条文依据" not in (st_e2e.draft or ""),
       (st_e2e.draft or "")[-200:])
    ok("端到端答案仍然是完整申请书",
       "劳动人事争议仲裁申请书" in (st_e2e.draft or ""))
    store.delete_session("_t_e2e")

    # 反面：真的引了不存在的条号时，提示必须留着（别为了滤假警报把真警报也关掉）
    st_bad = mk_state()
    st_bad.scratchpad = [doc_step]
    st_bad.draft = "依据《劳动合同法》第九百九十九条规定……"
    vr = llm.verify(st_bad.draft, [], IDX)
    ok("引了库里没有的条号时自检不通过", not vr.passed, str(vr))
    ok("这类真警报的措辞与假警报不同（能区分开）",
       all(i.reason != "答案未标注任何条文依据" for i in vr.issues),
       str([i.reason for i in vr.issues]))

    print("\n== 「未覆盖」不能当免检口令（v112） ==")
    # 原来是「答案里含 未覆盖/未找到/无法确定 → 一上来就判通过」。
    # 在线实测里模型很快学会了这句口令：它把「这次辞退违不违法判断不了」
    # 也说成「未覆盖」——那是事实判断不了，不是知识库没有——于是同一段答案里
    # 所有引用（哪怕引错条号）都不用再核了。
    # 正确顺序：先抽引用，有引用就一律核；只有「无引用 + 明确拒答」才免检。
    # 用**系统自己产出的**拒答文案，不要手写：手写的句子和真实文案措辞不一致，
    # 测的其实是自己编的那一句。这里踩过一次——真实拒答写的是「没有找到直接依据」，
    # 而免检词表只有「未找到」，于是真正的拒答被判不通过，答案末尾被追一句
    # 「核对提示：答案未标注任何条文依据」。
    _st_ref = AgentState(session_id="_r", question="如何烤蛋糕")
    _refuse = llm._mock_generate(_st_ref, lambda k, p: None)
    _vr_ref = llm.verify(_refuse, [], IDX)
    ok("系统产出的零命中拒答被自检判通过（不追「未标注依据」）",
       _vr_ref.passed, str([i.reason for i in _vr_ref.issues]))
    ok("自省：这段确实是拒答文案（否则上一条测的不是这件事）",
       "没有找到直接依据" in _refuse, _refuse[:60])
    vr_bypass = llm.verify(
        "【未覆盖】本库没有相关规定。\n\n"
        "依据《劳动合同法》第九百九十九条规定，公司应当支付赔偿。", [], IDX)
    ok("含「未覆盖」但引了不存在的条号 → 仍然不通过",
       not vr_bypass.passed, str(vr_bypass))
    ok("这类问题不会被降级成「没引条文」糊过去",
       all(i.reason != "答案未标注任何条文依据" for i in vr_bypass.issues),
       str([i.reason for i in vr_bypass.issues]))
    # 引的是真条文、只是本轮没检索到 —— 算「无依据引用」，同样不该被洗掉。
    vr_off = llm.verify("【未覆盖】关于赔偿标准，依据《劳动合同法》第八十七条规定。",
                        [], IDX)
    ok("含「未覆盖」但引了没检索到的真条文 → 报「无依据引用」",
       not vr_off.passed and any("无依据引用" in i.reason for i in vr_off.issues),
       str([i.reason for i in vr_off.issues]))
    # 反向自省：确认上面那句真的抽出了引用。若 extract_cites 抽不出来，
    # 「仍然不通过」就会变成靠另一条规则（没引条文）成立——看着绿，其实没测到这件事。
    from app.cite import extract_cites                           # noqa: E402
    _cites = extract_cites("【未覆盖】依据《劳动合同法》第九百九十九条规定。", index=IDX)
    ok("自省：这句确实抽出了引用（证明走的是核对分支，不是拒答分支）",
       len(_cites) == 1, str(_cites))

    print("\n== 一句话要两样东西：文书 + 金额，两个都得给 ==")
    # 实测踩到的坑（在线）：用户说「帮我写申请书，顺便告诉我能拿多少」，
    # 模型算了钱、然后回「申请书写不了，因为条文里没有格式规定」——两样都办砸。
    # 回放这边原本也是「先写文书，写完立刻收工」，金额一样会丢。
    st_both = runner.run(
        "我在北京，2024年3月入职，月薪12000，去年9月被公司辞退，合同签了。"
        "帮我写一份劳动仲裁申请书，顺便告诉我能拿多少",
        session_id="_t_both")
    tools_both = [s.tool for s in st_both.scratchpad]
    ok("调了文书工具", "draft_document" in tools_both, str(tools_both))
    ok("也算出了金额", "calculate_compensation" in tools_both, str(tools_both))
    ans_both = st_both.draft or ""
    ok("答案里有完整申请书", "劳动人事争议仲裁申请书" in ans_both, ans_both[:80])
    # 去年9月 = 2025-09 → 用 2024 年度（11937，三倍 35811）；
    # 月薪 12000 未超三倍 → 不封顶；18 个月 → 2 个月，二倍 4 个月 = 48000。
    ok("答案里也有金额 48,000", "48,000" in ans_both,
       [ln for ln in ans_both.splitlines() if "元" in ln][-3:])
    ok("金额没有被填进仲裁请求段",
       "＿＿＿＿元" in block(ans_both, "仲裁请求", "事实与理由"),
       block(ans_both, "仲裁请求", "事实与理由")[:120])
    ok("收尾明确说金额要自己核对后再填请求事项",
       "自己核对" in ans_both and "请求事项" in ans_both, ans_both[-260:])
    store.delete_session("_t_both")

    print(f"\n{'=' * 56}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print(f"  ✗ {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
