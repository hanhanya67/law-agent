# -*- coding: utf-8 -*-
"""生成夹具 `pay_slip.png`（工资条）。**平时不用跑**，只在需要改这张图时跑。

它存在的理由是「这张图从哪来的」：`check_live.py` 第 8 节断言
「读出月工资 12000」，写死判据的前提是图本身不变，于是得有一段
能说明它怎么被画出来的代码，而不是一张谁也说不清来历的 PNG。

**跑它需要 PIL，而 PIL 不在 requirements.txt 里**——这是刻意的：应用本身
不需要 PIL（图片解码交给模型），只有这个生成脚本需要。所以图是随包发的，
接收方不用装 PIL 也能让第 8 节跑起来；要重新生成图才需要
`pip install pillow`。

改图前先想清楚：`check_live.py` 里断言的是 12000 和 `doc_type=pay_slip`，
改了数值或版式就要同步改断言，否则第 8 节会红——那是断言在正常工作。

用法：
    python eval/fixtures/make_pay_slip.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "pay_slip.png"


def draw_pay_slip() -> Path:
    """画一张 620x420 的工资条。用系统里有的中文字体，找不到就退回默认。"""
    w, h = 620, 420
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    font = None
    big = None
    for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc",
                 r"C:\Windows\Fonts\simhei.ttf"):
        if Path(cand).is_file():
            try:
                font = ImageFont.truetype(cand, 22)
                big = ImageFont.truetype(cand, 30)
                break
            except OSError:
                continue
    if font is None:                       # 没中文字体就画英文，仍能验读图能力
        font = ImageFont.load_default()
        big = font

    d.text((190, 24), "工 资 条", fill="black", font=big)
    d.line((40, 74, 580, 74), fill="black", width=2)
    rows = [
        ("姓名", "张伟"),
        ("部门", "生产部"),
        ("工资期间", "2026年8月"),
        ("基本工资", "8,000.00"),
        ("岗位津贴", "2,500.00"),
        ("绩效奖金", "1,500.00"),
        ("应发合计", "12,000.00"),
        ("代扣个税", "290.00"),
        ("实发工资", "11,710.00"),
    ]
    y = 100
    for k, v in rows:
        d.text((60, y), k, fill="black", font=font)
        d.text((300, y), v, fill="black", font=font)
        y += 34
    d.line((40, y + 6, 580, y + 6), fill="black", width=2)
    d.text((60, y + 20), "单位：某某制造有限公司", fill="#333333", font=font)
    img.save(OUT)
    return OUT


if __name__ == "__main__":
    p = draw_pay_slip()
    print(f"已生成 {p}（{p.stat().st_size / 1024:.1f} KB）")
