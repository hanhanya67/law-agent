# -*- coding: utf-8 -*-
"""显式立场 / 语气自适应表达回归（v117）。

一句话概括这套测试在守什么：**画像不许碰到答案**。

最危险的不是「语气识别不准」，而是有人图省事把 stance 接进 verdict 或
ANSWER_SYSTEM —— 那等于拿最不可靠的信号改最重要的法律结论。所以这里
逐组合断言 presentation() 里既没有答案正文，也没有任何能改文字的字段，
且 answer_unchanged 恒为 True。

另外专守两条：
  · 模块里**不存在**从语气推断立场的入口（连函数名都不许出现）；
  · 语气建议默认不生效，必须用户确认过。否则「自动切换表达」只是
    另一种形式的替人认定。

篡改自检（改完记得改回来）：
  · 给 persona.py 加个 infer_stance() → [2] 反推断入口那条红；
  · 把 confirmed 判断删掉 → [3] 默认不生效那组红；
  · 往 presentation() 返回值塞 "answer" 字段 → [4] 硬边界那组红；
  · 把 normalize_stance 的白名单检查换成 startswith → [1] 白名单那条红。
"""
from __future__ import annotations

import itertools
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("DEEPSEEK_API_KEY", None)

from app import persona as P          # noqa: E402
from app import cases as C          # noqa: E402

TOTAL = 0
FAILED: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}   {detail}")


def test_stance() -> None:
    print("\n[1] 立场只能是显式白名单值")
    for v in ("employee", "employer", "agent", "explore"):
        ok(f"合法立场 {v} 被接受", P.normalize_stance(v) == v)
    for v in ("", None, "worker", "职工", "hr", "经理",
              "employee;drop table", 123, {"a": 1}):
        ok(f"非法/模糊取值 {v!r} 一律拒绝", P.normalize_stance(v) is None,
           str(P.normalize_stance(v)))
    ok("大写加空格能归一", P.normalize_stance("  EMPLOYER  ") == "employer")
    ok("立场选项四个且各带说明",
       len(P.STANCES) == 4 and all(s["label"] and s["hint"] for s in P.STANCES))


def test_no_inference() -> None:
    print("\n[2] 不存在「从语气推断身份」的入口")
    src = (ROOT / "app" / "persona.py").read_text(encoding="utf-8")
    ok("源码里没有 infer/guess/detect + identity/stance/role 的函数",
       not re.search(r"def\s+\w*(infer|guess|detect)\w*(identity|stance|role)\w*",
                     src, re.I))
    tone = P.detect_tone("老板不给钱，气死了，怎么办啊")
    ok("detect_tone 不返回 stance / identity / applicant 任何一项",
       not ({"stance", "identity", "applicant", "role"} & set(tone)), str(tone))
    ok("detect_tone 明确标 suggested_only",
       tone["suggested_only"] is True)
    ok("语气建议的说明里点明与身份无关",
       "身份" in tone["note"])

    plain = P.detect_tone("老板不给钱，气死了，这可怎么办啊，救命")
    legal = P.detect_tone("请依据《劳动合同法》第八十二条及第二十七条说明二倍工资的仲裁时效")
    ok("口语情绪化提问建议简洁表述", plain["suggested"] == "plain", str(plain))
    ok("法条型提问建议标准表述", legal["suggested"] == "standard", str(legal))
    ok("信号列表不为空且最多 4 条",
       plain["signals"] and len(plain["signals"]) <= 4, str(plain["signals"]))


