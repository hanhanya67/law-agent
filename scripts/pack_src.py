#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 law-agent 打成源码包（zip）。

为什么用黑名单而不是白名单：源码包要的是「全」，白名单会漏掉新加的文件
而没人发现。黑名单反过来——每一项都必须能写出剔处理由，写不出来的就进包。

为什么排除表写在代码里而不是命令行里：下次照着跑一遍就能复现同一份包，
不用翻聊天记录去回忆当初到底剔了什么。

用法：
    python scripts/pack_src.py                     # 打到桌面，**默认带 .env（含真实 key）**
    python scripts/pack_src.py --no-env            # 不带 .env：回放包，解压后不花钱
    python scripts/pack_src.py --out X:\a.zip      # 指定位置
    python scripts/pack_src.py --dry-run           # 只看清单，不落盘
    python scripts/pack_src.py --no-reports        # 不带 22 份历史评测报告

为什么默认带 .env（v132 起翻转）：
    这套东西是「作品」，交出去就要能直接演示——全流程接通真模型、九件工具
    全部可用，而不是让收货方先去申请 key 再改配置。所以默认带上，解压即用。

    代价必须说清：.env 里是**能直接花钱的**真实 key，进包等于把额度交出去。
    所以文件名默认带 `-with-key`，包内第一屏就是警示，两层提醒都在。
    要出一份能随便发的版本，显式加 `--no-env`（文件名不带后缀，包内说明
    会写「回放模式，llm_enabled=false」）。

    之前的默认是反过来的（不带 key，要 key 得加 --with-env）。翻转的理由：
    这份包的实际用途是交作品，不是开源分发；默认回放会让「解压后能不能
    演示」变成每个收到它的人各自要解决的问题。--with-env 仍然接受，
    等价于默认值，旧命令不会报错。
"""

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = ROOT.name

# ---------------------------------------------------------------- 排除表
# 每条都是 (glob, 剔处理由)。理由会写进包内的 打包说明.txt，
# 让拿到包的人知道「少了什么」是刻意的、不是打包漏了。

EXCLUDE_DIR_NAMES = {
    ".venv",            # 65 MB：pyvenv.cfg 写死开发机 Python 路径，换机器必然失效
    "__pycache__",      # 字节码缓存
    ".pytest_cache",
    ".git",
    "archive",          # eval/archive：历史归档
    "archives",         # data/archives：无主会话导出，含对话正文
    "backups",          # data/backups：数据库备份，含账号哈希与全部会话
}

# 按**前缀**排除的目录。
#
# 为什么不能写死具体名字：这张表原来写的是 `.snapshot_v19` 和 `_shot`，
# 而目录早就不止那一个了——`.snapshot_v79/80/81/82`（4 份旧代码，57 个文件
# 4.1 MB）和根目录的 `_shots/`（10 张 1.2 MB 的 PNG）**全都被打进包里**。
# 黑名单的意义是「宁可全、也要明确说出剔了什么」，可一旦写死一个具体版本号，
# 它就会在下一版悄悄失效，而且失效时没有任何报错——包照打，只是变胖变乱，
# 里面还躺着几份旧代码。接收方拿到以后照着旧快照改，才是真麻烦。
EXCLUDE_DIR_PREFIXES = {
    ".snapshot_": "改动前的代码快照，只用于回退（按前缀排除，不写死版本号）",
    "_shot": "浏览器 profile 与截图，与运行无关（_shot / _shots 都算）",
    # 整目录备份是改造前的工作区副本：里面躺着旧 README、旧打包脚本、
    # 旧交付说明和上一版的验收数字。它进包不会报错，只会让收货方手里
    # 同时有两份互相矛盾的代码 —— 比漏文件更难发现，因为它「也在包里」。
    "_bak_": "改造前的整目录备份，含旧版文档与旧版打包脚本（按前缀排除）",
}

_DIR_WHY = {
    ".venv": "65 MB 虚拟环境，绑定开发机 Python 路径，换机器必然失效",
    "__pycache__": "字节码缓存",
    ".pytest_cache": "测试缓存",
    ".git": "版本库",
    "archive": "历史归档",
    "archives": "无主会话导出，含对话正文",
    "backups": "数据库备份，含账号哈希与全部会话",
}

# (glob, 理由)
EXCLUDE_FILES = [
    # v132 起默认放行（collect() 里 with_env 默认为 True）。这行留着是因为
    # `--no-env` 还要用到它——"不带 key"的那版包必须有一条规则把它挡在外面，
    # 而且理由要写在能看见的地方，不能只是 `if` 里一句 continue。
    (".env",            "真实 API key（默认已放行；--no-env 时按这条剔除，"
                        "包会退回回放模式）"),
    ("data/*.db*",      "law.db / eval.db 及其 WAL：账号哈希 + 全部会话正文，"
                        "首次运行自动重建"),
    # 改语料前留的手工备份。v131 打出来才发现 `domain_statutes.jsonl.bak_*`
    # 两份跟着进了包：`data/*.db*` 只拦数据库，`.jsonl.bak_20261106_224607`
    # 既不以 .db 结尾也不在别的规则里，于是安静地躺在 data/ 下。
    # 它不影响运行，但会让接收方分不清哪份是现行语料——正是「黑名单写死
    # 具体文件名、下一版悄悄失效」的那类事故。
    ("*.bak",           "手工改语料前的旧备份，与运行无关，且会让人分不清哪份是现行的"),
    ("*.bak_*",         "手工改语料前的旧备份（带时间戳），与运行无关，且会让人分不清哪份是现行的"),

    ("eval/_*.log",     "评测与页面的运行日志"),
    ("*.log",           "运行日志"),
    ("*.pyc",           "字节码"),
    ("*.pyo",           "字节码"),
    # check_bat 会在项目根写这两个副本，finally 里删。但 finally 管不住
    # 「进程被强杀」——那时它俩就留在目录里，而打包是扫目录的。
    ("_bat_*.bat",      "check_bat 的临时副本（正常跑完会删，强杀时会残留）"),
    (".DS_Store",       "macOS 垃圾文件"),
]

# 包内说明里的「验证过什么」一段。写在代码里而不是命令行里，是为了让
# 下次重新打包时这段结论跟着包走，不会被人手敲漏。内容必须与实测一致——
# 改了代码就重跑验证并重打，别留着上一次的结论。
VERIFIED = """【验证过什么】（2026-11-13 v141 实测 · 回放版 / --no-env）

