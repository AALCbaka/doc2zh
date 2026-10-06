"""PDF 页面渲染：把结果页转成 PNG，用于界面内预览。

为什么不用 iframe 直接嵌 PDF：
    原生窗口走 WebView2，内嵌 PDF 的渲染行为不稳定（有的机器直接白屏、或不弹下载）。
    改成后端把页面渲染成图片，预览在任何环境下都一致，还能做缩放/翻页。
"""
from __future__ import annotations

import functools
from pathlib import Path

import fitz  # PyMuPDF


@functools.lru_cache(maxsize=256)
def render_page(pdf_path: str, page_index: int, dpi: int = 110) -> bytes:
    """渲染指定页（0-based）为 PNG 字节。带缓存，重复预览不重复渲染。"""
    dpi = max(60, min(int(dpi), 200))
    with fitz.open(pdf_path) as doc:
        if page_index < 0 or page_index >= doc.page_count:
            raise IndexError(f"页码超出范围：{page_index + 1}/{doc.page_count}")
        page = doc.load_page(page_index)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        return pix.tobytes("png")


def page_count(pdf_path: str) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def map_source_page_to_output(pdf_path: str, source_page: int, source_total: int) -> int:
    """把「原始 PDF 的第 N 页」映射成「输出对照 PDF 的页码」。

    对上页并排模式：输出页序与源页序一一对应（1:1）。
    对整页交替模式：输出为 原文页/译文页 交替，源页 N 对应输出页 2N-1。
    """
    out_total = page_count(pdf_path)
    if source_total and out_total >= source_total * 2 - 1:
        guess = (source_page - 1) * 2
        if guess < out_total:
            return guess
    return min(max(0, source_page - 1), max(0, out_total - 1))


def resolve(pdf_path: str | Path) -> Path:
    p = Path(pdf_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    return p
