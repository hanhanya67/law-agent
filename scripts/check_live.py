#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在线验收：真 key 下把「全部功能」跑一遍（花钱，所以不在 run_all 里）。

为什么必须单独一套：其余 34 个套件全部**强制回放**（run_all 清空
DEEPSEEK_API_KEY，check_ui 也硬编码空 key）。回放模式下决策由规则写死，
于是「模型会不会重复调用工具」「金额是不是模型自己乘的」这类问题
**一条断言都没有**，而它们在真模型下每次都发生。v132 的两个核心修复
（循环去重、算钱软闸）就是在这种盲区里躺了很久。

这一套验的是「带 key 的包解压之后，有没有哪一块是坏的」：

  1. key 链路        对话 / Function Calling / 流式（check_key 的三项）
  2. 元信息           /api/meta 必须 llm_enabled=true（不是回放）
  3. 真实问答（劳动法）事件序列完整、有引用、**没有 degraded 事件**
  4. 循环去重         同参数工具调用不得重复（真模型现场验证，非假模型）
  5. 算钱走工具       问金额时 calculate_compensation 必须出现在轨迹里
  6. 多法域           换到消费内核再问一次，答案得是那一部法的事
  7. MCP              `python -m app.mcp_server --self-check` 六项
  8. 多模态读图       拿一张随包发的工资条 PNG，看它能不能读出月工资
  9. 内核分流         v133 七处劳动法文案不串味 + v135 偏门问题真的去检索
 10. v139 三页       图书馆取原文 / 在线出题真的走了模型 / **判答引用逐条回库**

第 8 项单独说一句：`test_vision.py` 跑的是回放，返回的是模块里写死的那张
「回放样例」——字段归一化、unreadable 处理、槽位合并都能验，唯独验不了
「这个模型真的能看图」。而多模态是评分表里的独立一项，所以留一张固定的
图（`eval/fixtures/pay_slip.png`，见同目录 README）在这里读一次：图不变，
判据才敢写死成「必须读出 12000」。

第 9 项同样不进 run_all：那 34 套全是回放，而「追问里有没有冒出离职」在
回放里由规则写死，怎么改都不会串味；只有真模型走完整链路时，clarify /
越界 / 首屏 / 空结果这几条才会被实际触发。v135 之后入口不再一票否决，
越界与偏门问法也会先走检索，所以那条的判据从「有没有吐固定拒答」改成
「有没有真的 search_statute 过」——这才是「先尝试理解」可验收的定义。
配置层四个内核逐个查是零成本的，链路层跑婚家七问（用户截图那个分区）。

用法：
    ./.venv/Scripts/python.exe scripts/check_live.py
    ./.venv/Scripts/python.exe scripts/check_live.py --skip-law2   # 少问一次省钱

成本：原有 3 次真实问答 + 1 次读图，第 9 节 7 轮，第 10 节再 3 次出题调用，
约 10–18 万 token。DeepSeek 上是一两毛钱的事，但**余额见底或撞 429 时会
静默降级成回放**，所以第 2 项先查 llm_enabled、每节都查 degraded。
撞过 429 先发一次最小调用探，别照着响应体里的「将在 X 重置」干等——
那是配额周期，短时突发几分钟就自己通了。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _svc import start_service, stop_service                 # noqa: E402
from app.auth import AuthStore                               # noqa: E402
from app.config import DB_PATH                                # noqa: E402

PASS, FAIL = [], []


def ok(name: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"    {detail}" if detail != "" and not cond else ""))