这一版是显式 `--no-env` 打的：没有 .env，llm_enabled=false。
默认打包（v132 起）会带上 key，那份的验收看「内置 key 版」那一节。

解压到一个全新目录（没有 .env、没有数据库）之后：

  · 服务能起：/api/meta 返回 2066 条条文、9 部法律、llm_enabled=false、
    retrieval=bm25 —— 语料是从 statutes.jsonl 自动建出来的，
    包里不需要带数据库。
  · 判例库随包发出（data/cases.jsonl，21 件）。决策卡与附录五块都是纯规则产物，
    不调模型、不联网，回放模式下一样全；页面如实标「本地判例库，未联网」。
  · 网络自动检测（v118）默认开启：启动时后台只做一次 TCP 握手，
    /api/meta、/api/status 与判例卡都显示当前是离线还是在线，
    无网自动降级回本地判例库。**检测到有网也不会自动联网**——
    外部检索默认关闭（CASE_SEARCH_ADAPTER=0），必须显式配置并在页面上
    点「联网补充线索」才发请求，SSE 答案流与 MCP 服务永远不联网。
    test_network.py 的 76 项验模块层；probe_network_live.py 的 14 项另起
    真服务、用本机回环数据源验「开关全开」路径，两者都不碰外网。
  · 多领域内核（v119–v123）：一个引擎挂五个法律内核——劳动用工 /
    消费·食安 / 民法典·合同 / 道交·人损 / 婚家·继承。内核只在顶部那条
    索引条里选，选定后绑定该会话，不进全局状态；不传或传错一律回落劳动法。
    每个内核各有自己的语料集合、定性规则表、专属能力、工具表（劳动法 9 个，
    其余各 5 个）、**整页纸色**（背景/卡片/分隔线/顶栏，浅暗各一套，不只是
    主色）、印章与示例问题。换内核时页面用一个从画面正中扩散的圆刷成新纸色
    （浅色下圆边带一道主色描边），圆铺满即结束，没有任何收尾动作（v129 删掉了
    原来那下「整页上浮 6px」——静止画面上的二次跳动，读起来像震一下）。
    任务是带法域的：每张任务卡按「这件案子属于哪部法」上色并挂徽章，
    与当前页面上选中的内核无关；点「进入」时整页跟着切到那件案子的法域。
    任务是自动生成的（v123）：空对话里问第一句就建一个任务，标题取第一句提问
    （寒暄另叫「XX咨询」），顶部随即出现当前任务条；在这一条咨询里追问都记在
    同一个任务下，换了法再问则另起一个任务，原任务原样留在任务页里。
    劳动法那条路径逐字保持 v118 行为，test_profiles.py 的 317 项把这一点钉死。
  · 界面与结果逻辑（v124）：登录页分「用户 / 管理员」，管理员态收掉注册
    入口，服务端注册接口把 role 写死为 user（不是只藏一个按钮）；背景是
    54 秒自动呼吸的明暗＋流动渐变＋双环刻度等纯 CSS 循环动画，系统开了
    「减少动态效果」会一次性全关。没建任务时首页只剩大标题、五枚法案
    胶囊和一个大输入框，第一问后自动建任务并切回完整界面。答案里多了
    一张程序分支流程图和一张「结论卡」——按已确认案情要素、核算结果、
    期限风险直接给结论（建议上诉 / 不建议上诉 / 建议先补材料 /
    建议先核时效），结论下面写清依据：已核算金额、法定期间、凭证状态，
    例如「可得约 1,200 元，而仲裁到一审通常以月计，往返与代理成本高于
    可得金额，划不来」。v136 起卡面只留一句「仅供参考，不构成法律意见。」，
    不再写「条件式」「不替你拍板」「由本人决定」这类垫话；图和结论取自
    同一份数据。另外四个内核也各有文书骨架与
    含起诉 / 上诉分支的程序时间表。两个案件级开关默认全关（v125 起入口移到输入框上方），
    一个任务对应一份，按 task_id 只存开关
    （正文随案情实时重算、不入库），切任务自动恢复、删任务级联清。
    文书请求事项与金额一律留空，只是骨架不是可直接提交的法律文书。
    test_casekit.py 的 120 项把结论卡红线（不出现「建议起诉 / 建议仲裁 /
    建议申诉」、不给百分比）、五内核分支、留空、开关持久化
    与越权拒绝逐条钉死。
  · 对话框布局（v125）：左侧任务列表与页面入口常驻，空状态不再收侧栏；
    输入框加长（常规 100px / 空状态 112px）并加宽；「是否建议开庭」
    「开庭时间提醒」「申请书书写」三个功能移到对话框上方，前一个是
    最近一轮结论卡的速览，后两个仍是按 task_id 存的案件级开关；
    四张示例提问收进输入框左边的「提问参考」下拉，需要时才点开。
    开庭提醒引的《劳动争议调解仲裁法》第三十五条由静态门禁对着
    data/statutes.jsonl 逐条核验，不是编出来的条号。
  · 「值不值得追」决策卡（v126）：答案下面不再铺「办案包」五块并列拼盘，
    改成一张主卡，五格依次为时间成本 / 经济成本 / 历史上成功与失败的
    相似案例计数 / 收益比较（可得金额 vs 要投入的时间与费用）/ 末格
    「建议上不上诉」直接给结论。小额争议会明说「可得约几百元，划不来——
    往返与代理成本高于可得金额」，需要补材料时说「建议先补材料」；
    仍不给胜率；旧的程序时间轴、请求事项候选、举证体检、
    判例原文与立场画像收进「更多材料」可展开，功能与回归都不丢。
  · 时效计算按法域分表（v126）：劳动争议仍是一年仲裁时效 + 在职讨薪
    不受限；其余四内核走《民法典》第一百八十八条的三年诉讼时效，并列出
    第一百九十四 / 一百九十五 / 一百九十七 / 一百九十八条的中止、中断提示。
    「不适用诉讼时效的请求权」「撤销权存续期间」两类不给日期；勾选了
    中断事由也只提示按新起算日重算，不编新截止日。所有条号都由静态门禁
    对着 data/statutes.jsonl 核验。任务页与时效计算页都不显示切法索引条，
    法域跟着当前咨询走；切法后时效表自动重建，不会拿劳动法的表算别的法。
  · 外观与配色（v127–v131）：
    顶栏右侧是两态开关（左日读 / 右夜读，Ctrl+J），设置弹窗「配色」那行直接
    写出当前档位。**只有两档，没有任何一档去看系统配色**——首帧由三页 <head>
    里的内联脚本按 localStorage 定下来（脏值一律落回日读），所以夜读用户
    刷新时不会先闪一道白光；原生部件（滚动条、下拉箭头、日期面板、自动填充
    底色）由 color-scheme 决定，它不看主题属性，两档都已显式钉死。
    换主题时圆内是新色、**圆外的旧帧一动不动**（旧帧动画被整条关掉；原来它会
    淡到 88% 再被圆盖掉，读起来是「切一下闪一下」），换法时旧帧照常淡出，
    两趟不是同一套动画。换法也不再启动平滑滚动（滚动会在圆铺满后继续滑一段，
    看起来像切完还在动），改成在圆底下直接跳。
    `?v=131` 且 /assets 带 Cache-Control: no-cache——旧版 ui.js 是三态循环
    （日读→夜读→跟随系统），被浏览器缓存住就会出现「怎么还有跟随系统」。
  · 分区文案与前端（v133–v134）：四个接入内核的追问 / 拒答 / 空结果 /
    期限 / 首屏问候改走 LawProfile 配置，不再串到「是否离职」；前端劳动争议
    专属文案（新建任务命名提示、个人档案五字段、首登引导第 2/3 步）由
    data-labor-only 标记收起，只隐藏显示层，字段与保存逻辑不删，
    ONB_LAST 随内核缩步，避免走到空白页。劳动法内核逐字不变；
    test_profiles.py 317 项、check_pages.py 静态门禁、check_ui.py 真实浏览器
    274 项分别钉住这三处，含「可见文本按 display 逐层构造」与就地摘标记验红，
    防可见文本探针读到空串造成的假绿；v143 起还钉住单步内核下步骤条整条收起、
    Tab 关在首登弹窗内、Esc 能关、短视口（844×390）下弹窗不出屏且滚得到按钮；
    v144 起钉住首屏「提问参考」展开时五枚胶囊一颗都不压、免责声明一个字都不盖、
    窄屏（390）面板不出右缘、收起后首屏回原位；v145 起钉住徽章子页默认摊开
    （能收起再摊开）、窄屏概览四宫格仍是 2×2、打卡文案不把数字与量词拆两行、
    窄屏知识树不缩成缩略图（卡内可横滑）；v146 起钉住窄屏首屏输入框能写字
    （正文 ≥180px、独占一行）、所有单行输入框的占位提示不被裁（canvas 量）、
    历史记录标题不折行、后台管理窄屏不横滚（侧栏变顶栏、卡片两列）；
    v147 起钉住**桌面 1440 下历史记录标题也不折行**（`input[type=search]`
    的 `width:100%` 会把中文标题压到 min-content，缩成「历史记」/「录」）。
  · 一键回归 `python scripts/run_all.py`：**37 / 37 通过，2 分半到 3 分钟**。
    （2026-11-12 v139 收口后实测 37 / 37；比 v123 多出的五套是 test_loop.py、
    test_casekit.py 与 v138 的 test_dslots.py；耗时随机器负载浮动。
    这里不给具体秒数：写死过两次，每次跑完就变成假话。
    若某次跑出 32 / 33，
    红的多半是「启动脚本」——它收尾要删自己写的临时夹具 `_bat_occupied.bat`，
    被受限环境的「会话累计删除配额」拦在第 20 项之后整段中断；同一个套件单独跑
    `python scripts/check_bat.py` 是 34 项全部通过。**那是环境的红，不是代码的
    红**——普通 Windows 上没有这道删除配额。）
    解压即可跑，不需要先建账号、先起服务——需要服务的 6 个套件各起各的，
    端口随机挑，用到的临时账号跑完就删。
    整套**强制回放**：run_all 默认把 DEEPSEEK_API_KEY 清空，所以这一路
    不花钱、也不受模型当天快慢影响。要验真实链路（key / 余额 / 降级恢复）
    得显式加 `--with-model`，那一路上才联网、才会产生费用。
    秒数不是验收标准，机器慢些多跑几分钟也正常——看的是 37 个通过数。
  · 没配 key 时，「搜索/反馈/状态」会打印「跳过 8 条」并单列出来。
    这 8 条断言的是「开了模型之后运行态怎么变」，LLM_ENABLED 为假时
    接口恒报 disabled/replay，那是正确答案，不是失败。跳过数不计入
    通过数（混进去就是「看着全绿，其实 8 条没跑」）。

