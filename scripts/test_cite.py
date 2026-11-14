# -*- coding: utf-8 -*-
"""引用抽取的回归测试。每个用例都是踩过的坑，改抽取器后先跑这个。

用法：python scripts/test_cite.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.cite import extract_cites, find_article  # noqa: E402
from app.config import CORPUS_PATH                # noqa: E402
from app.llm import _mock_verify                  # noqa: E402
from app.retrieval import LawIndex, cn2int        # noqa: E402

IDX = LawIndex(CORPUS_PATH)


def resolved(text: str) -> list[str]:
    """把抽取结果解析成条文 id，方便断言（比断法律名稳，不受简称影响）。"""
    out = []
    for law, no in extract_cites(text, index=IDX):
        art = find_article(IDX, law, no)
        out.append(art["id"] if art else f"?{law}_{cn2int(no)}")
    return out


CASES: list[tuple[str, str, list[str]]] = [
    (
        "承前省略书名号",
        "《劳动合同法》第四十四条规定终止情形；第四十六条第五项规定应付补偿，按第四十七条计算。",
        ["劳动合同法_44", "劳动合同法_46", "劳动合同法_47"],
    ),
    (
        "序数词不是引用",
        "这是第二条绕道。这是第一条建议。",
        [],
    ),
    (
        "裸法律名优先于上一位书名号",
        "依《劳动合同法》第二十五条，按劳动合同法第三十条第二款申请支付令。",
        ["劳动合同法_25", "劳动合同法_30"],
    ),
    (
        "通知书不是法律，不能当主语",
        "出具《解除竞业限制协议通知书》，明确写本人依据司法解释一第三十八条解除竞业限制约定。",
        ["劳动争议解释一_38"],
    ),
    (
        "同号回溯：裸提的条号本段已出现过",
        "《中华人民共和国劳动合同法》第二十五条把违约金情形限在第二十二条、第二十三条；"
        "《中华人民共和国民法典》第五百八十五条讲的是违约金过高可酌减；"
        "在劳动关系里要先过第二十五条这道门槛。",
        ["劳动合同法_25", "劳动合同法_22", "劳动合同法_23", "民法典_585"],
    ),
    (
        "存在性回退：条号在前一部法里不存在",
        "《劳动合同法》第四十条规定代通知金，《劳动合同法实施条例》第二十二条规定该按什么标准算，"
        "也要按第四十七条支付补偿。",
        ["劳动合同法_40", "实施条例_22", "劳动合同法_47"],
    ),
    (
        "解释一与解释二是两部法，不能混",
        "《解释（一）》第三十二条与《解释（二）》第二十一条冲突，后者废止前者。",
        ["劳动争议解释一_32", "劳动争议解释二_21"],
    ),
    (
        "单条引用",
        "《劳动合同法》第二条适用于境内用人单位。",
        ["劳动合同法_2"],
    ),
    (
        "同句并列的条号归属一致",
        "《劳动合同法》第四十条规定代通知金。"
        "《最高人民法院关于审理劳动争议案件适用法律问题的解释（一）》第四十七条规定，"
        "公司已建立工会组织、解除符合第三十九条或第四十条规定但没有事先通知工会的。",
        ["劳动合同法_40", "劳动争议解释一_47", "劳动合同法_39"],
    ),
    (
        "司法解释自己的条号不能被上一条规则误伤",
        "《解释（一）》第三十九条讲竞业限制协议解除。",
        ["劳动争议解释一_39"],
    ),
    (
        "裸法律名不止「…法」结尾",
        "按实施条例第二十七条，经济补偿的月工资按应得工资算，低于最低工资标准的按最低工资算。",
        ["实施条例_27"],
    ),
    (
        "同一部法的同一条被提两次只算一处",
        "《中华人民共和国劳动合同法实施条例》第二十七条规定按应得工资算；"
        "另按实施条例第二十七条，低于当地最低工资标准的按最低工资标准算。",
        ["实施条例_27"],
    ),
    (
        "「这个规定」不是法律名",
        "根据这个规定第三条，公司要赔钱。",
        [],
    ),
    (
        "数字写法（阿拉伯数字）",
        "《劳动合同法》第82条规定二倍工资。",
        ["劳动合同法_82"],
    ),
]


# 已知边界：不判对错，只把行为钉住，改代码时能看出有没有变
KNOWN_LIMITS: list[tuple[str, str, list[str]]] = [
    (
        "孤立文本：唯一一次提到某法后裸提别的法条号",
        "《民法典》第五百八十五条讲违约金可酌减；在劳动关系里要先过第二十五条这道门槛。",
        ["民法典_585", "民法典_25"],
    ),
]


def main() -> int:
    bad = 0
    for name, text, want in CASES:
        got = resolved(text)
        ok = got == want
        if not ok:
            bad += 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"      期望 {want}")
            print(f"      实得 {got}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} 通过")

    # ---- 补充：承前指代 / 法名清洗 / 自检能不能抓住错引用 ----
    # 这三件事来自 r10 的一处真错：答案写「依调解仲裁法第二条适用本法；
    # **同法第七十七条**又明确…」，而第七十七条是《劳动法》的，调解仲裁法
    # 只到 54 条。自检当时用的是只认书名号的正则，这条**压根没被抽出来**，
    # 于是判了通过——评测层看得见、产品层看不见。
    extra: list[tuple[str, bool, str]] = []
    t1 = ("年终奖属于劳动报酬，依《调解仲裁法》第二条适用本法；"
          "同法第七十七条又明确可以申请仲裁。")
    got1 = extract_cites(t1, index=IDX)
    extra.append(("「同法第七十七条」要归到前一部法，不能丢",
                  got1 == [("调解仲裁法", "二"), ("调解仲裁法", "七十七")],
                  str(got1)))
    t2 = "依据是调解仲裁法第二十一条的管辖规定。"
    got2 = extract_cites(t2, index=IDX)
    extra.append(("法名不带动词前缀（「依据是」的「是」要剥掉）",
                  got2 == [("调解仲裁法", "二十一")], str(got2)))
    vr = _mock_verify(t1, [], IDX)
    extra.append(("自检认得出库里没有的条号，不能判通过", not vr.passed,
                  "；".join(i.reason for i in vr.issues)))
    # 上面那条单独用空证据证明不了什么：空证据下任何引用都会报问题。
    # 真正要钉的是「证据里**有**第二条、只有同法第七十七条是假的」这种场景——
    # 旧实现（只认书名号）在这里会完全不报，于是把一条错引用放了过去。
    from app.schemas import Evidence                     # noqa: PLC0415
    art = find_article(IDX, "调解仲裁法", "二")
    ev = [Evidence(article_id=art["id"], law_name=art["law_name"],
                   article_no=art["article_no"],
                   article_no_cn=art.get("article_no_cn", ""),
                   content=art["content"], score=0.0, source="direct")]
    vr2 = _mock_verify(t1, ev, IDX)
    extra.append(("别的引用都对，只错一条也要报出来（不能被放行）",
                  any("本库没有这一条" in i.reason for i in vr2.issues),
                  "；".join(i.reason for i in vr2.issues) or "一条 issue 都没有"))

    print("\n[补充] 承前指代 / 法名清洗 / 自检能否抓住错引用")
    for name, cond, detail in extra:
        if not cond:
            bad += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  → {detail}" if not cond else ""))
    total = len(CASES) + len(extra)
    print(f"\n合计 {total - bad}/{total} 通过")

    print("\n已知边界（不计入通过率）：")
    for name, text, want in KNOWN_LIMITS:
        got = resolved(text)
        flag = "行为未变" if got == want else f"行为变了：{got}"
        print(f"  · {name}\n      {flag}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
