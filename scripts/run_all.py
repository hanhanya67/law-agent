# -*- coding: utf-8 -*-
"""
一键回归：把散在 scripts/ 里的检查串成一张表。

为什么要它：
  改完一处代码，该跑哪几个检查全靠记忆。漏跑一个的代价不是「没测到」，
  而是「以为测过了」——这比不测更危险。这个脚本不发明新检查，只负责
  按固定顺序跑完、把结果收成一张表，并且**失败也继续跑完**：
  只报第一个错，剩下的可能都被同一个原因挡住，看不到全貌。

顺序是刻意的：
  纯逻辑的在前（几秒、零成本、失败就说明代码写错了），
  依赖服务和浏览器的在后（慢、可能因为端口没开而假失败）。

跑法：
    ./.venv/Scripts/python.exe scripts/run_all.py            # 全部（跳过要花钱的）
    ./.venv/Scripts/python.exe scripts/run_all.py --offline  # 不启服务，跳过界面检查
    ./.venv/Scripts/python.exe scripts/run_all.py --with-model  # 判定回归调真模型（花钱）
    ./.venv/Scripts/python.exe scripts/run_all.py --shots    # 顺带重出截图

**有一条链路不在这里**：`scripts/check_live.py`（在线验收，67 项，11 轮问答
+ 1 次多模态读图，其中 1 轮是流程早返回、不花钱）。
它必须真连模型才有意义（验的就是「带 key 时全功能可用」），所以不进这套默认
流程——默认流程全部强制回放，不花钱、也不受当天网络影响。要验它单独跑：

    ./.venv/Scripts/python.exe scripts/check_live.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:            # noqa: BLE001
    pass


def wpad(s: str, width: int) -> str:
    """按显示宽度补空格。`f"{x:<10}"` 是按字符数补的，中文占两格，
    所以「主集」「key 自检」「口语→术语映射」会各自歪一点，整张表看着像没对齐。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