拿这份包的人请照这一节重跑一遍再验收。数字对不上，说明包里的东西
和本节写的不一致——先信你跑出来的，别信这段文字。
"""

# 带 key 版（--with-env）的「验证过什么」。
#
# 为什么不能和上面共用一段：上面那句「llm_enabled=false」在带 key 的包里
# 是**假话**——包里有能用的 key，服务一起来就是在线模式。而这一节恰恰是
# 收货方照着验收的地方，写错了会把人引到「是不是 key 没生效」的死胡同。
# 两版包各有一段，main 里按 --with-env 选。
VERIFIED_WITH_KEY = """【验证过什么】（2026-11-13 v141 实测 · 内置 key 版）

解压到一个全新目录（没有数据库、没有历史会话）之后：

  · 包里的 .env 实测可用：`python scripts/check_key.py` 三项全过
    （2026-11-13 v141 复验）——基础对话 / Function Calling（模型自己
    调用了 search_statute）/ 流式输出（首字 1.7 秒）。它会打印 key 的
    前 6 位与长度，不打印全文。
  · 服务能起：/api/meta 返回 2066 条条文、9 部法律、retrieval=bm25、
    corpus_seed=false、**llm_enabled=true**。语料由 data/statutes.jsonl
    自动建出，包里不带数据库。
  · 内核可切：/api/meta 的 profiles 返回五个内核（labor / consumer /
    civil / traffic / family），默认 labor。各自 laws 字段不同——婚家只有
    民法典、道交是民法典 + 道路交通安全法、合同是民法典 + 合同法——
    说明分库真的生效，不是换了个提示词。
    **劳动法那一条的 laws 是空数组**：它不是「没配法」，而是不过滤、直接用
    主索引（9 部法 2066 条都在里面），其余四个内核才按 corpus_laws 过滤。
    看到 `laws: []` 别当成配置缺失。
  · 一键回归 `python scripts/run_all.py`：**37 / 37 通过，约 2 分半**。
    （受限环境里「启动脚本」那一项可能被删除配额拦下——那一条单独跑
    `python scripts/check_bat.py` 是 34 项全部通过，见无 key 版同一节的说明。）
    注意它默认把 DEEPSEEK_API_KEY 清空——这是刻意的，让回归不花钱、
    也不受模型当天快慢影响。要验真实链路（key / 余额 / 降级恢复）
    显式加 `--with-model`，那一路上才联网、才会产生费用。
  · v124 的登录分角色、空状态首页、分支流程图、结论卡与案件级
    附件都是前端 / 规则产物，在线模式下一样可用；判例卡的「不给胜率」
    红线不受是否联网影响。
  · v126 的「值不值得追」决策卡与按法域分表的时效计算同样是纯规则产物：
    在线模式下五格数据、三年诉讼时效、中止中断提示都一致，不额外调模型。
    旧办案包五块仍完整保留在「更多材料」里，MCP 的 case_analysis 输出不变。
  · v127–v131 的外观改动与在线/离线无关：主题只有日读 / 夜读两档（首帧即定，
    不看系统配色，原生滚动条由 color-scheme 两档钉死），换主题时圆外旧帧不动、
    换法时旧帧照常淡出，换法不再启动平滑滚动。这一版资源版本号是 `?v=131`。
  · **在线验收 `python scripts/check_live.py`：68 / 68 全过**（2026-11-13 v141 复核断言数；在线验收路径 v135 起未改，
    10 轮问答（其中 1 轮零调用）+ 1 次读图，约四分钟）。这一套专门补上其余套件——它们全部强制回放，回放模式的决策由规则写死，所以
    「模型会不会重复调工具」「金额是不是模型自己乘的」在那边一条断言都没有。
    它验九件事：
      1) key 链路：对话 / Function Calling / 流式三项全过；
      2) /api/meta 的 llm_enabled=true（先证明真的不是回放）；
      3) 真实问答的事件流完整：有 token 正文、有 verify、有可核验引用、
         答案含【结论】【依据】【建议】三段、**没有 degraded 事件**；
      4) 同参数工具调用零重复；总步数不超理论上界 13（v135 修正口径：
         `tool_start` 混着 L1.5 计划检索（不消耗预算）与 L2 主循环，
         不能把两者之和去比 MAX_STEPS=8，否则模型多分解一条子查询就假红）；
      5) 问金额时 calculate_compensation 必须出现在轨迹里，
         且答案里有工具算出的 112,000（不是模型自己乘的）；
      6) 换消费内核再问一次，答案落在食品安全法上——分库索引在线也成立；
      7) MCP self-check 六项（它显式清空 key，离线只读）；
      8) 多模态读图：拿 `eval/fixtures/pay_slip.png` 让它读，必须读出
         月工资 12000，并如实报告「图里没有社保项目」这类看不出的项。
         为什么用固定图而不是现画：判据是「必须等于 12000」，图一换，
         读出来的数就变，断言会时绿时红。该图 30 KB，随包发出；
      9) 内核分流（v133 + v135）：四个接入内核的追问 / 拒答 / 空结果提示逐个查，
         不许出现「离职 / 辞退 / 月工资 / 入职 / 劳动法」；婚家分区跑七问
         （首屏、越界、出轨、时效，再加 v135 的债务、宠物、短句），断言越界
         那问也**真的 search_statute 过**、偏门问法不再被一句固定四选一或
         越界拒答挡回、全程无 degraded。劳动法内核三项配置仍为空，原链路逐字不变。
  · **v135「无论问什么先尝试理解」**：在线的接入内核不再在入口把 clarify /
    out_of_scope 一票否决，归一成正常咨询后先走检索，由既有的零召回 /
    judge 不相关 / partial 三处出口决定怎么答；追问改由模型带着用户原问生成，
    静态澄清问句降为离线与调用失败兜底。放开之后真 key 又抓到一处老串味
    （婚家答案让用户补入职离职日期与月工资），已按内核分流修掉。
    `test_profiles.py` 317 项（含就地验红 5 项）、`check_live.py` 68 项把两件事钉住。
    劳动法内核、以及全部离线回放路径一字未改。
  · **前端按内核分流（v134）**：新建任务的命名提示、个人档案五个劳动争议
    字段、首登引导第 2/3 步，在非 labor 内核下由 `data-labor-only` 标记 +
    CSS 收起；只隐藏显示层，字段与保存逻辑不删（档案全局一份），
    ONB_LAST 随内核缩步，避免走到空白页；v143 起还钉住单步内核下步骤条
    整条收起、Tab 关在首登弹窗内、Esc 能关、短视口下弹窗不出屏；v144 起钉住
    首屏「提问参考」展开时不压胶囊 / 不切断免责声明、窄屏不出右缘、收起回原位。
    `check_pages.py` 静态门禁与
    `check_ui.py` 真实浏览器 274 项断言把这件事钉住，含可见文本按
    display 逐层构造、读到 >80 字护栏、就地摘掉标记验红（防探针读到
    空串造成假绿）；2026-11-13 实测快照 74 / 74 一致。
  · **v132 修掉的一个真问题（只有真 key 才暴露）**：模型会在循环里把同一批
    检索式重复发好几轮。真 key 实测两条不同问题都是「3 条查询 × 3 轮 = 9 步」，
    步数预算全烧在重复上，第 4 件工具（算钱 / 查时效）永远轮不到——而回放模式
    的决策是规则写死的、从不重复，33 个套件里一条断言都没有。
    修法是两层：决策上下文里显式列出「已经检索过什么」（模型不再重发），
    循环里再按「工具名 + 规范化参数」去重兜底（重复的不执行、不记步、不发事件）。
    现在同样的问题 4 步答完。`scripts/test_loop.py` 用假模型把这件事钉住，
    10 项，含反向验证（去掉去重会失败 5 项）。
  · **另一处**：模型查完条文会自己乘出金额（连「北京社平 12116」都是凭记忆写的，
    这次恰好对）。现在只要「要素齐 + 已有条文 + 在问金额」就直接派给
    calculate_compensation，不等步数见底；缺入职日期时改为先查当地社平工资，
    让答案至少能说清封顶线；完全没算过时，递给生成侧的是一句硬约束
    「一个具体数字都不许出现」。
  · 端到端真模型问答（2026-11-06 v117 当时实测）：注册 →
    提问「公司一直没跟我签劳动合同，还拖欠两个月工资，能要回多少钱？」→
    流式返回 200，事件序列
    session → memory → intent → plan → tool… → verdict → thinking →
    token… → verify → extras → done → turn（15.2s）。
    答案是「结论 / 依据 / 建议」三段、免责在末尾、12 条可核验引用；
    /api/status 报 ok、7 次模型调用 0 错误，确实在线不是回放。
    extras 五块齐全：时间轴 8 阶段 / 请求候选 2 条 / 举证体检 6 条 /
    判例参考 4 件相似案件（附官方处理结果原文、结局计数、成本收益 6 行，
    来源标「本地判例库，未联网」，整卡无百分比胜率）/ 画像如实标 unset、
    简洁表述未自动开启。请求事项金额全部留空、默认一条不勾。
  · MCP 服务在在线模式下仍是只读离线：`python -m app.mcp_server --self-check`
    六项全过；`test_mcp.py` 的子进程显式清空 key，57 项协议回归不花钱。
  · 网络自动检测在这版会显示真实结果（本机实测 api.deepseek.com:443 可达），
    但带 key 不等于开了外部检索：`external_search_enabled` 仍是 false，
    点「联网补充线索」也只回 disabled、不发出请求。要真联网得另外设
    CASE_SEARCH_ADAPTER=1 并配 LAW_CASE_SEARCH_URL，见「交付说明.txt」第六节。
  · 额度提示：上面那一问是 9 步 + 一次长生成，key 有消耗但不贵。
    连续给对方演示十几次没问题；要跑整套在线评测（86 题 × 真模型）
    请自己评估余额，或把 .env 临时改名退回回放模式再跑。
    打包前这把 key 曾出现 402 余额耗尽，充值后已复验全链路可用；
    若收货方再次遇到 402，先查账户余额而不是怀疑代码。

