# -*- coding: utf-8 -*-
"""接入内核的案情槽位与追问（v138）。不联网、不花钱。

这一套守的是四件事，每件都对应一处真实的坏结果：

1. **字段不串**。消费内核抽出来的是「商品/金额/问题」，不能存成劳动法的
   `monthly_wage`——那张 12 元的发票一旦变成「月薪 12 元」，补偿计算会拿它
   往下乘。所以「只认自己字段表里的键」必须由测试钉住。
2. **追问问对东西**。婚家第一句该问「你想解决哪一块」，不是「什么时候结婚」，
   更不是「是公司辞退还是自己辞职」。追问顺序由 `slot_priority` 决定。
3. **问过不再问**。用户答了「花了三万二」，如果规则没抽出来，下一轮又把
   同一个问题原样问一遍——这是用户最先崩的地方。所以「上一轮问过的要素
   本轮跳过」要能验。
4. **劳动法一个字没变**。345 套断言里有一批是逐字比对劳动法槽位的，这一套
   也要顺手钉一下：`CaseSlots` 的形状、`missing_critical()` 的返回值、
   追问文案，全都不许动。

跑法：
    ./.venv/Scripts/python.exe scripts/test_dslots.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import dslots as D                                    # noqa: E402
from app import profiles                                        # noqa: E402
from app.agent import AgentRunner, _looks_like_supplement       # noqa: E402
from app.llm import LLM, _guess_domain_slots, _money_in         # noqa: E402
from app.memory import MemoryStore                              # noqa: E402
from app.retrieval import get_profile_index                     # noqa: E402
from app.schemas import AgentState, Budget, CaseSlots           # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  → {detail}" if detail != "" else ""))


def _llm() -> LLM:
    llm = LLM()
    llm.online = False
    # 本机有 key 时 run() 会 maybe_recover() 把 online 翻回 True，
    # 那样这一套会静默变成联网用例。钉死它。
    llm.maybe_recover = lambda: False        # type: ignore[method-assign]
    return llm


# ---------------------------------------------------------------- [1] 字段表
def check_spec() -> None:
    print("[1] 字段表：每个接入内核一张，劳动法那张不参与")
    for pid in ("consumer", "civil", "traffic", "family"):
        p = profiles.resolve(pid)
        spec = D.spec(p)
        ok(f"{pid} 有字段表且非空", bool(spec))
        ok(f"{pid} 的追问顺序只用字段表里的键",
           set(D.priority(p)) == set(spec), D.priority(p)[:3])
        ok(f"{pid} 的追问顺序以 slot_priority 打头",
           list(D.priority(p)[:len(p.slot_priority)]) == list(p.slot_priority),
           p.slot_priority)
        ok(f"{pid} 每个字段都配了一句追问说法",
           all(D.ask_for(p, k) for k in spec),
           [k for k in spec if not D.ask_for(p, k)])
        ok(f"{pid} 的追问说法是中文且不像字段名",
           all(any("\u4e00" <= c <= "\u9fff" for c in D.ask_for(p, k))
               for k in spec))

    # 四个内核的字段表不该长得一样——一样就说明有人偷懒抄了同一份。
    sets = {pid: set(D.spec(profiles.resolve(pid))) for pid in
            ("consumer", "civil", "traffic", "family")}
    dup = [(a, b) for a in sets for b in sets
           if a < b and len(sets[a] & sets[b]) >= 3]
    ok("四个接入内核的字段表互不相同（不是一份抄四遍）", not dup, dup)

    ok("劳动法内核不走这套字段表（它有自己的 CaseSlots）",
       not D.spec(profiles.resolve("labor")) is None
       and "leave_reason" in D.spec(profiles.resolve("labor")))
    ok("劳动法的案件槽位形状没变",
       set(CaseSlots().model_fields) == {
           "region", "employment_type", "has_contract", "start_date",
           "end_date", "monthly_wage", "leave_reason", "social_insurance"})
    ok("劳动法的追问优先级没变",
       CaseSlots().missing_critical() ==
       ["leave_reason", "monthly_wage", "start_date", "region"])


# ---------------------------------------------------------------- [2] 合并
def check_merge() -> None:
    print("[2] 合并：只认字段表、空值不覆盖、脏值不进库")
    p = profiles.resolve("consumer")
    vals: dict[str, str] = {}
    changed = D.merge(vals, p, {"goods": "面包", "monthly_wage": 8000,
                                "leave_reason": "公司辞退"})
    ok("字段表外的键被丢掉（消费内核不存月工资）",
       changed == ["goods"] and "monthly_wage" not in vals
       and "leave_reason" not in vals, (changed, vals))
    ok("值一律存成字符串", isinstance(vals["goods"], str))

    D.merge(vals, p, {"goods": ""})
    ok("空值不覆盖已确认的事实", vals["goods"] == "面包", vals)

    D.merge(vals, p, {"price": 12})
    ok("数字型输入存成字符串", vals["price"] == "12", vals.get("price"))

    D.merge(vals, p, {"problem": "不知道"})
    ok("「不知道 / 未提及」不当作事实存下来", "problem" not in vals, vals)

    D.merge(vals, p, {"problem": ["过期", "有异物"]})
    ok("数组折成顿号串", vals.get("problem") == "过期、有异物", vals.get("problem"))

    long = "很" * 200
    D.merge(vals, p, {"has_voucher": long})
    ok("超长值被截断（别让粘贴的一大段占满提示词）",
       len(vals.get("has_voucher") or "") == D.MAX_LEN, len(vals.get("has_voucher") or ""))

    D.merge(vals, p, {"is_food": True})
    ok("布尔存成「是/否」", vals.get("is_food") == "是", vals.get("is_food"))

    ok("brief 用的是中文字段名",
       "商品或服务=面包" in D.brief(p, vals), D.brief(p, vals))
    ok("空态与 CaseSlots.brief() 同字面（否则摘要里会多一行「暂无」）",
       D.brief(p, {}) == CaseSlots().brief() == D.EMPTY)
    ok("五个字段都确认之后 missing 为空（不再追问）",
       D.missing(p, vals) == [], D.missing(p, vals))
    ok("缺一项时只报那一项", D.missing(p, {"goods": "面包"}) ==
       [k for k in D.priority(p) if k != "goods"], D.missing(p, {"goods": "面包"}))


# ---------------------------------------------------------------- [3] 规则抽取
def check_guess() -> None:
    print("[3] 离线规则抽取：只认能一眼看准的")
    con = profiles.resolve("consumer")
    got = _guess_domain_slots("买到过期面包，能要十倍赔偿吗", con)
    ok("消费句抽出商品与问题",
       got.get("goods") == "面包" and got.get("problem") == "过期", got)
    ok("消费句认出这是食品", got.get("is_food") == "是", got)
    ok("问题里没提金额就不编金额", "price" not in got, got)

    got = _guess_domain_slots("过期牛奶12块，有小票", con)
    ok("金额带单位才认", got.get("price") == "12", got)
    ok("「有小票」判成留有凭证", got.get("has_voucher") == "是", got)

    fam = profiles.resolve("family")
    got = _guess_domain_slots("离婚的话房子怎么分", fam)
    ok("婚家句抽出争议焦点与财产",
       got.get("dispute") == "离婚" and got.get("property") == "房子", got)

    tra = profiles.resolve("traffic")
    got = _guess_domain_slots("事故认定书写的是对方全责，住院十二天", tra)
    ok("道交句抽出责任认定", got.get("liability") == "全责", got)
    ok("道交句抽出伤情", got.get("injury") == "住院", got)
    ok("「住院十二天」的十二天不被当成金额",
       "expenses" not in got, got)

    # 金额：四种认法 + 三种不认法。认错了会直接写进答案，宁可不认。
    for text, want in (("已经花了三万二", "32000"), ("花了3万", "30000"),
                       ("1200元", "1200"), ("买的三百元的东西", "300")):
        ok(f"金额认得「{text}」", _money_in(text) == want, _money_in(text))
    for text in ("三万多元", "住院十二天", "2024年3月", "赔三倍"):
        ok(f"金额不认「{text}」", _money_in(text) == "", _money_in(text))

    # 字段表里没有的键不输出：civil 的字段表里没有 goods。
    civ = profiles.resolve("civil")
    got = _guess_domain_slots("买到过期面包", civ)
    ok("抽出的键一定落在本内核字段表内",
       set(got) <= set(D.spec(civ)), got)


# ---------------------------------------------------------------- [4] 端到端
def check_flow() -> None:
    print("[4] 端到端：追问 → 用户答 → 记住 → 不再问")
    store = MemoryStore(Path(tempfile.mkdtemp()) / "dslots.db")
    p = profiles.resolve("traffic")
    runner = AgentRunner(get_profile_index(p), _llm(), store)
    with profiles.use(p):
        st1 = runner.run("我被车撞了对方全责，误工费怎么算", session_id="t1")
        ok("道交算钱题问了「花了多少钱」",
           "花了多少钱" in (st1.pending_question or ""), st1.pending_question)
        ok("追问附在免责声明之前（顺序不能反）",
           (st1.draft or "").index("还需要确认")
           < (st1.draft or "").index("本回答仅供参考"), (st1.draft or "")[-80:])
        ok("已有的事实先记下来（责任认定）", st1.dslots.get("liability") == "全责",
           st1.dslots)

        # 用户照追问答一句：中文金额也得认，否则会问第二遍。
        st2 = runner.run("已经花了三万二，住院十二天", session_id="t1")
        ok("中文金额被抽进槽位", st2.dslots.get("expenses") == "32000", st2.dslots)
        ok("已经答过的金额不再追问", not st2.pending_question, st2.pending_question)
        ok("追问被识别成「补充上一问」，不是新问题",
           st2.question == "我被车撞了对方全责，误工费怎么算", st2.question)
        ok("补充信息判定能吃到带数字的短句",
           _looks_like_supplement("已经花了三万二，住院十二天"))

        # 第三轮：换一件事问，不能把上一轮的追问再端出来。
        st3 = runner.run("那护理费呢", session_id="t1")
        ok("换问题时不重放上一轮的追问",
           (st3.pending_question or "") != (st1.pending_question or ""),
           st3.pending_question)

    # 落库：换个 runner 读同一个会话，记忆还在。
    runner2 = AgentRunner(get_profile_index(p), _llm(), store)
    with profiles.use(p):
        st4 = runner2.run("护理费怎么算", session_id="t1")
    ok("重新进会话时要素仍然在（不是只活在内存里）",
       st4.dslots.get("expenses") == "32000", st4.dslots)

    # 换内核读同一个会话：不许读到别人的字段。
    runner3 = AgentRunner(get_profile_index(profiles.resolve("consumer")), _llm(), store)
    with profiles.use(profiles.resolve("consumer")):
        st5 = runner3.run("买的牛奶过期了", session_id="t1")
    ok("换内核不串：消费内核读不到道交的字段",
       "expenses" not in st5.dslots and "liability" not in st5.dslots, st5.dslots)

    # clarify 分支：一问一答之后，同一句话再问不重放。
    fam = profiles.resolve("family")
    runner4 = AgentRunner(get_profile_index(fam), _llm(), store)
    runner4.llm.classify_intent = lambda *a, **k: "clarify"  # type: ignore[method-assign]
    with profiles.use(fam):
        c1 = runner4.run("对方出轨", session_id="t2")
        c2 = runner4.run("对方出轨", session_id="t2")
    ok("婚家 clarify 第一问问的是「哪一件事」",
       "哪一件事" in (c1.draft or ""), c1.draft)
    ok("同一句话再问一遍时换一个要素问",
       (c2.draft or "") != (c1.draft or ""), c2.draft)

    # 追问里不许出现别的领域的要素。
    smell = ("离职", "辞退", "月工资", "入职", "劳动仲裁", "经济补偿")
    bad = [pid for pid in ("consumer", "civil", "traffic", "family")
           for w in smell
           if w in (lambda q: q)(D.ask_for(profiles.resolve(pid),
                                           D.priority(profiles.resolve(pid))[0]))]
    ok("四个内核第一句追问都不含劳动争议话术", not bad, bad)


# ---------------------------------------------------------------- [5] 状态
def check_state() -> None:
    print("[5] AgentState：两套槽位各归各，摘要按内核出")
    st = AgentState(session_id="s", question="q", budget=Budget(steps_left=2))
    ok("劳动法状态下摘要读 CaseSlots", st.case_brief() == CaseSlots().brief())

    with profiles.use("family"):
        st.slots = CaseSlots(region="杭州")
        ok("接入内核：只有档案地区时也说得出来",
           "所在地区=杭州" in st.case_brief(), st.case_brief())
        st.dslots = {"dispute": "离婚", "property": "房子"}
        brief = st.case_brief()
        ok("接入内核摘要带上自己的要素",
           "争议焦点=离婚" in brief and "主要财产=房子" in brief, brief)
        ok("接入内核摘要保留档案地区（不重复问城市）", "杭州" in brief, brief)

    # 道交的字段表里本来就有 region，不该出现两遍。
    with profiles.use("traffic"):
        st2 = AgentState(session_id="s2", question="q")
        st2.slots = CaseSlots(region="杭州")
        st2.dslots = {"region": "杭州", "liability": "全责"}
        ok("内核对 region 有自己字段时不重复拼一遍",
           st2.case_brief().count("杭州") == 1, st2.case_brief())


# ---------------------------------------------------------------- [6] 接口
def check_api() -> None:
    """/api/chat/session/{id} 带 dslots、/dslots 手改入口的闭环。

    这一节是这套里唯一碰 HTTP 的：上面五节全走 AgentRunner + MemoryStore
    （不联网、不起服务），但「档案页能不能看到/改掉自己抽到的要素」只能
    从接口层验——那是前端唯一能触到的面。用 in-process TestClient，不联网。
    延迟 import server：模块顶部保持纯单元，别让首节就被「建管理员」拖慢。
    """
    print("[6] 接口：session 带 dslots，/dslots 手改闭环")
    import os
    import re
    os.environ.setdefault("LAW_DB_PATH", str(Path(tempfile.mkdtemp()) / "api.db"))
    os.environ.setdefault("ALLOW_REGISTER", "1")
    os.environ.setdefault("REG_PER_IP_HOUR", "100")

    from fastapi.testclient import TestClient
    from app import server as srv
    from app.config import CSRF_NAME

    class Csrf(TestClient):
        def request(self, method, url, **kw):          # type: ignore[override]
            m = str(method).upper()
            if m not in ("GET", "HEAD", "OPTIONS"):
                t = self.cookies.get(CSRF_NAME)
                if t:
                    h = dict(kw.get("headers") or {})
                    h.setdefault("X-CSRF-Token", t)
                    kw["headers"] = h
            return super().request(method, url, **kw)

    if not srv._boot_admin:
        srv._auth.ensure_admin("root", "adminpass123")
    c = Csrf(srv.app)
    c.post("/api/auth/register",
           json={"username": "u1", "password": "pass1234", "nickname": "测试"})
    c.post("/api/auth/login", json={"username": "u1", "password": "pass1234"})

    def sid_of(txt):
        m = re.search(r"event: session\ndata: (\{.*?\})\n\n", txt)
        return json.loads(m.group(1))["session_id"] if m else None

    # meta 暴露了接入内核的字段表（前端靠它渲染档案页）。
    meta = c.get("/api/meta").json()
    by_id = {p["id"]: p for p in meta.get("profiles") or []}
    ok("meta 暴露 consumer 字段表",
       bool((by_id.get("consumer") or {}).get("slot_labels")),
       by_id.get("consumer", {}).get("slot_labels"))

    # 消费会话：抽到的要素能从 session 接口读回。
    r = c.post("/api/chat", json={"question": "买到过期面包能赔多少",
                                  "profile": "consumer"})
    sid = sid_of(r.text)
    ok("消费会话能建起来", bool(sid), sid)
    d = c.get(f"/api/chat/session/{sid}").json()
    ok("session 接口带 dslots 且按内核抽到要素",
       d.get("profile_id") == "consumer" and "dslots" in d and bool(d.get("dslots")),
       d.get("dslots"))

    # 手改：写、读回、空串删、非法字段拒、labor 拒。
    r = c.post(f"/api/chat/session/{sid}/dslots",
               json={"goods": "牛奶", "price": "12", "is_food": "是"})
    ok("手改写入返回 changed 与 labels", r.status_code == 200
       and "goods" in r.json().get("changed", [])
       and "商品或服务" in r.json().get("labels", {}).values(), r.json())
    d = c.get(f"/api/chat/session/{sid}").json()
    ok("手改后 session 读回新值", d.get("dslots", {}).get("goods") == "牛奶",
       d.get("dslots"))
    r = c.post(f"/api/chat/session/{sid}/dslots", json={"price": ""})
    ok("空串 = 删掉这一项", r.status_code == 200
       and "price" in r.json().get("changed", []), r.json())
    d = c.get(f"/api/chat/session/{sid}").json()
    ok("删除后读回不含该项", "price" not in d.get("dslots", {}), d.get("dslots"))
    r = c.post(f"/api/chat/session/{sid}/dslots", json={"monthly_wage": "8000"})
    ok("劳动法字段名被拒（不静默丢）", r.status_code == 400, r.json())
    # 当前库里 goods=牛奶，再写一次牛奶——同值不算 changed。
    r = c.post(f"/api/chat/session/{sid}/dslots", json={"goods": "牛奶"})
    ok("同值写回不算 changed", r.json().get("changed", []) == [], r.json())

    # labor 会话走 /slots 不走 /dslots，调 /dslots 要被拒。
    r = c.post("/api/chat", json={"question": "公司没签合同能要二倍工资",
                                  "profile": "labor"})
    lsid = sid_of(r.text)
    r = c.post(f"/api/chat/session/{lsid}/dslots", json={"region": "杭州"})
    ok("labor 会话调 /dslots 被拒（要素归 /slots）", r.status_code == 400,
       r.json())


def main() -> int:
    check_spec()
    check_merge()
    check_guess()
    check_flow()
    check_state()
    check_api()
    print("\n" + "=" * 62)
    if FAIL:
        print(f"{len(PASS) + len(FAIL)} 项中 {len(FAIL)} 项失败：")
        for f in FAIL:
            print("  - " + f)
        return 1
    print(f"{len(PASS)} 项全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
