# -*- coding: utf-8 -*-
"""
检索层。基础版用纯 Python BM25，零依赖、零模型下载、开箱即跑。

为什么基础版不上向量检索：
  条文体量在万级以内时，BM25 + 章节路径 + 中文 bigram 已经能拿到
  可用的召回率，而且启动只需毫秒。等语料量上去、或改成语义化问法
  之后，再把 RETRIEVAL_BACKEND 切成 embedding——业务代码不用动。

换后端时要遵守的契约：
  LawIndex.search(query, topk, only_valid, law_hint) -> list[(score, article_dict)]
  LawIndex.get_article(law_name, article_no) -> article_dict | None
  LawIndex.expand(article_id) -> list[article_dict]
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import CORPUS_PATH, DOMAIN_CORPUS_PATH, GENERIC_BOOST, GENERIC_LAWS

_CN_NUM = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2int(s: str) -> int:
    """中文数字转整数。'八十二' -> 82。法律条号必须是精确的，不能近似。"""
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    total = section = num = 0
    for ch in s:
        if ch in _CN_NUM:
            num = _CN_NUM[ch]
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            if u == 10000:
                section = (section + num) * u
                total += section
                section = 0
            else:
                section += (num or 1) * u
            num = 0
    return total + section + num


def tokenize(text: str) -> list[str]:
    """中文按字符二元组切，英文数字按词切。

    为什么不用 jieba：基础版要零依赖。中文 bigram 在短文本检索上
    已经够用（法律条文措辞固定，重复率高），而且不会因为切错词丢命中。
    后续接 jieba 只需替换这一个函数。
    """
    tokens: list[str] = []
    for m in re.finditer(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text):
        seg = m.group()
        if seg[0].isascii():
            tokens.append(seg.lower())
        elif len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs: list[Counter] = []
        self.lens: list[int] = []
        df: Counter = Counter()
        for d in docs:
            toks = tokenize(d)
            tf = Counter(toks)
            self.tfs.append(tf)
            self.lens.append(len(toks))
            for w in tf:
                df[w] += 1
        self.n = len(docs)
        self.avgdl = (sum(self.lens) / self.n) if self.n else 0.0
        # 用 log(1 + ...) 保证 idf 恒正，避免常见词贡献负分
        self.idf = {w: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for w, c in df.items()}

    def search(self, query: str, topk: int = 5) -> list[tuple[float, int]]:
        qt = tokenize(query)
        if not qt or not self.n:
            return []
        scores: list[tuple[float, int]] = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            dl = self.lens[i] or 1
            for w in qt:
                f = tf.get(w)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += self.idf.get(w, 0.0) * f * (self.k1 + 1) / denom
            if s > 0:
                scores.append((s, i))
        scores.sort(key=lambda x: x[0], reverse=True)
        return scores[:topk]


class LawIndex:
    def __init__(self, corpus_path: str | Path,
                 extra_path: str | Path | None = None,
                 laws: frozenset[str] | set[str] | None = None,
                 boost_laws: frozenset[str] | set[str] | None = None,
                 boost: float | None = None):
        """`laws` / `boost` 是 v119 内核分库用的，**绑在索引实例上**。

        为什么绑实例而不是给 search() 加参数：tools.py 里十来处
        `index.search(...)` 都是老签名，加参数就得逐个改，任何一处漏传
        都会让劳动法内核悄悄换一套加权。绑实例之后，server 只要换一个
        index 对象交给 runner，整条工具链自动跟着换，老调用一行不改。

        默认值严格等于 v118：`laws=None` 不按法律名过滤、`boost_laws`
        取 GENERIC_LAWS、`boost` 取 GENERIC_BOOST。所以劳动法内核的
        BM25 语料集合、IDF、分数逐位不变——这是 31 套件能继续全绿的前提。
        """
        self.path = Path(corpus_path)
        self.extra_path = Path(extra_path) if extra_path else None
        self._laws = frozenset(laws) if laws is not None else None
        self._boost_laws = (frozenset(boost_laws) if boost_laws is not None
                            else frozenset(GENERIC_LAWS))
        self._boost = GENERIC_BOOST if boost is None else boost
        self.articles: list[dict[str, Any]] = []
        self.by_id: dict[str, dict] = {}
        self.by_law: dict[str, list[dict]] = defaultdict(list)
        self.citation_map: dict[str, list[str]] = {}
        self._load()
        self._build_citation_graph()
        self.bm25 = BM25([self._doc_text(a) for a in self.articles])

    # ---------- 加载 ----------
    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"找不到语料文件：{self.path}\n"
                f"请先运行：python scripts/make_seed.py"
            )
        # 主语料 + 可选的接入库语料。过滤放在 append 之前，于是 by_id /
        # by_law / BM25 的语料集合天然一致，不会出现「检索得到但取条取不到」。
        for path in (self.path, self.extra_path):
            if path is None:
                continue
            if not path.exists():
                # 额外语料缺失不是致命错误：接入内核会零命中并诚实拒答，
                # 但不能让整个服务起不来——离线交付包可能不带这个文件。
                continue
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    a = json.loads(line)
                    # 条号统一成字符串。语料可能是手写或脚本生成的，int/str 都出现过，
                    # 不归一化会让下游 Evidence(article_no: str) 直接校验失败。
                    a["article_no"] = str(a.get("article_no", "")).strip()
                    if self._laws is not None and a["law_short"] not in self._laws:
                        continue
                    self.articles.append(a)
                    self.by_id[a["id"]] = a
                    self.by_law[a["law_short"]].append(a)

    @staticmethod
    def _doc_text(a: dict) -> str:
        """索引文本 = 章节路径 + 法律简称 + 条号 + 正文。

        把章节路径拼进来，是为了让"第四章 解除和终止"这类结构性问法
        也能命中，同时也部分替代了"本法"的指代消解。
        """
        return f"{a['law_short']} {a['chapter_path']} {a['article_no_cn']} {a['content']}"

    # ---------- 引用图 ----------
    def _build_citation_graph(self) -> None:
        """解析"本法第X条"这类交叉引用，建一跳有向图。

        法律文本的独特难点：第八十七条说"依照第四十七条规定的标准"，
        向量检索只捞到八十七条，模型不知道四十七条写了什么就开始编。
        有了这张图，命中后可以按图把被引用条文一起带进上下文。
        """
        pat = re.compile(r"第([零一二三四五六七八九十百千]+)条")
        for a in self.articles:
            targets: list[str] = []
            for m in pat.finditer(a["content"]):
                n = cn2int(m.group(1))
                if not n or str(n) == a["article_no"]:
                    continue
                tid = f"{a['law_short']}_{n}"
                if tid in self.by_id:
                    targets.append(tid)
            if targets:
                self.citation_map[a["id"]] = list(dict.fromkeys(targets))

    # ---------- 查询 ----------
    def search(self, query: str, topk: int = 5,
               only_valid: bool = True, law_hint: str = "") -> list[tuple[float, dict]]:
        # 候选池要开得比 topk 大：领域加权是在池子里重排，池子太小的话，
        # 被降权的条文（民法典）占满前几名，劳动法条文根本进不了池，加权就白做。
        hits = self.bm25.search(query, topk=max(topk * 6, 30))
        out: list[tuple[float, dict]] = []
        for score, i in hits:
            a = self.articles[i]
            hinted = bool(law_hint) and (law_hint in a["law_name"]
                                         or law_hint in a["law_short"]
                                         or a["law_short"] in law_hint)
            # 「部分失效」必须放行：解释（一）整体仍现行有效，只是第三十二条
            # 第一款被解释（二）废止了。按整部法的状态一刀切，会让这条永远
            # 搜不到，版本感知也就永远演示不出来。
            if only_valid and (a.get("status") or "现行有效") not in ("现行有效", "部分失效"):
                # 已废止的法（合同法）默认不参与检索；但用户点名问「合同法第114条」
                # 时是 law_hint 命中的，必须放行——否则版本题永远检索零命中。
                if not hinted:
                    continue
            if law_hint and not hinted:
                continue
            # 领域加权：泛民事法降权，把位置让回劳动法条文。
            # 绑在索引实例上：劳动法内核逐字沿用 0.55，接入内核为 1.0。
            w = self._boost if a["law_short"] in self._boost_laws else 1.0
            out.append((score * w, a))
        out.sort(key=lambda x: -x[0])
        return out[:topk]

    def get_article(self, law_name: str, article_no: str) -> dict | None:
        """按法律名 + 条号精确取。law_name 支持简称模糊匹配，条号支持中英数。

        子串匹配必须分两轮，不能按语料顺序取第一条：
        「合同法」是「劳动合同法」的后缀（也是「民法典合同编」这类写法的前缀），
        所以 "合同法" in "中华人民共和国劳动合同法" 恒为 True。第 79 条在全库
        有四个候选，语料顺序是 劳动合同法 → 劳动法 → 合同法 → 民法典，
        于是「合同法第七十九条」（债权转让）被答成「劳动合同法第七十九条」
        （举报奖励）——条号对、法错、内容完全无关，是最难被指标发现的一类错。
        这也正是 _law_near_article 在问句层专门防过的同一个坑，取条层原来没防。
        """
        m = re.search(r"(\d+|[零一二三四五六七八九十百千]+)", str(article_no))
        if not m:
            return None
        n = cn2int(m.group(1))
        law_key = (law_name or "").strip()
        cands = [a for a in self.articles if a["article_no"] == str(n)]
        if not cands:
            return None
        if not law_key:
            return cands[0] if len(cands) == 1 else None

        # 第一轮：法名或简称完全一致，直接拿下。
        for a in cands:
            if law_key in (a["law_name"], a["law_short"]):
                return a

        # 第二轮：包含关系。多个候选都能匹配时，取**长度最接近**的那个，
        # 也就是最紧的写法——「合同法」对 中华人民共和国合同法 差 6，
        # 对 中华人民共和国劳动合同法 差 8，于是取前者。
        fits = [a for a in cands
                if law_key in a["law_name"] or law_key in a["law_short"]
                or a["law_short"] in law_key or a["law_name"] in law_key]
        if fits:
            return min(fits, key=lambda a: (abs(len(a["law_name"]) - len(law_key)),
                                            len(a["law_name"])))
        # 条号对但法律名对不上时，退化为按条号找唯一解
        return cands[0] if len(cands) == 1 else None

    def expand(self, article_id: str) -> list[dict]:
        return [self.by_id[i] for i in self.citation_map.get(article_id, []) if i in self.by_id]

    def stats(self) -> dict:
        laws = defaultdict(int)
        for a in self.articles:
            laws[a["law_short"]] += 1
        return {"articles": len(self.articles), "laws": dict(laws),
                "citation_edges": sum(len(v) for v in self.citation_map.values())}


_index: LawIndex | None = None


def get_index(corpus_path: str | Path) -> LawIndex:
    """进程内单例。BM25 只在首次访问时构建，之后复用。"""
    global _index
    if _index is None or str(_index.path) != str(corpus_path):
        _index = LawIndex(corpus_path)
    return _index


# 接入内核各有一份独立索引，和劳动法主索引互不干扰。独立缓存的必要性：
# 若复用 `_index` 单例，两个标签页来回切内核会不停重建 BM25（1.4MB 语料
# 每次几十毫秒），而且切回劳动法时路径没变会命中一份被过滤过的脏索引。
_profile_indexes: dict[str, LawIndex] = {}


def get_profile_index(profile) -> LawIndex:
    """按内核取索引。劳动法内核直接返回 v118 那一份，一个字节都不变。"""
    if profile.id == "labor" or profile.corpus_laws is None:
        return get_index(CORPUS_PATH)
    if profile.id not in _profile_indexes:
        _profile_indexes[profile.id] = LawIndex(
            CORPUS_PATH,
            extra_path=DOMAIN_CORPUS_PATH if profile.needs_domain_corpus else None,
            laws=profile.corpus_laws,
            boost_laws=profile.boost_laws,
            boost=profile.boost)
    return _profile_indexes[profile.id]