【这一版和不带 key 版的区别】
只有 .env 一个文件不同，其余逐字节相同。把 .env 改名或删掉就退回
回放模式，全部套件照样全过——这也是「key 失效时不会全线崩」的证明。

拿这份包的人请照这一节重跑一遍再验收。数字对不上，说明包里的东西
和本节写的不一致——先信你跑出来的，别信这段文字。
"""


def _mask(secret: str) -> str:
    """密钥掩码：留前 6 后 4，中间用 * 填。

    长度不足时整段打码——短串掩掉头尾等于没掩。
    """
    s = (secret or "").strip()
    if len(s) < 16:
        return "*" * len(s)
    return f"{s[:6]}{'*' * 8}{s[-4:]}"


def excluded(rel: str) -> str | None:
    """返回剔处理由；返回 None 表示这个文件该进包。

    先按精确目录名，再按前缀。精确在前是因为它的理由写得更具体；
    前缀在后兜住「同一类东西的下一版」——那正是写死名字会漏掉的地方。
    """
    parts = rel.split("/")
    for p in parts[:-1]:
        if p in EXCLUDE_DIR_NAMES:
            return _DIR_WHY[p]
        for pre, why in EXCLUDE_DIR_PREFIXES.items():
            if p.startswith(pre):
                return why
    for pat, why in EXCLUDE_FILES:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(parts[-1], pat):
            return why
    return None


def collect(skip_reports: bool = False, with_env: bool = False):
    """扫出要进包的文件，返回 [(rel_posix, 绝对路径)] 和剔除统计。

    with_env 为真时只多放行根目录的 `.env`，别的排除项一条不动——
    「带 key」不等于「什么都不排除」，数据库、备份、虚拟环境照样不进包。
    """
    keep, drop = [], {}
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT).as_posix()
        if skip_reports and fnmatch.fnmatch(rel, "eval/report_*.json"):
            drop.setdefault("eval/report_*.json（--no-reports）", []).append(rel)
            continue
        if with_env and rel == ".env":
            keep.append((rel, p))
            continue
        why = excluded(rel)
        if why:
            drop.setdefault(why, []).append(rel)
        else:
            keep.append((rel, p))
    return keep, drop


def _suite_count() -> str:
    """从 run_all.py 静态数出回归套件数。

    为什么不用写死的数字：这个数每加一个套件就变，而「打包说明」恰恰是最没人
    想起来同步的地方——它写死过 15，实际早就 20 了，一路带进了三个交付包。
    用 ast 解析而不是 import：import 会执行 run_all 的顶层代码。
    """
    try:
        tree = ast.parse((ROOT / "scripts" / "run_all.py").read_text(encoding="utf-8"))
        n = 0
        for node in tree.body:
            if isinstance(node, ast.Assign) and \
                    getattr(node.targets[0], "id", "") in ("STEPS", "WITH_SERVICE"):
                n += len(ast.literal_eval(node.value))
        return str(n) if n else "若干"
    except (OSError, SyntaxError, ValueError):
        return "若干"       # 数不出来就含糊过去，别在交付说明里编一个


def _corpus_count() -> str:
    """包内条文语料有多少行。零行/读不到时不报数字。"""
    try:
        with (ROOT / "data" / "statutes.jsonl").open(encoding="utf-8") as f:
            n = sum(1 for line in f if line.strip())
        return f"{n} 条条文" if n else "条文语料"
    except OSError:
        return "条文语料"


def _law_count() -> str:
    """语料涉及多少部法律（distinct law_short）。读不到时不报数字。"""
    try:
        laws: set[str] = set()
        with (ROOT / "data" / "statutes.jsonl").open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    laws.add(json.loads(line)["law_short"])
                except (ValueError, KeyError, TypeError):
                    pass
        return str(len(laws)) if laws else "若干"
    except OSError:
        return "若干"


def check_verified(verified: str) -> list[str]:
    """VERIFIED 里写死的那几个数字，必须和算出来的一致。

    同一份 pack_src 里：note_text 的「包里有什么」一节已经用
    `_suite_count()` / `_corpus_count()` 动态化了；可 VERIFIED（「验证过什么」
    一节，收货方照它验收）却把同样的 条数 / 部数 / 套数 写死成字面量。
    哪天语料加了一部法、run_all 加了一个套件，目录结构那行会自动跟，
    VERIFIED 这行不会——于是包里的「验证过什么」和「包里有什么」自己打自己脸。

    只查能客观算出来的三个；「约 120 秒」「跳过 8 条」这种运行相关、无法在
    打包时复算的，不在这里拦。
    """
    problems: list[str] = []
    # 条文数：VERIFIED 写 "2066 条条文"，_corpus_count 也返回 "2066 条条文"
    m = re.search(r"(\d+) 条条文", verified)
    mc = re.search(r"(\d+)", _corpus_count())
    if m and mc and m.group(1) != mc.group(1):
        problems.append(f"VERIFIED 写 {m.group(1)} 条条文，实际语料 {mc.group(1)} 条")
    # 部数
    m2 = re.search(r"(\d+) 部法律", verified)
    rl = _law_count()
    if m2 and rl.isdigit() and m2.group(1) != rl:
        problems.append(f"VERIFIED 写 {m2.group(1)} 部法律，实际语料 {rl} 部")
    # 套数：VERIFIED 写 "22 / 22 通过"
    m3 = re.search(r"(\d+) / \d+ 通过", verified)
    rs = _suite_count()
    if m3 and rs.isdigit() and m3.group(1) != rs:
        problems.append(f"VERIFIED 写 {m3.group(1)} 个套件，实际 run_all 登记 {rs} 个")
    return problems



def _readme_size() -> str:
    try:
        kb = (ROOT / "README.md").stat().st_size / 1024
    except OSError:
        return "项目文档"
    return f"约 {kb / 1024:.1f} MB" if kb >= 1024 else f"约 {kb:.0f} KB"


def note_text(keep, verified: str, with_env: bool = False) -> str:
    """包内的 打包说明.txt。

    with_env 会同时改三处：顶部警示、第 4 条怎么跑、以及「刻意剔除的」里
    原来写着「.env 真实 API key」。只改其中一处，剩下两处就变成了假话——
    说明里写着「没有 key 也能跑回放模式」，而包里躺着能花钱的 key。

    v132 起 with_env 默认 True，所以**默认那版就是带警示的**；--no-env 才走 else。
    """
    n_py = sum(1 for r, _ in keep if r.endswith(".py"))
    total = sum(p.stat().st_size for _, p in keep)
    if with_env:
        banner = """
