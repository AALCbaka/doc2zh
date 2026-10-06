"""诊断：原始 PDF 的行结构 vs 译文段落结构，判断是解析阶段还是翻译阶段把行合并了。

用法：python build/diag_lines.py <原始PDF> <对照PDF> <页码> [kind]
"""
import sys
from pathlib import Path

import fitz

src = Path(sys.argv[1]).resolve()
dual = Path(sys.argv[2]).resolve()
page_no = int(sys.argv[3]) if len(sys.argv) > 3 else 3


def rows_of(pdf: Path, page_no: int, side: str | None) -> list[tuple[float, float, str]]:
    """返回 [(y, x0, text)]。side: 'left'/'right'/None=全页。"""
    with fitz.open(pdf) as doc:
        page = doc.load_page(page_no - 1)
        w = page.rect.width
        out = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not text.strip():
                    continue
                x0, y0 = line["bbox"][0], line["bbox"][1]
                if side == "left" and x0 > w * 0.45:
                    continue
                if side == "right" and x0 <= w * 0.45:
                    continue
                out.append((round(y0, 1), round(x0, 1), text.strip()))
        out.sort()
        return out


print(f"原始 PDF: {src.name}  第 {page_no} 页")
print("=" * 100)
for y, x, t in rows_of(src, page_no, None):
    print(f"  y={y:7.1f} x={x:6.1f}  {t[:110]}")

print()
print(f"对照 PDF: {dual.name}  第 {page_no} 页（右半边=译文）")
print("=" * 100)
for y, x, t in rows_of(dual, page_no, "right"):
    print(f"  y={y:7.1f} x={x:6.1f}  {t[:110]}")