# (名称, 脚本, 额外参数, 需要服务, 说明)
STEPS = [
    ("引用抽取", "test_cite.py", [], "承前省略/裸法名/序数词干扰/同法指代/自检能否抓错引用"),
    ("gap 文案", "test_gap.py", [], "「本库未收录」正则拦截"),
    ("题集自检", "check_questions.py", [], "gold 引用的条文是否真在语料里"),
    ("题库失效审计", "check_gold_repeal.py", [],
     "gold / 证据有没有引用已废止条文（会过期的还有题库）"),
    ("题集回放", "check_eval.py", [],
     "86 题四套全离线：召回/要点/可溯源 100%、误拒 0%（8s，零成本）"),
    ("评测来源标注", "test_eval_mode.py", [],
     "51 项：来源三态/老明细不冒充在线/补跑命令/开跑时刻/复跑合并/下游口径"),
    ("口语→术语映射", "test_rewrite.py", [],
     "213 项：同义改写/术语不被带偏/口语骨架/计划执行/越界判定/定性规则分焦点/取法名/执行容量/版本对比/规则表缺口/结论到位/违约金正反/拆规则不抢题/依据有出处/期限题定性/答到问的那件事/口径题定性/废止说法与兜底建议/取条不串法/小条号引用/发薪方式/多久不等于时效/到期不续签/查原文用基础规则/越界闸门扩容/年限折算/85条赔偿金/工伤保险条例定性规则/只说被辞退+问金额的定性"),
    ("认证与越权", "test_auth.py", [], "37 项：越权/封号即时生效/末位管理员/概览运行态"),
    ("期限计算", "test_deadline.py", [],
     "43 项：期间算术/在职三态/在职时残留终止日不采信/缺工伤保险条例"),
    ("补偿计算", "test_calc.py", [],
     "104 项：三倍封顶/十二年线只压高薪/正好 12 年不压/二倍同样封顶/"
     "缺参数只给公式不出数字/地名写法归一/上年度方向/2024+2025 双年度/"
     "口语数字抽取（年份不许当工资）/金额递进生成 prompt/检索空转时把计算拉回来/"
     "v132 软闸（要素齐就派给工具）/半程闸（缺日期改查社平）"),
    # 这一套专治「只有真 key 才暴露」的重复调用：9 步里 6 步是同一批查询。
    # 既有 33 个套件全强制回放，回放的决策由规则写死、永远不会重复，
    # 所以这个 bug 在真模型下一次次发生、在回归里一条断言都没有。
    ("Agent 循环去重", "test_loop.py", [],
     "10 项：调用指纹规范化/两轮重复只执行一次/整轮重复后立即退出/"
     "同工具不同参数不误杀/不重复时行为不变（假模型，不联网不花钱）"),
    ("文书生成（模板）", "test_docgen.py", [], "46 项：请求事项必须留空、条号可核验"),
    # 上一套测的是 docgen 的**模板渲染**，这一套测的是它被包成 Agent 工具之后
    # 的**全链路**：决策该不该调、调完槽位有没有被档案污染、正文有没有递到
    # 生成侧、答案层会不会挂上「未标注条文依据」的假警报。两套都必要：
    # 模板改错字只在上一套报，工具接线断掉只在这一套报。
    ("文书工具（Agent）", "test_doc.py", [],
     "82 项：请求事项金额必须留空/档案不污染槽位/该写不该写的边界/"
     "正文原样递进 prompt/不走三段式/滤掉假警报/lookup 无证据不崩/"
     "「未覆盖」不能当免检口令/一句话要两样东西时文书和金额都给"),
    # 多模态那一层真正决定安危的不是模型读到什么，是**读到之后代码允许什么
    # 进槽位**。模型看错内容靠人确认兜，代码放错东西进来只能靠这一套兜。
    ("图片证据（多模态）", "test_vision.py", [],
     "55 项：文件头识别（不看后缀）/没有原文依据的字段一律丢/"
     "月薪数量级检查/布尔与日期归一/字段范围收敛/超限拒绝/只写勾中的"),
    # 办案包五块是纯规则产物，这一套同时守「条号回库核验」和「不替用户填金额」；
    # MCP 那一套真起 stdio 子进程，专抓进程边界上的静默故障。
    ("办案包（五块产物）", "test_extras.py", [],
     "96 项：条号逐条回库核验/在职讨薪不画截止日/金额恒留空/"
     "长词抑制/举证状态语义/回放与 MCP 共用入口"),
    ("MCP 服务", "test_mcp.py", [],
     "57 项：stdio 握手/通知不回包/六个只读能力/错误不崩进程/stdout 纯净"),
    ("判例参考与申诉决策", "test_cases.py", [],
     "60 项：来源与条号回库核验/立场过滤/结局分布只数事实不给胜率/"
     "成本收益给区间并分出 A/B/C 三类/结论可拍板/零新增 Agent 工具"),
    ("案件级附件与结论卡（v124/v136）", "test_casekit.py", [],
     "120 项：五内核分支程序表/文书骨架留空/结论卡红线/"
     "两个入口一致/开关按任务存/删任务级联清"),
    ("显式立场与表达", "test_persona.py", [],
     "63 项：白名单立场/没有语气推断入口/建议默认不生效/"
     "24 组合硬边界/立场联动 applicant/SSE 即时办案包吃到已保存画像"),
    ("网络自动检测与受控联网", "test_network.py", [],
     "76 项：三模式/TCP 握手与缓存/异常一律离线/默认不联网/"
     "无网自动降级/本机回环真 HTTP/线索不进结论"),
    ("搜索/反馈/状态", "test_feedback.py", [],
     "57 项：转义/会话隔离/改判撤销/未调用不报绿/失败告警/判定一致/降级冷却重试"),
    ("寒暄与补充", "test_intent.py", [],
     "38 项：寒暄不吞真问题、不再重放上一题、短句越界 vs 信息不足、"
     "degraded 之后不夹正文、办案包在 done 之前发完"),
    ("审计告警", "test_alerts.py", [], "三条规则的触发阈值与去重"),
    ("页面引用", "check_pages.py", [], "id ↔ 定义、页面接口 ↔ 注册路由"),
    # v139：图书馆把「按内核分库」从检索扩到读原文，主索引逐位不变这条
    # 必须有人钉；纯函数级，不起服务。
    ("法典图书馆", "test_library.py", [],
     "41 项：五内核分库不串/目录 id 回库/全文与摘录分开说/条号直达与关键词/"
     "跨内核取条拒绝/主索引条文数与 BM25 分数逐位不变"),
    ("配置对账", "check_env.py", [],
     "14 项：清单与文档对账（假开关/隐藏开关/整数默认值/交付说明数字/"
     "check_key 收到空流时的岔路）"),
    ("快照漂移", "check_snapshot.py", [],
     "交付快照是否落后于工作区（最新一份逐文件指纹，先自检再体检）"),
    ("判定回归", "test_judge.py", ["--dry-run"], "零成本先验，不代表判定质量"),
]

