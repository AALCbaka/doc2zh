"""在中间表示层拆分「清单式段落」——真正解决译文不换行的问题。

为什么必须在这一层
------------------
前几轮排查（见 docs/换行问题排查记录.md）确认：
  * 版面解析把「题干 + a) b) c) d)」识别成**一个段落**；
  * 送进模型的段落文本里换行早已消失；
  * 上游提示词明令 “Do NOT … split paragraphs”；
  * 即使让模型输出换行，**重排（typesetting）仍按段落粒度重新折行**，换行被抹平。
所以任何"文本层/提示词层"的修法都无效，必须在「段落」这一层拆开。

本模块怎么做（低风险要点）
--------------------------
不碰任何排版算法，只做一件事：在 `ParagraphFinder.process_page` 跑完之后，
把清单式段落按行替换成多个**独立段落**，然后：
  * 调用上游自己的 `update_paragraph_data()` 重算 box / unicode / xobj_id / 缩进；
  * 复制原段落的 `pdf_style`、`layout_id`、`layout_label`、`xobj_id`、`scale`。
剩下的交给原有的翻译与重排流程 —— 每个选项成为独立段落，自然各自占一行。

生效范围：仅当某个段落**所有**行都以清单标记开头（a) b) c) / 1. 2. / • / i) 等）时拆分，
普通正文段落（含换行续行）不受影响。可用 PDF_TRANSLATOR_IR_SPLIT=0 关闭。
"""
from __future__ import annotations

import os
import re
from dataclasses import fields as dataclass_fields

# 行首标记：必须出现在行首（允许前导空白）
LINE_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"[\(\[]?[a-hA-H][\)\].、]|"
    r"[\(\[]?\d{1,2}[\)\].、]|"
    r"[\(\[]?[ivxIVX]{1,4}[\)\.]|"
    r"[•·▪◦‣∙*]|"
    r"[-–—](?=\s)|"
    r"\d+\.\d+(?=\s)"
    r")"
)

MAX_MARKED_LINE_CHARS = 200  # 单行过长说明不是选项行
APPLIED_FLAG = "_ir_list_split_done"
STATS = {"pages": 0, "paragraphs_split": 0, "blocks_created": 0, "failures": 0}


def _enabled() -> bool:
    return os.environ.get("PDF_TRANSLATOR_IR_SPLIT", "1") not in ("0", "false", "False", "")


def _line_entries(paragraph):
    """取出段落里按顺序排列的『行』组成项：(composition, PdfLine, text)。"""
    from babeldoc.format.pdf.document_il.utils.layout_helper import get_char_unicode_string

    entries = []
    for composition in paragraph.pdf_paragraph_composition:
        line = getattr(composition, "pdf_line", None)
        if line is None:
            # 混入非行组成项（公式/单字符）时不拆，保持保守
            return None
        chars = list(getattr(line, "pdf_character", []) or [])
        if not chars:
            return None
        entries.append((composition, line, get_char_unicode_string(chars).strip()))
    return entries


def _is_list_like(entries) -> bool:
    """所有非空行都以清单标记开头，且至少 2 行。"""
    texts = [t for _, _, t in entries if t]
    if len(texts) < 2:
        return False
    marked = 0
    for t in texts:
        if len(t) > MAX_MARKED_LINE_CHARS:
            return False
        if LINE_MARKER_RE.match(t):
            marked += 1
    return marked == len(texts)


def _build_paragraph_like(original, line_composition):
    """按原段落的字段造一个新段落，只放一行。"""
    from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraph, PdfParagraphComposition

    new_para = PdfParagraph(pdf_paragraph_composition=[PdfParagraphComposition(pdf_line=line_composition.pdf_line)])
    for f in dataclass_fields(PdfParagraph):
        name = f.name
        if name in ("pdf_paragraph_composition", "box", "unicode", "first_line_indent"):
            continue
        try:
            setattr(new_para, name, getattr(original, name))
        except Exception:  # noqa: BLE001
            pass
    return new_para


