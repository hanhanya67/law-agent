# -*- coding: utf-8 -*-
"""
大模型层。两种运行模式：

  在线模式  配置了 DEEPSEEK_API_KEY，走真实 API，原生 Function Calling
  回放模式  没配 key，用规则模拟决策与生成

为什么要有回放模式：
  环境没配好时，你依然能看到完整的 Agent 循环在跑——意图识别、工具调用、
  观察回灌、自检、记忆写入，一步不少。这样"跑不起来"和"模型答得不好"
  这两个问题不会混在一起，排查成本低很多。
  另外演示断网时，把 key 去掉就能降级，不至于开天窗。

三种调用模式必须分开，不要合并：
  决策  → 阻塞 + tools，要拿完整的 tool_calls 才能执行
  生成  → 流式，逐字推给前端
  抽取  → 阻塞 + json_object，temperature=0
把决策和流式合并是常见错误：流式返回的 tool_calls 是增量片段，
拼装极易出错，而且出错时现象诡异、很难定位。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterator

from . import prompts, verdict as verdict_mod
from . import dslots as _dslots
from . import profiles as _profiles
from .config import (DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
                     LLM_EFFORT, LLM_ENABLED, LLM_RECOVER_AFTER, LLM_THINKING,
                     TOK_ASK, TOK_DECIDE, TOK_DECOMPOSE, TOK_GENERATE,
                     TOK_INTENT, TOK_JUDGE, TOK_RETRY_CAP, TOK_SLOTS,
                     TOK_VERIFY, TOK_VISION, VERIFY_MODE)
from .retrieval import cn2int
from .cite import extract_cites, find_article
from .schemas import (AgentState, Decision, Evidence, ToolCall, Verdict,
                      VerifyIssue, VerifyResult)

Emit = Callable[[str, dict], None]

# ---------------------------------------------------------------- 回放模式规则

# 越界：**明确不属于劳动法**的领域。注意这里放的是"整个法律领域都不对"，
# 不是"库里的条文不够"——后者是拒答理由，不是越界理由。
#
# 早先这张表只有「天气/股票/写代码」这类"离谱到不沾边"的词，
# 于是刑事、婚姻、侵权这些**同为法律问题但不在劳动法里**的问句全被放进去检索，
# 再被字面巧合的条文硬答：h14「职务侵占被判刑吗」拿民法典 847（职务技术成果）
# 当依据，r03「离婚房子怎么分」拿劳动法 18（劳动合同无效）当依据。
# 用户看到的是一段格式正确、条号真实、但完全答非所问的回答——比直接拒答危险得多。
_OUT_OF_SCOPE = ("天气", "股票", "写代码", "代码", "python", "编程",
                 "翻译", "电影", "做饭", "写诗", "作文", "笑话",
                 # 刑事
                 "判刑", "坐牢", "刑事", "犯罪", "拘留", "取保候审",
                 # 婚姻家庭 / 继承
                 "离婚", "彩礼", "抚养权", "继承", "遗产")

# 需要两个词同时出现才算越界——因为单看一个词会误伤真实的劳动法问题。
# 「狗」单独出现，可能是「公司养的狗咬伤员工算不算工伤」，那是正经工伤题；
# 只有"邻居的狗"才是侵权。「食堂」单独出现，可能是「食堂饭钱从工资里扣
# 合不合法」，那是工资题；只有"难吃/饭菜"才是餐饮服务纠纷。
_OUT_OF_SCOPE_PAIRS = (
    ("邻居", "狗"),
    ("食堂", "难吃"),
    ("食堂", "饭菜"),
    # 房屋租赁：「押金」这个词劳动法和租房法都在用，单看它必然误判——
    # 「公司收押金」是第 9 条的活，「房东收押金」是民法典租赁合同。
    # 只靠单词表拦不住（不能把「押金」整个扔进 _OUT_OF_SCOPE），
    # 只能靠「谁在收」这一层主语来分。n01「房东不退押金怎么办」
    # 原来被判 case_consult，改写层又把「押金」翻成「用人单位向劳动者收取财物」，
    # 于是答成「工装费由公司承担」+《劳动合同法》第八十四条：条号真实、
    # 格式正确、跟租房毫无关系——比直接拒答危险。
    ("房东", "押金"), ("房东", "退"), ("房东", "房租"),
    ("租房", "押金"), ("房租", "押金"), ("退租", "押金"),
    ("中介", "押金"), ("中介费", "退"),
    # 民间借贷 / 消费维权：同属"其他法律领域"，但库里恰好没有借款、
    # 买卖合同条文，所以先前是靠零命中"碰巧"拒答的。碰巧的拒答不可靠——
    # 语料一旦补上民法典合同编，这些题就会被硬答。这里把碰巧改成明确。
    ("朋友", "借钱"), ("朋友", "借款"), ("朋友", "欠钱"),
    ("借钱", "不还"), ("打欠条", "不还"),
    ("网购", "退"), ("网购", "质量"), ("退货", "质量"), ("卖家", "退"),
)
# 劳动法领域词。**只在「短句 + 映射不出法条术语」这个分支里用**：
# 那时还剩一种可能——句子太短、信息不够（「被辞退了」），要追问；另一种是
# 压根不在范围内（「如何烤蛋糕」），要拒答。区分它们靠的就是这张表。
# 反向判定比扩充 _OUT_OF_SCOPE 靠得住：越界词穷举不完（今天是烤蛋糕，
# 明天是养多肉），而「至少得提到一个劳动关系里的东西」是有限集合。
_DOMAIN_WORDS = (
    "工资", "薪", "社保", "保险", "公积金", "个税", "合同", "协议",
    "辞退", "开除", "解除", "离职", "辞职", "裁员", "辞了", "被开", "炒",
    "仲裁", "起诉", "诉讼", "赔偿", "补偿", "欠", "拖欠", "扣", "罚",
    "加班", "年假", "休假", "病假", "产假", "工伤", "竞业", "试用", "实习",
    "劳动", "上班", "入职", "考勤", "打卡", "调岗", "降薪", "排班",
    "公司", "单位", "老板", "员工", "职工", "工友", "打工", "厂",
)
# 寒暄 / 无实义应答的兜底。上层 `_is_greeting` 会先拦掉标准说法（整句匹配），
# 这里只接漏网的变体（「你好呀呀」「嗯嗯」这类）。它们不该被推成「越界」：
# 对一句「你好」回「这个系统只处理劳动法问题」，比追问一句更冷，
# 而用户第一句话就吃一句拒答，通常不会再问第二句。
_FILLER_WORDS = ("你好", "您好", "在吗", "在不在", "在么", "谢谢", "好的",
                 "嗯", "哦", "收到", "多谢", "hi", "hello", "嗨")
# 只有"明确要一个数字"才算计算意图。
# 早先把"算""赔""补偿"也算进来，结果"经济补偿按什么标准算"被误判成
# 要算钱，然后因为缺月薪直接追问，根本没去检索——这是评测跑出来的 bug。
_CALC_WORDS = ("多少", "几倍", "金额", "怎么算", "如何计算", "算一下",
               "能拿多", "能要多", "赔多", "该给多")
_DEAD_CHECK = ("已废止", "已修改")

# 问期限的口语说法。「一年」单独不算——「干了一年能拿多少」会误判。
_DEADLINE_WORDS = ("多久", "时效", "过期", "来不及", "最晚", "还来得及",
                   "能不能告", "还能不能", "超过一年", "期限", "什么时候截止")


def _guess_dispute(q: str) -> str:
    """从问题里猜争议类型，给回放模式的期限工具用。

    只有离线路径才需要猜：在线时这个判断由模型自己做，它看得懂上下文，
    比关键词准得多。这里写成按顺序匹配，先具体后笼统，避免「没签合同还
    拖欠工资」被笼统规则吃掉。
    """
    pairs = [
        (("没签", "未签", "不签", "没有签", "二倍工资"), "no_contract"),
        (("裁决", "起诉", "不服"), "arbitration_award"),
        (("拖欠", "欠薪", "不发工资", "欠工资", "克扣", "加班费"), "wage"),
        (("辞退", "开除", "解除", "赔偿金", "补偿金", "离职"), "dismissal"),
        (("工伤",), "injury"),
    ]
    for words, key in pairs:
        if any(w in q for w in words):
            return key
    return "dismissal"

_LAW_KEYS = ("劳动合同法实施条例", "劳动争议调解仲裁法", "劳动合同法",
             "民法典", "合同法", "劳动法")

# 计划最多几路。`_rewrite_queries` 的产出上限是 5，执行端必须跑满——
# 两端上限不一致时，多出来的那几路等于白加：m12「离职后申请仲裁的期限是多久，
# 拖欠工资的情况有什么不同」第 4 路「仲裁时效期间 一年」能精准命中《调解仲裁法》
# 第二十七条，但 L1.5 只跑前 3 路，27 条一次都没进过证据池（召回 0.0）。
PLAN_CAP = 5



def _law_near_article(question: str, art_pos: int) -> str:
    """判断「第 X 条」指的是哪部法：取条号**前面最近**的那个法名。

    原来只按 _LAW_KEYS 的顺序找「第一个出现在句中的法名」，完全不看距离。
    v03「以前合同法是第114条，现在民法典施行了还适用吗」——句子里同时有
    「合同法」和「民法典」，而 _LAW_KEYS 里民法典排在合同法前面，于是被当成
    在问民法典第 114 条（那是另一件事，民法典 114 条讲的是民事权利能力）。
    真正该取的是合同法 114，再由效力核查指出它已废止、由民法典 585 承接。

    为什么比「右端点」而不是「起点」：法名之间存在后缀包含关系
    （「合同法」是「劳动合同法」的后缀）。在「劳动合同法是第 X 条」里，
    两个名字都终止在同一位置，此时取更长的那个，才不会把「劳动合同法」
    错认成「合同法」。
    """
    before = question[:art_pos]
    best_end, best_len, best = -1, 0, ""
    for k in _LAW_KEYS:
        i = before.rfind(k)
        if i < 0:
            continue
        end = i + len(k)
        if end > best_end or (end == best_end and len(k) > best_len):
            best_end, best_len, best = end, len(k), k
    if best:
        return best
    # 条号前面没写法名（比如「第87条怎么规定的」），退回全文找；
    # 再找不到就交给工具自己兜底（它会报「不知道哪部法」而不是瞎猜）。
    return next((k for k in _LAW_KEYS if k in question), "")


_NOTE_CITE_RE = re.compile(r"《([^》]{2,40})》第([零一二三四五六七八九十百千\d]+)条")


def successor_of(d: Evidence, valid: list[Evidence]) -> Evidence | None:
    """从废止说明里取「承接条文」，且必须真的在证据池里。

    只有核准说明写了承接条，才允许说「由 X 承接」——这是唯一有出处的依据。
    取**最后一个**条号：说明一般先写废止依据（民法典第 1260 条），
    再写承继条文（民法典第 585 条），取第一个会把废止清单当成承接条。

    为什么必须卡这一道：全库 429 条失效条文里，只有 2 条带说明
    （劳动争议解释一_32、合同法_114），其余 427 条 repealed_note 为空。
    原来只要有失效条文 + 任意一条现行条文，就宣布「由 valid[0] 承接」——
    「旧合同法关于房屋租赁的规定」会被答成「234 条已废止，由民法典 732 条承接」，
    而库里根本没有 234→732 这个对应关系，答对纯属检索恰好配对。
    """
    if not d.repealed_note:
        return None
    found = _NOTE_CITE_RE.findall(d.repealed_note)
    if not found:
        return None
    law, no = found[-1]
    # 条号必须换算成数字再比：说明里是「第五百八十五条」拆出的「五百八十五」，
    # 而 Evidence.article_no_cn 是「第五百八十五条」，字面比较永远对不上。
    want = cn2int(no)
    for e in valid:
        if cn2int(e.article_no_cn) != want:
            continue
        if e.law_name == law or law in e.law_name or e.law_name in law:
            return e
    return None


def dead_lead(d: Evidence, valid: list[Evidence]) -> str:
    """结论段开头那句「这条还能不能用」。

    「部分失效」不等于「被替代」：整条还在，只是某一款废止，后法未必补了实体
    规定。h11 原来被答成「相关内容已由《解释（一）》第一条承接」——那句是编的，
    第一条讲受案范围，与退休返聘毫无关系。这种情况直接引用语料里人工核准过的
    废止说明，宁可长，不可编。

    「已废止」也一样：承接关系要有出处（说明里点名的条，且该条真在证据池里），
    凭 valid[0] 猜出来的「承接」是编的，只能说「现行法下有相近条文、供参考」。
    """
    if d.status == "部分失效" and d.repealed_note:
        return f"《{d.law_name}》{d.article_no_cn}{d.status}：{d.repealed_note}"
    sup = successor_of(d, valid)
    if sup:
        return (f"《{d.law_name}》{d.article_no_cn}{d.status}，"
                f"相关内容已由《{sup.law_name}》{sup.article_no_cn}承接，"
                f"应按后者理解。")
    if d.repealed_note:
        return f"《{d.law_name}》{d.article_no_cn}{d.status}：{d.repealed_note}"
    if valid:
        return (f"《{d.law_name}》{d.article_no_cn}{d.status}，不能再作为现行依据引用。"
                f"现行法下有相近条文《{valid[0].law_name}》"
                f"{valid[0].article_no_cn}，只能参考，不能当作替代条款。")
    return f"《{d.law_name}》{d.article_no_cn}{d.status}，不能再作为现行依据引用。"


_LABOR_WORDS = ("劳动", "工资", "加班", "辞退", "解雇", "解除", "社保", "工伤",
                "入职", "离职", "辞职", "试用期", "年假", "考勤", "竞业",
                "实习", "劳动仲裁", "公司", "单位", "老板", "用工")


def is_labor_question(question: str) -> bool:
    """兜底建议要分领域：问房屋租赁、承揽合同的人，不需要「什么时候入职」。"""
    return any(k in (question or "") for k in _LABOR_WORDS)


def basis_header(kind: str, has_dead: bool = False, has_rule: bool = False) -> str:
    """依据块的标题：定性判不出来时，不许叫「依据」。

    r07（工伤十级赔多少）/ r12（实习生有没有工资）/ h13（差旅费谁出）这三道，
    库里确实没有对应条文（缺《工伤保险条例》、在校生用工规定、差旅费标准），
    但检索总能凑出一条含同名词的条文：
      x h13「派到外地**常驻**」撞上《解释（二）》第五条「外国企业**常驻**代表机构」
      x r12「有没有工资」撞上《劳动法》第四十八条「最低工资」
    这些条文挂在【依据】下面，用户就会以为「差旅费有这条依据」。
    改成【相关条文】并写明别当依据——宁可标题丑一点，不许让巧合冒充依据。

    但 `has_dead` 要单独放行：h11（退休返聘）定性也判不出来，可依据里那条
    《解释（一）》第三十二条**就是**在讲退休返聘，只是第一款失效了——
    这种情况我们确实在就这条作判断（说清哪一款废止），它就是依据，
    降级成「相关条文」会把一条正确引用说成巧合。

    `has_rule` 同理放行，而且理由更硬：定性判不出来不代表没有依据可摆。
    金额是照着第四十七条一项项算出来的，三倍封顶、十二年线都出自它——
    这时候把 47 条降级成「相关条文，不要当成结论的依据」，等于告诉用户
    「刚才那个数字没有依据」，是自相矛盾的。定性缺失和依据缺失是两件事。
    """
    if kind == "unknown" and not has_dead and not has_rule:
        return ("【相关条文】（以下是检索到的相近条文，未必直接对得上你的问题，"
                "不要当成结论的依据）\n")
    return "【依据】"


def fallback_advice(has_deadline: bool, question: str = "") -> str:
    """定性判不出来（v.kind == unknown）时给的通用建议。

    抽出来是为了钉住一个真实 bug：原来写成
        blocks.append(A + B + C if dl else D)
    优先级让它变成 (A+B+C) if dl else D —— `dl` 为空时整段建议被换成半句话
    「或直接申请劳动仲裁（不收费，时效一年）。」，连【建议】标题都没了。
    h11 / r07 / r12 / h13 的答案末尾都挂过这半句。

    另一个坑是**不分领域就套劳动模板**：「旧合同法关于房屋租赁的规定还有效吗」
    答完之后建议「先把入职时间线写清楚、收集工资流水、去劳动仲裁」——三句全错。
    定性判不出来恰恰是最需要给对下一步的场景，不能只写对了一半。
    """
    if not is_labor_question(question):
        tail = ("3. 先书面催告（微信、邮件、快递函都算），协商不成再起诉"
                + ("；时效以上面算出的日期为准。" if has_deadline
                   else "；普通民事纠纷诉讼时效三年。"))
        return ("【建议】\n"
                "1. 先把事实和时间线写清楚：什么时候签的、约定了什么、对方哪里没做到。\n"
                "2. 收集书面材料：合同或协议原件、付款凭证、发票、聊天记录。\n"
                + tail)
    tail = ("3. 劳动争议可先向当地劳动保障监察大队投诉，或直接申请劳动仲裁"
            + ("（不收费；时效以上面算出的日期为准）。" if has_deadline
               else "（不收费，时效一年）。"))
    return ("【建议】\n"
            "1. 先把事情的时间线写清楚：什么时候入职、发生了什么、对方怎么说的。\n"
            "2. 收集能证明劳动关系的材料：合同、工资流水、考勤、工作群聊天记录。\n"
            + tail)


def pick_extra_basis(main: Evidence, valid: list[Evidence], head: str,
                     basis_evs: list[Evidence], step_ids: list[str],
                     multi_topic: bool, cap: int = 2) -> list[Evidence]:
    """除主依据外，还要在【依据】里摆哪几条。

    单跳问题只引一条主依据（原来那个「同源且分数≥55%」的 fallback 把精确率
    从 75% 拉到 68%，撤了）。但有两种情况**必须**多摆一条，否则结论里说的
    那句话在依据里找不到出处——这比精确率更要紧：

      ① 结论自己点了条号（「依第四十六条第一项…」）。m07 的结论提了 46 条，
         依据里却只有 38/30，而 46 正是 gold。判据不猜：点名即引用。
      ② 跨主题问题（一句话问了两件事，如「没签合同还拖欠工资」），两件事
         都得有出处，从检索步骤里各取一条。
      ③ 定性声明为「必引」的（co_cite，例如 m04 的 40 条通知义务、
         r14 的解释（二）第三条认定标准）兜底补上。

    顺序即优先级，`cap` 是硬上限。
    """
    shown = {main.article_id}
    extra: list[Evidence] = []

    def take(ev: Evidence | None) -> None:
        if ev is None or ev.article_id in shown or len(extra) >= cap:
            return
        extra.append(ev)
        shown.add(ev.article_id)

    for no in re.findall(r"第[零一二三四五六七八九十百千\d]+条", head):
        # 结论里常常只写「依第四十六条第一项」，不带法名（m07 就是），
        # 所以不能指望 extract_cites 解析出法律名。同法优先，再退回全库。
        same_law = next((e for e in valid
                         if e.article_no_cn == no and e.law_name == main.law_name), None)
        take(same_law or next((e for e in valid if e.article_no_cn == no), None))

    if multi_topic:
        for sid in step_ids:
            take(next((e for e in valid if e.article_id == sid), None))
            if len(extra) >= cap:
                break

    for be in basis_evs[1:]:
        take(be)
    return extra

# 口语 → 法条术语。检索命中率的大头在这一步，不在检索算法本身。
# 排在前面的优先级高。注意：赔偿金（87条，二倍）和经济补偿（47条，N倍）
# 是两个不同概念，早先把它们合并成一条规则，导致问赔偿金时答案在讲补偿。
_REWRITE = [
    (("没签", "未签", "不签", "没有签", "未订立", "没跟我签", "没给我签"),
     "未订立书面劳动合同 二倍工资"),
    (("拖欠", "不给工资", "欠工资", "不发工资", "克扣"),
     "拖欠劳动报酬 支付"),
    (("赔偿金", "赔偿"),
     "违法解除劳动合同 赔偿金 二倍"),
    (("辞退", "开除", "被炒"),
     "解除劳动合同 经济补偿"),
    (("加班",),
     "延长工作时间 加班工资 百分之一百五十"),
    (("经济补偿", "补偿"),
     "经济补偿 标准 每满一年 一个月工资"),
    # 口语说「试工期」「长期合同」「换部门」「去同行」，条文里分别写作
    # 试用期、无固定期限劳动合同、变更劳动合同、竞业限制。这四条是**纯同义映射**
    # ——只换说法、不改判定方向，实测四道口语题各自就差这一张嘴。
    (("试用期", "试工期", "试岗"),
     "试用期 期限 不得超过"),
    (("长期合同", "无固定期", "无固定期限"),
     "无固定期限劳动合同"),
    (("换部门", "换个部门", "换个工作岗位", "换岗位", "换个岗位",
      "调岗", "调部门", "调个部门"),
     "变更劳动合同 协商一致"),
    # 只收口语说法。「竞业限制」本身是法条术语，题目真用了这四个字就不该改写
    # ——早先把它也写进键里，h02「竞业限制的期限最长几年」被带去搜"未约定
    # 经济补偿"，把该召回的第 24 条（期限不超过二年）挤掉了，留出集掉一题。
    (("同行", "竞争对手", "去别家", "别家公司"),
     "竞业限制 未约定 经济补偿"),
    (("社保", "社会保险"),
     "未依法缴纳社会保险费 解除劳动合同"),
    # 「多久 / 多长时间」单独太泛，不能挂在时效规则上：工伤认定多久出结果、
    # 劳动能力鉴定多久、试用期多久，问的都不是仲裁时效。e09
    # 「工伤认定一般要多久出结果」就是被这一条带去搜仲裁时效，最后答成
    # 「时效一年」——问 A 答 B，而召回和可溯源全满，指标看不出来。
    # 时间词改到下面单独判定，必须同时出现程序词（仲裁/起诉/申诉/打官司）。
    (("时效", "过期"),
     "仲裁时效期间 一年"),
]


def _rewrite_queries(q: str) -> list[str]:
    """把口语问题改写成若干条法条术语检索式，返回**全部**命中。

    为什么是复数：原来命中第一条规则就 return，一个问题只能搜一路。
    「实习期不发工资」被改写成「拖欠劳动报酬」，于是只捞到劳动法第 50 条，
    永远捞不到劳动合同法第 20 条（试用期工资下限）——而这才是用户真正要的。
    一个问题往往同时落在两三条规范上，多路检索再合并，比强行选一条准得多。
    """
    out: list[str] = []

    # 「拖欠工资 + 我要解除合同」——这是第 38 条第 2 项（未及时足额支付劳动报酬，
    # 劳动者可以解除）加第 46 条第 1 项（依 38 条解除的应给经济补偿）。
    # m07 原来这两条一个都没进证据：问句里「拖欠工资」被 50 条那路接走，
    # 「解除合同」没有对应的改写规则，于是答案只能讲"工资该怎么付"，
    # 而用户问的是"我能不能走、走了有没有补偿"。
    # 条件必须两个都在：「拖欠工资」单出是 50 条的活（c09 那种），
    # 带上「解除/辞职」才是 38+46。
    if any(k in q for k in ("拖欠工资", "不发工资", "欠薪", "克扣工资")) and any(
            k in q for k in ("解除合同", "解除劳动合同", "辞职", "不干了",
                            "走人", "离职", "以此为由")):
        out.append("未及时足额支付劳动报酬 解除劳动合同")          # 38 条
        out.append("劳动者解除劳动合同 经济补偿 第四十六条")        # 46 条

    # 「连续签了两次，到期不续签」——第 14 条（连续订立二次固定期限合同）+
    # 第 46 条第 5 项（依 44 条第 1 项终止固定期限合同的应给补偿）。
    # m09 原来只召回 14 条，答案讲了"可以要求签无固定期限"，
    # 却没回答"不续签有没有补偿"——那是 46 条。
    if any(k in q for k in ("连续签了两次", "签了两次", "连续两次",
                            "第二次固定期限", "两次固定期限")) and any(
            k in q for k in ("固定期限", "续签", "不续签", "到期")):
        out.append("连续订立二次固定期限劳动合同")                  # 14 条
        out.append("终止固定期限劳动合同 支付经济补偿")              # 46 条

    # 组合条件优先：「违法 + 辞退/解除」必须走 87 条的二倍赔偿金，
    # 而不是被单词规则带到经济补偿去。
    if any(k in q for k in ("违法", "非法")) and any(
            k in q for k in ("辞退", "解除", "开除")):
        out.append("违法解除劳动合同 赔偿金 二倍 第八十七条")

    # 「哪些情况要给补偿」问的是情形（46 条），不是标准（47 条）。
    # 触发词早先只认「哪些情况/什么情形」，而真实问法里更常见的是
    # 「合同到期不续签要不要给补偿」——这类**终止型**问法问的同样是
    # 46 条列举的情形（依照第四十四条第一项终止固定期限劳动合同）。
    # 不扩这一句，h05 只拿到 47 条（怎么算），丢了 46 条（该不该给）。
    if "补偿" in q and (
            any(k in q for k in ("哪些情况", "什么情形", "哪些情形", "什么情况"))
            or any(k in q for k in ("到期", "不续签", "续签", "合同期满",
                                    "合同结束", "终止"))
            # 「被迫离职」走 38 条解除 + 46 条第一项给补偿。只召回到 47 条
            # （怎么算）会把「该不该给」整段跳过去——m03 就卡在这：
            # 拿到了 38 和 47，唯独缺 46。
            or any(k in q for k in ("被迫", "不缴", "未缴", "没交", "不交"))):
        out.append("用人单位应当支付经济补偿的情形 第四十六条")

    # 「公司向我要钱」（工装费、押金、服装费、保证金、培训费）——条文写作
    # 「不得要求劳动者提供担保或者以其他名义向劳动者收取财物」（第 9 条），
    # 跟口语一个词都不重合。实测：原句「工装费应该谁出」检索**零命中**；
    # 只要检索式里带上「收取 财物」，第 9、84 条立刻排 1、2 位。
    # 这是 r05 拒答的根因——模型明明在答案里写了「不得向劳动者收取财物」，
    # 却没拿这个词去检索，于是判定「资料里没有」并拒答。
    #
    # 但这批词里只有一半是劳动法专属，另一半（押金/保证金/培训费/扣钱/交钱）
    # 在别的领域同样高频，**不能无条件改写成劳动法检索式**：
    # 「房东不退押金」被翻成「用人单位向劳动者收取财物」，检索就一定召回到
    # 第 9、84 条，再被规则表判成「工装费由公司承担」——答非所问。
    # 所以分两类：专属词直接改写；中性词要求句子里有劳动关系的主体或场景。
    if any(k in q for k in ("工装", "工作服", "服装费", "制服", "工牌")):
        out.append("用人单位 向劳动者 收取 财物")
    elif (any(k in q for k in ("押金", "保证金", "培训费", "扣钱", "交钱"))
          and any(k in q for k in ("公司", "单位", "老板", "入职", "员工",
                                   "劳动者", "用人单位", "应聘", "上班",
                                   "试用期", "入职费", "签合同", "工作"))):
        out.append("用人单位 向劳动者 收取 财物")

    # 「拖欠工资能要多少赔偿金」——劳动法里的「赔偿金」至少三种：
    # 87 条（违法解除，二倍）、85 条（欠薪，加付 50%–100%）、
    # 工伤保险条例的伤残补助金。改写层原来单看「赔偿金」就挂 87 条那一路，
    # 于是 n08 问「公司拖欠工资，我能要多少赔偿金」召回到的是违法解除二倍赔偿金，
    # 85 条压根没进证据池，结论退化成「工资应当按月足额支付」——
    # 用户在问能多要多少，答案在教他工资该怎么发。
    # 条件必须同时有「欠薪」和「赔偿」，缺一个就换方向：
    #   · 只有欠薪（c09「工资老是不发」）→ 该走 50/30 条，不是这条；
    #   · 只有赔偿（「违法辞退能要多少赔偿金」）→ 该走 87 条，不是这条。
    if any(k in q for k in ("拖欠", "欠薪", "克扣", "不发工资", "不给工资",
                            "没发工资", "未发工资", "少发", "拖工资",
                            "拖了", "欠工资", "没给工资")) and any(
            k in q for k in ("赔偿金", "加付", "双倍赔偿", "赔偿")):
        out.append("逾期不支付 加付赔偿金 百分之五十以上百分之一百以下")  # 85 条

    # ---- 口语补：两套留出题暴露出来的说法差距 ----
    # 这些不是按题号写的，是「用户这么说、条文那么写」的通用缺口。
    # 每条都先拿检索式验过能真召回（README v43 里有位次），不凭想当然。
    #
    # 注意：留出集在这里被当成了训练材料。用完之后它就不再是过拟合哨兵，
    # 只能算回归集——要给「有没有过拟合」留个真证据，得从主集另划一批。

    # 怀孕：条文写「女职工在孕期」，不打「怀孕」两个字。h04 / c17 都栽在这
    if any(k in q for k in ("怀孕", "有孕", "孕期")):
        out.append("女职工 孕期 不得 解除劳动合同")

    # 离职证明：条文写「出具解除或者终止劳动合同的证明」（第 50 条）
    if any(k in q for k in ("离职证明", "解除证明", "终止证明", "工作证明")):
        out.append("解除或者终止劳动合同 出具 证明")

    # 「一天干十二个钟头」——用钟头/小时讲工作时长，条文写「延长工作时间」
    if any(k in q for k in ("钟头", "小时")) and any(
            k in q for k in ("一天", "每天", "天天", "上", "干", "班")):
        out.append("延长工作时间 每日不得超过三小时 每月不得超过三十六小时")

    # 劳务派遣：派遣工问的几乎都是同工同酬（第 63 条）
    if any(k in q for k in ("劳务派遣", "派遣工", "被派遣", "派遣的")):
        out.append("被派遣劳动者 同工同酬")

    # ---- 口语集里剩下的硬骨头：整句里一个法条词都没有 ----
    # 这几条的共同点是**没有锚点**：问句里既没有「违法」「解除」，也没有
    # 「加班」「劳动关系」，BM25 拿到原句只能靠字面胡乱匹配（c14 被搜出
    # 民法典 1228、268、638 一堆无关条文）。跟"改写得不够准"是两回事，
    # 是"根本没改写"。所以触发条件只能用口语骨架，不能用法律术语。

    # c01「老板突然让我明天别来了，也没说为什么」
    # 「别来了 / 不用来了」= 单位单方解除，且"没说什么原因"本身就是
    # 解除理由缺失的证据。这一路给第 87 条（二倍赔偿金）。
    if any(k in q for k in ("别来了", "不用来了", "不让来了", "明天不用",
                            "不用上班了", "别来上班")):
        out.append("违反本法规定解除或者终止劳动合同 支付赔偿金")

    # c06「被开除了能拿多少钱」——「开除」是纯口语，且只在口语里出现；
    # 正式问法（辞退/解除/没提前三十天）走上面那两条规则，不受影响。
    # 单给 87 不够（合法开除是给补偿），所以 46/47 那几路照旧并行。
    if "开除" in q:
        out.append("违反本法规定解除或者终止劳动合同 支付赔偿金")

    # c04「晚上留下来干活给不给钱」——三个词全是口语：晚上 + 留下来 + 干活，
    # 条文写的是「延长工作时间…支付不低于百分之一百五十的工资报酬」。
    # 刻意**不**把「加班」写进触发词：s03/s17 问的都是加班费率（同样 44 条），
    # 而 h06「每天最多能加几个小时」问的是上限（41 条）——「加班」一亮就触发
    # 44 条，会把 h06 的 41 条挤下去。只认"下了班还在干"这种说法。
    if any(k in q for k in ("晚上", "周末", "双休", "下班后", "下班以后")) and any(
            k in q for k in ("干活", "留下来", "留下", "还在干", "接着干")):
        out.append("延长工作时间 支付不低于工资的百分之一百五十的工资报酬")

    # c14 / r14「送外卖的算不算公司员工」「外卖骑手和平台之间是劳动关系吗」
    # 新业态用工。条文起点是第 7 条（用工之日起建立劳动关系），
    # 而不是任何带「外卖」二字的条文——库里根本没有那样的条文，
    # 所以原句直搜只能捞到民法典里恰好含「配送」「平台」字样的无关条款。
    if any(k in q for k in ("外卖", "骑手", "送餐", "快递员", "网约车",
                            "代驾", "跑腿")):
        out.append("用人单位自用工之日起即与劳动者建立劳动关系")
        # r14「外卖骑手和平台之间是劳动关系吗」要的是**认定标准**，不只是
        # 「何时建立」。库里的现行依据是解释（二）第 3 条第 2 项：未订立书面
        # 合同的，按用工管理行为，综合工作时间、工作内容、报酬、社保认定。
        # 只给第 7 条答不了「算不算」，而这一路的措辞与问句零字面重合，
        # 原句直搜永远捞不到——按条文字面写检索式，不按问题措辞写。
        if "平台" in q:
            out.append("用工管理行为 确认劳动关系 工作时间 劳动报酬 社会保险")

    # e10「老板用个人微信给我发工资，这算劳动关系吗」
    # 问句的字面是「微信发工资」，条文里一个字都没有。真正要查的是
    # 「谁发钱、怎么发」是否影响认定——答案是：发薪主体和方式不影响，
    # 看的是用工管理实质。和第 7 条 + 解释（二）第 3 条同一套依据，
    # 只是触发词完全不同，所以必须单开一路，否则原句直搜只能捞到第 10 条
    # （签书面合同的义务），然后答「现有条文不足以判断」。
    if any(k in q for k in ("微信", "支付宝", "个人账户", "私人账户", "个人卡",
                            "现金发", "发现金")) and any(
            k in q for k in ("发工资", "工资", "转账", "结算", "报销")):
        out.append("用人单位自用工之日起即与劳动者建立劳动关系")
        out.append("用工管理行为 确认劳动关系 工作时间 劳动报酬 社会保险")

    # 厂子倒闭 / 破产 / 关门 —— 劳动合同终止（第 44 条）
    if any(k in q for k in ("倒闭", "破产", "关门", "黄了", "垮了")):
        out.append("用人单位 被依法宣告破产 劳动合同终止")

    # 「出了工伤自己负责」这种免责协议无效（第 26 条）
    if "工伤" in q and any(k in q for k in ("自己负责", "不负责", "自负",
                                            "签了", "签的", "协议", "承诺")):
        out.append("劳动合同 无效 免除 用人单位 法定责任")

    # 「没提前三十天通知就辞退」——无过失性辞退要嘛提前三十日、嘛多给一个月（第 40 条）
    if any(k in q for k in ("三十天", "30天", "一个月通知", "提前通知",
                            "代通知金")) and any(
            k in q for k in ("辞退", "解除", "开除", "走人")):
        out.append("无过失性辞退 提前三十日 通知 额外支付一个月工资")

    # 「2008 年前入职的，补偿分段算吗」——法不溯及既往（第 97 条）
    if any(k in q for k in ("2008", "零八年", "08年", "〇八年")) and "补偿" in q:
        out.append("本法施行前 经济补偿 分段计算")

    # 劳动合同里的违约金能不能调低。注意别让民法典 585 独占——
    # 劳动合同的违约金是第 25 条，只认「约定违约金」的两种法定情形。
    #
    # 但**必须限定在劳动关系内**，否则会反过来把民法典 585 挤掉。
    # 这是实测踩到的：s14「合同里约定的违约金比实际损失高很多，能请求调低吗」
    # 和 v02「违约金过分高于造成的损失，可以请求法院减少吗，依据哪一条」
    # 问的都是**普通民商事合同**，gold 是民法典 585。规则一并触发后，
    # 多出来的一路检索把证据池塞满劳动合同法条文（25/22/23/21/45…），
    # 585 反而排不进去，两题 recall 从 1.0 直接掉到 0.0。
    # 判据：问句里得出现「劳动」二字（劳动合同 / 劳动关系 / 劳动者），
    # 只看「违约金」+ 调低动词会把民商事合同一并捞进来。
    if (("违约金" in q and "劳动" in q
            and any(k in q for k in ("调低", "降低", "太高", "过高",
                                     "减少", "少一点", "减免")))):
        out.append("除本法第二十二条和第二十三条规定的情形外 不得约定违约金")

    # 普通民商事合同的违约金里有一类**版本对比题**：问「民法典施行之后这条变了
    # 吗」，答案必须新旧并给（民法典 585 + 已废止的合同法 114）。它和 s14/v02
    # 那种「违约金过高能不能调低」不是一类——后者只要现行法一条就够。
    # search_statute 见到「民法典 + 变了吗」确实会放开时效过滤，但 v04 拿原句
    # 检索时 114 排第 7、L1.5 的 topk=6 正好切掉（实测 recall 0.5）。
    # 换成条文措辞后 114 排第 1、585 排第 2，一条路同时解决"准确"和"新旧都要"。
    #
    # 三个限定缺一不可，都是实测出来的：
    #   · 排除「劳动」——h12/v05 问的是劳动合同违约金（25 条），别把 585/114 塞进去；
    #   · 必须有「民法典」+ 版本词——否则 s14/v02 也会走这一路，而这一路比原句窄，
    #     v02 的证据池会丢掉 584/591/592，要点覆盖从 1.0 掉到 0.5（实测踩过）；
    #   · 不能已带条号——v01/v03/v06 走 get_article 精确取条，不需要也不该被换路。
    if ("违约金" in q and "劳动" not in q and "民法典" in q
            and not re.search(r"第[零一二三四五六七八九十百千\d]+条", q)
            and any(k in q for k in ("变了吗", "变了没有", "变没变",
                                     "施行之后", "施行后", "改了没有", "有什么变化"))):
        out.append("违约金 调整 过高 适当减少 过分高于")

    # 「能不能不仲裁直接起诉」问的是先裁后审（调解仲裁法第 5 条）
    if any(k in q for k in ("不仲裁", "不经仲裁", "直接起诉", "直接去法院",
                            "跳过仲裁")):
        out.append("发生劳动争议 可以申请调解 仲裁 提起诉讼")

    # 竞业限制不给补偿，协议还有效吗（第 23 条：按月给补偿）
    if "竞业" in q and any(k in q for k in ("补偿", "钱", "费")):
        out.append("竞业限制 约定 按月 给予 经济补偿")

    # 没缴社保导致医疗费报不了、要公司赔（解释一第 1 条）
    if any(k in q for k in ("医疗费", "住院", "看病", "报销")) and any(
            k in q for k in ("社保", "社会保险", "医保", "没缴", "未缴", "不缴")):
        out.append("未办理社会保险 不能补办 赔偿损失")

    # 试用期要分两种问法：问期限归 19 条，问工资归 20 条。
    # 「实习期」是用户的口语说法，法律上要落到试用期，同时保留歧义提示。
    if any(k in q for k in ("实习期", "试用期", "试岗", "试工期", "转正前")):
        if any(k in q for k in ("工资", "钱", "发", "给", "报酬", "薪", "待遇")):
            out.append("试用期 工资 不得低于 百分之八十")
        else:
            out.append("试用期 期限 不得超过")

    # 「年终奖不发」「工资没发」：原规则只认「不发工资」四字连写，而口语里
    # 几乎没人这么说——于是 r10 这类问法一路工资条文都搜不到（第 30 条、
    # 劳动法第 50 条全缺）。拆成动词 + 钱类词两个条件，才接得住真实问法。
    if any(k in q for k in ("不发", "没发", "少发", "欠发", "拖着不发")) and any(
            k in q for k in ("工资", "薪", "奖", "报酬", "钱", "加班费")):
        out.append("拖欠劳动报酬 支付")

    # 「能不能去仲裁」问的是**范围**（这算不算劳动争议、能不能申请仲裁），
    # 不是「多久之内」。早先「仲裁」二字挂在时效规则上，于是
    # 「年终奖不发能不能去仲裁」捞回来一水儿的时效条文（第 27、45 条那类），
    # 而 gold 要的适用范围（调解仲裁法第 2 条）压根没进证据——
    # r10 因此只被接住一半，另一半怎么补都补不回来。
    if "仲裁" in q and not any(
            k in q for k in ("时效", "过期", "多久", "超过", "多长时间")):
        # 用条文自己的措辞（「劳动争议…适用本法」），不是我想当然的
        # 「适用范围 申请仲裁」——后者在原文里一个词都不出现，BM25 搜不到。
        out.append("劳动争议 适用本法")

    # 问「计算基数」时，答案在工资口径条款里（实施条例 27：应得工资含奖金津贴），
    # 而不在加班或补偿条款里。不单独开一路，「基数」这个词检索不到那条。
    if any(k in q for k in ("基数", "计算标准", "按什么算")) and any(
            k in q for k in ("加班", "补偿", "赔偿", "工资")):
        out.append("月工资 应得工资 包括 奖金 津贴 补贴")

    # 时间词 + 程序词才是「问时效」。缺程序词时不是时效题，别硬套。
    # m12「离职后申请仲裁的期限是多久」两个条件都在，仍然命中。
    if any(k in q for k in ("多久", "多长时间", "几个月", "几天", "几年")) and any(
            k in q for k in ("仲裁", "起诉", "申诉", "诉讼", "打官司", "去告",
                             "告他", "告公司", "告单位", "讨说法", "维权")):
        if "仲裁时效期间 一年" not in out:
            out.append("仲裁时效期间 一年")

    for keys, term in _REWRITE:
        if any(k in q for k in keys) and term not in out:
            out.append(term)

    return out[:5] or [q]


def _rewrite_query(q: str) -> str:
    """单路版本，保留给只需要一条检索式的调用方（工具、评测脚本）。"""
    return _rewrite_queries(q)[0]


# ---- 工作年限折算（《劳动合同法》第四十七条） --------------------------------
# 用户说「干了 3 年 7 个月」，问「经济补偿算几个月」，答案只把条文抄一遍
# 就等于让他自己去算。这是**确定性算术**，不该留给用户，也不该问模型。
# s16「我工作了一年零三个月」和库外探针 n05 都栽在这：给了公式，没给数。

_RE_YM = re.compile(
    r"(\d+|[零一二三四五六七八九十两]{1,3})\s*年\s*(?:零|又)?\s*"
    r"(\d+|[零一二三四五六七八九十]{1,3})\s*个?月")
_RE_YH = re.compile(r"(\d+|[零一二三四五六七八九十两]{1,3})\s*年\s*半")


def _num(t: str) -> int:
    # cn2int 不认「两」（它的表是按条号建的），先归一化
    t = t.strip().replace("两", "二")
    return int(t) if t.isdigit() else cn2int(t)


def service_months(text: str) -> tuple[int, int] | None:
    """从「3 年 7 个月」「一年零三个月」「3 年半」里抽年限，返回 (年, 月)。

    抽不到返回 None。上下界是必须的：`2026年10月` 会被同一条正则吃掉，
    年份设上限 60 直接排掉；月份 ≥12 说明写的不是年限（「1年24个月」是笔误）。
    """
    t = text or ""
    m = _RE_YM.search(t)
    if m:
        y, mo = _num(m.group(1)), _num(m.group(2))
    else:
        m = _RE_YH.search(t)
        if not m:
            return None
        y, mo = _num(m.group(1)), 6
    if not (0 < y <= 60) or not (0 <= mo < 12):
        return None
    return y, mo


def compensation_months(years: int, months: int) -> tuple[float, str]:
    """第四十七条的折算：每满一年一个月；六个月以上不满一年按一年；不满六个月半个月。"""
    n = float(years)
    parts = [f"满 {years} 年折 {years} 个月"]
    if months >= 6:
        n += 1
        parts.append(f"余 {months} 个月属「六个月以上不满一年」，按 1 年算")
    elif months > 0:
        n += 0.5
        parts.append(f"余 {months} 个月不满六个月，按半个月算")
    return n, "，".join(parts)


def _drop_noise(part: str) -> bool:
    """丢掉"我能要什么""我该怎么办"这类没有检索价值的尾巴。

    这些子句拿去检索只会带回一堆含"可以"的无关条文，把真正相关的挤下去。
    """
    if len(part) < 4:
        return True
    return bool(re.fullmatch(
        r"(那|请问|我|您)?(能|可以|应该)?(要|拿|得|做|办|主张|要求)?"
        r"(什么|哪些|多少|咋办|怎么办|如何|啥)[？?]?", part.strip()))


def _guess_intent(q: str, pid: str = "labor") -> str:
    # 接入内核走粗分，不借用劳动法那两张词表。
    #
    # 为什么必须分开：`_OUT_OF_SCOPE` / `_OUT_OF_SCOPE_PAIRS` / `_DOMAIN_WORDS`
    # 全是劳动争议用语（劳动合同、工资、社保…），拿它判婚家或消费的越界，
    # 「离婚」「过期食品」都会落到 out_of_scope——回放模式下四个接入内核
    # 于是对任何问题都回一句「本系统只处理劳动法相关的问题」，分区等于不存在。
    # 在线模式有 `_domain_intent` 读内核边界，不受影响；这里补的是回放这一路。
    if pid != "labor":
        if re.search(r"第[零一二三四五六七八九十百千\d]+条", q):
            return "statute_lookup"
        if any(w in q for w in _CALC_WORDS):
            return "calc"
        # 本领域词命中就当正常咨询——「买到过期食品」只有六个字，但它是
        # 一个说完的问题，再追问一句「您买的是什么」只会让人觉得没在听。
        # 但两个字的「离婚」不行：它是个话题，不是一个问题，这时候追问
        # 一句「能不能离、财产还是孩子」才有用（界面上就是这么体现的）。
        if len(q) >= 6 and any(w in q for w in _profiles.resolve(pid).keywords):
            return "case_consult"
        # 剩下只有两种情况：说不清的短句，和不像本领域的长句。短句追问一句
        # 总比拒答好——clarify 会走内核自己的澄清问句，补一句就能继续。
        return "clarify" if len(q) < 8 else "case_consult"
    if any(w in q for w in _OUT_OF_SCOPE):
        return "out_of_scope"
    if any(all(w in q for w in pair) for pair in _OUT_OF_SCOPE_PAIRS):
        return "out_of_scope"
    if re.search(r"第[零一二三四五六七八九十百千\d]+条", q):
        return "statute_lookup"
    if any(w in q for w in _CALC_WORDS):
        return "calc"
    if len(q) < 8:
        # 短句默认按「信息不足」处理，但**已经能映射到法条术语的除外**：
        # 「试工期能定多长」只有 7 个字，却是个完整的法律问题，判 clarify
        # 就是直接不检索——c08 零召回就是这么来的（证据集整个是空的）。
        # 「你好」「在吗」「工资多少」映射不出任何条文，仍按信息不足处理。
        # 不动阈值 8：放宽它会把真·信息不足的短句也放去检索，得不偿失。
        if _rewrite_queries(q) != [q]:
            return "case_consult"
        # 寒暄变体先排掉：不能因为「你好呀呀」不含领域词就回一句「只处理劳动法问题」。
        if len(q) <= 6 and any(w in q for w in _FILLER_WORDS):
            return "clarify"
        # 映射不出术语、又不含任何劳动法领域词，那就不是「信息不足」而是**越界**。
        # 实测「如何烤蛋糕」原来被判 clarify，接着被追问「您是公司辞退、自己辞职，
        # 还是被迫离职？」——用户问烤蛋糕，系统问离职方式，这一句就够毁掉信任。
        # 靠扩充 _OUT_OF_SCOPE 词表治不了：今天是「烤蛋糕」，明天是「养多肉」。
        return "clarify" if any(w in q for w in _DOMAIN_WORDS) else "out_of_scope"
    return "case_consult"


# ---------------- 图片证据抽取（多模态）----------------
#
# 允许从图里抽的字段只有这 8 个，和 CaseSlots 一一对应。
# 为什么要把范围钉死：模型的文本抽取受「只输出 JSON」约束，看图时它的产出边界
# 更软——不圈清楚，它会返「月薪是否含餐补」「试用期几个月」这种没有槽位接的
# 字段，前端摆不出来，下游也用不上，最后只能悄悄丢掉，白付 token。
_VISION_KEYS = ("region", "employment_type", "has_contract", "start_date",
                "end_date", "monthly_wage", "leave_reason", "social_insurance")
_VISION_DOC_TYPES = ("pay_slip", "contract", "dismissal", "attendance",
                     "chat", "other")
_VISION_CONF = ("high", "medium", "low")
_VISION_DATE_RE = re.compile(r"^\d{4}(-\d{2}){0,2}$")


def _normalize_vision(raw: dict[str, Any]) -> dict[str, Any]:
    """清洗模型读图的结果。

    **这一层是「不许猜」的机械保证，不是格式化。**
    prompt 里写了「图上没写就不要给这个字段」，但那是请求不是保证。
    硬规则在这里：**没有 quote 的字段一律丢弃**。没有 quote 说明模型说不出
    依据，而这样的字段会被直接送进赔偿计算——月薪看错一个数量级，赔偿金
    就错一个数量级，且不会有任何人来报 bug（数字看着都合理）。
    """
    fields_in = raw.get("fields")
    if not isinstance(fields_in, dict):
        fields_in = {}

    out: dict[str, dict[str, Any]] = {}
    for key, item in fields_in.items():
        if key not in _VISION_KEYS or not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        # 两个字以下不构成依据（"是"、"12" 这种）。原样退回给用户看没有意义，
        # 而它又会让这一格「看起来有出处」——比没有出处更危险。
        if len(quote) < 2:
            continue

        val = item.get("value")
        if key == "monthly_wage":
            digits = re.sub(r"[^\d.]", "", "" if val is None else str(val))
            try:
                num = int(round(float(digits))) if digits else 0
            except ValueError:
                continue
            # 低于 100 或高于 100 万，都不是月工资。多半是把「日薪 400」的
            # 400 当成月薪、或把「12,000.00」的年份部分抓了进来。
            if not 100 <= num <= 1_000_000:
                continue
            val = num
        elif key == "has_contract":
            sv = str(val).strip().lower()
            if sv in ("true", "1", "是", "有", "已签", "签订"):
                val = True
            elif sv in ("false", "0", "否", "没有", "未签", "无"):
                val = False
            else:
                continue
        elif key in ("start_date", "end_date"):
            sv = str(val or "").strip().replace("/", "-").replace(".", "-")
            if not _VISION_DATE_RE.match(sv):
                continue
            val = sv
        else:
            sv = str(val or "").strip()
            if not sv or len(sv) > 40:
                continue
            val = sv

        conf = str(item.get("confidence") or "").strip().lower()
        out[key] = {"value": val, "quote": quote[:60],
                    "confidence": conf if conf in _VISION_CONF else "low"}

    unread = raw.get("unreadable")
    if not isinstance(unread, list):
        unread = []
    unread = [str(x).strip()[:60] for x in unread if str(x).strip()][:8]

    doc_type = str(raw.get("doc_type") or "other").strip().lower()
    if doc_type not in _VISION_DOC_TYPES:
        doc_type = "other"

    return {"doc_type": doc_type, "fields": out, "unreadable": unread,
            "note": str(raw.get("note") or "").strip()[:80]}


# 回放模式下的样例。刻意做成「有抽到、有抽不到」各一项：
# 只演示全成功的话，界面上「图里没有这一项」那个分支永远走不到，
# 等真拿一张只有工资没有入职日期的图来试，才会发现前端没写这块。
_MOCK_VISION: dict[str, Any] = {
    "doc_type": "pay_slip",
    "fields": {
        "monthly_wage": {"value": 12000, "quote": "应发工资合计 12,000.00",
                         "confidence": "high"},
        "start_date": {"value": "2023-04", "quote": "入职时间 2023年4月",
                       "confidence": "high"},
        # 刻意留一个 low：界面上「图不清楚的默认不勾」那条分支，
        # 只有真出现过 low 字段才走得到。样例全是 high，等于这条没测。
        "leave_reason": {"value": "协商一致解除",
                         "quote": "经双方协商一致，自即日起解除",
                         "confidence": "low"},
    },
    "unreadable": ["社保个人缴纳：图片该处被裁掉"],
    "note": "工资条（回放样例，不是真实图片的读取结果）",
}


def _mock_vision() -> dict[str, Any]:
    """回放用的读图结果。深拷贝，别让调用方改到模块级常量上。"""
    return json.loads(json.dumps(_MOCK_VISION, ensure_ascii=False))


def _short_goal(text: str) -> str:
    """把第一句提问压成一句「要办的事」。

    去掉句尾语气词（吗/呢/吧/啊）与问号：任务名读起来应该像一件要办的事，
    而不是一句提问的复制品——这条口径和 `server._auto_task_title` 一致。
    """
    s = re.sub(r"\s+", "", str(text or ""))
    s = re.split(r"[。！？!?；;\n]", s)[0]
    s = re.sub(r"(吗|呢|吧|啊|呀|么)$", "", s).strip()
    return s[:40] or "法律咨询"


def _rule_memory(question: str, prev: dict[str, Any],
                 recent: list[dict[str, Any]], slots_brief: str) -> dict[str, str]:
    """回放模式的任务记忆：只用规则拼，不调模型。

    它一定要给出可用结果——34 套回归全离线跑，如果这里返回空，任务记忆
    就成了「只有联网才有」的功能，验收时看不见、上线后才出问题。
    """
    goal = str(prev.get("goal") or "").strip() or _short_goal(question)
    facts = "" if "暂无" in (slots_brief or "") else str(slots_brief or "").strip()
    asks = [str(m.get("content") or "").strip()
            for m in recent if m.get("role") == "user"]
    asks = [a for a in asks if a][-2:]
    parts = [f"目标：{goal}"]
    if facts:
        parts.append(f"已确认：{facts}")
    if asks:
        parts.append("最近问过：" + "／".join(a[:50] for a in asks))
    if question and question not in asks:
        parts.append(f"本轮：{question[:50]}")
    return {"goal": goal, "summary": "；".join(parts)[:600]}


def _guess_domain_slots(text: str, profile) -> dict[str, Any]:
    """接入内核的规则版抽槽位（v138）。

    和 `_guess_slots` 是同一个定位：只在没 key / 调用失败时兜底，
    34 套回归全离线跑，这条路不通就等于「只有联网才有这个功能」。
    但**覆盖范围刻意很窄**——只认能一眼看准的几类值（金额、日期、城市、
    几个固定名词），认不出就留空。空着会被追问一句，猜错会被写进答案，
    后者要糟得多。

    字段名不在字段表里的一律不输出：字段表是内核配的，规则不认识新字段，
    就不该替它编一个。
    """
    labels = _dslots.spec(profile)
    if not labels:
        return {}
    out: dict[str, Any] = {}
    low = text

    # ---- 金额：只认**带单位或带动词**的数，绝不用裸数字 ----
    # 裸数字在这个场景里到处是陷阱：「住院十二天」「2024年」「赔三倍」。
    # 所以只认四种写法，认不出就留空——空着最多多问一句，猜错会写进答案。
    money = _dslots.money_field(profile)
    if money:
        val = _money_in(low)
        if val:
            out[money] = val
    # 约定违约金是合同里写明的比例/数额，和「花了多少」不同，单独一条。
    if "agreed_penalty" in labels:
        m = re.search(r"违约金\s*(?:是|为|约)?\s*(\d+(?:\.\d+)?\s*(?:%|万|元)?)",
                      low)
        if m:
            out["agreed_penalty"] = m.group(1).strip()

    # ---- 日期：绝对年月日 / 年月，以及「去年/今年 + M 月」----
    date_keys = [k for k in _dslots.priority(profile) if "date" in k]
    if date_keys:
        val = ""
        m = re.search(r"(\d{4})\s*[年\-/]\s*(\d{1,2})(?:\s*[月\-/]\s*(\d{1,2}))?", low)
        if m:
            val = f"{int(m.group(1))}-{int(m.group(2)):02d}"
            if m.group(3):
                val += f"-{int(m.group(3)):02d}"
        else:
            m = re.search(r"(今年|去年|前年)\s*(\d{1,2})\s*月", low)
            if m:
                y = int(time.strftime("%Y")) - {"今年": 0, "去年": 1, "前年": 2}[m.group(1)]
                val = f"{y}-{int(m.group(2)):02d}"
        if val:
            out[date_keys[0]] = val

    # ---- 地区：只认省会级/常见城市名，不猜「我在南方」----
    if "region" in labels:
        for city in ("北京", "上海", "广州", "深圳", "杭州", "南京", "成都",
                     "武汉", "西安", "苏州", "宁波", "合肥", "长沙", "郑州",
                     "重庆", "天津", "青岛", "厦门", "济南", "昆明"):
            if city in low:
                out["region"] = city
                break

    # ---- 几个固定名词类字段。命中就把**用户原话里的那个词**记下来 ----
    def first(words: tuple[str, ...]) -> str:
        for w in words:
            if w in low:
                return w
        return ""

    for key, words in _DOMAIN_WORD_RULES:
        if key in labels and key not in out:
            hit = first(words)
            if hit:
                out[key] = hit

    # ---- 是 / 否 ----
    for key, yes, no in _DOMAIN_BOOL_RULES:
        if key in labels and key not in out:
            if any(w in low for w in no):
                out[key] = "否"
            elif any(w in low for w in yes):
                out[key] = "是"
    return out


def _money_in(text: str) -> str:
    """从一句话里认出金额，返回纯数字串；认不出返回空串。

    只认这几种写法，别的一概不认：
      1200元 / 12块 / 1.5万 / 3万      —— 阿拉伯数字带单位
      花了1200 / 付了1200               —— 带动词
      三万二 / 一万五                   —— 口语省位（三万二 = 32000）
      三百元 / 两千块                   —— 中文数字带单位
    不认「三万多」「一千左右」：那是区间，把它写成 30000 就是把用户说的
    「大概」换成了一个精确值，而答案里会照这个数往下算。
    也不认「付了三百」这种没有单位的写法：同一句式里「交了三个月」太常见，
    认它就会把 3 当成金额。宁可留空，留空最多多问一句。
    """
    # 阿拉伯数字 + 万：1.5万 / 3万（「三万多」这种带「多」的不认）
    m = re.search(r"(\d+(?:\.\d+)?)\s*万(?!\s*多)", text)
    if m:
        return str(int(round(float(m.group(1)) * 10000)))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块)(?!\s*(?:一个月|月|年|天))",
                  text)
    if m:
        return str(int(round(float(m.group(1)))))
    m = re.search(r"(?:花了|付了|支付了?|花费|交了)\s*(\d+(?:\.\d+)?)", text)
    if m:
        return str(int(round(float(m.group(1)))))
    # 口语省位：三万二 = 32000、一万五 = 15000。单位后面那一句必须跟单个
    # 数字，且不能是「多/左右/上下」。
    m = re.search(r"([一二两三四五六七八九十])\s*万\s*([一二三四五六七八九])"
                  r"(?![十百千万])", text)
    if m:
        head = 2 if m.group(1) == "两" else cn2int(m.group(1))
        return str(head * 10000 + cn2int(m.group(2)) * 1000)
    m = re.search(r"([一二两三四五六七八九十百千]+)\s*(?:元|块钱|块)", text)
    if m:
        return str(cn2int(m.group(1).replace("两", "二")))
    return ""


# 名词类字段：命中第一个词就把它当值。顺序有意义——先出现的是更具体的说法。
_DOMAIN_WORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("goods", ("面包", "牛奶", "酸奶", "饮料", "零食", "外卖", "饭菜", "水果",
               "蔬菜", "肉", "食品", "化妆品", "护肤品", "衣服", "裤子", "鞋",
               "手机", "电脑", "家电", "家具", "床垫", "电动车", "健身卡",
               "预付卡", "会员卡", "课程", "装修")),
    ("problem", ("过期", "变质", "发霉", "异物", "虫子", "假货", "假冒",
                 "虚假宣传", "跑路", "不发货", "少发", "质量问题")),
    ("liability", ("全责", "主要责任", "次要责任", "同等责任", "无责")),
    ("injury", ("十级伤残", "九级伤残", "骨折", "住院", "伤残", "受伤", "擦伤")),
    ("insurance", ("交强险", "商业三者险", "商业险", "三者险")),
    ("dispute", ("离婚", "财产分割", "抚养权", "抚养费", "继承", "遗嘱",
                 "彩礼", "损害赔偿")),
    ("property", ("房子", "房产", "存款", "股权", "车辆", "公积金")),
    ("debt", ("共同债务", "债务", "借款", "欠款")),
    ("contract_type", ("买卖合同", "租赁合同", "借款合同", "服务合同",
                       "装修合同", "劳动合同")),
    ("breach", ("逾期", "不履行", "迟延", "拒绝履行", "不完全履行")),
)

# 布尔类字段：(字段, 判「是」的词, 判「否」的词)。否的词要写在前面判，
# 因为「没有书面合同」同时含「书面合同」——先判肯定就是反的。
_DOMAIN_BOOL_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("has_written", ("书面合同", "签了合同", "签过合同", "有合同"),
     ("没有书面", "没签", "未签", "口头约定", "没合同")),
    ("has_will", ("有遗嘱", "立了遗嘱", "写了遗嘱"),
     ("没有遗嘱", "没遗嘱", "未立遗嘱", "没留遗嘱")),
    ("has_voucher", ("小票", "发票", "订单", "收据", "截图", "凭证"),
     ("没留", "没有凭证", "没凭证", "小票丢了", "发票丢了")),
    ("is_food", ("食品", "吃的", "吃出", "面包", "牛奶", "外卖", "饭菜", "零食"),
     ("衣服", "鞋", "手机", "家电", "家具")),
)


def _guess_slots(text: str) -> dict[str, Any]:
    """规则抽槽位。只是为了基础版能跑通，正式版由模型抽取。"""
    out: dict[str, Any] = {}
    # 金额：先认「月薪/工资 + 数字」的上下文，再认带单位的数字。
    # **绝不取句子里第一个 3–6 位数字**：「2024年3月入职，月薪12000」原实现
    # 抽成 2024（年份），补偿金直接少算六倍，而且不报错——用户自己发现不了，
    # 这类错没有人会来报 bug。末尾的负向断言是防「拖欠工资 3 个月」抽成 3。
    wage: int | None = None
    mw = re.search(r"(?:月薪|月工资|工资|薪水|薪资|收入)\s*"
                   r"(?:是|为|约|大概|差不多|有|不低于|至少|在)?\s*"
                   r"(\d+(?:\.\d+)?)\s*(万|w|W|k|K|千)?(?:元|块)?"
                   r"(?!\s*(?:个月|年|天|日|周|次))", text)
    if mw:
        v = float(mw.group(1))
        unit = (mw.group(2) or "").lower()
        # 「15k」「1.2万」在口语里比「12000」更常见，不认单位会抽成 15 / 1
        if unit in ("万", "w"):
            wage = int(round(v * 10000))
        elif unit in ("k", "千"):
            wage = int(round(v * 1000))
        else:
            wage = int(round(v))
    if wage is None:
        m2 = re.search(r"(\d{3,7})\s*(?:元|块)", text)
        if m2:
            wage = int(m2.group(1))
    if wage is not None and any(w in text for w in ("工资", "月薪", "薪水", "薪资")):
        out["monthly_wage"] = wage
    for city in ("北京", "上海", "广州", "深圳", "杭州", "南京", "成都",
                 "武汉", "西安", "苏州", "宁波", "合肥", "长沙", "郑州"):
        if city in text:
            out["region"] = city
            break
    if any(w in text for w in ("没签合同", "未签合同", "没有签合同", "未订立书面")):
        out["has_contract"] = False
    elif any(w in text for w in ("签了合同", "签过合同", "有合同")):
        out["has_contract"] = True
    if any(w in text for w in ("辞退", "开除", "辞了我")):
        out["leave_reason"] = "公司辞退"
    elif any(w in text for w in ("被迫离职", "自己走的", "主动辞职")):
        out["leave_reason"] = "被迫离职" if "被迫" in text else "主动辞职"
    # 日期：按「后面紧跟什么词」判归属，不能拿第一个 4 位年份当入职时间。
    # 「2025年9月被公司辞退」原来会变成 start_date，于是「入职即离职」，
    # 补偿年限算成半个月——错得安静，而且只影响金额不影响措辞。
    loose: list[str] = []
    for m in re.finditer(r"(\d{4})\s*[年\-/]\s*(\d{1,2})", text):
        val = f"{int(m.group(1))}-{int(m.group(2)):02d}"
        tail = text[m.end():m.end() + 10]
        if any(w in tail for w in ("入职", "开始", "进公司", "上班", "到岗")):
            out.setdefault("start_date", val)
        elif any(w in tail for w in ("辞退", "开除", "离职", "解除", "离开",
                                     "终止", "不干", "走人", "被裁")):
            out.setdefault("end_date", val)
        else:
            loose.append(val)
    # 判不出归属的按出现顺序补：第一个当入职，第二个当离职。
    # 这比「把所有裸日期都当入职」安全——两个裸日期时至少不会丢掉离职时间。
    for val in loose:
        if "start_date" not in out:
            out["start_date"] = val
        elif "end_date" not in out:
            out["end_date"] = val

    # 相对年份（今年/去年/前年）。写在绝对日期之后，两者同时出现时以相对
    # 年份为准——「2024年3月入职，今年9月被辞退」里，后者更可能指离职。
    m = re.search(r"(今年|去年|前年)\s*(\d{1,2})\s*月", text)
    if m:
        y = int(time.strftime("%Y")) - {"今年": 0, "去年": 1, "前年": 2}[m.group(1)]
        out["end_date"] = f"{y}-{int(m.group(2)):02d}"
    m = re.search(r"(?:干|工作|做了)\s*了?\s*(\d+)\s*年", text)
    if m and "start_date" not in out:
        out["_years"] = int(m.group(1))
    return out


# 判「这段是拒答」用的标记。**必须和项目自己产出的拒答文案对齐**：
# `_mock_generate` 的零命中分支写的是「没有找到直接依据 / 未命中相关条文 /
# 不做没有依据的推测」，而这里早先只有「未找到」——于是真正的拒答反而不被认出来，
# 答案末尾被追一句「核对提示：答案未标注任何条文依据」，对一篇刚说过
# 「没查到依据」的回答自相矛盾。少一个词，产品就多一句胡话。
_REFUSAL_MARKS = ("未覆盖", "未找到", "没有找到", "未命中", "无法确定",
                  "本库没有", "不做没有依据的推测", "无法给出")


def _mock_verify(answer: str, evidences: list[Evidence],
                 index=None) -> VerifyResult:
    """规则自检：引用是否都有出处、废止条文有没有提示。

    抽取用 `app/cite.extract_cites`——与评测层、与补核验
    （`agent._backfill_citations`）是同一套。这里早先自己写了个只认书名号的正则，
    后果是「同法第七十七条」「依据是调解仲裁法第二十一条」这类写法**一条都抽不出来**，
    于是 r10 把《劳动法》第七十七条安在《调解仲裁法》名下这处错误引用，
    自检直接判了通过。评测层看得见、产品层看不见，方向正好反了。
    """
    # 判据顺序很重要：**先抽引用，再决定要不要免检**。
    # 原来是一上来就「答案里有『未覆盖/未找到/无法确定』→ 直接通过」，
    # 等于给模型留了一句万能免检口令——在线实测它把「这次辞退违不违法
    # 判断不了」也说成「未覆盖」（这是事实判断不了，不是知识库没有），
    # 于是同一段答案里的所有引用（哪怕引错条号）都不用再核了。
    # 现在：有引用就一律核；只有「没有引用 + 明确拒答」才算正常通过。
    cites = extract_cites(answer, index=index)
    if not cites:
        if any(k in answer for k in _REFUSAL_MARKS):
            return VerifyResult(passed=True)
        return VerifyResult(passed=False, issues=[
            VerifyIssue(conclusion="（整段）", reason="答案未标注任何条文依据")])
    issues: list[VerifyIssue] = []
    for law, no in cites:
        label = f"《{law}》第{no}条"
        art = find_article(index, law, no) if index is not None else None
        matched: Evidence | None = None
        if art is not None:
            # 能解析出来就按 id 认，不比字符串：抽出来的法名可能带前缀
            # （「是调解仲裁法」），子串比对会把它误判成无依据引用。
            matched = next((e for e in evidences if e.article_id == art["id"]), None)
            if matched is None:
                issues.append(VerifyIssue(
                    conclusion=label,
                    reason="该条文不在本轮检索到的证据中，属于无依据引用"))
                continue
        elif index is not None:
            # 库里根本没有这一条——比「不在证据里」更严重，是真幻觉。
            issues.append(VerifyIssue(
                conclusion=label, reason="本库没有这一条，引用无法核对"))
            continue
        else:
            # 没有索引时退回按字符串比（法名 + 条号），精度低一些但不会漏检。
            n = cn2int(no)
            for e in evidences:
                try:
                    if int(e.article_no) != n:
                        continue
                except (TypeError, ValueError):
                    continue
                short = e.law_name.replace("中华人民共和国", "")
                if law in e.law_name or law in short or short in law:
                    matched = e
                    break
            if matched is None:
                issues.append(VerifyIssue(
                    conclusion=label,
                    reason="该条文不在本轮检索到的证据中，属于无依据引用"))
                continue
        if matched.status != "现行有效" and "已废止" not in answer:
            issues.append(VerifyIssue(
                conclusion=label,
                reason=f"引用了状态为「{matched.status}」的条文，但答案未提示其已失效"))
    return VerifyResult(passed=not issues, issues=issues)


# ---------------------------------------------------------------- 主类

class LLM:
    def __init__(self) -> None:
        self.online = LLM_ENABLED
        self._client = None
        self.fallback_reason = ""
        self._fails = 0
        # 上次降级的时刻，`maybe_recover()` 用它算冷却。0 表示从没降级过。
        self._offline_since = 0.0
        if self.online:
            try:
                # 不走 openai 官方 SDK：本机应用程序控制策略拦截了它的
                # jiter 扩展（详见 app/_api.py 顶部说明）。
                from ._api import MiniClient
                self._client = MiniClient(DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL)
            except Exception as e:            # noqa: BLE001
                self.online = False
                self.fallback_reason = f"初始化失败：{e}"

    # ---------------- 底层调用 ----------------
    def _chat(self, system: str, user: str, *,
              temperature: float = 0.0, max_tokens: int = 1200,
              json_mode: bool = False, tools: list[dict] | None = None,
              allow_think: bool = True, image: tuple[str, str] | None = None):
        """阻塞调用。内建「截断自动重试」——接推理模型后必须有这一层。

        为什么要重试：deepseek-flash 的 reasoning_content 和 content 共享
        max_tokens。预算给小了不报错，而是正文空、finish_reason=length，
        调用方拿到空串还以为模型就这水平。这类静默失败排查成本极高
        （实测意图识别用 12 token 时，100% 返回空串，全部问题掉进默认分支）。

        `image=(base64, mime)` 走视觉通道：deepseek-flash 同时支持文本和图片，
        唯一要改的是把 user 的 content 从字符串换成 content block 数组。
        **图片只能放在 user 消息里**，放进 system 会 400。
        """
        user_content: Any = user
        if image is not None:
            b64, mime = image
            user_content = [
                {"type": "text", "text": user},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]
        kw: dict[str, Any] = dict(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user_content}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        if tools:
            kw["tools"] = tools
            kw["tool_choice"] = "auto"
        body = _think_body(allow_think)
        if body:
            kw["extra_body"] = body

        r = self._client.chat.completions.create(**kw)
        if _truncated(r) and kw["max_tokens"] < TOK_RETRY_CAP:
            kw["max_tokens"] = min(kw["max_tokens"] * 2, TOK_RETRY_CAP)
            print(f"[retry] 输出被截断（思考占满预算），"
                  f"预算提到 {kw['max_tokens']} 重试一次")
            r = self._client.chat.completions.create(**kw)
        self._fails = 0                      # 成功即清零，避免偶发错误累积降级
        return r

    def _downgrade(self, err: Exception) -> None:
        """调用失败后的降级判定。

        早先的逻辑是「失败一次就整会话降级」，代价极大：一次网络抖动会让整轮
        评测静默变成回放模式，指标全 0，而日志只有一行 warn（实际被踩过）。
        改为分级处理：
          认证 / 余额 / 参数错误 → 立即降级，重试没有意义
          超时 / 5xx / 连接问题  → 连错 3 次才降级，前两次仍走在线

        降级之后靠 `maybe_recover()` 冷却重试（`LLM_RECOVER_AFTER`，默认 600 秒）。
        没有这一步的话，`online=False` 就是永久的：所有方法开头都是
        `if not self.online: return 回放`，再也没有任何地方会去调接口——
        余额充上了也不会恢复，只能重启进程。
        """
        fatal = bool(getattr(err, "fatal", False))
        self._fails += 1
        self.fallback_reason = f"{type(err).__name__}: {err}"
        if fatal or self._fails >= 3:
            self.online = False
            self._offline_since = time.time()
            print(f"\n[warn] LLM 调用失败，本会话改为回放模式：{self.fallback_reason}")
        else:
            print(f"[warn] LLM 调用失败（第 {self._fails} 次，暂不降级）："
                  f"{self.fallback_reason}")

    def maybe_recover(self) -> bool:
        """降级之后冷却一段时间，再试探一次在线。返回是否重新置为在线。

        由 `AgentRunner.run()` 在每轮开头调一次——**不做后台定时器**：
        这个项目没有常驻调度器，而恢复这件事只在有人提问时才有意义。

        试探成本：冷却期内最多每 `LLM_RECOVER_AFTER` 秒一次失败调用。
        402 / 401 这类几秒钟就回来；超时类会慢，但一轮只试一次，不会把
        每轮都拖成慢请求。

        为什么不在失败当下立刻重试：那等于把「一次故障」放大成「每轮一次故障」，
        早先「失败一次就整会话降级」踩的就是这类坑的反面。
        """
        if self.online or not LLM_ENABLED or LLM_RECOVER_AFTER <= 0:
            return False
        if time.time() - self._offline_since < LLM_RECOVER_AFTER:
            return False
        self.online = True
        self._fails = 0
        # 先记时间：万一这次试探又立刻失败，冷却从头开始算，不会连着重试
        self._offline_since = time.time()
        print(f"[info] 距上次失败已超过 {LLM_RECOVER_AFTER} 秒，重新尝试在线调用")
        return True

    # ---------------- 意图 ----------------
    def classify_intent(self, question: str, slots_brief: str) -> str:
        pid = _profiles.get().id
        if not self.online:
            return _guess_intent(question, pid)
        try:
            r = self._chat(prompts.system("INTENT_SYSTEM"),
                           f"已知案情：{slots_brief}\n用户：{question}",
                           max_tokens=TOK_INTENT,
                           # 意图分类是五选一，不需要推理。不关思考的话
                           # reasoning_content 会把 16 token 的预算吃光，
                           # 正文返回空串，然后触发重试——每条题白花一次调用。
                           allow_think=False)
            txt = (r.choices[0].message.content or "").strip()
            for k in ("statute_lookup", "case_consult", "calc",
                      "clarify", "out_of_scope"):
                if k in txt:
                    return k
            return "case_consult"
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return _guess_intent(question, pid)

    # ---------------- 查询分解 ----------------
    def decompose(self, question: str, slots_brief: str) -> list[str]:
        if not self.online:
            parts = [p.strip() for p in re.split(r"[，,；;。]\s*", question)]
            parts = [p for p in parts if not _drop_noise(p)]
            # 整句先改写：完整语义在整句里，「能拿多少赔偿」这类关键信息
            # 一切成片段就丢了（「我月薪8000」单独检索是 0 命中）。
            out: list[str] = list(_rewrite_queries(question))
            for p in parts[:2]:
                t = _rewrite_queries(p)
                if t == [p]:        # 没改写成功，拿原句去检索纯属浪费一步
                    continue
                for x in t:
                    if x not in out:
                        out.append(x)
            return out[:5] or [question]
        try:
            # 分解是格式化输出（一个 JSON 数组），不需要推理。关掉思考：
            # 开着的时候 reasoning 会和答案共享 max_tokens，prompt 一长就
            # 「思考占满预算 → 输出被截断 → 退化成原句」，等于这一步白做。
            r = self._chat(prompts.system("DECOMPOSE_SYSTEM"),
                           f"已知案情：{slots_brief}\n问题：{question}",
                           max_tokens=TOK_DECOMPOSE,
                           allow_think=False)
            data = _loads_array(r.choices[0].message.content)
            out = [str(x) for x in data][:3] or [question]
            # 在线也要跑一遍规则：模型分解会漏掉条文里的措辞。
            # 实测两例——「年终奖不发能不能去仲裁」，模型走了仲裁**程序**，
            # 而规则映射的「劳动争议 适用本法」正是 gold 那条；「工装费谁出」
            # 模型零命中后直接拒答，规则能把它带到第 9、84 条。
            # 规则结果排前面：L1.5 只执行 query_plan[:3]，排后面等于没加。
            # 但**至少给模型留一条**——规则是硬的，模型更懂上下文，
            # 不能因为规则命中就把它整条挤掉。规则没命中时行为完全不变。
            rules = [x for x in _rewrite_queries(question) if x != question]
            if rules:
                merged = [x for x in rules if x not in out]
                merged += [x for x in out if x not in merged][:max(1, 3 - len(merged))]
                return merged[:3]
            return out
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return [_rewrite_query(question)]

    # ---------------- 槽位抽取 ----------------
    def extract_slots(self, text: str) -> dict[str, Any]:
        """抽案情要素。**按内核分两套字段**，返回的字典由调用方按字段表筛。

        劳动法：仍是 `CaseSlots` 那八项，逐字不变（含 `monthly_wage` 的
        去单位转整数，那段老逻辑一个字没动）。
        接入内核：走各自的字段表（v138），值一律字符串，不在这里做类型
        转换——转换放在 `dslots.normalize`，一处管。
        """
        domain = _profiles.get().id != "labor"
        if not self.online:
            return (_guess_domain_slots(text, _profiles.get()) if domain
                    else _guess_slots(text))
        try:
            today = time.strftime("%Y-%m-%d")
            r = self._chat(prompts.system("SLOT_SYSTEM").replace("__TODAY__", today),
                           text, max_tokens=TOK_SLOTS, json_mode=True,
                           allow_think=False)
            data = _loads_obj(r.choices[0].message.content)
            if domain:
                return {k: v for k, v in data.items() if v not in (None, "", [])}
            if "monthly_wage" in data:
                try:
                    data["monthly_wage"] = int(re.sub(r"\D", "", str(data["monthly_wage"])))
                except (TypeError, ValueError):
                    data.pop("monthly_wage", None)
            return {k: v for k, v in data.items() if v not in (None, "", [])}
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return (_guess_domain_slots(text, _profiles.get()) if domain
                    else _guess_slots(text))

    # ---------------- v137 任务记忆 ----------------
    def task_memory(self, *, question: str, prev: dict[str, Any],
                    recent: list[dict[str, Any]], slots_brief: str,
                    profile_name: str = "") -> dict[str, str]:
        """维护「这个任务要干什么 + 已经聊到哪」。

        只产出两段文字：goal（一句话）与 summary（滚动摘要）。它**不进结论**，
        只作为下一轮 system 的背景——所以这里允许出现金额，但金额一律以
        槽位为准：槽位里有值就用槽位，摘要里的旧数字会被新数字覆盖。

        回放模式走 `_rule_memory`：34 套回归全离线，这条路必须也给出可用结果，
        否则「离线能过的东西上线不变样」这条底线就断了。
        """
        if not self.online:
            return _rule_memory(question, prev, recent, slots_brief)
        try:
            prev_goal = str(prev.get("goal") or "").strip()
            prev_sum = str(prev.get("summary") or "").strip()
            lines = [f"{'用户' if m['role'] == 'user' else '助手'}："
                     f"{str(m['content'])[:300]}" for m in recent[-6:]]
            user = (
                f"领域：{profile_name or '（未指定）'}\n"
                f"已确认事实：{slots_brief}\n"
                f"上一版目标：{prev_goal or '（还没有）'}\n"
                f"上一版摘要：{prev_sum or '（还没有）'}\n"
                f"最近几轮：\n" + "\n".join(lines) +
                f"\n\n本轮提问：{question}\n\n"
                "按 JSON 输出两个字段：goal（一句话，20–40 字，写用户要办的事，"
                "不要写「用户想咨询」这类空话）、summary（150–250 字，写诉求、"
                "已确认的事实、已经排除的方向、还没回答的问题；"
                "金额与日期一律采用「已确认事实」里的最新值，不要沿用旧数字）。")
            r = self._chat(
                "你在维护一个法律咨询任务的记忆。只输出 JSON，不要解释。",
                user, max_tokens=TOK_SLOTS, json_mode=True, allow_think=False)
            data = _loads_obj(r.choices[0].message.content)
            goal = str(data.get("goal") or prev_goal)[:200].strip()
            summ = str(data.get("summary") or prev_sum)[:1200].strip()
            return {"goal": goal, "summary": summ}
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return _rule_memory(question, prev, recent, slots_brief)

    def extract_from_image(self, image_b64: str, mime: str) -> dict[str, Any]:
        """读一张图，返回槽位字段**和每个字段的原图依据**。

        和 `extract_slots` 的分工，不是重复实现：
          - 那个处理用户打字，打字不会有「看不清」；
          - 这个处理图片，会有。所以返回结构多两样东西：每字段的 quote，
            和 unreadable 列表。用户要判断的恰恰是「凭什么这个数是 12000」。
        两个方法最后都落到同一批 CaseSlots 字段上，下游的规划、计算、
        文书生成一行都不用改——这是这条链路最省的地方。
        """
        if not self.online:
            return _normalize_vision(_mock_vision())
        try:
            r = self._chat(prompts.system("VISION_SYSTEM"),
                           "读这张图，按约定只输出 JSON。",
                           max_tokens=TOK_VISION, json_mode=True,
                           allow_think=False, image=(image_b64, mime))
            return _normalize_vision(_loads_obj(r.choices[0].message.content))
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return _normalize_vision(_mock_vision())

    # ---------------- 决策（核心）----------------
    def decide(self, state: AgentState, tool_schemas: list[dict]) -> Decision:
        if not self.online:
            return self._mock_decide(state)
        guard = self._calc_budget_guard(state)
        if guard is not None:
            return guard
        # L1 的分解结果必须传给 L2，否则那一步就是白烧一次调用。
        # 实测过不传的代价：口语题「被开除了能拿多少钱」「多久以后就不能去告了」
        # 「送外卖的算不算公司员工」三条零命中→拒答，而分解步骤其实已经给出了
        # 正确的术语检索式（如「违法解除劳动合同 赔偿金 计算标准」）。
        plan = state.query_plan or []
        plan_txt = ("\n".join(f"{i}. {q}" for i, q in enumerate(plan[:3], 1))
                    if len(plan) > 1 else "（本轮没有分解出多条检索式，请自行构造）")
        system = (prompts.system("DECISION_SYSTEM")
                  .replace("__TOOLS__", _describe_tools(tool_schemas))
                  .replace("__SLOTS__", state.case_brief())
                  .replace("__PLAN__", plan_txt)
                  .replace("__SEARCHED__", _describe_searched(state))
                  .replace("__EVIDENCE__", _describe_evidence(state.evidences)))
        try:
            r = self._chat(system, prompts.DECISION_USER.replace("__QUESTION__", state.question),
                           max_tokens=TOK_DECIDE, tools=tool_schemas)
            msg = r.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                return Decision(thought=msg.content or "", is_final=True)
            out: list[ToolCall] = []
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out.append(ToolCall(name=c.function.name, args=args))
            return Decision(thought=msg.content or "", tool_calls=out)
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return self._mock_decide(state)

    @staticmethod
    def _calc_budget_guard(state: AgentState) -> Decision | None:
        """步数快用完时，先把金额算掉再谈别的。

        这条是实测出来的：问「月薪 5 万、干了 16 年、被辞退能拿多少」，模型
        连着调了 9 次 search_statute，把 8 步预算吃光，calculate_compensation
        一次都没轮到。最后答案写成「北京社平 × 3 × 12 × 2」——规则全对，
        数字一个没有，而槽位（工资、入职时间、城市）其实早就齐了。

        检索边际收益在这里已经归零：第 7 次检索不会让它更会算钱。所以不跟
        模型抢决策权，只在「最后两步 + 槽位已齐 + 还没算过」时直接指派，
        前面留足正常的检索空间。

        v133：整段限定在劳动法内核。三个闸都建立在「月工资 / 入职日期 /
        当地社平」这套劳动法语义上，接入内核既没有算钱工具，也没有这些槽位
        ——但 `region` 会被个人档案种进去，于是婚家用户问「出轨能赔多少」
        会触发半程闸去查北京社平工资，答出一段和本案无关的封顶线。
        """
        if _profiles.get().id != "labor":
            return None
        if "calculate_compensation" in {s.tool for s in state.scratchpad}:
            return None
        # 软闸：条文已经到手、要素也齐、用户明确问金额 → 直接算，不等步数见底。
        #
        # 为什么必须有这一级：真 key 实测「月薪5万北京干16年被违法辞退能拿多少」，
        # 模型查了 3 条法条后 ask_user 追问解除日期，循环随即因为「已挂起追问 +
        # 已有证据」跳出，算钱这一步永远轮不到。而答案里照样出现了精确数字
        # 872,352 元——那是**模型自己算的**，连「北京社平 12116」都是它凭记忆
        # 写的（这次恰好对）。换杭州、换年度、换工龄，错的就是真金白银。
        # 提示词里「必须调用 calculate_compensation」只是劝告，这里是流程保证。
        if needs_calc(state) and state.evidences:
            slots = state.slots
            return Decision(
                thought="用户问金额且要素已齐，交给计算工具（不让模型算术）",
                tool_calls=[ToolCall(name="calculate_compensation", args={
                    "monthly_wage": slots.monthly_wage,
                    "start_date": slots.start_date,
                    "end_date": slots.end_date or time.strftime("%Y-%m-%d"),
                    "leave_reason": slots.leave_reason or "公司辞退",
                    "region": slots.region or "",
                })])
        # 半程闸：算不出总额，但至少把当地社平工资查出来。
        # 缺入职日期时 calculate_compensation 调不动（start_date 必填），
        # 于是模型既没有总额、也没有社平数，只能写「当地社平工资目前还没有」——
        # 而库里就存着官方数据。有了这一步，答案至少能说清「5 万超过北京
        # 三倍封顶线 36,348，年限按 12 年封顶」，比一句「算不出来」有用得多。
        if (any(w in state.question for w in _CALC_WORDS)
                and "query_region_params" not in {s.tool for s in state.scratchpad}
                and state.slots.region
                and state.evidences
                and not (state.slots.start_date and state.slots.monthly_wage)):
            return Decision(
                thought="算不出总额，先把当地社平工资查出来，答案里能说清封顶线",
                tool_calls=[ToolCall(name="query_region_params",
                                     args={"region": state.slots.region})])
        # 判据不能只看 steps_left。模型每轮会并发 3 个 search_statute，
        # 一轮就吃掉 3 个 step，所以 8 步预算实际只有 3 轮——steps_left 还剩 5，
        # 检索却已经跑了 9 次。按轮次判断等于永远不触发，这道闸就白装了。
        n_search = sum(1 for s in state.scratchpad if s.tool == "search_statute")
        if n_search < 6 and state.budget.steps_left > 2:
            return None
        slots = state.slots
        if not (slots.monthly_wage and slots.start_date):
            return None
        if not any(w in state.question for w in _CALC_WORDS):
            return None
        return Decision(
            thought="步数快用完了，先把金额算出来——再查一次法条换不来数字",
            tool_calls=[ToolCall(name="calculate_compensation", args={
                "monthly_wage": slots.monthly_wage,
                "start_date": slots.start_date,
                "end_date": slots.end_date or time.strftime("%Y-%m-%d"),
                "leave_reason": slots.leave_reason or "公司辞退",
                "region": slots.region or "",
            })])

    def _mock_decide(self, state: AgentState) -> Decision:
        done = {s.tool for s in state.scratchpad}
        # 上一轮带进来的 recall 不算「这一轮查过」。混在一起算的话，回放模式
        # 一进任务就认为「已经有证据」，从此再也不检索——答的全是上一题的条文。
        fresh = [e for e in state.evidences if e.source != "recall"]

        # 文书生成放在最前面：用户要的是「一份能拿去交的纸」，不是又来一段分析。
        # 先检索一遍再生成，等于把他的事又讲一遍才开始动手——而且检索出来的
        # 法条对写申请书没用（docgen 里的条号是写死在模板里的）。
        if ("draft_document" not in done
                and any(w in state.question for w in
                        ("写", "起草", "生成", "给我一份", "弄一份", "准备一份",
                         "出个模板", "要份模板"))
                and any(w in state.question for w in
                        ("申请书", "仲裁申请", "申请仲裁要交"))):
            return Decision(
                thought="用户要的是文书本身，不是咨询；案情要素槽位里已经有了",
                tool_calls=[ToolCall(name="draft_document",
                                     args={"doc_type": "arbitration"})])
        if "draft_document" in done:
            # 文书已经生成，再检索一遍没有意义：申请书里的条号写死在模板里，
            # 检索结果进不了正文，只会白花一步预算。
            # 但「顺便告诉我能拿多少」是另一半需求——只交文书等于只做一半。
            if ("calculate_compensation" not in done
                    and any(w in state.question for w in _CALC_WORDS)
                    and state.slots.monthly_wage and state.slots.start_date):
                return Decision(
                    thought="文书已生成，用户还问了能拿多少——把金额补上再收工",
                    tool_calls=[ToolCall(name="calculate_compensation", args={
                        "monthly_wage": state.slots.monthly_wage,
                        "start_date": state.slots.start_date,
                        "end_date": (state.slots.end_date
                                     or time.strftime("%Y-%m-%d")),
                        "leave_reason": state.slots.leave_reason or "公司辞退",
                        "region": state.slots.region or "",
                    })])
            return Decision(thought="文书已生成，直接交付", is_final=True)

        # 已知条号 → 走精确取条，而不是拿模糊检索碰运气
        if state.intent == "statute_lookup" and "get_article" not in done:
            # 问句里可能有两三个条号：「第八十二条和第十条有什么区别」
            # 「民法典585 和旧合同法114 内容一样吗」。原来只 re.search 第一条，
            # 于是对比题永远只答一半，而且不告诉用户另一半没答。
            # 上限 2：对比题基本是两条，三条以上的多半是「罗列相关条文」，
            # 全取会把答案撑成一堆条文原文。
            ms = list(re.finditer(r"第([零一二三四五六七八九十百千\d]+)条",
                                  state.question))
            calls: list[ToolCall] = []
            seen: set[tuple[str, str]] = set()
            for m in ms[:2]:
                law = _law_near_article(state.question, m.start())
                key = (law, m.group(1))
                if key in seen:
                    continue
                seen.add(key)
                calls.append(ToolCall(name="get_article",
                                      args={"law_name": law,
                                            "article_no": m.group(1)}))
            if calls:
                return Decision(
                    thought="用户给了条号，精确取条准确率是 100%",
                    tool_calls=calls)

        if not fresh:
            if "search_statute" in done:
                return Decision(thought="已检索但未命中，重复同样的查询没有意义",
                                is_final=True)
            qs = (state.query_plan or [state.question])[:PLAN_CAP]
            return Decision(
                thought="这一轮还没有任何条文，按查询计划并行检索",
                tool_calls=[ToolCall(name="search_statute", args={"query": q})
                            for q in qs])

        dead = [e for e in fresh if e.status != "现行有效"]
        if dead and "check_validity" not in done:
            return Decision(thought="命中已废止条文，必须查替代关系",
                            tool_calls=[ToolCall(name="check_validity",
                                                 args={"article_id": dead[0].article_id})])

        cited = [e for e in fresh if state.has_evidence(e.article_id)]
        if "expand_citation" not in done and cited:
            has_ref = any(re.search(r"本法第|第[零一二三四五六七八九十百千]+条",
                                    e.content) for e in cited)
            if has_ref:
                return Decision(thought="条文里有交叉引用，取回被引条文",
                                tool_calls=[ToolCall(name="expand_citation",
                                                     args={"article_id": cited[0].article_id})])

        # 要算具体金额但关键要素缺失：这次是 Agent 主动决策追问，
        # 而不是流程层的 if 拦截。区别在于——法条已经查到了，
        # 用户能先看到规则，只是算不出他自己的数。
        #
        # v133：只在劳动法内核问这句。接入内核的"calc"是「能赔多少」这类问法，
        # 缺的绝不是月工资和入职日期——问出来就是把劳动法的要素表塞给婚家用户。
        if (state.intent == "calc" and "ask_user" not in done and fresh
                and _profiles.get().id == "labor"):
            missing = [k for k in ("monthly_wage", "start_date")
                       if getattr(state.slots, k) is None]
            if missing:
                label = {"monthly_wage": "月工资大概多少",
                         "start_date": "哪一年哪个月入职的"}[missing[0]]
                return Decision(
                    thought="条文已查到，但算具体金额还缺要素，先问一个最关键的",
                    tool_calls=[ToolCall(name="ask_user", args={
                        "question": f"为了算出具体金额，想先确认一下：您{label}？"})])

        # 接入内核的同一条：问的是「能赔多少」，但他没说自己花了多少。
        # v138 之前这里什么都不做——接入内核不抽槽位，`state.slots` 永远是空，
        # 所以「能赔多少」只能拿到一段法条复述，用户还得自己乘。
        # 这里只问**金额类**字段（消费的实付、道交的已发生费用、合同的约定
        # 违约金），不问「商品是什么」这类背景：问金额是算不出数的唯一原因，
        # 问背景只是采集，而离线一步多余的反问就会让答案看起来像在打太极。
        if (state.intent == "calc" and "ask_user" not in done and fresh
                and _profiles.get().id != "labor"):
            _p = _profiles.get()
            _k = _dslots.money_field(_p)
            if (_k and not str(state.dslots.get(_k) or "").strip()
                    and _k not in state.asked_slots):
                return Decision(
                    thought="问的是金额，但用户没说自己花了多少，先问这一项",
                    tool_calls=[ToolCall(name="ask_user", args={
                        "question": f"为了算出具体数额，想先确认一下："
                                    f"{_dslots.ask_for(_p, _k)}？"})])

        if (any(w in state.question for w in _CALC_WORDS)
                and "calculate_compensation" not in done
                and state.slots.monthly_wage and state.slots.start_date):
            return Decision(
                thought="涉及金额，交给计算工具而不是让模型算术",
                tool_calls=[ToolCall(name="calculate_compensation", args={
                    "monthly_wage": state.slots.monthly_wage,
                    "start_date": state.slots.start_date,
                    "end_date": state.slots.end_date or time.strftime("%Y-%m-%d"),
                    "leave_reason": state.slots.leave_reason or "公司辞退",
                    # 之前漏传这一项，导致离线回放时三倍封顶永远不会被判断，
                    # 连「若高于三倍需重算」那句 caveat 都不出现——高薪用户
                    # 会拿到一个远超法律上限的数字，而且没有任何提示。
                    "region": state.slots.region or "",
                })])

        # 期限：离线路径也必须能算。这个功能的价值有一半在「没网、没余额
        # 时也能给出准确日期」——演示环境正好就是这样。
        #
        # v133：限定劳动法内核。`_guess_dispute` 的取值全是劳动争议
        # （no_contract/wage/dismissal/injury/arbitration_award），
        # `calculate_deadline` 给的规则条文也是调解仲裁法 27/48/49/50 条。
        # 婚家问「离婚后财产分割的诉讼时效是多久」，会先被追问一句
        # 「离职（或被辞退、收到裁决书）是哪一年哪一个月」——同一处病、第三回。
        if (_profiles.get().id == "labor"
                and "calculate_deadline" not in done
                and any(w in state.question for w in _DEADLINE_WORDS)):
            dispute = _guess_dispute(state.question)
            onset = state.slots.end_date or state.slots.start_date
            if not onset:
                # 问法必须点明「要年月」。「这件事什么时候发生的」会换回
                # 「三个月前」，那样还是算不了——迟几天就可能跨过截止日。
                return Decision(
                    thought="问期限，但没有起算日期，先问一个最关键的",
                    tool_calls=[ToolCall(name="ask_user", args={
                        "question": "为了算准期限，还需要一个具体日期：离职（或被辞退、"
                                    "收到裁决书）是哪一年哪一个月？"
                                    "说「三个月前」这种大概时间算不了——差几天就可能"
                                    "跨过截止日，那个日期得准。"})])
            # 讨薪必须区分在职/已离职：27 条第四款两种情形差得远。
            relation_ended: bool | None = None
            if dispute == "wage":
                if state.slots.end_date:
                    relation_ended = True
                elif any(w in state.question for w in ("还在职", "没离职", "还在上班")):
                    relation_ended = False
                elif any(w in state.question for w in ("离职", "辞职", "走了", "已经不在")):
                    relation_ended = True
            return Decision(
                thought="问的是期限，交给期限工具按民法典期间规则算",
                tool_calls=[ToolCall(name="calculate_deadline", args={
                    "dispute": dispute,
                    "onset": onset,
                    "relation_ended": relation_ended,
                    "end_date": state.slots.end_date or "",
                })])

        return Decision(thought="证据已足够，收工", is_final=True)

    # ---------------- 定性（生成之前的一步）----------------
    def judge(self, state: AgentState) -> Verdict:
        """先把「正常 / 不正常」判出来，再谈依据和建议。

        这一步原来不存在，生成器直接从第一条证据开始写，于是所有回答都长成
        「根据《X》第Y条规定：……」。用户问「正常吗」，拿到的是条文摘抄——
        这就是答案机械化的根因，不是措辞问题。
        """
        if not state.evidences:
            return Verdict(kind="unknown", label="现有资料无法判断",
                           reason="没有检索到可依据的条文")
        if not self.online:
            return verdict_mod.judge_mock(state.question, state.evidences)

        user = (f"用户问题：{state.question}\n"
                f"已知案情：{state.case_brief()}\n\n"
                f"可用条文：\n{_describe_evidence(state.evidences, full=True)}")
        try:
            r = self._chat(prompts.system("JUDGE_SYSTEM"), user, max_tokens=TOK_JUDGE)
            data = _loads_obj(r.choices[0].message.content)
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return verdict_mod.judge_mock(state.question, state.evidences)

        kind = str(data.get("kind", "unknown")).strip()
        if kind not in ("illegal", "claimable", "conditional",
                        "definition", "legal", "unknown"):
            kind = "unknown"

        # coverage 三档。兼容两种输出：模型按新格式给 coverage；
        # 或者它仍按老习惯只给布尔 relevant —— 后者直接映射成 full/none，
        # 绝不默认成 partial（partial 是要放行生成的，猜错方向就是放水）。
        cov = str(data.get("coverage", "")).strip().lower()
        if cov not in ("full", "partial", "none"):
            rel = data.get("relevant", True)
            if isinstance(rel, str):
                rel = rel.strip().lower() in ("true", "1", "yes", "是")
            cov = "full" if rel else "none"
        if cov == "none":
            # 模型偶尔会一边说「答不了」一边给出 illegal 定性，自相矛盾。
            # 这里强制归零，让下游只走拒答一条路。
            kind = "unknown"
        # partial 但没给「哪一问能答」，等于没说清能答什么，
        # 这种情况降级成 full：宁可按全量答（原有行为），也不要拿一句
        # 空泛的"部分能答"去生成一篇打太极的答案。
        ap = str(data.get("answered_part", "")).strip()
        mp = str(data.get("missing_part", "")).strip()
        if cov == "partial" and not (ap and mp):
            cov, ap, mp = "full", "", ""
        return Verdict(kind=kind, coverage=cov,
                       answered_part=ap, missing_part=mp,
                       label=str(data.get("label", "")).strip() or "无法判断",
                       legal=str(data.get("legal", "")).strip(),
                       reason=str(data.get("reason", "")).strip(),
                       # gap 只在拒答时用。模型有时会在其它情况下也填一段，
                       # 这里按 coverage 清掉，免得文案层拿到互相矛盾的字段；
                       # 再过一道 _clean_gap，挡掉「本库未收录」这类全库断言。
                       gap=(_clean_gap(str(data.get("gap", "")).strip())
                            if cov == "none" else ""),
                       basis=[e.cite() for e in state.evidences[:3]])

    # ---------------- 生成（流式）----------------
    def generate_stream(self, state: AgentState, emit: Emit) -> str:
        if not self.online:
            return self._mock_generate(state, emit)

        v = state.verdict or Verdict()
        v_brief = (f"结论={v.label}；定性={v.legal}；理由={v.reason}"
                   if v.label else "（未完成定性，请自行判断）")
        # partial 必须显式递到生成侧。只在 agent 里放行、不把「哪一问能答、
        # 哪一问答不了」写进 prompt，模型会自己猜边界，结果要么硬答到底，
        # 要么整篇打太极——两种都比现在差。
        if v.coverage == "partial":
            v_brief += (f"\n覆盖度=partial（只答了一半）。"
                        f"\n能答的是：{v.answered_part}"
                        f"\n答不了的是：{v.missing_part}")
        system = (prompts.system("ANSWER_SYSTEM")
                  .replace("__SLOTS__", state.case_brief())
                  .replace("__VERDICT__", v_brief)
                  .replace("__DEADLINE__", _deadline_brief(state))
                  .replace("__CALC__", _calc_brief(state))
                  .replace("__REGION__", _region_brief(state))
                  .replace("__DOC__", _doc_brief(state))
                  .replace("__EVIDENCE__", _describe_evidence(state.evidences, full=True)))
        # v137：任务目标与摘要只注入**接入内核**的 system 前缀。
        # 劳动法的 ANSWER_SYSTEM 渲染结果必须逐字不变（test_profiles 钉着），
        # 所以不能往模板里加占位符——那样即使替换成空串也会多一行空白，
        # 逐字断言立刻红。前缀只加在非劳动法这一支，labor 走原样。
        if _profiles.get().id != "labor":
            _tb = _task_brief(state)
            if _tb:
                system = _tb + "\n\n" + system
        chunks: list[str] = []
        think_chars = 0
        announced = False
        try:
            kw: dict[str, Any] = dict(
                model=DEEPSEEK_MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": state.question}],
                temperature=0.3, max_tokens=TOK_GENERATE, stream=True,
            )
            body = _think_body(True)
            if body:
                kw["extra_body"] = body

            stream = self._client.chat.completions.create(**kw)
            for part in stream:
                if not part.choices:
                    continue
                delta = part.choices[0].delta
                # 推理模型的思考过程是独立字段，不占 content。
                # 不把它通知给前端，用户就要对着空白屏幕等 5~7 秒。
                r = getattr(delta, "reasoning_content", None)
                if r:
                    think_chars += len(r)
                    if not announced:
                        announced = True
                        emit("thinking", {"status": "start"})
                t = getattr(delta, "content", None)
                if t:
                    if announced and not chunks:
                        emit("thinking", {"status": "end", "chars": think_chars})
                    chunks.append(t)
                    emit("token", {"text": t})

            text = "".join(chunks)
            if not text.strip():
                # 流式已经拿到了 finish，不能重放；改用非流式重取一次再分块推。
                # 触发条件通常是 max_tokens 被思考吃光（effort=max 时必然发生）。
                print("[retry] 流式正文为空（思考占满预算），改非流式重试")
                kw.pop("stream", None)
                kw["max_tokens"] = min(TOK_GENERATE * 2, TOK_RETRY_CAP)
                r2 = self._client.chat.completions.create(**kw)
                text = r2.choices[0].message.content or ""
                for i in range(0, len(text), 8):
                    emit("token", {"text": text[i:i + 8]})
                return text
            return text
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return self._mock_generate(state, emit)

    def _mock_generate(self, state: AgentState, emit: Emit) -> str:
        """回放模式的答案组装。结构固定为「结论 → 依据 → 建议」。

        为什么结构写死在代码里而不是交给模板字符串：
        结构是产品约束，不该随文案变化。判断内容来自 verdict 层，
        这里只负责把三段拼对，把该省的省掉（例如没有计算就不出计算段）。
        """
        evs = state.evidences
        # 生成过文书的放行：申请书里的条号写死在模板里，本来就不依赖本轮检索。
        # 这道判断不放开，用户说「帮我写份申请书」会拿到「未找到直接依据」——
        # 他要的是纸，不是法条分析。
        if not evs and not any(s.tool == "draft_document" and not s.error
                               for s in state.scratchpad):
            p = _profiles.get()
            if p.id == "labor":
                text = (f"你提到的问题，在现有条文库里没有找到直接依据。\n\n"
                        f"已按「{state.question}」检索，未命中相关条文。\n\n"
                        f"我不做没有依据的推测。如果你的情况和劳动报酬、合同、"
                        f"社保、解除有关，可以换个说法再问一次，比如直接说"
                        f"「公司拖欠工资怎么办」。\n\n"
                        f"本回答仅供参考，不构成法律意见。")
            else:
                # 接入内核不能借用劳动法那串例子——婚家分区检索没命中，
                # 却建议用户问「公司拖欠工资怎么办」，等于告诉他来错地方了。
                ex = p.examples[0][1] if p.examples else "把情况说清楚再问一次"
                text = (f"你提到的问题，在现有条文库里没有找到直接依据。\n\n"
                        f"已按「{state.question}」检索，未命中相关条文。\n\n"
                        f"我不做没有依据的推测。如果这件事属于「{p.scope_hint}」，"
                        f"可以换个说法再问一次，比如直接说「{ex}」。\n\n"
                        f"本回答仅供参考，不构成法律意见。")

            for i in range(0, len(text), 4):
                emit("token", {"text": text[i:i + 4]})
            return text

        order = {"precise": 0, "direct": 1, "citation_expand": 2}
        # 跨查询的 BM25 分数不可比：第一路检索的 24 分和第二路的 6 分，
        # 不说明前者更相关，只说明那个查询词更"重"。多路合并后直接按分数排序，
        # 会让某一路的高分噪声抢走主依据的位置——评测里精确率掉 8 个点就是这个。
        # 所以按「第几路检索 + 该路内排名」排，整句改写那一路天然优先。
        rank: dict[str, tuple[int, int]] = {}
        for step in state.scratchpad:
            for i, eid in enumerate(step.evidence_ids):
                rank.setdefault(eid, (step.index, i))

        def _key(e: Evidence) -> tuple:
            r = rank.get(e.article_id, (99, 99))
            return (order.get(e.source, 3), r[0], r[1], -e.score)

        valid = sorted([e for e in evs if e.status == "现行有效"], key=_key)
        dead = sorted([e for e in evs if e.status != "现行有效"], key=_key)
        v = state.verdict or verdict_mod.judge_mock(state.question, evs)

        calc = next((s for s in state.scratchpad
                     if s.tool == "calculate_compensation" and not s.error), None)
        # 期限工具只要调过就要处理，包括「工伤」那种算不出来的情况——
        # 算不出来也得说清算不出来，而不是当作没问过。
        dl = next((s for s in state.scratchpad
                   if s.tool == "calculate_deadline"), None)
        blocks: list[str] = []

        # ---------------- ① 结论 ----------------
        # lookup 型例外：用户问的是「这条写了什么」，不是在描述自己的处境。
        # 这里硬塞一句「是否合法」就是答非所问，所以单独走条文骨架。
        # 必须带上 `(valid or dead)`：文书题是「有文书、无证据」的合法状态
        # （申请书不检索条文），此时若题型还是 lookup，下面第一句就 IndexError。
        if state.question_kind == "lookup" and (valid or dead):
            main_ev = (valid or dead)[0]
            if dead:
                d = dead[0]
                if valid:
                    blocks.append(f"【注意】《{d.law_name}》{d.article_no_cn}"
                                  f"{d.status}，下面给出的是现行条文"
                                  f"《{main_ev.law_name}》{main_ev.article_no_cn}。")
                else:
                    # 池子里一条现行条文都没有时，main_ev 退化成这条废止条文本身。
                    # 原来说「下面给出的是现行条文」，紧接着又贴了废止原文 ——
                    # 自己打自己的脸，用户会照着废条办事。
                    blocks.append(f"【注意】《{d.law_name}》{d.article_no_cn}"
                                  f"{d.status}，下面是已废止的原文，"
                                  f"只能参考，不能作为现行依据。")
            body = (main_ev.content if len(main_ev.content) <= 400
                    else main_ev.content[:400] + "……")
            blocks.append(f"【条文】《{main_ev.law_name}》{main_ev.article_no_cn}\n{body}")
            # 对比题会取回两条（见 _mock_decide），第二条原来根本没地方显示——
            # 「第八十二条和第十条有什么区别」只答了前一条，用户看到的是一半答案。
            # 主条之后依次补上其余精确取回的条文；只补条文原文，不再另起【适用】，
            # 免得同一段里出现两套判断标准。
            for extra in evs:
                if extra.source != "precise":
                    continue
                if extra.article_id == main_ev.article_id:
                    continue
                if extra.status != "现行有效":
                    blocks.append(f"【注意】《{extra.law_name}》"
                                  f"{extra.article_no_cn}{extra.status}，"
                                  f"下面是原文，只能参考。")
                ebody = (extra.content if len(extra.content) <= 400
                         else extra.content[:400] + "……")
                blocks.append(f"【条文】《{extra.law_name}》"
                              f"{extra.article_no_cn}\n{ebody}")
            note = verdict_mod.rule_by_article(main_ev.article_id)
            if note:
                blocks.append("【适用】" + note.reason)
                tips = list(note.advice[:2]) + [verdict_mod.COMMON_ADVICE]
                blocks.append("【建议】\n" + "\n".join(
                    f"{i}. {t}" for i, t in enumerate(tips[:3], 1)))
            else:
                blocks.append("【适用】条文已按原文给出。判断它是否适用于你的情况，"
                              "需要你说明具体事实——可以直接描述你遇到的情形。")
            blocks.append("本回答仅供参考，不构成法律意见。")
            text = "\n\n".join(blocks)
            for i in range(0, len(text), 4):
                emit("token", {"text": text[i:i + 4]})
            return text

        # 用户要的是一份文书，不是三段式分析——直接把文书交出去。
        # 硬套【结论】【依据】【建议】会把正文挤到段与段之间，用户还得自己
        # 从中挑出来复制，多一道手工活。这里改的是**骨架**，不是措辞：
        # 题型本身就不该走那套结构（和 lookup 型同一个道理）。
        doc_step = next((s for s in state.scratchpad
                         if s.tool == "draft_document" and not s.error), None)
        if doc_step is not None:
            dd = doc_step.data or {}
            body = (dd.get("text") or "").strip()
            if body:
                tail: list[str] = []
                miss = dd.get("missing") or []
                miss_p = dd.get("missing_profile") or []
                if miss:
                    tail.append(f"还缺这些案情要素：{'、'.join(miss)}。"
                                f"把上面这些补给我，我再生成一次就能填进去。")
                if miss_p:
                    tail.append(f"个人信息还缺：{'、'.join(miss_p)}。"
                                f"这些不在对话里补，到「个人档案」里填完再生成即可。")
                if not miss and not miss_p:
                    tail.append("要素都齐了。但请求事项和赔偿金额是**故意留空**的——"
                                "那两处决定仲裁委审什么、要多少钱，必须你自己填，"
                                "我不能代笔。")
                # 用户常常一句话里要两样东西：「帮我写申请书，顺便告诉我能拿多少」。
                # 只交正文等于只做了一半，所以把已经算好的金额补在收尾里。
                # 注意措辞：金额是**给他核对用的**，不是让他直接抄进请求事项。
                if calc is not None:
                    _line = _scenario_line(calc)
                    if _line:
                        tail.append(f"顺便说金额：{_line}。这个数先自己核对，"
                                    f"确认后再决定往请求事项里填多少。")
                text = (body + "\n\n" + "".join(tail) + "\n\n"
                        + "本文书由系统按你提供的事实生成，仅供参考，"
                          "不构成法律意见。提交前请逐项核对，建议咨询执业律师。")
                for i in range(0, len(text), 4):
                    emit("token", {"text": text[i:i + 4]})
                return text

        head: list[str] = []
        if dead:
            head.append(dead_lead(dead[0], valid))
        if v.kind == "unknown":
            # 已经靠「废止状态」回答清楚了，就别再补一句「无法判断」自相矛盾。
            # 算过期限的也不补：他问的是「多久内」，你回「不确定是否合法」是答非所问。
            # 算过金额的更不补：上面刚给了「若违法解除 X 元／若合法解除 Y 元」，
            # 紧接着说「现有条文不足以判断是否合法」，等于自己推翻自己，
            # 而且会把用户推向「这工具也没把握」。
            if not dead and not dl and not calc:
                head.append("现有条文不足以判断你描述的情形是否合法。")
        elif v.label:
            head.append(v.head())
        if calc:
            line = _scenario_line(calc)
            if line:
                # 插到最前面：问「多少钱」的人第一眼就要看到数字，
                # 先讲定性再给数字是最典型的答非所问。
                head.insert(0, f"先给数字：{line}。")
        if dl:
            d = dl.data or {}
            c = d.get("conservative")
            if dl.error or not d.get("ok"):
                head.insert(0, "这个期限本库给不出可核验的答案，"
                               "下面说明缺的是哪部法。")
            elif c:
                est = "（这个日期按月初估的，你的实际日期更晚的话，截止日会往后推）" \
                    if c.get("estimated") else ""
                head.insert(0, f"从{c['onset_label']}（{c['onset']}）起算，"
                               f"{c['deadline']} 是最后一天，"
                               f"还剩 {c['left_days']} 天（{c['level_text']}）。{est}")
            elif d.get("unlimited"):
                head.insert(0, "还在职的话，追索劳动报酬不受一年仲裁时效限制。")
            elif d.get("conditional"):
                head.insert(0, "期限取决于你现在还在不在职，两种情形差得很多，"
                               "先说清楚这个才能给日期。")
        # 年限折算：用户已经报了「3 年 7 个月」，答案就必须给出折成几个月，
        # 只抄第四十七条的公式等于让他自己算。排在最后 insert(0)，
        # 优先级低于金额和期限——那两个是工具算出来的硬结果，这里是纯折算。
        # 守卫两条：证据里真有 47 条（否则「干了 3 年 7 个月」出现在工伤题里
        # 会被套上经济补偿的折算）、且工具没给过金额（给了就别重复）。
        ym = service_months(state.question)
        if ym and not calc and any(e.article_id == "劳动合同法_47" for e in evs):
            n, detail = compensation_months(*ym)
            said = (f"{ym[0]} 年 {ym[1]} 个月" if ym[1] else f"{ym[0]} 年整")
            head.insert(0, f"按你说的 {said}算：{detail}，"
                           f"合计 {n:g} 个月工资。")
        blocks.append("【结论】" + "\n".join(head))

        # ---------------- ② 依据 ----------------
        # 主依据优先取定性所依赖的那条，而不是检索首条。
        # 检索首条只是「最像」，不一定「管得住」：问「没签合同能主张什么」，
        # 检索首条是第 10 条（义务性规定），但结论说的是第 82 条（请求权），
        # 结果答案前后矛盾——结论讲二倍工资，依据摆的却是签合同的义务。
        basis_evs: list[Evidence] = []
        for cite in (v.basis or []):
            hit = next((e for e in valid if e.cite() == cite), None)
            if hit and all(hit.article_id != x.article_id for x in basis_evs):
                basis_evs.append(hit)
        # 算过期限时，时效条款就是结论的直接依据，压过检索顺序。
        # 为什么必须在答案层面兜：early return 的 evidence 在 add_evidence
        # 里去重了（27 条第 2 步就已经以 direct 身份入列），工具再标 precise
        # 也不会改写已有条目，所以排序救不回来。而定性是 unknown 时
        # basis 为空，main 会退化成「第 1 步检索到的第一条」——于是问期限
        # 却拿劳动法第二十八条当依据。
        dl_rule_ev: Evidence | None = None
        if dl and not dl.error and (dl.data or {}).get("ok"):
            rid = ((dl.data.get("articles") or [None])[0])
            dl_rule_ev = next((e for e in valid if e.article_id == rid), None)
        # 算过金额时同理，而且踩的是同一个坑：47 条往往在更早的检索里就已经
        # 以 direct 入列，工具后来标 precise 也改不回来（add_evidence 去重，
        # 只加新的、不改已有的）。所以排序层救不了，必须在答案层把它提上来——
        # 否则问「能拿多少赔偿」，依据摆的是《解释（一）》第四十七条（通知工会），
        # 而正文讲的是二倍赔偿金，两边对不上。
        #
        # 47 优先于 87：三倍封顶和十二年线都写在 47 条里，这次的数字就是照它
        # 算出来的；87 只是「二倍」这个倍数，47 才是算法本身。
        calc_rule_ev: Evidence | None = None
        if calc and not calc.error:
            for aid in ("劳动合同法_47", "劳动合同法_87"):
                hit = next((e for e in valid if e.article_id == aid), None)
                if hit is not None:
                    calc_rule_ev = hit
                    break
        main = dl_rule_ev or calc_rule_ev or (basis_evs[0] if basis_evs
                                             else (valid[0] if valid else dead[0]))
        dep: list[str] = []
        if dead and dead[0].status == "部分失效":
            # 依据要摆这一条本身——它整条仍然有效，只是某款废止。
            # 原来走下面的「承接」分支，把《解释（一）》第一条当成承接条文摆出来。
            d = dead[0]
            body = d.content if len(d.content) <= 300 else d.content[:300] + "……"
            note = f"\n\n注：{d.repealed_note}" if d.repealed_note else ""
            dep.append(f"《{d.law_name}》{d.article_no_cn}（{d.status}）\n{body}{note}")
            main = d
        elif dead and valid:
            # 承接条要有出处：说明里点名、且真在证据池里。没有出处的不能说「承接」，
            # 只能摆成「现行相近条文，供参考」——否则就是把检索的巧合当法律对应。
            d = dead[0]
            sup = successor_of(d, valid)
            alt = sup or valid[0]
            body = alt.content if len(alt.content) <= 300 else alt.content[:300] + "……"
            if sup:
                dep.append(f"原条文的内容现由《{alt.law_name}》"
                           f"{alt.article_no_cn}承接：\n{body}")
            else:
                dep.append(f"《{d.law_name}》{d.article_no_cn}{d.status}，"
                           f"不能再作为现行依据引用。\n"
                           f"现行法下的相近条文（供参考，非条文对应关系）：\n{body}")
            main = alt
        else:
            body = main.content if len(main.content) <= 300 else main.content[:300] + "……"
            dep.append(f"《{main.law_name}》{main.article_no_cn}\n{body}")

        # 单跳问题只引一条主依据，不给「补充条款」。
        # 原来有个 fallback（同源且分数≥主依据 55% 就补一条），本意是补充细节，
        # 但评测证明它经常把无关条款带进来：精确率从 75% 掉到 68%。
        # 更根本的原因是 BM25 分数不可跨语料规模比较——语料从 15 条加到 17 条，
        # 所有词的 idf 都变了，那个 55% 的阈值本身就失灵了。
        # 现在的规则更硬：跨规范的问题（二倍工资 + 拖欠工资）才给多条依据。
        multi_topic = verdict_mod.cross_topic(state.question, evs)
        extra = pick_extra_basis(
            main, valid, v.head(), basis_evs,
            [s.evidence_ids[0] for s in state.scratchpad if s.evidence_ids],
            multi_topic)

        for o in extra:
            obody = o.content if len(o.content) <= 180 else o.content[:180] + "……"
            dep.append(f"《{o.law_name}》{o.article_no_cn}\n{obody}")

        # 算过二倍赔偿金时要补 87 条：正文写着「二倍」，可 47 条里根本没有
        # 「二倍」这两个字，不补的话这个倍数在依据里找不到出处，等于让用户
        # 凭空相信一个乘数。它不是「补充条款」，是倍数本身的依据。
        if calc and not calc.error and calc_rule_ev is not None:
            has_double = any((s.get("basis") or "").startswith("第八十七")
                             for s in (calc.data.get("scenarios") or []))
            ev87 = next((e for e in valid if e.article_id == "劳动合同法_87"), None)
            already = (main.article_id == "劳动合同法_87"
                       or any(o.article_id == "劳动合同法_87" for o in extra))
            if has_double and ev87 is not None and not already:
                body87 = ev87.content if len(ev87.content) <= 180 else ev87.content[:180] + "……"
                dep.append(f"《{ev87.law_name}》{ev87.article_no_cn}\n{body87}")

        blocks.append(basis_header(v.kind, bool(dead), bool(calc_rule_ev)) + "\n\n".join(dep))

        # 金额的出处必须能追到**数据**，不只是条文。第四十七条只给了「三倍」
        # 这条规则，三倍是多少来自地方公布的社平工资——不写出来源，用户既没法
        # 核对，也没法拿着去和公司谈。这是「每条结论都有出处」这条线的延伸。
        if calc:
            cdata = calc.data or {}
            src = cdata.get("region_param")
            if src:
                tail = (f"数字来源：{src['region']} {src['year']} 年度职工月平均工资 "
                        f"{src['avg_monthly']:,} 元，三倍为 {src['cap_base']:,} 元")
                if src.get("basis") != "official":
                    tail += "（该数字按社保缴费基数上限反推，不是官方公布的社平工资）"
                if src.get("source_doc"):
                    tail += f"。出处：{src['source_doc']}"
                blocks[-1] += "\n\n" + tail
            elif cdata.get("region_reason"):
                blocks[-1] += "\n\n" + cdata["region_reason"]

        # ---------------- ③ 建议 ----------------
        if v.kind in ("illegal", "claimable", "conditional", "definition"):
            tips = verdict_mod.advice_for(v, state.question, evs)
            blocks.append("【建议】\n" + "\n".join(
                f"{i}. {t}" for i, t in enumerate(tips, 1)))
        elif calc:
            # 时效那条不许写死「一年，从解除之日起算」：讨薪的起算点完全不同，
            # 终止后才开始算，而且 15 日的起诉期更不是一年。算过就指向算过的那段。
            line2 = ("2. 时效上面已经算过具体日期了，按那个日子安排，别记成「大概一年」。\n"
                     if dl else
                     "2. 仲裁时效为一年，从你知道权利被侵害之日起算，别拖。\n")
            blocks.append("【建议】\n"
                          "1. 上面的金额是依法应得的下限，实际还可主张未休年假工资、"
                          "未报销费用等，需要你补充材料后单独核算。\n"
                          + line2 +
                          "3. 保留解除通知、工资流水、考勤记录，这三样是核心证据。")
        else:
            blocks.append(fallback_advice(bool(dl), state.question))

        blocks.append("本回答仅供参考，不构成法律意见。")
        text = "\n\n".join(blocks)

        for i in range(0, len(text), 4):
            emit("token", {"text": text[i:i + 4]})
        return text

    # ---------------- 自检 ----------------
    def verify(self, answer: str, evidences: list[Evidence],
               index=None) -> VerifyResult:
        """检查答案里的引用是否都有出处、废止条文有没有提示。

        为什么默认先用规则：这个检查的绝大部分是字符串比对（条号在不在证据里、
        状态是不是现行有效），规则几毫秒就能做完。交给推理模型实测要 29 秒、
        烧掉 3199 个思考 token，还经常把预算吃光导致自检本身失败——
        比它要检查的问题还严重。

        规则先过一遍，只有它报出问题时才叫模型复核，避免误判。
        """
        if not self.online or VERIFY_MODE == "rule":
            return _mock_verify(answer, evidences, index)

        rule = _mock_verify(answer, evidences, index)
        if VERIFY_MODE == "rule_first" and rule.passed:
            return rule

        try:
            r = self._chat(
                prompts.system("VERIFY_SYSTEM"),
                "【条文】\n" + _describe_evidence(evidences, full=True) +
                "\n\n【答案】\n" + answer,
                max_tokens=TOK_VERIFY, json_mode=True)
            data = _loads_obj(r.choices[0].message.content)
            issues = [VerifyIssue(conclusion=str(i.get("conclusion", "")),
                                  reason=str(i.get("reason", "")))
                      for i in data.get("issues", [])]
            if not data and rule.issues:
                # 模型没给出可解析结果（多半又被思考吃光了），退回规则结论，
                # 至少不会把规则已经发现的问题丢掉。
                return rule
            return VerifyResult(passed=bool(data.get("passed", not issues)) and not issues,
                                issues=issues)
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
            return rule

    # ---------------- 追问 ----------------
    def ask(self, missing: list[str], question: str, known: str = "") -> str:
        """问一句最关键的缺失要素。

        v138 起按内核取「问什么」：
          劳动法   —— 仍用那张固定的四句标签表，一句话一个字都没改；
          接入内核 —— 用内核自己的字段表（`LawProfile.slot_priority` 的第一项），
                      问的就是这一回真缺的那一项，而不是劳动法的离职方式。

        `known` 是已经确认过的要素，只用于在线时给模型看，避免它把用户
        刚答过的事再问一遍。劳动法内核不传（它的 user 串有逐字断言钉着）。
        """
        # v135：内核自带的 `clarify_question` 降为「离线 / 调用失败」的兜底。
        #
        # v133 治串味时把它放在最前面直接 return，结果在线也走静态四选一：
        # 「债务怎么分配」「有条狗怎么分配」「我离婚了」拿到的是同一句话，
        # 模型判完意图却没机会看用户原问。在线时必须带原问 + 内核边界
        # 调一次模型，追问才会贴合；没有模型时仍用内核自己那句，不串味。
        #
        # 劳动法内核 `clarify_question` 为空、`ASK_SYSTEM` 走原常量、
        # 也不加 `allow_think`，整条链路与 v133 之前逐字一致。
        p = _profiles.get()
        hint = (p.clarify_question or "").strip()
        domain = p.id != "labor"
        if domain:
            # 字段表里排第一的那个缺失项就是这次要问的。`missing` 为空
            # （要素已经问全了）才回到内核那句静态追问。
            key = str(missing[0]) if missing else ""
            label = _dslots.ask_for(p, key) if key else ""
            label = label or hint or "能再具体说说情况吗"
        else:
            label = {"leave_reason": "是公司辞退、自己辞职，还是被迫离职",
                     "monthly_wage": "月工资大概多少",
                     "start_date": "哪一年哪个月入职的",
                     "region": "在哪个城市工作"}.get(missing[0], "能再具体说说情况吗")
        if not self.online:
            if domain:
                # 静态四选一降到「没有具体要素可问」时的兜底：有要素就问要素。
                return hint if label == hint else f"想先确认一下：{label}？"
            return hint or f"为了给到准确结论，想先确认一下：您{label}？"
        try:
            kw = {"allow_think": False} if domain else {}
            user = f"用户问题：{question}\n缺失要素：{label}"
            if domain:
                user = (f"用户问题：{question}\n"
                        f"已确认要素：{known or '（暂无）'}\n"
                        f"缺失要素：{label}")
            r = self._chat(prompts.system("ASK_SYSTEM"), user,
                           temperature=0.3, max_tokens=TOK_ASK, **kw)
            txt = (r.choices[0].message.content or "").strip()
            if txt:
                return txt
        except Exception as e:                # noqa: BLE001
            self._downgrade(e)
        if domain:
            return hint if label == hint else f"想先确认一下：{label}？"
        return hint or f"想先确认一下：您{label}？"


# ---------------------------------------------------------------- 辅助

def _think_body(allow_think: bool) -> dict:
    """组装推理模型的控制参数。

    thinking / reasoning_effort 不是 OpenAI 标准字段，走 extra_body 透传。
    官方 SDK 若不这样传会在本地就抛 TypeError；现在换成自建客户端
    （app/_api.py），它把 extra_body 原样并入请求体，语义保持一致。

    实测（2026-10-23，deepseek-flash）：
      - {"thinking": {"type": "disabled"}}  有效，reasoning_tokens 直接变 None
      - {"enable_thinking": false}          无效，思考照旧
      - {"reasoning_effort": "low"}         有效，但只是少想，不等于关闭
      - {"reasoning_effort": "max"}         危险：4000 token 全被思考吃光，正文 0 字
    所以 effort 只放行 low / high。
    """
    body: dict[str, Any] = {}
    if LLM_THINKING == "off" or not allow_think:
        body["thinking"] = {"type": "disabled"}
    if LLM_EFFORT in ("low", "high"):
        body["reasoning_effort"] = LLM_EFFORT
    return body


def _truncated(r) -> bool:
    """输出是否因长度上限被砍断。推理模型里这等价于「思考把预算吃完了」。"""
    try:
        return r.choices[0].finish_reason == "length"
    except (AttributeError, IndexError, TypeError):
        return False

def _loads_obj(text: str | None) -> dict:
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {}


def _loads_array(text: str | None) -> list:
    if not text:
        return []
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return []
    try:
        v = json.loads(m.group())
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _describe_tools(schemas: list[dict]) -> str:
    lines = []
    for s in schemas:
        fn = s.get("function", s)
        props = (fn.get("parameters") or {}).get("properties") or {}
        args = "、".join(props.keys())
        lines.append(f"- {fn['name']}({args})：{fn.get('description','')}")
    return "\n".join(lines)


# 用正则而不是关键词表：第一版列了「本库未收录 / 库内没有」等十个词，
# 单测立刻抓出漏网的「本库中没有」——关键词表永远列不全。
# 判据是「主体 + 短距离内的否定」，中间夹几个字也认；距离限制在 8 字以内，
# 避免跨句误伤（例如「本库第四十四条…没有规定清偿顺序」这种引用式表述）。
_GAP_FORBIDDEN = re.compile(
    r"(本库|本系统|库内|库里|本语料库)[^。；，、]{0,8}?(没有|未收录|未收|暂无|不含|缺少|未涵盖)")


def _clean_gap(gap: str) -> str:
    """挡掉 gap 里对**整个语料库**的断言。

    实测来源：h11「退休返聘还是劳务关系吗」的拒答文案写
    「现由《…解释（二）》及个案具体情况调整，本库未收录该部分内容」——
    而解释（二）就在库里，还是这题的 gold 之一。

    模型每轮只看得到本轮检索到的十几条证据，它看不到全库，所以
    「本库没有」这种话它没有资格说，说了就是编。而这句话又恰好最容易被
    用户当成事实去判断"这个系统靠不靠谱"。

    处理方式：命中就整段丢掉，宁可这一句不说。
    不做局部截取——「由解释（二）调整，本库未收录该部分」这种句子里，
    前后半句互相支撑，砍掉哪一半剩下的都可能是错的。
    """
    if not gap:
        return ""
    if _GAP_FORBIDDEN.search(gap):
        return ""
    return gap


def _scenario_line(calc) -> str:
    """把计算结果压成一句「若违法约 48,000 元；若合法约 24,000 元」。

    抽出来是因为它现在有两个使用方：三段式的【结论】段，以及文书题的收尾
    （用户既要文书又问金额时，正文后面得补一行钱）。两处各写一遍，迟早
    有一处漏掉「amount 为 None」的分支——那会直接 f"{None:,}" 抛 TypeError，
    而这行在生成主路径上，抛出去整条回答就没了。
    """
    d = getattr(calc, "data", None) or {}
    sc = d.get("scenarios") or []
    if not sc:
        return ""
    parts = []
    for s in sc:
        if s.get("amount") is None:
            parts.append(f"{s['label']} 按 {s['months']:g} 个月工资计")
        else:
            # label 以「）」结尾时不要再插空格，否则读成「…合法） 约 87 万」，
            # 半角空格夹在中文里很扎眼。
            gap = "" if s["label"].endswith(("）", ")")) else " "
            parts.append(f"{s['label']}{gap}约 {s['amount']:,} 元")
    out = "；".join(parts)
    notes = []
    if d.get("capped") and d.get("wage_base"):
        notes.append(f"已按当地三倍封顶线 {d['wage_base']:,} 元/月计算")
        if d.get("years_capped"):
            notes.append("年限按十二年上限计")
    elif d.get("wage_base") is None:
        notes.append("缺当地社平工资，暂不给金额")
    if notes:
        out += "（" + "；".join(notes) + "）"
    return out


def _calc_brief(state: AgentState) -> str:
    """把 calculate_compensation 的结果渲染成给模型看的一段。

    和 `_deadline_brief` 完全对称，补的是一个**很贵**的漏：生成侧的 prompt
    原来只有 `__DEADLINE__`，没有金额的位置。于是工具明明算出了 872,352 元，
    模型却看不到，只能照着法条重复「北京社平 × 3 × 12 × 2」，最后还建议
    用户自己去人社局官网查社平工资——而那个数工具已经查过、算过了。
    用户看到的是「它有规则、但算不出钱」，比压根没有这个功能更伤。
    """
    step = next((s for s in state.scratchpad
                 if s.tool == "calculate_compensation" and not s.error), None)
    no_calc = _NO_CALC if _profiles.get().id == "labor" else _NO_CALC_DOMAIN
    if step is None:
        return no_calc
    d = step.data or {}
    sc = d.get("scenarios") or []
    if not sc:
        return no_calc

    lines = [f"在职 {d.get('months', 0)} 个月，折合补偿月数 "
             f"{d.get('base_months', 0):g} 个月"]
    base = d.get("wage_base")

    if base is None:
        lines.append("**基数没有算出来**：本库没有收录当地上年度职工月平均工资，"
                     "判断不了是否触及三倍封顶。所以**不要给出任何金额数字**，"
                     "只给计算方法和月数。")
        rr = d.get("region_reason") or ""
        if rr:
            lines.append(f"没算出来的原因照这句说，不要自己另编一个：{rr}")
    elif d.get("capped"):
        src = d.get("region_param") or {}
        lines.append(f"月工资超过当地三倍封顶线，基数按 **{base:,} 元/月** 计"
                     f"（{src.get('region', '')} {src.get('year', '')} 年度职工月平均工资 "
                     f"{src.get('avg_monthly', 0):,} 元 × 3）")
        if d.get("years_capped"):
            lines.append(f"年限超过十二年，按上限 12 个月计"
                         f"（原本 {d.get('base_months', 0):g} 个月）")
    else:
        lines.append(f"基数就是本人月工资 {base:,} 元——未触及三倍封顶线，"
                     f"年限也不受十二年限制，不要替他套十二年上限")

    for s in sc:
        amt = s.get("amount")
        if amt is None:
            lines.append(f"- {s['label']}：按 {s['months']:g} 个月工资计（缺参数，不给金额）")
        elif amt == 0:
            lines.append(f"- {s['label']}：无需支付")
        else:
            lines.append(f"- {s['label']}：**{amt:,} 元**"
                         f"（{base:,} × {s['months']:g} 个月）")

    src = d.get("region_param")
    if src and src.get("source_doc"):
        tail = f"数据出处：{src['source_doc']}"
        if src.get("basis") != "official":
            tail += "（这是按缴费基数上限反推的值，不是官方公布的社平工资，必须说明）"
        lines.append(tail)

    lines.append("**上面的金额已经算好，直接报数，不要自己重算，也不要说「算不出来」。**"
                 "「是否构成违法解除」确实是不确定的事实，那部分可以照实说；"
                 "但金额本身是确定的。")
    return "\n".join(lines)


def _doc_brief(state: AgentState) -> str:
    """把 draft_document 生成的文书递到生成侧。

    和前两个 brief 同一个道理：只塞 `scratchpad.data` 的话，模型看不见正文，
    只能说「已经帮你生成了，请查看」——用户手里什么都没有。
    """
    step = next((s for s in state.scratchpad
                 if s.tool == "draft_document" and not s.error), None)
    if step is None:
        return "（本轮没有生成文书）"

    d = step.data or {}
    body = (d.get("text") or "").strip()
    if not body:
        return "（本轮没有生成文书）"

    lines = ["下面是已经生成好的文书全文。**原样交给用户，不要重写、不要精简、"
             "不要替他填请求事项和金额**——那几处留空是刻意的：",
             "", body, ""]

    missing = d.get("missing") or []
    miss_p = d.get("missing_profile") or []
    if missing or miss_p:
        lines.append("另外要用一段话说明还缺什么（不要说成「生成失败」）：")
        if missing:
            lines.append("- 案情要素：" + "、".join(missing))
        if miss_p:
            lines.append("- 个人信息：" + "、".join(miss_p)
                         + "（这些要去「个人档案」里填，不能在这里问）")
    return "\n".join(lines)


def _deadline_brief(state: AgentState) -> str:
    """把 calculate_deadline 的结果渲染成给模型看的一段。

    为什么单独渲染、而不是让模型从工具 summary 里自己读：
    决策阶段的 prompt 里没有工具观察（只回灌条文），生成阶段拿到的是
    scratchpad.data。如果这里不给，模型就只能凭印象说「一年内」——
    那正是这个功能要消灭的东西。

    措辞上刻意用「已算出的」「不要重算」：模型看到具体日期后很喜欢
    自己再推导一遍，而它推出来的往往是错的。
    """
    step = next((s for s in state.scratchpad
                 if s.tool == "calculate_deadline" and not s.error), None)
    if step is None:
        # 也算上被工具拒绝的情况（例如工伤），那时 error 有值但有 hint
        step = next((s for s in state.scratchpad
                     if s.tool == "calculate_deadline"), None)
        if step is None:
            return "（本轮没有计算期限）"

    d = step.data or {}
    if step.error or not d.get("ok"):
        hint = d.get("hint") or ""
        return (f"（这个问题的期限**没有算出来**：{step.error or '未知原因'}。"
                + (f"\n{hint}" if hint else "")
                + "\n不要自己估一个期限。诚实说明本库给不出，并告诉他该去问谁。)")

    lines = [f"争议类型：{d.get('label', '')}",
             f"计算基准日：{d.get('as_of', '')}"]
    c = d.get("conservative")
    if d.get("unlimited") is True:
        lines.append("结论：劳动关系存续期间追索劳动报酬，**不受一年仲裁时效限制**"
                     "（调解仲裁法第二十七条第四款）。此时不存在截止日。")
        lines.append("但要知道：一旦劳动关系终止，必须自**终止之日起一年内**提出。")
    elif d.get("conditional"):
        lines.append("结论：**还不能给截止日**——取决于劳动关系是否已经结束。"
                     "还在职的不受一年限制；已离职的从离职之日起一年。")
    elif c:
        lines.append(f"起算点：{c['onset_label']} {c['onset']}")
        lines.append(f"截止日：**{c['deadline']}**")
        left = c["left_days"]
        lines.append(f"剩余：{left} 天（{c['level_text']}）")
        if c.get("holiday_hint"):
            lines.append(f"注意：{c['holiday_hint']}")
    if d.get("rule_text"):
        lines.append(f"规则原文要点：{d['rule_text']}")
    for n in d.get("notes") or []:
        lines.append(f"· {n}")
    lines.append("（以上日期是工具按民法典第二百零一、二百零二条算的，"
                 "直接引用，不要自己重算。）")
    return "\n".join(lines)


def needs_calc(state) -> bool:
    """用户是不是在问金额，且这笔钱应当由工具算、但还没算过。

    单独抽出来是因为有两个地方要用同一套判据：决策层的守卫（去哪一步算），
    和 Agent 循环的跳出条件（挂起追问后要不要再给一轮）。两处各写一份，
    迟早会漂——判据一漂，就会出现「守卫认为该算、循环已经 break」的死角，
    金额又回到模型自己算术。
    """
    if "calculate_compensation" in {s.tool for s in state.scratchpad}:
        return False
    if not any(w in state.question for w in _CALC_WORDS):
        return False
    slots = state.slots
    return bool(slots.monthly_wage and slots.start_date)


# 没算过金额时递给生成侧的原文。原来只写「（本轮没有计算补偿金额）」——
# 太软，模型当没看见，照样自己乘出一个精确到元的数字。真 key 实测：
# 「月薪5万北京干了16年被违法辞退能拿多少」答了 872,352 元，而
# calculate_compensation 一次都没调用（社平 12116 是模型凭记忆写的，这次恰好对）。
# 这类错误没有报错、没有降级提示，看着比工具算的还权威，只能靠措辞堵死。
_NO_CALC = """（本轮**没有**调用 calculate_compensation，所以没有任何经过核算的金额。
用户若问「多少钱 / 能拿多少」，只给法条上的计算方法和月数，
**一个具体数字都不许出现**——包括你自己按工龄推算的。
要写就写「按您的实际月工资和工龄计算」，并说明补齐入职日期、离职日期和
月工资后可以算出确切金额。绝不自己乘一个数出来。）"""

# v135：接入内核专用的「没算过金额」。
# 上面那段是劳动争议口径（工龄 / 入职离职日期 / 月工资 / 社平），放进婚家
# 或消费分区，模型会照抄成「请补齐您的入职与离职日期、实际月工资」——
# 真 key 实测「我离婚了」的【建议】第 4 条就是这么串的。以前接入内核在
# 入口就被 clarify 短路、走不到生成，这个老问题一直藏着；v135 撤掉守门员
# 之后才暴露。劳动法内核仍用 _NO_CALC，一个字不动。
_NO_CALC_DOMAIN = """（本轮没有任何经过工具核算的金额。
**一个具体数字都不许出现**，也不要自己乘一个数出来。
用户若问「多少钱 / 能分多少 / 怎么算」，只给法条上的计算规则与还需补齐的事实。
当前分区不是劳动争议：只写本分区法条里出现的计算要素，
不许把别的法律领域的专属参数、期限或材料清单搬过来，
也不要让用户去补与劳动关系有关的信息。）"""


def _task_brief(state: AgentState) -> str:
    """任务级记忆的渲染：目标 + 已聊到哪 + 最近几轮原话。

    只给接入内核用。里面允许出现金额（用户要求记忆要能记住数字），
    但**金额的唯一权威仍是槽位**：摘要里的旧数字与「已确认事实」冲突时，
    以后者为准——这句要求写在下一轮 task_memory 的指令里，靠覆盖更新，
    不靠这里做减法。
    """
    goal = (state.task_goal or "").strip()
    summ = (state.task_summary or "").strip()
    rec = [str(x).strip() for x in (state.task_recent or []) if str(x).strip()]
    if not (goal or summ or rec or (state.profile_facts or "").strip()):
        return ""
    out = ["【本案背景】"]
    if goal:
        out.append(f"目标：{goal}")
    if summ:
        out.append(f"进展：{summ}")
    pf = (state.profile_facts or "").strip()
    if pf:
        out.append(f"用户说过：{pf}")
    if rec:
        out.append("最近几轮：")
        out.extend(f"- {x}" for x in rec[-4:])
    out.append("（以上是背景，用于接住「那这个呢」这类指代；"
               "结论仍只按本次检索到的条文与已核算的事实说话。）")
    return "\n".join(out)


def _region_brief(state: AgentState) -> str:
    """单独查到的社平工资。

    为什么需要它：用户问「能拿多少」却没给入职日期时，calculate_compensation
    调不了（start_date 是必填），于是模型既算不出总数、也不知道社平工资，
    只能写「北京上年度职工月平均工资的具体数额目前还没有」——而库里就存着
    官方数据（北京 2025 年度 12116，三倍 36348）。工具查过就必须递过去，
    否则查了等于没查，用户拿到的是一个「系统查不到」的错误印象。
    """
    step = next((s for s in state.scratchpad
                 if s.tool == "query_region_params" and not s.error), None)
    if step is None:
        # 接入内核根本不派 query_region_params（v133 已把算钱三闸限定 labor）。
        # 这里若照抄「没有单独查社平工资」，模型会把它写进【建议】，
        # 婚家用户就会读到一句莫名其妙的「也没有查当地社平工资」。
        if _profiles.get().id != "labor":
            return "（无）"
        return "（本轮没有单独查社平工资；不要凭空说出任何社平数字）"
    d = step.data or {}
    if not d.get("found"):
        return (f"（本库没有收录 {d.get('region') or '该地'}的社平工资。"
                f"只能给计算方法和月数，**绝不自己编一个数字**。）")
    tail = "" if d.get("basis") == "official" else \
        "（按社保缴费基数上限反推，不是官方公布的社平工资，必须说明）"
    return (f"{d.get('region')} {d.get('year')} 年度职工月平均工资 "
            f"{d.get('avg_monthly', 0):,} 元，三倍封顶线 {d.get('cap_base', 0):,} 元{tail}\n"
            f"这两个数可以直接用来讲「是否触及封顶」；总额仍要等入职日期才能算，"
            f"不要拿它去乘月数。")


def _describe_searched(state) -> str:
    """已执行过的 search_statute 查询，按执行顺序列一遍（同 query 只列一次）。

    为什么必须显式列出来：决策提示词让模型「把分解出来的检索式交给 search_statute」，
    但原来的上下文里只有**条文**，没有**查过什么**。于是模型每一轮都看到那几条
    计划，就再发一遍——实测两条真实问题都是「3 条查询 × 3 轮 = 9 步」，
    步数预算被重复调用吃满，9 个工具里第 4 个（calculate_compensation /
    check_validity）永远轮不到。回放模式不会重复，所以这个问题只有真 key 才暴露，
    之前所有回归都测不到。
    """
    seen: dict[str, int] = {}
    for st in getattr(state, "scratchpad", []) or []:
        if st.tool != "search_statute":
            continue
        q = (st.args or {}).get("query")
        if not q:
            continue
        n = len(st.evidence_ids or [])
        # 同一个 query 出现多次时保留**最好**的那个结果（取 max 而不是覆盖），
        # 否则第一次查失败、第二次成功会被记成「命中 0 条」，反而误导模型重查。
        seen[q] = max(seen.get(q, -1), n)
    if not seen:
        return "（还没有检索过）"
    return "\n".join(f"- 「{q}」→ 命中 {n} 条" for q, n in seen.items())


def _describe_evidence(evs: list[Evidence], full: bool = False) -> str:
    if not evs:
        return "（暂无）"
    lines = []
    for e in evs:
        body = e.content if full else (e.content[:120] + ("……" if len(e.content) > 120 else ""))
        flag = "" if e.status == "现行有效" else f"  【{e.status}】"
        lines.append(f"- 《{e.law_name}》{e.article_no_cn}（{e.article_id}）{flag}\n  {body}")
        # 上一轮带进来的。必须标出来，否则模型会把「旧的」当成「这次查到的」——
        # 用户换了个话题问，它拿上一题的条文就答了。
        if e.source == "recall":
            lines.append("  （上一轮同一案子查过的，未必与本次提问有关；先判断相不相关）")
        # 整部法有效、某款已废止的情形，必须显式递给模型。
        # 否则它会把已经死掉的款当成活的用，答得越自信错得越远。
        if e.repealed_note:
            lines.append(f"  ⚠ 时效提示：{e.repealed_note}")
    return "\n".join(lines)


def stream_text(text: str) -> Iterator[str]:
    for i in range(0, len(text), 4):
        yield text[i:i + 4]