# 这两个套件都要一个真实的服务。以前元组里有个 `need_svc` 标记，但 `run_one()`
# 连看都没看它——本脚本从不起服务，于是它们连的是「8000 上碰巧存在的那个」，
# 而那个服务属于哪个目录没人校验（详见 README「界面交互串台」一节）。
# 现在两套都自己起服务、端口随机挑（公共件在 scripts/_svc.py），
# 所以这个标记连「说明用途」的价值也没有了，直接删掉——
# 留着一个没人实现的开关，会让人以为已经有人管了。
WITH_SERVICE = [
    ("CSRF 防护", "test_csrf.py", [], "12 项：自己起服务"),
    ("档案与任务", "test_profile_task.py", [],
     "82 项：首登档案/任务 CRUD 与隔离/会话继承任务要素/档案地区进槽位/"
     "任务级证据跨会话/按任务聚合申请书/删除会话与任务（级联 + 越权 403）/"
     "未分类自动归入「历史咨询」"),
    ("语料降级", "check_corpus.py", [],
     "17 项：缺语料时两处都明说且不吞运行模式，语料在位时不误报；重建不丢法"),
    ("启动脚本", "check_bat.py", [],
     "34 项：编码/引用/块内括号/端口预检/两个入口真跑/缺语料分支/数字对账/端口一致性/排除表命中"),
    ("界面交互", "check_ui.py", [], "自己起服务 + 真实浏览器点一遍"),
    # v118 收口：test_network 验的是模块层，check_ui 在默认配置下验的是
    # 「不联网」，两者都没覆盖「开关全开之后服务层能不能把 external=1
    # 透传、拿回线索、又不污染本地结论」。这段才是「联网补全了没有」的证明，
    # 不能只留一个没人跑的一次性脚本。
    ("联网开启路径（真服务）", "probe_network_live.py", [],
     "14 项：真起服务 + 本机回环数据源/开关全开真拿到线索/胜率条目被丢/"
     "顶层四键不被污染/不带 external 就不联网/两接口逐字段一致"),
    # v119：加内核最容易出的事故是「默认劳动法被悄悄改坏」。这一套把
    # labor 的提示词、索引分数、五块结构逐字钉死，再验两个接入内核、
    # 专属能力「不许猜」、以及真服务层的会话绑定与防串库。
    ("可插拔法律内核", "test_profiles.py", [],
     "317 项：五内核注册与白名单/labor 提示词逐字不变/检索分库与主索引零改动/"
     "定性规则表按内核切换/办案包五块与专属块/道交清单与婚家要件不许猜/"
     "追问/拒答/空结果/期限/首屏按内核（v133/v138）/任务记忆只进接入内核/"
     "真服务会话绑定与防串库"),
    # v138：接入内核自己的案情槽位。和上一套的分工——
    # 上一套验「按内核分流」，这一套验「分出来的字段真的被抽到、存住、用在
    # 追问上」。两处都会红，但红的原因不同，混在一个脚本里会互相掩护。
    ("接入内核追问（v138）", "test_dslots.py", [],
     "82 项：五内核各一张字段表/只认本内核的键/空值与「不知道」不进库/"
     "离线规则抽取（金额只认四种写法）/追问→回答→记住→不再问/"
     "换内核不串字段/劳动法槽位形状与追问文案逐字不变/"
     "session 接口带 dslots、/dslots 手改闭环（写/读回/空串删/非法拒/labor 拒）"),
    # v139：学习数据按 (user_id,pid) 隔离，接口层还要验登录/CSRF/跨用户拒绝，
    # 所以放需要服务的这一组。
    ("自主学习与个人主页", "test_study.py", [],
     "94 项：回放包离线出题可答/三种题型（单选/判断/填空）/题型配比按用户"
     "点的来（v142）/卷子与交卷报告/"
     "成绩口径与逐题判答一致/遗忘曲线/打卡日历/错题重练/跨用户与跨内核拒绝"),
]


