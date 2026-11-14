# -*- coding: utf-8 -*-
"""
判定层回归：给定固定的证据集，judge 该判 full / partial / none。

为什么单独做一个，不靠全量评测：
  改 judge prompt 后原来得重跑四套 86 条（约 20 分钟、1 元多），而且分数里混着
  检索抖动——分不清是判定变差还是这轮没搜到条文。v19 那轮就是这样：主集召回
  从 96.5% 掉到 93.0%，三条里两条是证据池根本没有 gold（检索层），只有 h11 一条
  是判定层。混在一起看，改判定永远看不清。

  这里把两件事拆开：**证据集在用例里写死**，只问「这批条文拿给 judge，它该怎么判」。
  14 例、每例一次调用，约 4 万 token，几毛钱以内，30 秒跑完。

用例（`eval/judge_cases.jsonl`）的 allow 是**允许集合**，不是一个答案：
  h11 只允许 full（证据里解释（一）32 条直接定性，判 none 就是把用户挡在门外）
  r14 允许 full 或 partial（都给了好答案，但不得 none）
  c21 只允许 partial（44 条只答一半，判 full 是硬答、判 none 是浪费半个答案）
  r12 只允许 none（证据里没有任何一条给「实习生」这个身份定性）

**两类错误分开统计**，因为代价完全不同：
  full/partial → none   误拒。用户来问事，被一句"没有规定"打发走。
  none/partial → full   放水。拿相近条文硬套，用户拿着错结论去维权。
后者更严重，所以哪怕总数一样也要单独看。

用法：
  ./.venv/Scripts/python.exe scripts/test_judge.py             # 真实模型
  ./.venv/Scripts/python.exe scripts/test_judge.py --dry-run   # 不调 API，只验证脚本
  ./.venv/Scripts/python.exe scripts/test_judge.py --dry-run --demo-fail
      # 故意制造一例「放水」，检查误拒/放水的统计方向
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import llm as llm_mod                       # noqa: E402
from app.config import CORPUS_PATH                   # noqa: E402
from app.llm import LLM                              # noqa: E402
from app.retrieval import LawIndex                   # noqa: E402
from app.schemas import AgentState, Evidence         # noqa: E402

CASES = ROOT / "eval" / "judge_cases.jsonl"


def load_ev(index: LawIndex, ev_ids: list[str]) -> list[Evidence]:
    out = []
    for aid in ev_ids:
        a = index.by_id.get(aid)
        if not a:
            continue
        out.append(Evidence(article_id=a["id"], law_name=a["law_name"],
                            article_no=str(a["article_no"]),
                            article_no_cn=a["article_no_cn"],
                            content=a["content"],
                            status=a.get("status", "现行有效")))
    return out


class _FakeResp:
    """dry-run 用的假响应。只用来验证脚本本身能跑通，不代表判定质量。"""

    def __init__(self, content: str):
        msg = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": msg})()]


def make_fake_chat(seq: list[str]):
    it = iter(seq)

    def _chat(self, system, user, **kw):                 # noqa: ANN001
        return _FakeResp(next(it, '{"coverage":"none"}'))

    return _chat


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不调 API，用假响应验证脚本")
    ap.add_argument("--demo-fail", action="store_true",
                    help="配合 --dry-run：故意制造一次「放水」，用来检查失败方向统计")
    ap.add_argument("--ids", default="", help="只跑指定 id，逗号分隔")
    args = ap.parse_args()

    index = LawIndex(CORPUS_PATH)
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.ids:
        want = {s.strip() for s in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in want]

    llm = LLM()
    if args.dry_run:
        # 两种模式，别混：
        #   默认        假响应按每个用例的第一个允许值作答 → 全过。语义是
        #               「脚本本身能跑通」，属于健康检查，可以进一键回归。
        #   --demo-fail 故意让非 none 的用例一律答 full → 必然放水一例（partial
        #               那类）。语义是「验证失败方向统计对不对」，本来就该红，
        #               所以不给它 0 退出码，免得混进 CI 变成常红灯。
        seq = []
        for c in cases:
            if args.demo_fail:
                cov = "none" if c["allow"] == ["none"] else "full"
            else:
                cov = c["allow"][0]
            item = {"coverage": cov, "kind": "unknown", "label": "测试",
                    "reason": "dry-run"}
            if cov == "partial":
                # 产品侧有一条硬规则：partial 但没写清「哪一问能答 / 哪一问答不了」
                # 就降级成 full（宁可按全量答，也不打太极）。假响应不补这两个
                # 字段，partial 会被这条规则吃掉，dry-run 永远看到 full。
                item["answered_part"] = "能答的部分（dry-run 占位）"
                item["missing_part"] = "答不了的部分（dry-run 占位）"
            seq.append(json.dumps(item, ensure_ascii=False))
        llm._chat = make_fake_chat(seq).__get__(llm, type(llm))
        # 必须同时把 online 置真：judge() 开头是 `if not self.online: return
        # judge_mock(...)`。没配 .env 时它直接走规则判定，假响应根本用不上，
        # 于是「健康检查」变成「离线判定质量体检」，在全新环境里必然红 5 例——
        # 那 5 例是真的（离线规则判定确实会放水），但不是这个套件该管的事。
        # dry-run 的语义是「脚本本身能不能跑通」，不该受账户有没有 key 影响。
        llm.online = True
        print("[dry-run] 用假响应走流程，不消耗额度\n")
    elif not llm.online:
        print("没配 key。要验证判定质量必须用真实模型；只检查脚本请加 --dry-run")
        return

    print(f"{'id':6s} {'期望':16s} {'实际':8s} 判定")
    print("-" * 72)
    npass = 0
    wrong = {"误拒(full/partial→none)": 0, "放水(none/partial→full)": 0,
             "其它": 0}
    detail = []
    for c in cases:
        evs = load_ev(index, c["ev_ids"])
        if not evs:
            print(f'{c["id"]:6s} 证据为空，跳过')
            continue
        st = AgentState(session_id="judge-test", question=c["question"], evidences=evs)
        v = llm.judge(st)
        ok = v.coverage in c["allow"]
        npass += ok
        mark = "PASS" if ok else "FAIL"
        print(f'{c["id"]:6s} {"/".join(c["allow"]):16s} {v.coverage:8s} {mark}')
        if not ok:
            if v.coverage == "none":
                wrong["误拒(full/partial→none)"] += 1
            elif v.coverage == "full":
                wrong["放水(none/partial→full)"] += 1
            else:
                wrong["其它"] += 1
            detail.append((c, v))

    total = len([c for c in cases if load_ev(index, c["ev_ids"])])
    print("-" * 72)
    print(f"通过 {npass}/{total}")
    for k, v in wrong.items():
        if v:
            print(f"  {k}: {v} 例")
    if detail:
        print("\n失败明细：")
        for c, v in detail:
            print(f'  {c["id"]}  期望 {"/".join(c["allow"])}，实际 {v.coverage}')
            print(f'    {c["note"]}')
            if v.reason:
                print(f'    judge 理由：{v.reason}')
            if v.coverage == "partial":
                print(f'    能答：{v.answered_part} ｜ 答不了：{v.missing_part}')
    sys.exit(0 if npass == total else 1)


if __name__ == "__main__":
    main()