def test_confirmation() -> None:
    print("\n[3] 语气建议默认不生效，必须用户点过")
    q = "老板不给钱，气死了，这可怎么办啊，救命"
    # 期望值写死，不用被测模块里的常量 —— 防假绿灯。
    auto = P.build_persona(q)
    ok("语气信号很强时仍保持 standard", auto["mode"] == "standard", str(auto["mode"]))
    ok("未确认时 applied 为 False", auto["tone"]["applied"] is False)
    ok("未确认时 requested 为空", auto["tone"]["requested"] is None)
    ok("只传 tone_mode 但不确认，仍不生效",
       P.build_persona(q, tone_mode="plain")["mode"] == "standard")
    ok("确认过的 plain 才生效",
       P.build_persona(q, tone_mode="plain", confirmed=True)["mode"] == "plain")
    ok("确认过但取值非法时不生效",
       P.build_persona(q, tone_mode="whatever", confirmed=True)["mode"] == "standard")
    ok("默认 stance 为 None", P.build_persona(q)["stance"] is None)
    ok("默认 stance_source 为 unset",
       P.build_persona(q)["stance_source"] == "unset")
    explicit = P.build_persona(q, stance="employer")
    ok("显式选择后 stance_source 变 explicit",
       explicit["stance_source"] == "explicit" and explicit["stance"] == "employer")
    ok("画像说明里明确不根据语气判断身份",
       "不会根据语气判断你的身份" in explicit["note"])
    ok("画像带硬边界声明", "不改结论" in explicit["hard_boundary"])


def test_hard_boundary() -> None:
    print("\n[4] 硬边界：任何组合下都不碰答案")
    q = "老板不给钱，怎么办"
    combos = itertools.product(
        (None, "employee", "employer", "agent", "explore", "瞎编的"),
        (None, "plain", "standard", "乱写"),
        (False, True))
    n = 0
    bad_boundary: list[str] = []
    bad_stance: list[str] = []
    for stance, mode, confirmed in combos:
        p = P.build_persona(q, stance=stance, tone_mode=mode, confirmed=confirmed)
        pres = P.presentation(p)
        n += 1
        tag = f"{stance}/{mode}/{confirmed}"
        if pres["answer_unchanged"] is not True:
            bad_boundary.append(tag + " answer_unchanged 被改")
        if {"answer", "text", "draft", "verdict", "citations",
                "amount", "basis"} & set(pres):
            bad_boundary.append(tag + " 混进答案字段")
        if p["stance"] != P.normalize_stance(stance):
            bad_stance.append(tag + " 落了非法立场")
    # 不能写 ok(..., True) 再靠 assert 兜底：条件恒真的断言是假绿灯，
    # 前面 v116 已经在 X.BLANK 上栽过一次。这里把失败累积起来再报告。
    ok(f"{n} 种组合：answer_unchanged 恒真且不含答案/定性/金额/条号字段",
       not bad_boundary, str(bad_boundary[:3]))
    ok(f"{n} 种组合：非法 stance 一律不落到画像里",
       not bad_stance, str(bad_stance[:3]))

    pres = P.presentation(P.build_persona(q, tone_mode="plain", confirmed=True))
    ok("简洁模式只折叠「依据」段", pres["fold_sections"] == ["依据"], str(pres))
    ok("简洁模式仍是结论先行", pres["show_first"] == "结论")
    ok("简洁模式徽标可关闭", "可关闭" in pres["badge"])
    ok("标准模式不折叠任何段", P.presentation(None)["fold_sections"] == [])
    ok("表达配置里没有可写回答案的函数或回调",
       all(not callable(v) for v in pres.values()))


def test_next_steps() -> None:
    print("\n[5] 立场真正影响的是「下一步看什么」")
    e = P.next_steps("employee")
    r = P.next_steps("employer")
    ok("职工与单位视角给出的下一步不同", e != r)
    ok("职工视角提到举证与时效",
       any("举证" in s or "时效" in s for s in e))
    ok("单位视角提到台账与规章制度",
       any("台账" in s or "规章制度" in s for s in r))
    ok("未选立场时按职工视角给，不假装知道",
       P.next_steps(None) == e)
    blob = " ".join(e + r + P.next_steps("explore"))
    ok("下一步里没有金额数字", not re.search(r"\d{3,}", blob), blob)
    ok("下一步里没有条号", "第" not in blob or not re.search(r"第.+条", blob))
    ok("下一步里没有「应该赢 / 会输」式预测",
       not re.search(r"(会赢|会输|胜诉|败诉|必然)", blob))
    ok("explore 视角提示看部分支持案件",
       any("部分支持" in s for s in P.next_steps("explore")))