def run_one(name: str, script: str, extra: list[str],
            note: str, timeout: int = 180) -> tuple[bool, float, str]:
    cmd = [str(PY), str(ROOT / "scripts" / script)] + extra
    env = dict(os.environ)
    # 子脚本普遍写了 stdout.reconfigure('utf-8')，父进程这边也统一，
    # 否则 Windows 控制台按 GBK 解码会抛 UnicodeDecodeError。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                           timeout=timeout, env=env)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode == 0, time.time() - t0, (out + err)
    except subprocess.TimeoutExpired:
        return False, time.time() - t0, f"超时（{timeout}s）"


def _delete_blocked(out: str) -> bool:
    """这一套是不是被「删除配额」拦停的（环境问题，不是代码坏了）。

    本机受限环境会在**同一轮累计删除文件数**超过阈值之后拦停删除动作。
    被拦的那个子进程是当场死掉的——不是抛 OSError，所以子脚本里的
    `except OSError: pass` 接不住，日志里只剩沙箱打的一行
    `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`，然后戛然而止。

    最容易被拦的是 check_bat.py：它要在根目录反复创建/删除
    `_bat_occupied.bat` / `_bat_free.bat`，而它前面排着几个会删上千个
    临时文件的套件（无头浏览器的 profile、临时语料目录）。
    实测现象：整套 2~3 秒就 FAIL，日志断在「没有去开浏览器」之后、
    「端口空闲时服务真起来了」之前，看起来像启动脚本自己崩了；
    **单独复跑必 34/34 绿**。

    所以这里只做一件事：把话说到人不用再翻 .bat 括号的程度。
    不改成 PASS——那才是真骗人。
    """
    return "[safe-delete]" in out