⚠ 本包含真实 API key（.env）—— 这是刻意的，为了「解压即用、不用配置」
   拿到它的人可以直接花这个 key 的额度。请只在自己人之间流转，
   不要上传到 GitHub / 网盘公开链接 / 任何公开场合。
   换 key：编辑包内 .env 的 DEEPSEEK_API_KEY。
   想不花钱地跑：把 .env 改名（如 .env.off）即可退回回放模式。
   要一版能随便发的：在源码目录执行 pack_src.py --no-env 重新打。
"""
        step4 = """4. 本包**已内置 .env**（含实测可用的 DEEPSEEK_API_KEY，模型 deepseek-flash），
   解压即可用真模型：/api/meta 应返回 llm_enabled=true。
   不需要自己申请 key、也不需要改任何配置。
   九件工具（检索/取条/时效/交叉引用/算钱/查社平/算期限/写文书/追问）
   在在线模式下全部可用；金额与期限都由工具算，不由模型估。
   注意 key 有额度：连续演示几十次问答没问题，跑整套在线评测（86 题）会明显消耗。
   想退回不花钱的回放模式：把 .env 改名或删掉，llm_enabled 变 false，
   完整循环（意图→分解→检索→工具→观察→定性→自检→记忆）一步不少，
   只是决策由规则模板给出。"""
        drop_note = """  .env            已打进包（v132 起默认带；只有 --no-env 才剔除，见上方警示）
  data/*.db*      账号哈希 + 全部会话正文（首次运行自动重建）"""
    else:
        banner = """
注：本包是 `--no-env` 出的**回放版**，不含 key、不联网、不花钱。
    解压即可运行，但 llm_enabled=false，决策由规则模板给出。
    要接真模型：cp .env.example .env，填 DEEPSEEK_API_KEY。
"""
        step4 = """4. 没有 .env，程序进入「回放模式」，llm_enabled=false，
   Agent 的完整循环（意图→分解→检索→工具→观察→定性→自检→记忆）一步不少，
   只是决策由规则模板给出，答案质量不代表模型水平。
   要接真模型：cp .env.example .env，填 DEEPSEEK_API_KEY。"""
        drop_note = """  .env            真实 API key（本包是 --no-env 出的，刻意不含；
                  默认打包会带上，见说明开头）
  data/*.db*      账号哈希 + 全部会话正文（首次运行自动重建）"""
    return f"""law-agent 源码包
================
打包时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
文件数  ：{len(keep)}（其中 Python {n_py} 个）
未压缩  ：{total / 1024 / 1024:.1f} MB
{banner}
【怎么跑起来】
1. Python 3.10+（代码用了 `int | None` 这种写法，3.9 及更早会在
   import 阶段就语法错，看起来像「代码坏了」。两个 bat 会先做版本门禁）
2. pip install -r requirements.txt
   核心只有 pydantic；Web 界面额外要 fastapi + uvicorn
3. Web 界面：双击 启动-网页.bat      命令行：双击 启动-命令行.bat
   （两个 bat 都是纯 ASCII：cmd 按字节偏移解析批处理，中文行会让偏移漂移，
   中文提示放在 notice.txt 里用 type 打印）
{step4}
5. 完整中文说明见「交付说明.txt」。

【包里有什么】
app/       服务端与 Agent 内核（FastAPI）
static/    前端页面（原生 JS，无 CDN，字体用系统字体）
scripts/   脚本：run_all.py（{_suite_count()} 个回归套件入口）、评测辅助、一次性探针
eval/      评测：run_eval.py / recompute.py、4 套题库、历史报告
data/      语料：statutes.jsonl（{_corpus_count()}）+ raw/ 下法条原文；
           cases.jsonl 是 21 件人社部/最高法劳动人事争议典型案例（离线，
           附官方处理结果原文与来源）；
           region_params.json 是三地社平工资（北京/上海/杭州，含 2024、2025
           两个年度，算三倍封顶用；可自行补城市或年度）
README.md  项目文档（{_readme_size()}，含全部设计取舍与踩坑记录）
mcp_config.json  MCP 客户端配置示例（`python -m app.mcp_server`，stdio、只读、不需要 key；
                六个能力，用法见「交付说明.txt」第四节）

答案下方是「值不值得继续追」决策卡（v126）：时间成本 / 经济成本 / 历史
成败案例 / 收益比较 / 「建议上不上诉」，五格都是规则生成、不调模型、
不联网，回放模式下也有；可得金额一律留空，不给胜率，结论按已核算金额
与法定期间直接给（v136 起不再写「条件式」「不替你拍板」）。
旧的程序时间轴、请求事项候选、举证体检、判例原文与显式画像收进
「更多材料」可展开。时效计算按法域分表：劳动争议一年仲裁时效，
其余四内核《民法典》三年诉讼时效。
附录里：
时间轴 8 阶段、举证体检 6 行、相似案例附官方处理结果原文与来源；
请求事项金额一律留空，判例卡不给胜诉率。
v124 起时间轴多一张程序分支流程图，判例卡多一张「结论卡」（直接写
建议上诉 / 不建议上诉 / 先补材料 / 先核时效，依据是已核算金额与法定
期间，只附一句「仅供参考，不构成法律意见。」）；输入框下面另有两个默认
关闭的开关，可为每个案件单独打开「程序时间表」「申请书骨架」，五个法律
内核都能用。v125 起这两个开关的入口搬到对话框上方，和「是否建议开庭」
排成一排功能按钮；示例提问收进输入框左边的「提问参考」下拉，
左侧任务列表在空状态下也一直留着。用法见「交付说明.txt」第四、五、八、九、十节。

【刻意剔除的】
见本脚本 scripts/pack_src.py 里的 EXCLUDE_DIR_NAMES / EXCLUDE_FILES，
每一项都写了理由。重点是这四类：
{drop_note}
  eval/_shot/     浏览器截图（本机数百 MB、上千个文件）
  .venv/          65 MB，绑定开发机路径
剔除项一份都没进包——这是打包脚本的排除表决定的，不是漏了。

{verified}
"""


def desktop_dir() -> Path | None:
    """找「系统自己认为的桌面」，找不到返回 None。

    为什么不能直接 Path.home() / "Desktop"：这台机器的桌面被重定向到了别的盘，
    C:\\Users\\<name>\\Desktop 根本不存在；被 OneDrive 接管时又是第三条路径。
    这两种情况下 `Path.home() / "Desktop"` 都会安静地落回一个错地方——
    包照样打出来，只是没人去那儿找。

    所以先问注册表 Shell Folders（重定向/接管都会写在这里），
    再退到字面的 Desktop，都没有才交给调用方决定怎么兜。
    """
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as k:
                raw, _ = winreg.QueryValueEx(k, "Desktop")
            d = Path(os.path.expandvars(str(raw)))
            if d.is_dir():
                return d
        except OSError:
            pass        # 没有注册表项/权限不足都算「问不到」，接着走下面
    d = Path.home() / "Desktop"
    return d if d.is_dir() else None


# ---------------------------------------------------------------- 敏感形态
# 排除表能挡住 .env 和 *.db，但挡不住「有人把 key 贴进 README」「某次调试
# 把令牌写进脚本」「.env 被改名成 .env.bak 就不在排除表里了」。所以写包之前
# 再按内容扫一遍——排除表看的是文件名，这一道看的是内容。
SECRET_PATTERNS = [
    ("API key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("家目录路径", re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+")),
]
# 家目录路径只警告不拦：源码包的调试记录里出现作者自己机器的路径很常见，
# 拦下来只会逼人删掉有用的记录。key 和私钥是真不能进包。
BLOCKING = {"API key", "私钥"}
TEXT_SUFFIX = {".py", ".md", ".txt", ".html", ".js", ".css",
               ".json", ".jsonl", ".bat", ".cmd", ".example"}
# 按后缀筛会漏掉 `.env`：`Path('.env').suffix` 是空串（它被当成「没有后缀
# 的隐藏文件」），于是这个最该被扫的文件恰好跳过了检查。加一条按文件名认。
# 这不是理论问题——第一次用 --with-env 打的时候，输出正是「未发现 key」。
TEXT_NAMES = {".env", ".env.example", ".gitignore", ".gitattributes"}


def scan_sensitive(keep, extra: list[tuple[str, str]] | None = None,
                   allow_key_files: set[str] | None = None) -> tuple[list, list, list]:
    """扫即将进包的内容。返回 (拦下的, 只提醒的, 按开关放行的)。

    extra 是「不在 keep 里但会被写进包」的东西——目前只有内存里生成的
    「打包说明.txt」。不传它就等于扫了个寂寞：那条路径是 `--verified`
    直接拼进去的，正是最容易夹带东西的地方。

    allow_key_files 只对 `--with-env` 生效，且只放行这些文件里的 key。
    放行不等于不记账：命中的照样回传（第三个返回值），main 会把它打印出来
    ——「我确认要带」和「没人发现它被带走了」是两件事，输出上要能区分。
    其它文件里出现 key 形态仍然拦，否则「开关开着」就成了全包免检。
    """
    blocks, warns, carried = [], [], []
    sources: list[tuple[str, str]] = []
    for rel, p in keep:
        if p.suffix.lower() not in TEXT_SUFFIX and p.name not in TEXT_NAMES:
            continue
        try:
            sources.append((rel, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue        # 读不了就别猜，交给写 zip 那一步去报错
    sources.extend(extra or [])

    for rel, text in sources:
        for label, pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                item = (label, rel, m.group(0)[:48])
                if label in BLOCKING and allow_key_files and rel in allow_key_files:
                    carried.append(item)
                elif label in BLOCKING:
                    blocks.append(item)
                else:
                    warns.append(item)
    return blocks, warns, carried


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-reports", action="store_true")
    # 默认值留空，真正取值在下面按 --with-env 分流：两版包的「验证过什么」
    # 不是同一段话（一版 llm_enabled=false，一版 true）。写死一个默认值，
    # 另一版就会带着错的那段发出去。
    ap.add_argument("--verified", default="")
    ap.add_argument("--allow-secrets", action="store_true",
                    help="内容里出现 key/私钥形态也照打（确认是文档示例时才用）")
    ap.add_argument("--with-env", dest="with_env", action="store_true",
                    help="把 .env（含真实 key）一起打进包（v132 起这是默认值，参数保留为兼容）")
    ap.add_argument("--no-env", dest="with_env", action="store_false",
                    help="不带 .env：解压后是回放模式，llm_enabled=false，不花钱")
    ap.set_defaults(with_env=True)
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        print("需要 Python 3.10+")
        return 2

    keep, drop = collect(args.no_reports, args.with_env)
    if not keep:
        print("没有文件可打")
        return 1
    if args.with_env and not (ROOT / ".env").is_file():
        # 「打了带 key 的包」和「包里其实没 key」是两件事。没有 .env 时
        # 照打会得到一个「文件名带 -with-key、里面却是回放包」的东西，
        # 收货方以为 key 没生效，能查半天。
        print("默认要带 key（v132 起），但工作区没有 .env。")
        print("  要么先配好：cp .env.example .env，填 DEEPSEEK_API_KEY；")
        print("  要么显式加 --no-env，出一版解压即回放、不花钱的包。")
        return 5

    # ---- 分类统计 ----
    by_top = {}
    for rel, p in keep:
        top = rel.split("/")[0] if "/" in rel else "(根目录)"
        a, b = by_top.get(top, (0, 0))
        by_top[top] = (a + 1, b + p.stat().st_size)

    print(f"进包 {len(keep)} 个文件：")
    print(f"  {'目录':<14}{'文件数':>8}{'体积':>12}")
    for top in sorted(by_top, key=lambda k: -by_top[k][1]):
        n, sz = by_top[top]
        print(f"  {top:<14}{n:>8}{sz / 1024 / 1024:>10.1f} MB")
    total = sum(p.stat().st_size for _, p in keep)
    print(f"  {'合计':<14}{len(keep):>8}{total / 1024 / 1024:>10.1f} MB")

    if drop:
        print("\n剔除：")
        for why, files in sorted(drop.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(files):>4} 个  {why}")

    # 敏感内容扫描。dry-run 也扫——想先看看有没有问题，正好是这个模式的用途。
    # 注意 note 只生成一次：它带时间戳，生成两次内容会不同，写进去的就不是
    # 扫描过的那份了。
    verified = args.verified or (VERIFIED_WITH_KEY if args.with_env else VERIFIED)
    note = note_text(keep, verified, args.with_env)
    blocks, warns, carried = scan_sensitive(
        keep, [(f"{NAME}/打包说明.txt", note)],
        allow_key_files={".env"} if args.with_env else None,
    )
    print("\n敏感扫描：", end="")
    if blocks:
        print(f"发现 {len(blocks)} 处本不该进包的内容")
        for label, rel, s in blocks[:5]:
            print(f"  !! {label}  {rel}  ->  {s}")
    else:
        print("未发现 key / 私钥")
    if carried:
        # 放行不等于没看见：这几行是「知道带了、带的是哪几条」的凭据。
        #
        # **只打掩码，不打全文。** 原来这里直接 `-> {s}` 把整条 key 印到终端，
        # 等于把密钥写进终端回滚缓冲、CI 日志、以及（如果截了屏）聊天记录。
        # 「打包时看过一眼」和「把 key 明文留在日志里」是两件事：
        # 前者必要，后者是泄露。掩码保留前后各几位，足够确认打的是哪一把。
        print(f"  ⚠ 按 --with-env 放行 {len(carried)} 处（.env 里的真实 key）")
        for label, rel, s in carried:
            print(f"     {label}  {rel}  ->  {_mask(s)}")
        print("     这个包能直接花 key 的额度，只发给自己人。")
    if warns:
        print(f"  提示：{len(warns)} 处个人家目录路径（只提醒，不拦）")
        for _, rel, s in warns[:3]:
            print(f"     {rel}  ->  {s}")

    if blocks and not args.allow_secrets:
        if args.dry_run:
            print("  （dry-run 不写包，所以只报不拦）")
        else:
            print("\n拒绝打包：上面这些内容不该跟着源码走。")
            print("确认是文档里的假样例，加 --allow-secrets。")
            return 3

    # VERIFIED 里写死的数字和现状对不上，等于包里自己写了句假话。
    # dry-run 只报不拦；真打就拒绝，逼人先更新 pack_src 里的 VERIFIED 常量。
    vprob = check_verified(verified)
    if vprob:
        print("\nVERIFIED 数字与现状不符：")
        for p in vprob:
            print(f"  !! {p}")
        print("  改 scripts/pack_src.py 里的 VERIFIED 常量（让它和现在的语料/套数一致），")
        print("  或确认这本身就是一次真实漂移——先信算出来的，别信那段文字。")
        if not args.dry_run:
            return 4

    if args.dry_run:
        return 0

    # 文件名带 -with-key：两个包（回放 / 内置 key）落在同一个桌面时，
    # 光看体积分不出谁是谁，而发错的那个会把额度送出去。
    tag = "-with-key" if args.with_env else ""
    name = f"{NAME}-src{tag}-{datetime.now():%Y%m%d}.zip"
    if args.out:
        out = Path(args.out)
    else:
        desk = desktop_dir()
        if desk is None:
            # 找不到桌面就退到用户目录，但必须**说出来**。静默换地方存，
            # 用户会以为包没打出来，然后跑第二遍。
            out = Path.home() / name
            print(f"\n⚠ 没有可写的桌面目录，改存用户目录：{Path.home()}")
        else:
            out = desk / name
    out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(f"{NAME}/打包说明.txt", note)
        for rel, p in keep:
            z.write(p, f"{NAME}/{rel}")

    print(f"\n已写出：{out}  ({out.stat().st_size / 1024 / 1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