class Client:
    """最小 HTTP 客户端：只处理 cookie（登录态 + CSRF）。"""

    def __init__(self, base: str):
        self.base = base
        self.cookies: dict[str, str] = {}

    def _req(self, path: str, data: dict | None = None, timeout: int = 30):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(self.base + path, data=body,
                                     method="POST" if data is not None else "GET")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}"
                                               for k, v in self.cookies.items()))
        # 双提交：cookie 里叫 law_csrf，请求头叫 x-csrf-token，两者必须一致。
        # 名字写错过一次，表现是 /api/chat 直接 403 —— 看着像没登录。
        csrf = self.cookies.get("law_csrf")
        if csrf:
            req.add_header("X-CSRF-Token", csrf)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}"), {}
        for sc in resp.headers.get_all("Set-Cookie") or []:
            kv = sc.split(";", 1)[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.cookies[k.strip()] = v.strip()
        return resp.status, json.loads(resp.read().decode("utf-8") or "{}"), {}

    def login(self, user: str, pw: str) -> int:
        code, _, _ = self._req("/api/auth/login", {"username": user, "password": pw})
        return code

    def chat(self, question: str, profile: str | None = None,
             timeout: int = 180) -> list[tuple[str, dict]]:
        """跑一轮问答，返回 [(event, data)]。"""
        payload = {"question": question}
        if profile:
            payload["profile"] = profile
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + "/api/chat", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream")
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}"
                                               for k, v in self.cookies.items()))
        csrf = self.cookies.get("law_csrf")
        if csrf:
            req.add_header("X-CSRF-Token", csrf)
        events = []
        resp = urllib.request.urlopen(req, timeout=timeout)
        cur = None
        for raw in resp:
            line = raw.decode("utf-8", "ignore").rstrip("\n")
            if line.startswith("event:"):
                cur = line[6:].strip()
            elif line.startswith("data:") and cur:
                try:
                    events.append((cur, json.loads(line[5:].strip())))
                except json.JSONDecodeError:
                    pass
                cur = None
        resp.close()
        return events


def _tools_of(events) -> list[tuple[str, str]]:
    """从 tool_start 事件里取 (工具名, 参数 JSON)。"""
    out = []
    for ev, d in events:
        if ev == "tool_start":
            out.append((d.get("tool") or "",
                        json.dumps(d.get("args") or {}, sort_keys=True,
                                   ensure_ascii=False)))
    return out