def main() -> int:
    args = set(sys.argv[1:])
    offline = "--offline" in args
    with_model = "--with-model" in args
    shots = "--shots" in args

    steps = list(STEPS)
    if with_model:
        # 判定回归去掉 --dry-run，真调模型；再加一条只有有钱时才验得到的链：
        # 「降级 → 冷却 → 恢复 → 又真的在线」。它需要一次真实的失败（换成坏 key
        # 打一次 401），余额充足时永远不会自然发生，所以平时跑不到。
        # 这里不替用户决定花不花钱，只是把「现在已经要花钱了」说清楚。
        steps = [(n, s, [a for a in x if a != "--dry-run"], note)
                 for (n, s, x, note) in steps]
        # key 自检排在最前：key 失效 / 模型名被改 / 余额见底时，后面每条都会
        # **悄悄降级成回放**，卷子照样出分、看着还行，其实答案是规则拼的。
        # 这种「绿得莫名其妙」比一条红字难查得多，所以先花几十 token 验一次。
        steps.insert(0, ("key 自检", "check_key.py", [],
                         "key/模型名/Function Calling/余额（要联网，花几十 token）"))
        steps.append(("降级恢复真机", "check_recover.py", [],
                      "8 项：真 401 → 降级 → 冷却 → 又真的在线"))
    if not offline:
        steps += WITH_SERVICE

    # 「跳过要花钱的」这句承诺，以前只靠「我们碰巧没写调用模型的代码」——没有机制。
    # 实测不是这样：本机有 .env 时会真连模型（寒暄套件 1s → 35s，整套 106s → 183s），
    # 而且红绿取决于当天网络与余额。清空 key 把它变成机制：子进程全继承空值，
    # 而 app/config.py 用的是 os.environ.setdefault，已存在的空串不会被 .env 覆盖。
    if not with_model:
        os.environ["DEEPSEEK_API_KEY"] = ""

    print("=" * 74)
    print("  一键回归")
    print("=" * 74)
    if with_model:
        print("  ⚠ 会调用真实模型，产生费用（key 自检 + 判定回归 + 降级恢复真机）")
    else:
        print("  （已清空 DEEPSEEK_API_KEY：这次不碰模型；要验真实链路加 --with-model）")
    if offline:
        print("  （--offline：跳过需要服务的检查）")
    else:
        # 数一下而不是写死「两个」：这个数字跟着 WITH_SERVICE 变，
        # 写死之后加了一项也没人回头改，读的人会以为漏跑了一个。
        print(f"  注：需要服务的 {len(WITH_SERVICE)} 个套件各自起自己的服务，端口随机挑，")
        print("      不依赖 8000 上已有的任何一个。")
    print()

    results = []
    blocked: dict[str, str] = {}      # 套件名 → 脚本名，只装「被删除配额拦停」的
    for name, script, extra, note in steps:
        ok, secs, out = run_one(name, script, extra, note)
        results.append((name, ok, secs, note))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {wpad(name, 14)}{secs:>6.2f}s   {note}")
        if not ok:
            # 只打尾部：真正的报错一般在最后 20 行，前面全是 PASS 刷屏
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-20:]
            print("  " + "-" * 70)
            for ln in tail:
                print("     " + ln)
            print("  " + "-" * 70)
            if _delete_blocked(out):
                blocked[name] = script
                print(f"     ↑ 这是**环境拦停**，不是代码坏了：日志里有 [safe-delete]，"
                      f"子进程被删断在\n"
                      f"       第 {len([ln for ln in out.splitlines() if '[PASS]' in ln])}"
                      f" 项之后。单独复跑确认：\n"
                      f"       ./.venv/Scripts/python.exe scripts/{script}")

    if shots:
        print("\n  重出截图…")
        # 420s 而不是默认 180s：截图要起三个服务、开无头 Edge、拍三十多张，
        # 实测 177s——离 180 只剩 3 秒。再多一张图，这一项就会「超时」，
        # 而超时的输出长得像截图失败，会把人引去查样式。
        # 超时值应该由「这个脚本本来要跑多久」决定，不是全局一个数。
        ok, secs, out = run_one("截图", "shot_pages.py", [], "", timeout=420)
        print(f"  [{'PASS' if ok else 'FAIL'}] {wpad('截图', 14)}{secs:>6.2f}s")
        if not ok:
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-12:]
            for ln in tail:
                print("     " + ln)
        # 不写死张数：v117 加了三张办案包特写之后，「29 张」当场成了假话，
        # 而这种只出现在日志里的数字没人会回头校对。
        results.append(("截图", ok, secs,
                        "全套页面截图（含 3 张办案包特写），含 7 条断言"))

    passed = sum(1 for _, ok, _, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 74)
    print(f"  {passed} / {total} 通过   总耗时 {sum(s for _, _, s, _ in results):.1f}s")
    print("=" * 74)

    if passed < total:
        print("\n失败清单：")
        for name, ok, _, note in results:
            if not ok:
                print(f"  · {name}（{note}）")
                if name in blocked:
                    print(f"    → 环境拦停（[safe-delete]），不是代码："
                          f"单独跑 scripts/{blocked[name]} 复验")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
