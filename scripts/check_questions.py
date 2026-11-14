"""校验题库：gold 条号是否在库里、keywords 是否能在 gold 条文原文中找到。

用法：
    python scripts/check_questions.py eval/holdout.jsonl
    python scripts/check_questions.py eval/questions.jsonl

关键词检查只是「提示」不是「错误」——关键词是拿去和**答案文本**做子串比对的，
模型完全可能换个说法（比如把「分段」说成「施行之日起计算」）。但若关键词在
条文原文里都找不到，多半是出题时凭印象写的，值得复核一遍。
"""
import json
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import CORPUS_PATH  # noqa: E402
from app.retrieval import LawIndex  # noqa: E402


def main(path: str) -> int:
    index = LawIndex(CORPUS_PATH)
    qs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    bad = hint = 0
    for q in qs:
        texts = []
        for g in q.get("gold") or []:
            art = index.by_id.get(g)
            if art is None:
                print(f"[错误] {q['id']} gold 不存在: {g}")
                bad += 1
            else:
                texts.append(art.get("content") or "")
        blob = "\n".join(texts)
        for k in q.get("keywords") or []:
            if k not in blob:
                print(f"[提示] {q['id']} 关键词 {k!r} 不在 gold 条文原文里")
                hint += 1
    types: dict[str, int] = {}
    for q in qs:
        types[q["type"]] = types.get(q["type"], 0) + 1
    print(f"共 {len(qs)} 条｜gold 引用错误 {bad} 处｜关键词提示 {hint} 处")
    print("题型分布：", types)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "eval/questions.jsonl"))