def main() -> int:
    skip_law2 = "--skip-law2" in sys.argv
    print("=" * 66)
    print("在线验收（真 key）")
    print("=" * 66)

    # ---- 1. key 链路 ----
    print("\n== 1. key 链路（check_key.py 三项）==")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_key.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    ok("check_key 返回 0（对话/Function Calling/流式全过）", r.returncode == 0,
       f"rc={r.returncode} 尾部：{out.strip().splitlines()[-3:] if out else ''}")

    # ---- 准备账号 ----
    store = AuthStore(DB_PATH)
    users = {u["username"]: u for u in store.list_users(size=100)["items"]}
    demo = users.get("live-check-demo")
    if demo is None:
        demo = store.create_user("live-check-demo", "live-check-pw-123",
                                 role="user", ip="127.0.0.1")
        print("  （新建自检账号 live-check-demo）")
    store.save_profile(demo["id"], {"real_name": "在线自检", "region": "北京"},
                       onboarding="done")

    # 起服务，**不传空 key**：这一套就是要它联网。
    svc, base = start_service()
    print(f"  服务：{base}")
    try:
        # ---- 2. 元信息 ----
        print("\n== 2. 元信息：确实在线 ==")
        c = Client(base)
        code = c.login("live-check-demo", "live-check-pw-123")
        ok("登录成功", code == 200, f"code={code}")
        st, meta, _ = c._req("/api/meta")
        ok("/api/meta 200", st == 200, st)
        ok("llm_enabled=true（不是回放）", meta.get("llm_enabled") is True,
           meta.get("llm_enabled"))
        # laws 是「每部法各多少条」的字典，不是数字。按 len() 判 9 部。
        laws = meta.get("laws")
        ok("语料在位（2066 条 / 9 部法）",
           meta.get("articles") == 2066 and isinstance(laws, dict) and len(laws) == 9,
           f"articles={meta.get('articles')} laws={list(laws)[:3] if isinstance(laws, dict) else laws}")

        # ---- 3. 真实问答：劳动法 ----
        print("\n== 3. 真实问答（劳动法内核）==")
        t0 = time.time()
        ev1 = c.chat("公司没签劳动合同还拖欠两个月工资，我能要什么")
        ms = int((time.time() - t0) * 1000)
        names = {e for e, _ in ev1}
        tools = _tools_of(ev1)
        answer = "".join(d.get("text", "") for e, d in ev1 if e == "token")
        ok("事件流有 answer/token（真的生成了正文）",
           "token" in names and len(answer) > 80, f"names={sorted(names)}")
        ok("没有 degraded 事件（没降级成回放）", "degraded" not in names)
        ok("有 verify 事件（自检跑过）", "verify" in names)
        ver = next((d for e, d in ev1 if e == "verify"), {})
        ok("有可核验引用", bool(ver.get("citations")), ver.get("citations"))
        ok("答案含三段骨架（结论/依据/建议）",
           all(k in answer for k in ("【结论】", "【依据】", "【建议】")),
           answer[:80])

        # ---- 4. 循环去重（真模型现场）----
        print("\n== 4. 循环去重（真模型，不是假模型）==")
        dup = len(tools) - len(set(tools))
        ok("同参数工具调用没有重复", dup == 0, f"重复 {dup} 次：{tools}")
        # v135 口径修正：`tool_start` 混着两笔账——L1.5 的确定性计划检索
        # （最多 PLAN_CAP 条，**不消耗** budget.steps_left）和 L2 主循环
        # （最多 MAX_STEPS 步）。原来写死 <=8，等于把两者之和去比主循环
        # 预算，模型多分解一条子查询就假红；v135 实测 9 步、重复 0 次，
        # 是正常的 3 条计划检索 + 6 步循环。真正抓空转的是上面那条
        # 同参数去重，这里只守理论上界，防止循环彻底失控。
        from app.config import MAX_STEPS
        from app.llm import PLAN_CAP
        cap = MAX_STEPS + PLAN_CAP
        ok(f"总步数没有失控（计划检索 + 主循环，上界 {cap}）",
           len(tools) <= cap, f"{len(tools)} 步：{tools}")

        # ---- 5. 算钱走工具 ----
        print("\n== 5. 金额必须由工具算 ==")
        ev2 = c.chat("我2020年3月入职北京一家公司，月薪8000，2026年10月被违法辞退，能拿多少赔偿")
        names2 = {e for e, _ in ev2}
        tools2 = _tools_of(ev2)
        answer2 = "".join(d.get("text", "") for e, d in ev2 if e == "token")
        ok("调用了 calculate_compensation",
           any(n == "calculate_compensation" for n, _ in tools2),
           [n for n, _ in tools2])
        ok("答案里出现具体金额（工具算得出）",
           "112,000" in answer2 or "112000" in answer2, answer2[:120])
        ok("这一轮没有重复调用",
           len(tools2) == len(set(tools2)), tools2)
        ok("没有 degraded", "degraded" not in names2)

        # ---- 6. 多法域（可选，省一次钱）----
        if not skip_law2:
            print("\n== 6. 多法域：切到消费内核再问一次 ==")
            ev3 = c.chat("超市买到过期食品，能要求赔多少", profile="consumer")
            names3 = {e for e, _ in ev3}
            answer3 = "".join(d.get("text", "") for e, d in ev3 if e == "token")
            ok("消费内核也出了答案", len(answer3) > 80, answer3[:60])
            ok("没有 degraded（内核索引也能在线跑）", "degraded" not in names3)
            ok("答案落在食品安全这一部法上",
               ("食品安全法" in answer3) or ("十倍" in answer3) or ("一千" in answer3),
               answer3[:120])

        # ---- 7. MCP ----
        print("\n== 7. MCP 服务自检 ==")
        r2 = subprocess.run([sys.executable, "-m", "app.mcp_server", "--self-check"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", cwd=str(ROOT),
                            env={**__import__("os").environ, "DEEPSEEK_API_KEY": ""})
        tail = ((r2.stdout or "") + (r2.stderr or "")).strip().splitlines()
        ok("MCP self-check 通过（6 项，离线只读）", r2.returncode == 0,
           tail[-4:] if tail else "")

        # ---- 8. 多模态读图 ----
        print("\n== 8. 多模态读图（真模型看真图）==")
        img = ROOT / "eval" / "fixtures" / "pay_slip.png"
        ok("夹具图在位（随包发的那张工资条）", img.is_file(), str(img))
        if img.is_file():
            import base64

            from app import vision
            from app.llm import LLM

            raw = img.read_bytes()
            try:
                mime = vision.sniff_image(raw)
            except Exception as exc:                        # noqa: BLE001
                mime, raw = f"sniff 失败：{exc}", b""
            ok("按魔数认出是 PNG（不看扩展名）", mime == "image/png", mime)
            if raw:
                vres = LLM().extract_from_image(
                    base64.b64encode(raw).decode(), mime) or {}
                vf = vres.get("fields") or {}
                mv = vf.get("monthly_wage")
                mv = mv.get("value") if isinstance(mv, dict) else mv
                norm = str(mv or "").replace(",", "").replace("元", "").strip()
                ok("读出月工资 12000（图上「应发合计 12,000.00」）",
                   norm.startswith("12000"), f"monthly_wage={mv!r}")
                ok("认出这是工资条", vres.get("doc_type") == "pay_slip",
                   vres.get("doc_type"))
                # 这条不是形式主义：这张图上确实没有社保/公积金项目，
                # 模型若一条 unreadable 都不报，说明它在**猜**，那比读错更坏。
                ok("如实报告看不出的项（没有硬猜）",
                   bool(vres.get("unreadable")), vres.get("unreadable"))

        # ---- 9. 内核分流（v133）+ 偏门问法先尝试理解（v135）----
        # 这七处原来都是劳动法写死的：clarify 追问离职、越界拒答说本系统只
        # 处理劳动法、首屏自称劳动法助手、检索空与时效题又问一遍入职/离职、
        # 算钱三闸、回放意图粗分。任何一个接入内核触发到，分区就等于不存在。
        #
        # v135 撤掉了入口的一票否决：clarify / out_of_scope 在线时归一成
        # case_consult，先检索再由证据决定怎么答。所以「越界」那问不能再
        # 断言「吐了固定拒答」，改断言「真的 search_statute 过」；另外补
        # 用户截图里被挡回去的三句偏门问法（债务、宠物、短句）。
        # 配置层逐个内核查（零成本），链路层跑婚家七问。
        print("\n== 9. 内核分流：劳动法文案不许串到别的分区 ==")
        from app import profiles

        # 配置文本是代码写死的，可以严格到禁「劳动法」三个字；模型答案里
        # 「劳动法」可能是合理对比（例如「这不属于劳动法」），只禁用那些
        # 一定代表身份错乱的词，避免断言随模型措辞时绿时红。
        CFG_SMELL = ("离职", "辞退", "月工资", "入职", "劳动仲裁", "12333",
                     "拖欠工资", "劳动法")
        ANS_SMELL = ("离职", "辞退", "月工资", "入职", "劳动仲裁", "12333",
                     "拖欠工资")
        OTHER = ("consumer", "civil", "traffic", "family")
        cfg = {pid: profiles.resolve(pid) for pid in OTHER}

        ok("四个接入内核都配了追问/拒答/空结果提示",
           all(p.clarify_question and p.refuse_text and p.empty_hint
               for p in cfg.values()),
           {k: (bool(v.clarify_question), bool(v.refuse_text),
                bool(v.empty_hint)) for k, v in cfg.items()})
        dirty = {
            pid: [w for w in CFG_SMELL
                  if w in (p.clarify_question + p.refuse_text + p.empty_hint)]
            for pid, p in cfg.items()
        }
        ok("四个内核的追问/拒答/空结果提示里没有劳动法字样",
           not any(dirty.values()), {k: v for k, v in dirty.items() if v})
        # 首屏示例单独查：consumer 故意留了一条标签「能力边界」的反例
        # 「公司拖欠工资，能按消法要三倍赔偿吗」，用来演示分区边界——它
        # 含「拖欠工资」是**设计**，不是串味。算进词表会逼人删掉一个合理
        # 示例，所以按标签豁免；其余示例仍逐条查。
        ex_dirty = {
            pid: [(tag, w) for tag, q in p.examples if "边界" not in tag
                  for w in CFG_SMELL if w in q]
            for pid, p in cfg.items()
        }
        ok("首屏示例里没有无意串进来的劳动法问句（能力边界反例除外）",
           not any(ex_dirty.values()),
           {k: v for k, v in ex_dirty.items() if v})
        ok("四个接入内核都有离线意图词表",
           all(p.keywords for p in cfg.values()),
           {k: len(v.keywords) for k, v in cfg.items()})
        lab = profiles.resolve("labor")
        ok("劳动法内核三项仍为空（原链路逐字不变）",
           not (lab.clarify_question or lab.refuse_text or lab.empty_hint),
           (lab.clarify_question, lab.refuse_text, lab.empty_hint))

        # 每次 chat 不带 session_id ⇒ 服务端每次开新会话，本次 profile 生效
        # （server.py 只有 incoming 分支才会按库里的绑定锁死内核）。
        def _ask_fam(q: str):
            try:
                ev = c.chat(q, profile="family")
            except Exception as exc:                # noqa: BLE001
                # 撞 429 / 超时时让下面报红，而不是抛 traceback 中断整套。
                print(f"    （婚家问答异常：{type(exc).__name__}: {exc}）")
                ev = []
            txt = "".join(d.get("text", "") for e, d in ev if e == "token")
            names = {e for e, _ in ev}
            tools = [n for n, _ in _tools_of(ev)]
            return txt, names, tools

        a_g, n_g, t_g = _ask_fam("你好")
        ok("婚家首屏自称本分区（不是劳动法助手）",
           "婚姻家庭与继承分区" in a_g and "劳动法" not in a_g, a_g[:60])
        ok("首屏是流程早返回，没有为一句问候去调工具", not t_g, t_g)

        a_o, n_o, t_o = _ask_fam("帮我写一首情诗")
        ok("真越界问题也先检索过（不再入口一票否决）",
           "search_statute" in t_o, t_o)
        ok("真越界问题没串到劳动法、没降级",
           "劳动法" not in a_o
           and not [w for w in ANS_SMELL if w in a_o]
           and "degraded" not in n_o, a_o[:80])

        a_x, n_x, _ = _ask_fam("对方出轨")
        ok("「对方出轨」在线出了正文、没降级",
           len(a_x) > 80 and "degraded" not in n_x, a_x[:80])
        ok("出轨题不再追问离职/辞退/月薪/入职",
           not [w for w in ANS_SMELL if w in a_x],
           [w for w in ANS_SMELL if w in a_x])

        a_d, n_d, t_d = _ask_fam("离婚后财产分割的诉讼时效是多久")
        ok("时效题不再追问离职起算日",
           not any(w in a_d for w in ("离职是哪一年", "哪一年哪个月入职"))
           and not [w for w in ANS_SMELL if w in a_d], a_d[:100])
        ok("时效题没有误调劳动争议的期限工具",
           "calculate_deadline" not in t_d, t_d)

        # ---- v135：用户截图里被静态追问 / 越界拒答挡回去的三句偏门问法 ----
        # 判据不写「答案必须多长」，写「系统有没有真的去查」——模型措辞
        # 会飘，检索是确定性的。查不到由 agent.py 的三处证据出口如实说
        # 「依据不足」，不许硬编，也不许把人推出门。
        fam_cfg = cfg["family"]
        a_b, n_b, t_b = _ask_fam("债务怎么分配")
        ok("「债务怎么分配」真的去检索了", "search_statute" in t_b, t_b)
        ok("「债务怎么分配」不再被静态四选一挡回",
           a_b != fam_cfg.clarify_question and len(a_b) > 40, a_b[:80])
        ok("「债务怎么分配」没有劳动争议串味、没降级",
           not [w for w in ANS_SMELL if w in a_b]
           and "degraded" not in n_b, a_b[:80])

        a_p, n_p, t_p = _ask_fam("有条狗怎么分配")
        ok("「有条狗怎么分配」真的去检索了", "search_statute" in t_p, t_p)
        ok("「有条狗怎么分配」不再是越界拒答（v133 会把人推出门）",
           "不在本分区范围内" not in a_p, a_p[:80])
        ok("「有条狗怎么分配」没有劳动争议串味、没降级",
           not [w for w in ANS_SMELL if w in a_p]
           and "degraded" not in n_p, a_p[:80])

        a_s, n_s, t_s = _ask_fam("我离婚了")
        ok("短句「我离婚了」也尝试过理解，不是恒定四选一",
           "search_statute" in t_s or a_s != fam_cfg.clarify_question,
           (a_s[:60], t_s))
        ok("短句「我离婚了」没有劳动争议串味、没降级",
           not [w for w in ANS_SMELL if w in a_s]
           and "degraded" not in n_s, a_s[:80])

        ok("婚家七问全程没有 degraded",
           not any("degraded" in ns for ns in
                   (n_g, n_o, n_x, n_d, n_b, n_p, n_s)))

        # ---- 10. v139 三页：在线出题 / 条号回填 / 学习进度 ----
        # 这一节存在的理由：test_library / test_study 全是回放，验的是
        # 「离线兜底能不能用」。而 v139 最关键的那条不变量——
        # **模型编的条号一律不采信，cites 恒为服务端回填的 article_id**——
        # 只有真模型跑一遍才有意义：回放题的 cites 是代码写死的，怎么改都绿。
        print("\n== 10. 法典图书馆 / 自主学习（在线，v139）==")
        st, books, _ = c._req("/api/library/books?pid=labor")
        ok("图书馆目录 200 且给全 9 部法",
           st == 200 and books.get("law_count") == 9
           and len(books.get("laws") or []) == 9,
           (st, books.get("law_count")))
        ok("图书馆条文数与语料一致（2066）",
           books.get("article_count") == 2066, books.get("article_count"))
        _ch = ((books.get("laws") or [{}])[0].get("chapters") or [{}])[0]
        aid = str(((_ch.get("articles") or [{}])[0].get("id")) or "")
        st, art, _ = c._req("/api/library/article?pid=labor&id="
                            + urllib.parse.quote(aid, safe=""))
        ok("按目录点条号能取回原文（id 回库）",
           st == 200 and art.get("id") == aid
           and len(str(art.get("content") or "")) > 20,
           (st, art.get("article_no")))
        st_x, art_x, _ = c._req("/api/library/article?pid=consumer&id="
                               + urllib.parse.quote(aid, safe=""))
        ok("跨内核取条被拒（劳动法条号在消费内核取不到）",
           st_x != 200 or not (art_x.get("content") or ""),
           (st_x, (art_x or {}).get("error")))

        st, quiz, _ = c._req("/api/study/quiz",
                             {"pid": "labor", "n": 2}, timeout=180)
        items = quiz.get("items") or []
        ok("在线出题给出了题目（不空手）",
           st == 200 and len(items) >= 1, (st, quiz.get("note")))
        ok("确实由模型出题（source=llm，不是悄悄回落离线）",
           quiz.get("source") == "llm", quiz.get("source"))
        ok("题目里不带正确答案/解析（F12 看不到）",
           all("answer" not in it and "explain" not in it for it in items),
           [sorted(it) for it in items][:1])
        ok("每题四个选项",
           all(len(it.get("options") or []) == 4 for it in items),
           [len(it.get("options") or []) for it in items])

        q0 = items[0] if items else {}
        st, g, _ = c._req("/api/study/grade",
                          {"pid": "labor", "quiz_id": q0.get("quiz_id"),
                           "answer": "A"}, timeout=120)
        ok("判答返回结果与解析",
           st == 200 and "correct" in g and bool(g.get("feedback")),
           (st, g.get("correct")))
        # 核心那条：线上题的 cites 必须是**能回库取到的 article_id**。
        # 让模型自己报条号，这里就会露——某个 id 取不回原文。
        bad = []
        for cid in (g.get("cites") or [])[:3]:
            cid = cid.get("id") if isinstance(cid, dict) else cid
            sti, ai, _ = c._req("/api/library/article?pid=labor&id="
                                + urllib.parse.quote(str(cid), safe=""))
            if sti != 200 or not (ai.get("content") or ""):
                bad.append(cid)
        ok("判答引用的条文能逐条回库取到（模型编的条号会在这里露）",
           bool(g.get("cites")) and not bad, bad or g.get("cites"))

        st, gx, _ = c._req("/api/study/grade",
                           {"pid": "family", "quiz_id": q0.get("quiz_id"),
                            "answer": "A"}, timeout=60)
        ok("跨法典判答被拒（劳动法的题不能当婚家的判）",
           st == 404, (st, (gx or {}).get("error")))

        st, dash, _ = c._req("/api/study/dashboard")
        profs = dash.get("profiles") or []
        ok("个人主页一次给全五个法典的进度",
           st == 200 and len(profs) == 5, (st, [p.get("pid") for p in profs]))
        lab = next((p for p in profs if p.get("pid") == "labor"), {})
        ok("刚答的那题进了进度（累计作答与已练条数都 ≥1）",
           int(dash.get("total_attempts") or 0) >= 1
           and int(lab.get("articles_seen") or 0) >= 1,
           (dash.get("total_attempts"), lab.get("articles_seen")))
        ok("进度是「章节覆盖率 70% + 正确率 30%」的合成值（0–100 的整数）",
           isinstance(lab.get("percent"), int) and 0 <= lab["percent"] <= 100,
           lab.get("percent"))

        st, dl, _ = c._req("/api/study/daily?pid=labor", timeout=180)
        d_items = dl.get("items") or []
        ok("每日一题在线也能出（四选一）",
           st == 200 and len(d_items) == 1
           and len(d_items[0].get("options") or []) == 4,
           (st, dl.get("source")))

        # ---- v140：卷子。这里只验「在线那张卷子的结构对不对」，
        # 判分口径由 test_study 的 92 项钉死（回放能跑，不必花真 key）。
        # 放这里是因为：在线时 choice 走模型、judge/blank 走规则，
        # **两种来源混在同一张卷子里会不会串味**，只有真 key 跑一次才知道。
        # 不传 qtypes，用默认 6:2:2 —— 6 题摊成 4/1/1，三种题型必然都出现。
        st, pap, _ = c._req("/api/study/paper",
                            {"pid": "labor", "n": 6}, timeout=240)
        p_items = pap.get("items") or []
        ok("在线能一次出一整张卷（6 题）",
           st == 200 and len(p_items) == 6 and bool(pap.get("paper_id")),
           (st, len(p_items), pap.get("paper_id")))
        types = {it.get("qtype") for it in p_items}
        ok("卷子里三种题型都出现过（单选/判断/填空）",
           types >= {"choice", "judge", "blank"}, sorted(types))
        ok("卷子题面一律不带答案（三种题型都不能漏）",
           bool(p_items) and all("answer" not in it and "explain" not in it
                                 for it in p_items),
           sorted(p_items[0]) if p_items else [])
        ok("填空题带占位符、判断题只有两个选项（题型没串）",
           bool(p_items)
           and all(("＿＿" in (it.get("stem") or ""))
                   for it in p_items if it.get("qtype") == "blank")
           and all(len(it.get("options") or []) == 2
                   for it in p_items if it.get("qtype") == "judge"),
           [(it.get("qtype"), len(it.get("options") or []))
            for it in p_items])
        st, pr, _ = c._req("/api/study/report?pid=labor&paper_id="
                           + urllib.parse.quote(
                               str(pap.get("paper_id") or ""), safe=""))
        rtxt = str((pr or {}).get("text") or "")
        ok("卷面报告可导出且不预测胜诉率",
           st == 200 and len(rtxt) > 80
           and "胜诉率" not in rtxt and "胜诉可能" not in rtxt,
           (st, rtxt[:40]))

        print(f"\n第一问耗时 {ms} ms")
    finally:
        stop_service(svc)

    print("\n" + "=" * 66)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n in FAIL:
            print(f"  ✗ {n}")
        return 1
    print("结论：带 key 的包在线功能全通。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