def _probe(msg: str) -> None:
    """排查用探针：子进程里也能留痕（stderr 在 Windows spawn 下不一定可见）。"""
    path = os.environ.get("PDF_TRANSLATOR_IR_TRACE", "")
    if not path:
        return
    try:
        import time

        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def split_list_paragraphs(page) -> int:
    """把一页里的清单式段落按行拆成多个段落。返回新建的段落数。"""
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder

    finder = ParagraphFinder.__new__(ParagraphFinder)  # 只借用其方法，不重复初始化
    paragraphs = list(getattr(page, "pdf_paragraph", []) or [])
    if not paragraphs:
        return 0

    rebuilt = []
    created = 0
    for paragraph in paragraphs:
        if getattr(paragraph, APPLIED_FLAG, False):
            rebuilt.append(paragraph)
            continue
        try:
            entries = _line_entries(paragraph)
        except Exception:  # noqa: BLE001
            entries = None
        if not entries or not _is_list_like(entries):
            rebuilt.append(paragraph)
            continue
        try:
            first = True
            for composition, _line, _text in entries:
                if first:
                    paragraph.pdf_paragraph_composition = [composition]
                    finder.update_paragraph_data(paragraph, update_unicode=True)
                    setattr(paragraph, APPLIED_FLAG, True)
                    rebuilt.append(paragraph)
                    first = False
                    continue
                new_para = _build_paragraph_like(paragraph, composition)
                finder.update_paragraph_data(new_para, update_unicode=True)
                setattr(new_para, APPLIED_FLAG, True)
                rebuilt.append(new_para)
                created += 1
            STATS["paragraphs_split"] += 1
        except Exception:  # noqa: BLE001
            STATS["failures"] += 1
            rebuilt.append(paragraph)
    if created:
        page.pdf_paragraph = rebuilt
        STATS["pages"] += 1
        STATS["blocks_created"] += created
    return created


def install() -> bool:
    """包装 ParagraphFinder.process_page，在其后做清单段落拆分。"""
    if not _enabled():
        return False
    try:
        from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    except Exception:  # noqa: BLE001
        return False

    original = getattr(ParagraphFinder, "process_page", None)
    if original is None or getattr(original, "_ir_split_patched", False):
        return True

    def patched(self, page):
        result = original(self, page)
        try:
            created = split_list_paragraphs(page)
            _probe(f"process_page 完成，本页新建段落 {created}")
        except Exception as exc:  # noqa: BLE001
            STATS["failures"] += 1
            _probe(f"拆分异常：{type(exc).__name__}: {exc}")
        return result

    patched._ir_split_patched = True
    patched._original = original
    ParagraphFinder.process_page = patched
    _probe("install(): ParagraphFinder.process_page 已打补丁")
    return True


def install_in_child() -> None:
    """让补丁在**子进程**里也生效。

    pdf2zh-next 把版面解析 + 翻译跑在 multiprocessing spawn 子进程里，
    主进程打的补丁不会自动带过去（子进程是全新解释器）。
    而 `ParagraphFinder.process_page` 是模块级函数，pickle 后子进程会重新 import
    该模块 —— 如果在子进程 import 到本模块时执行 install()，补丁就能在
    版面解析阶段之前生效。

    这里用审计钩子捕获子进程启动瞬间（事件 `multiprocessing.spawn` 或 `os.fork`），
    只在那一刻执行安装。相比替换子进程入口（那个做法已经验证会因
    __main__ 状态不跨进程而失败），这样更稳、也不影响主进程后续状态。
    """
    import sys

    if getattr(install_in_child, "_hooked", False):
        return
    install_in_child._hooked = True

    def _hook(event: str, _args) -> None:
        if event in ("multiprocessing.spawn", "multiprocessing.fork", "os.fork"):
            try:
                install()
            except Exception:  # noqa: BLE001
                pass

    try:
        sys.addaudithook(_hook)
    except Exception:  # noqa: BLE001
        pass
    install()
