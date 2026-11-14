# -*- coding: utf-8 -*-
"""
引用解析：把答案正文里的「法律名 + 条号」解析成库里唯一的那一条。

这个模块只做一件事，但必须是**唯一**做这件事的地方。产品层的
`agent._backfill_citations`（补核验）和度量层的 `eval.run_eval`（算引用合法
率、聚焦度）都从这里取，避免两边各写一份正则。两边分裂过一次，后果是：
评测按更严的标准抽出 122 处引用并宣布全部合法，产品层却只核验了其中一部分
——指标在替产品兜底，报告比实际能力好看。

为什么一个 `《X》第N条` 的正则不够用：

  1. 承前省略书名号  「《劳动合同法》第四十四条…；第四十六条第五项…」
  2. 裸法律名        「按劳动合同法第三十条」
  3. 序数词干扰      「这是第二条绕道」「这是第一条建议」
  4. 法律名写法不一  「解释一」「解释（一）」「司法解释一」都指同一部
  5. 条号跨法误归    「《实施条例》第二十二条…按第四十七条支付补偿」
                     实施条例只有 38 条，这里的 47 条显指《劳动合同法》
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .retrieval import cn2int

if TYPE_CHECKING:
    from .retrieval import LawIndex


# --------------------------------------------------------------------------
# 一、法律名归一与判等
# --------------------------------------------------------------------------
def norm_law_key(s: str) -> str:
    """法律名归一：去「中华人民共和国」、去各类括号。

    为什么必须去括号：模型写简称时经常写成「《解释（一）》」，也存在写成
    「《解释一》」的；库里存的全称是「…的解释（一）」。不去括号的话，
    「解释一」既不等于也不包含于全称，合法引用会被判成幻觉条号。
    """
    s = s.replace("中华人民共和国", "").strip()
    # 「司法解释（一）」与「解释（一）」是同一部法。模型两种写法都会用，
    # 不归一的话，同一条会被当成两处引用，去重和判等都会失效。
    s = s.replace("司法解释", "解释")
    for ch in "（）()［］[]【】《》":
        s = s.replace(ch, "")
    return s.strip()


# 短名后面接这些词，说明是另一部法而不是同一部法的全称。
# 《劳动合同法》与《劳动合同法实施条例》条号 1–38 完全重叠，混起来会张冠李戴。
_LAW_SUFFIX_BLOCK = ("实施", "条例", "办法", "规定", "细则", "决定", "解释", "通知")


def law_same(a: str, b: str) -> bool:
    """两个（已归一的）法律名是否指向同一部法。"""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if short not in long_:
        return False
    rest = long_[long_.index(short) + len(short):]
    return not rest.startswith(_LAW_SUFFIX_BLOCK)


# --------------------------------------------------------------------------
# 二、条号解析：法律名 + 条号 → 库里那一条
# --------------------------------------------------------------------------
def find_article(index: "LawIndex", law: str, no: str) -> dict | None:
    """按「法律名 + 条号」严格取条，不做条号兜底。

    为什么不能直接用 index.get_article：它有一步「条号唯一时忽略法律名」的
    兜底。答案引《民法典》第六十二条而库里没有民法典时，兜底会把《劳动法》
    第六十二条当成它，于是「补核验」反而补进来一条不相干的条文。
    """
    n = cn2int(no)
    if not n:
        return None
    key = norm_law_key(law)
    cands = [a for a in index.articles if a["article_no"] == str(n)]
    # 一轮：法律名完全一致（避免「劳动合同法」吃掉「劳动合同法实施条例」）
    for a in cands:
        full = norm_law_key(a["law_name"])
        if key in (norm_law_key(a["law_short"]), full):
            return a
    # 二轮：简称/全称互相包含（「解释一」↔「…的解释（一）」）
    for a in cands:
        full = norm_law_key(a["law_name"])
        short = norm_law_key(a["law_short"])
        if law_same(key, full) or law_same(key, short):
            return a
    return None


# --------------------------------------------------------------------------
# 三、从正文里抽引用
# --------------------------------------------------------------------------
_BARE_CITE = re.compile(r"第([零一二三四五六七八九十百千\d]+)条")
_LAST_LAW = re.compile(r"《([^》]{2,40})》")
# 只有像法律名的书名号才算「当前法律」：《解除竞业限制协议通知书》不是法律，
# 拿它当主语会把后面的第三十八条整条记错。
_IS_LAW = re.compile(r"(法|法典|条例|规定|办法|细则|解释[（(]?[一二三四五]?[）)]?)$")
# 「按劳动合同法第三十条」——作者把法律名写成了不带书名号的裸名。
# 此时不能沿用上一个《》的法律，否则劳动合同法第三十条会被记到解释（一）头上。
# 结尾不能只认「法」：模型也会写「按实施条例第二十七条」「依调解仲裁法第五条」。
_BARE_LAW = re.compile(
    r"(司法解释[一二三四五]|[\u4e00-\u9fa5]{2,15}(?:法典|条例|规定|办法|细则|法))\s*$")
# 「这是第二条绕道」「这是第一条建议」——序数词，不是引用。
# 这类误伤只出现在小条号上，所以只对第 1–3 条做严格判定；
# ≥4 条的裸提及基本都是真引用（没人会说「这是第七条路」）。
_ORDINAL_STOP = ("路", "建议", "意见", "途径", "绕道", "方案", "选择", "思路", "点")
_CITE_TAIL_TIGHT = ("第", "款", "项", "的", "之", "规定", "适用", "是",
                    # 句读与换行也算「引用完了」：答案里最标准的写法就是
                    # 「【依据】《解释（二）》第三条」换行接正文，条号后面直接是
                    # 换行。原来只认上面那几个字，于是第三、四版答案里的第 1–3 条
                    # 全被当成序数词丢掉——e10 明明写了依据，自检却报
                    # 「答案未标注任何条文依据」，可溯源也白算。
                    "。", "；", "，", "、", "：", "）", ")", "\n", "\r", " ", "　")
# 「第三十九条或第四十条」——同一句里的并列条号必然归同一部法。
# 若前一个没有明确归属、后一个已经带法律名出现过，就跟后者走。
# 实例：h03「《解释（一）》第四十七条规定，解除符合第三十九条或第四十条规定…
# 没有事先通知工会」——这里的 39/40 指《劳动合同法》，不是解释（一）自己的
# 第三十九条（那一条讲竞业限制协议解除，与本事无关）。
_COORD = re.compile(r"^[或和、及至与～~\-]\s*第([零一二三四五六七八九十百千\d]+)条")
# 「按劳动合同法第三十条」里的「按」不属于法律名，不去掉会导致去重失效。
# 「是」来自「依据**是**调解仲裁法第二十一条」——这个字会被 _BARE_LAW 一起抓进
# 法名，抽出来就变成《是调解仲裁法》。解析条文不受影响（find_article 有模糊匹配），
# 但它会原样印进给用户看的核对提示里。
_LAW_PREFIX = ("按", "依照", "依据", "根据", "按照", "参照", "适用", "据", "及", "与",
               "和", "对", "在", "属于", "是")


def _strip_prefix(law: str) -> str:
    """去掉「按/依据/根据」这类动词前缀——它们不属于法律名。

    只剥一次不够：「依据是调解仲裁法」先剥掉「依据」，还剩下「是调解仲裁法」。
    所以循环剥到剥不动为止。留一位保护，别把法律名剥成空串。
    """
    changed = True
    while changed:
        changed = False
        for p in _LAW_PREFIX:
            if law.startswith(p) and len(law) > len(p) + 1:
                law = law[len(p):]
                changed = True
                break
    return law


def _iter_cites(text: str, window: int, index: "LawIndex | None"):
    """逐处产出 (法律名, 条号原文, 起点, 终点)。extract_cites 与
    extract_cites_spans 共用这一份判定逻辑，避免又分裂成两个实现。

    终点取到「第N条」三个字结束，不含后面的「第几款/第几项」——判定条文是否被
    实质使用时，这些限定语属于正文的一部分，不能让引用自己算作证据。
    """
    last_law = ""
    last_pos = -10 ** 9
    # 条号 → 上文里明确归属过的法律名。
    # 用途见下面「同号回溯」：裸提的条号若本段已带法律名出现过，指的就是它。
    seen_article: dict[int, str] = {}
    for m in _BARE_CITE.finditer(text):
        pos = m.start()
        n = cn2int(m.group(1)) or 0
        nxt = text[m.end():m.end() + 2]
        if n <= 3:
            # 小条号才需要分辨「引用」还是「序数词」
            if any(p in text[max(0, pos - 6):pos] for p in ("这是", "那是")):
                continue                  # 「这是第二条绕道」「这是第一条建议」
            if nxt and not nxt.startswith(_CITE_TAIL_TIGHT):
                continue
            # 条号落在文末（nxt 为空）也算引用，不能因为后面没字就丢掉。
        elif nxt[:1] and any(nxt.startswith(s) for s in _ORDINAL_STOP):
            continue
        before = text[max(0, pos - window):pos]
        laws = [m2 for m2 in _LAST_LAW.finditer(before) if _IS_LAW.search(m2.group(1))]
        # 紧贴才算归属明确：「《X》第N条」中间只允许空白。
        # 隔了一段话再提的裸条号属于「承前」，它可能指上文另一部法——
        # 只看「前面有书名号」会把「…《民法典》第五百八十五条…；先过第二十五条
        # 这道门槛」里的 25 条记到民法典头上。
        tight = False
        if laws:
            lm = laws[-1]
            base = max(0, pos - window)
            last_law = lm.group(1)
            last_pos = base + lm.start()
            tight = not text[base + lm.end():pos].strip()
        # 裸法律名优先于上一个书名号：「按劳动合同法第三十条」
        bare = _BARE_LAW.search(text[max(0, pos - 20):pos])
        bare_law = _strip_prefix(bare.group(1)) if bare else ""
        # 光长得像法律名不够：「根据这个规定第三条」里的「这个规定」不是法律。
        # 判据是能不能真解析出条文——解析不出来就退回承前，别凭空造一个法律名
        # 出来（那会让这条引用变成假幻觉）。
        if bare_law and (index is None
                         or find_article(index, bare_law, m.group(1)) is not None):
            law = bare_law
            explicit = True
        elif tight:
            law = last_law                # 《X》第N条，紧贴，归属明确
            explicit = True
        else:
            # 同号回溯：本段里这个条号已经带法律名出现过，裸提指的就是它。
            # 实例：「《劳动合同法》第二十五条…；…《民法典》第五百八十五条…
            # 在劳动关系里要先过第二十五条这道门槛」——这个 25 条仍是劳动合同法
            # 的。没有这一步会凭空补出一条《民法典》第二十五条（讲自然人住所，
            # 和违约金毫无关系），把「多核验一条」变成「多核错一条」。
            law = seen_article.get(n)
            if law is None:
                # 再退一步：同句并列的条号归属一致（「第三十九条或第四十条」）
                co = _COORD.match(text[m.end():m.end() + 8])
                if co:
                    law = seen_article.get(cn2int(co.group(1)) or 0)
            law = law or last_law
            explicit = False
        if not law:
            continue
        if "本法" in law:                 # 「且劳动者没有本法第三十九条」
            law = last_law or law.replace("本法", "")
        if not explicit and n not in seen_article and pos - last_pos > window:
            continue                      # 靠承前猜的，离得太远就不敢认
        # 存在性回退：这条在当前归属的法里不存在，就到上文出现过的法里找。
        # 往回看的范围必须覆盖**整段前文**，不能只掐 300 字：实测一段答案里先引
        # 《劳动合同法实施条例》第十四条，几十字后写「依据第三十九条、第四十条
        # 解除你之前没有事先通知工会」——这两条指《劳动合同法》，但离得最近的
        # 书名号是实施条例，而实施条例只有 38 条。窗口太小时回退够不到前面出现
        # 过的《劳动合同法》，两处合法引用就被算成幻觉（v16 的 m02）。
        # 只在「当前法没有这一条」时才走到这里，所以放宽窗口不会误伤正常归属；
        # 而且按倒序找，仍然优先取离得近的那个法。
        if index is not None and find_article(index, law, m.group(1)) is None:
            cands = [m2.group(1) for m2 in _LAST_LAW.finditer(text[:pos])
                     if _IS_LAW.search(m2.group(1))]
            for cand in reversed(cands):
                if cand != law and find_article(index, cand, m.group(1)):
                    law = cand
                    break
        # 归属明确的引用记进来，供后面的裸提回溯（后出现的覆盖先出现的）
        if explicit:
            seen_article[n] = law
        yield law, m.group(1), pos, m.end()


def extract_cites(text: str, window: int = 160,
                  index: "LawIndex | None" = None) -> list[tuple[str, str]]:
    """抽出答案里的「法律名 + 条号」引用，支持承前省略书名号。

    三种写法都要认：
      1. 《劳动合同法》第四十六条            ← 书名号
      2. 《劳动合同法》第四十四条…；第四十六条  ← 承前省略书名号
      3. 按劳动合同法第三十条                ← 裸法律名（无书名号）
    同时要挡住「这是第二条绕道」这类普通措辞。

    传了 index 时会做「存在性回退」：承前省略的条号若在前一部法里根本不存在，
    就回到上文出现过的其他法律里找那一条。实例：「《劳动合同法实施条例》第二
    十二条规定…也要按第四十七条支付补偿」——实施条例只有 38 条，这里的第
    四十七条显然指《劳动合同法》第四十七条。

    返回 [(法律名, 条号原文)]，已按解析到的条文 id 去重（解析不出来的按名字去重）。
    """
    return dedup_by_id([(law, no) for law, no, _, _ in _iter_cites(text, window, index)],
                       index)


def extract_cites_spans(text: str, index: "LawIndex | None" = None,
                        window: int = 160) -> list[dict]:
    """同 extract_cites，但带上每处引用在原文里的位置。

    用途只有一个：判断「这个条号在正文里到底有没有被用起来」。要判这件事，
    必须知道条号出现在哪，才能取它前后那段解释性文字；光有 (法律名, 条号)
    是判不了的。
    """
    out, seen = [], set()
    for law, no, start, end in _iter_cites(text, window, index):
        art = find_article(index, law, no) if index is not None else None
        key = art["id"] if art else (norm_law_key(law), cn2int(no))
        if key in seen:
            continue
        seen.add(key)
        out.append({"law": law, "no": no, "start": start, "end": end,
                    "id": art["id"] if art else None, "article": art})
    return out


def dedup_by_id(cites: list[tuple[str, str]],
                index: "LawIndex | None" = None) -> list[tuple[str, str]]:
    """同一部法可能写成全称/简称/裸名，抽出来是不同字符串，但指向同一条。
    去重按解析到的条文 id 走，解析不出来的才退回按名字比。"""
    seen, out = set(), []
    for law, no in cites:
        art = find_article(index, law, no) if index is not None else None
        key = art["id"] if art else (norm_law_key(law), cn2int(no))
        if key in seen:
            continue
        seen.add(key)
        out.append((law, no))
    return out


def resolve_cites(text: str, index: "LawIndex",
                  known_ids: set[str] | frozenset[str] = frozenset()
                  ) -> tuple[list[dict], list[str]]:
    """把答案里的引用解析成条文对象。

    返回 (`库里存在、且不在 known_ids 里的条文列表`, `库里查不到的引用标签`)。
    后者的存在本身就是一种失败信号——那是模型编出来的条号，不该被吞掉。
    """
    extra: list[dict] = []
    phantom: list[str] = []
    for law, no in extract_cites(text, index=index):
        art = find_article(index, law, no)
        if art is None:
            label = f"《{law}》第{no}条"
            if label not in phantom:
                phantom.append(label)
            continue
        if art["id"] in known_ids:
            continue
        if any(e["id"] == art["id"] for e in extra):
            continue
        extra.append(art)
    return extra, phantom
