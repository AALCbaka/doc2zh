"""原型：预处理源 PDF —— 把「一个块里含多行清单」拆成每行一个独立块。

原理（已实测确认）：
  PyMuPDF 从源 PDF 解析出的文本块，本来就是「题干 + a) b) c) d)」合成一块（lines=5），
  块内行距仅 14.6pt，而块与块之间是 29pt。BabelDOC 按块成段，于是选项被并成一段。
  只要把源 PDF 的这类块拆成一行一块，上游一切照旧，段落自然就是每行一个。

做法：对每个"所有行都以清单标记开头"的块，用 redaction 抹掉原文，
      再按行逐行插回（使用原块的字体、字号、颜色与位置）。
输出到副本文件，原始 PDF 不动。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz

MARKER_RE = re.compile(
    r"^\s*(?:[\(\[]?[a-hA-H][\)\].、]|[\(\[]?\d{1,2}[\)\].、]|[\(\[]?[ivxIVX]{1,4}[\)\.]|[•·▪◦‣∙*]|[-–—](?=\s))"
)


def line_text(line):
    return "".join(s.get("text", "") for s in line.get("spans", [])).strip()


def is_list_block(block) -> bool:
    lines = [ln for ln in block.get("lines", []) if line_text(ln)]
    if len(lines) < 2:
        return False
    marked = sum(1 for ln in lines if MARKER_RE.match(line_text(ln)))
    return marked == len(lines)


def dominant_span(block):
    for ln in block.get("lines", []):
        for sp in ln.get("spans", []):
            if sp.get("text", "").strip():
                return sp
    return None


def split_pdf(src: Path, dst: Path, pages=None) -> dict:
    doc = fitz.open(src)
    stats = {"pages": 0, "blocks": 0, "lines": 0, "failed": 0}
    todo = range(doc.page_count) if pages is None else [p - 1 for p in pages]

    for pno in todo:
        page = doc.load_page(pno)
        data = page.get_text("dict")
        targets = [b for b in data["blocks"] if b.get("type") == 0 and is_list_block(b)]
        if not targets:
            continue
        stats["pages"] += 1

        # 1) 抹掉这些块的原文
        for b in targets:
            r = fitz.Rect(b["bbox"])
            page.add_redact_annot(r, fill=None)  # 只删文字，不填色
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        # 2) 逐行插回
        for b in targets:
            sp = dominant_span(b)
            if not sp:
                stats["failed"] += 1
                continue
            size = round(sp["size"], 2)
            color = sp["color"]
            rgb = ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255
            for ln in b["lines"]:
                txt = line_text(ln)
                if not txt:
                    continue
                x0 = ln["bbox"][0]
                baseline = ln["spans"][0]["origin"][1] if ln.get("spans") else ln["bbox"][3]
                try:
                    page.insert_text(
                        fitz.Point(x0, baseline),
                        txt,
                        fontname="helv",
                        fontsize=size,
                        color=rgb,
                        render_mode=0,
                    )
                    stats["lines"] += 1
                except Exception:  # noqa: BLE001
                    stats["failed"] += 1
            stats["blocks"] += 1

    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return stats


def blocks_report(pdf: Path, page_no: int) -> list[tuple[float, int, str]]:
    with fitz.open(pdf) as doc:
        page = doc.load_page(page_no - 1)
        out = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            lines = [line_text(ln) for ln in b.get("lines", []) if line_text(ln)]
            if lines:
                out.append((round(b["bbox"][1], 1), len(lines), lines[0][:60]))
        out.sort()
        return out


if __name__ == "__main__":
    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve()
    page = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    print("拆分前，第", page, "页的块：")
    for y, n, t in blocks_report(src, page):
        print(f"  y={y:6.1f} lines={n}  {t}")
    stats = split_pdf(src, dst, pages=[page])
    print("\n拆分统计:", stats)
    print("\n拆分后，第", page, "页的块：")
    for y, n, t in blocks_report(dst, page):
        print(f"  y={y:6.1f} lines={n}  {t}")