def test_joint() -> None:
    print("\n[6] 与判例决策的联动：立场只改 applicant 过滤")
    q = "竞业限制违约金"
    a = C.build_decision(q, stance="employee")
    b = C.build_decision(q, stance="employer")
    ok("不同立场得到不同的申请人过滤",
       a["scope"]["applicant"] != b["scope"]["applicant"])
    ok("单位视角如实报样本不足",
       b["similar"]["distribution"]["insufficient"] is True)
    ok("两种立场都仍然不给胜诉率",
       all("no_win_rate" in d["policy"] for d in (a, b)))
    ok("立场联动没有改变成本卡结构",
       len(a["cost_benefit"]["rows"]) == len(b["cost_benefit"]["rows"]))
    ok("画像与决策卡共用同一组立场取值",
       {s["value"] for s in P.STANCES} ==
       {"employee", "employer", "agent", "explore"})


def test_sse_wiring() -> None:
    """[7] 端到端：聊天第一屏的 SSE extras 也要吃到已保存的显式画像。

    只测 /api/case/extras 不够。用户问完第一句话看到的办案包来自 SSE，
    如果接线只做在独立接口里，第一屏永远是 unset，非得手动重算才生效——
    典型的「功能做了但用户看不见」。这里真起一次回放 runner 抓事件流。
    """
    print("\n[7] SSE 即时办案包与长期记忆的接线")
    import tempfile
    import time
    from app.agent import AgentRunner
    from app.config import CORPUS_PATH
    from app.llm import LLM
    from app.memory import MemoryStore
    from app.retrieval import LawIndex

    idx = LawIndex(CORPUS_PATH)
    store = MemoryStore(Path(tempfile.mkdtemp()) / "wire.db")
    llm = LLM()
    llm.online = False
    llm._offline_since = time.time()   # 与 test_intent 同理：钉死离线，不看余额
    runner = AgentRunner(idx, llm, store)
    q = "没签劳动合同，还拖欠两个月工资"

    def once(uid, sid):
        seen: dict = {}
        runner.run(q, session_id=sid, user_id=uid,
                   emit=lambda e, d: seen.setdefault(e, d))
        return seen.get("extras") or {}

    base = once(None, "wire-none")
    ok("user_id=None（命令行 / 评测）仍产出五块办案包",
       set(base) == {"timeline", "claims", "evidence_check",
                     "decision", "persona"}, str(sorted(base)))
    ok("无账号时画像如实标 unset",
       base["persona"]["stance"] is None
       and base["persona"]["stance_source"] == "unset")
    ok("无账号时判例仍按保守职工视角",
       base["decision"]["scope"]["applicant"] == "employee")

    uid = 9017
    fresh = once(uid, "wire-fresh")
    ok("登录但没选过立场时仍是 unset（不把默认值伪装成用户选过）",
       fresh["persona"]["stance_source"] == "unset")

    store.save_user_facts(uid, stance="employer", tone_mode="plain",
                          confirmed=True)
    saved = once(uid, "wire-saved")
    ok("保存单位立场后，SSE 办案包立刻按 employer 过滤",
       saved["decision"]["scope"]["applicant"] == "employer")
    ok("SSE 画像如实标 explicit",
       saved["persona"]["stance"] == "employer"
       and saved["persona"]["stance_source"] == "explicit")
    ok("确认过的简洁表述在 SSE 里生效",
       saved["persona"]["mode"] == "plain")
    ok("接线只碰附加产物，画像里没有答案正文 / 定性字段",
       "answer" not in saved["persona"]
       and "verdict" not in saved["persona"])

    store.save_user_facts(uid, confirmed=False)
    back = once(uid, "wire-unconfirmed")
    ok("取消确认后简洁表述自动退回 standard",
       back["persona"]["mode"] == "standard")
    ok("取消确认不抹掉已选的立场（COALESCE 语义）",
       back["persona"]["stance"] == "employer")


def main() -> int:
    print("显式立场 / 语气自适应表达回归（v117，强制回放）")
    print("=" * 62)
    test_stance()
    test_no_inference()
    test_confirmation()
    test_hard_boundary()
    test_next_steps()
    test_joint()
    test_sse_wiring()
    print("\n" + "=" * 62)
    if FAILED:
        print(f"{TOTAL} 项：{TOTAL - len(FAILED)} 通过，{len(FAILED)} 失败")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"{TOTAL} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
